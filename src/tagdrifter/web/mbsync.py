"""Keeps the MusicBrainz release cache filled, without creating peak times at MusicBrainz.

MusicBrainz asks background jobs not to wake at a set time of day (every install's 04:00
would be an artificial peak somewhere) but to "make calls at random intervals throughout
the day" (https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting). So lookups happen two
ways (both via musicbrainz.sync_releases, at most 1 request/s):

- **Background trickle.** While the app runs, a worker wakes at random intervals
  (`TRICKLE_INTERVAL`, 30-150 min, drawn afresh each time) and looks up a few due releases
  (`TRICKLE_BATCH`): new ones first, then refreshes. This is the only way lookups follow an
  *automatic* scan (scheduled, or the rescan after lookups), so the scan schedule never
  shapes the traffic.
- **After a scan someone started** ("Scan now", a rename's rescan): everything due is looked
  up at once, because a person is waiting for the result.

A run that stopped on an error (MusicBrainz overloaded or unreachable) pauses all lookups
for an hour, or longer if MusicBrainz asked (Retry-After). Only one run happens at a time.
When anything was fetched, a rescan applies it to the mb.* checks (cheap: tags come from the
cache).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import Settings
from ..db import Store
from ..musicbrainz import MusicBrainzClient, SyncResult, sync_releases

log = logging.getLogger(__name__)

TRICKLE_INTERVAL = (1800.0, 9000.0)  # seconds between background wake-ups, drawn each time
TRICKLE_BATCH = 5  # releases looked up per wake-up (~50-80 a day; a library needs ~20)
ERROR_COOLDOWN = 3600.0  # seconds without lookups after a run stopped on an error


@dataclass(frozen=True)
class MbSyncStatus:
    running: bool
    background: bool = False  # the running lookups are a background batch
    done: int = 0
    total: int = 0
    last: SyncResult | None = None  # the last finished run
    finished_at: float | None = None  # time.time()
    paused_until: float | None = None  # time.time(); no lookups before this after an error
    next_background: float | None = None  # time.time() of the next background wake-up

    @property
    def paused_until_time(self) -> datetime | None:
        return _time(self.paused_until)

    @property
    def next_background_time(self) -> datetime | None:
        return _time(self.next_background)


class MusicBrainzSync:
    def __init__(
        self,
        settings: Settings,
        client: MusicBrainzClient | None = None,
        *,
        trickle_interval: tuple[float, float] = TRICKLE_INTERVAL,
        trickle_batch: int = TRICKLE_BATCH,
        error_cooldown: float = ERROR_COOLDOWN,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.client = client or MusicBrainzClient(contact=settings.musicbrainz_contact)
        self.trickle_interval = trickle_interval
        self.trickle_batch = trickle_batch
        self.error_cooldown = error_cooldown
        self.rescan: Callable[[], object] | None = None  # set by ScanRunner
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._busy = False  # a run (either kind) is in progress
        self._background = False
        self._thread: threading.Thread | None = None  # the last run after a scan
        self._trickle: threading.Thread | None = None
        self._next_background: float | None = None
        self._paused_until: float | None = None
        self._done = 0
        self._total = 0
        self._last: SyncResult | None = None
        self._finished_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.musicbrainz_enabled

    # --- Triggers -----------------------------------------------------------------------

    def start(self) -> None:
        """Start the background trickle (with the app). Does nothing when lookups are off."""
        if not self.enabled or self._trickle is not None:
            return
        self._stop.clear()
        self._trickle = threading.Thread(
            target=self._trickle_loop, name="tagdrifter-musicbrainz-trickle", daemon=True
        )
        self._trickle.start()

    def shutdown(self) -> None:
        self._stop.set()

    def after_scan(self, *, automatic: bool = False) -> bool:
        """After a scan someone started, look up everything due now. Automatic scans
        (scheduled, or a rescan after lookups) leave it to the background trickle. Returns
        whether lookups were started."""
        if not self.enabled or automatic:
            return False
        due = self._due_count()
        if not due or not self._claim(background=False, total=due):
            return False
        self._thread = threading.Thread(
            target=self._run, args=(None,), name="tagdrifter-musicbrainz", daemon=True
        )
        self._thread.start()
        return True

    def trickle_once(self) -> bool:
        """One background wake-up: look up a few due releases, unless paused, busy or
        nothing is due. Returns whether lookups ran."""
        if not self.enabled:
            return False
        due = self._due_count()
        if not due or not self._claim(background=True, total=min(due, self.trickle_batch)):
            return False
        self._run(self.trickle_batch)
        return True

    # --- Status -------------------------------------------------------------------------

    def status(self) -> MbSyncStatus:
        with self._lock:
            paused = self._paused_until
            if paused is not None and self._clock() >= paused:
                paused = None
            return MbSyncStatus(
                running=self._busy,
                background=self._background,
                done=self._done,
                total=self._total,
                last=self._last,
                finished_at=self._finished_at,
                paused_until=paused,
                next_background=self._next_background,
            )

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the last run started after a scan (for tests)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- Internals ----------------------------------------------------------------------

    def _due_count(self) -> int:
        if self._paused_until is not None and self._clock() < self._paused_until:
            log.debug("MusicBrainz lookups paused after an error")
            return 0
        with Store(self.settings.database_path) as store:
            return len(store.mb_releases_due(_now()))

    def _claim(self, *, background: bool, total: int) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy, self._background = True, background
            self._done, self._total = 0, total
            return True

    def _trickle_loop(self) -> None:
        while True:
            delay = random.uniform(*self.trickle_interval)
            with self._lock:
                self._next_background = self._clock() + delay
            if self._stop.wait(delay):
                return
            try:
                self.trickle_once()
            except Exception:  # keep the trickle alive whatever happens
                log.exception("Background MusicBrainz lookups failed")

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self._done, self._total = done, total

    def _run(self, limit: int | None) -> None:
        try:
            with Store(self.settings.database_path) as store:
                result = sync_releases(
                    store, self.client, limit=limit, stop=self._stop, progress=self._progress
                )
        except Exception as exc:  # never let a bug here take down the app
            log.exception("MusicBrainz sync failed")
            result = SyncResult(error=f"{type(exc).__name__}: {exc}")
        log.info(
            "MusicBrainz%s: %d looked up, %d not found%s",
            " (background)" if limit else "",
            result.fetched,
            result.not_found,
            f"; stopped: {result.error}" if result.error else "",
        )
        with self._lock:
            self._busy = False
            self._last = result
            self._finished_at = self._clock()
            if result.error:
                wait = max(self.error_cooldown, result.retry_after or 0)
                self._paused_until = self._finished_at + wait
                log.info("MusicBrainz lookups paused for %.0f min", wait / 60)
        if result.changed and self.rescan is not None:
            self.rescan()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _time(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if epoch else None
