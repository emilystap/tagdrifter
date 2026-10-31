"""Keeps the MusicBrainz release cache filled, in the background, after scans.

After every finished scan, releases that are new to the library or due for a refresh are
looked up (musicbrainz.sync_releases, 1 request/s). After a *scheduled* scan the lookups wait
a random few minutes first, because MusicBrainz asks background jobs not to fire at fixed
times. When anything was fetched, a rescan applies it to the mb.* checks (cheap: tags come
from the cache, and the next sync finds nothing due).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import Settings
from ..db import Store
from ..musicbrainz import MusicBrainzClient, SyncResult, sync_releases

log = logging.getLogger(__name__)

SCHEDULED_DELAY = (60.0, 1800.0)  # seconds; random wait before lookups after a scheduled scan


@dataclass(frozen=True)
class MbSyncStatus:
    running: bool
    waiting: bool = False  # in the random delay before the first lookup
    done: int = 0
    total: int = 0
    last: SyncResult | None = None  # the last finished sync
    finished_at: float | None = None  # time.time()


class MusicBrainzSync:
    def __init__(
        self,
        settings: Settings,
        client: MusicBrainzClient | None = None,
        *,
        scheduled_delay: tuple[float, float] = SCHEDULED_DELAY,
    ) -> None:
        self.settings = settings
        self.client = client or MusicBrainzClient()
        self.scheduled_delay = scheduled_delay
        self.rescan = None  # set by ScanRunner: start a scan when lookups changed the cache
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._waiting = False
        self._done = 0
        self._total = 0
        self._last: SyncResult | None = None
        self._finished_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.musicbrainz_enabled

    def after_scan(self, *, scheduled: bool = False) -> bool:
        """Start looking up what's due, unless lookups are off, already running, or nothing
        is due. Returns whether a sync was started."""
        if not self.enabled:
            return False
        with Store(self.settings.database_path) as store:
            due = len(store.mb_releases_due(_now()))
        with self._lock:
            if not due or (self._thread is not None and self._thread.is_alive()):
                return False
            self._stop.clear()
            self._done, self._total = 0, due
            delay = random.uniform(*self.scheduled_delay) if scheduled else 0.0
            self._thread = threading.Thread(
                target=self._run, args=(delay,), name="tagdrifter-musicbrainz", daemon=True
            )
            self._thread.start()
            return True

    def status(self) -> MbSyncStatus:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return MbSyncStatus(
                running, self._waiting, self._done, self._total, self._last, self._finished_at
            )

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def shutdown(self) -> None:
        self._stop.set()

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self._done, self._total = done, total

    def _run(self, delay: float) -> None:
        if delay:
            log.info("MusicBrainz lookups start in %.0f min", delay / 60)
            self._waiting = True
            stopped = self._stop.wait(delay)
            self._waiting = False
            if stopped:
                return
        try:
            with Store(self.settings.database_path) as store:
                result = sync_releases(store, self.client, stop=self._stop, progress=self._progress)
        except Exception as exc:  # never let a bug here take down the app
            log.exception("MusicBrainz sync failed")
            result = SyncResult(error=f"{type(exc).__name__}: {exc}")
        log.info(
            "MusicBrainz: %d looked up, %d not found%s",
            result.fetched,
            result.not_found,
            f"; stopped: {result.error}" if result.error else "",
        )
        with self._lock:
            self._last = result
            self._finished_at = time.time()
        if result.changed and self.rescan is not None:
            self.rescan()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
