"""Runs triage (triage.py) in the background: every `triage_interval` seconds, and on request.

Each run plans the triage folder, looks up releases it is waiting for on MusicBrainz (through
`MusicBrainzSync.refresh`, so the shared rate limiter and pause apply), then moves every album
that is ready while scans are held off (`ScanRunner.paused`), and rescans once if anything
moved. Albums that need confirmation wait on the Triage page; a notification (event `triage`)
lists what moved and what newly waits for a decision. Only one run happens at a time, and a
confirmed move shares that lock.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import Settings
from ..db import Store
from ..notify import Notifications
from ..renamer import RenameError
from ..triage import (
    BLOCKED,
    CONFIRM,
    READY,
    MoveResult,
    TagCache,
    TriagePlan,
    confirm_album,
    move_album,
    plan_triage,
)
from .mbsync import LookupRefused, MusicBrainzSync
from .runner import ScanRunner

log = logging.getLogger(__name__)

MAX_CACHED_TAGS = 5000  # tag reads kept between runs (keyed by path, size and mtime)


class TriageBusy(Exception):
    """Another triage run or move is in progress."""


@dataclass
class TriageRun:
    finished_at: float
    automatic: bool
    moved: list[tuple[str, MoveResult]] = field(default_factory=list)  # (label, result)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (label, why)
    note: str = ""  # why ready albums didn't move, or lookups didn't happen
    rescanning: bool = False

    @property
    def finished(self) -> datetime:
        return datetime.fromtimestamp(self.finished_at, UTC)


class TriageWatcher:
    def __init__(
        self,
        settings: Settings,
        runner: ScanRunner,
        notifications: Notifications | None = None,
        mb_sync: MusicBrainzSync | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.notifications = notifications
        self.mb_sync = mb_sync
        self._lock = threading.Lock()  # one run or move at a time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache: TagCache = {}
        self._last: TriageRun | None = None
        self._announced: set[tuple[str, str]] = set()  # (key, fingerprint) already notified

    @property
    def enabled(self) -> bool:
        return self.settings.triage_path is not None

    @property
    def last(self) -> TriageRun | None:
        return self._last

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def start(self) -> None:
        """Start the timer (with the app), unless triage is off or only runs on request."""
        if not self.enabled or self.settings.triage_interval <= 0 or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tagdrifter-triage", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.triage_interval):
            try:
                self.run(automatic=True)
            except TriageBusy:
                pass
            except Exception:  # keep the timer alive whatever happens
                log.exception("Triage failed")

    # --- Planning and moving ------------------------------------------------------------

    def plan(self) -> TriagePlan:
        """What triage would do now (reads tags, changes nothing)."""
        if len(self._cache) > MAX_CACHED_TAGS:
            self._cache.clear()
        with Store(self.settings.database_path) as store:
            return plan_triage(self.settings, store, cache=self._cache)

    def run(self, *, automatic: bool = False) -> TriageRun:
        """Plan, look up waiting releases, and move every ready album. Raises TriageBusy when
        a run or move is already in progress."""
        if not self._lock.acquire(blocking=False):
            raise TriageBusy("Triage is already running; try again shortly.")
        try:
            run = self._run(automatic)
        finally:
            self._lock.release()
        self._last = run
        return run

    def confirm(self, key: str, fingerprint: str) -> TriageRun:
        """Move one album someone confirmed, if it's still the plan they saw. Raises
        TriageBusy, RuntimeError (a scan or rename is running) or RenameError."""
        if not self._lock.acquire(blocking=False):
            raise TriageBusy("Triage is running; try again shortly.")
        try:
            with self.runner.paused(), Store(self.settings.database_path) as store:
                try:
                    result = confirm_album(
                        self.settings, store, key, fingerprint, cache=self._cache
                    )
                except RenameError as exc:
                    if exc.partial and self.notifications is not None:
                        self.notifications.rename_failed(key, str(exc))
                    raise
        finally:
            self._lock.release()
        run = TriageRun(time.time(), automatic=False, moved=[(result.target, result)])
        run.rescanning = self.runner.start(automatic=True, reason="triage")
        log.info("Triage: moved %s (confirmed)", result.target)
        return run

    def _run(self, automatic: bool) -> TriageRun:
        run = TriageRun(time.time(), automatic)
        plan = self.plan()
        if not plan.status.ok:
            run.note = " ".join(plan.status.problems)
            return run
        lookups = [a.release_mbid for a in plan.albums if a.needs_lookup]
        if lookups and self.mb_sync is not None:
            try:
                result = self.mb_sync.refresh(lookups)
            except LookupRefused as exc:
                run.note = f"MusicBrainz: {exc}"
            else:
                if result.error:
                    run.note = f"MusicBrainz lookups stopped: {result.error}"
                if result.changed:
                    plan = self.plan()

        ready = plan.by_status(READY)
        if ready and plan.status.warnings:
            run.note = " ".join(plan.status.warnings)
            ready = []
        if ready:
            try:
                with self.runner.paused(), Store(self.settings.database_path) as store:
                    for album in ready:
                        try:
                            run.moved.append((album.label, move_album(self.settings, store, album)))
                        except RenameError as exc:
                            run.failed.append((album.label, str(exc)))
                            if exc.partial:
                                if self.notifications is not None:
                                    self.notifications.rename_failed(album.target, str(exc))
                                break
            except RuntimeError as exc:  # a scan or rename is running: next time
                run.note = f"Nothing moved: {exc}"
        for label, result in run.moved:
            log.info("Triage: moved %s to %s", label, result.target)
        for label, why in run.failed:
            log.warning("Triage: couldn't move %s: %s", label, why)
        if run.moved:
            run.rescanning = self.runner.start(automatic=True, reason="triage")
        self._announce(run, plan)
        return run

    def _announce(self, run: TriageRun, plan: TriagePlan) -> None:
        """Tell about moves, and about albums that newly wait for a decision."""
        waiting = [
            a
            for a in plan.by_status(CONFIRM) + plan.by_status(BLOCKED)
            if (a.key, a.fingerprint) not in self._announced
        ]
        self._announced |= {(a.key, a.fingerprint) for a in waiting}
        if self.notifications is not None and (run.moved or waiting):
            self.notifications.triage_finished(
                [(label, r.target) for label, r in run.moved],
                [(a.label, a.status) for a in waiting],
            )
