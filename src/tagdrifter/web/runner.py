"""Runs one library scan at a time in a background thread and exposes its progress."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ..config import Settings
from ..db import Store
from ..notify import Notifications
from ..scanner import ScanProgress, ScanResult, run_scan
from .mbsync import MusicBrainzSync

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanStatus:
    """A snapshot for the UI. `progress` is None until the scan reports its first step."""

    running: bool
    progress: ScanProgress | None = None
    phase_elapsed: float = 0.0  # seconds since the current phase started
    started_at: float | None = None  # time.time() when the scan started
    result: ScanResult | None = None  # the last finished scan started from this process
    error: str | None = None  # why the last scan failed

    @property
    def rate(self) -> float:
        """Items per second in the current phase (meaningful while reading tags)."""
        if self.progress is None or self.phase_elapsed <= 0:
            return 0.0
        return self.progress.done / self.phase_elapsed

    @property
    def eta(self) -> float | None:
        p = self.progress
        if p is None or not p.total or not self.rate:
            return None
        return (p.total - p.done) / self.rate

    @property
    def fraction(self) -> float | None:
        p = self.progress
        return p.done / p.total if p is not None and p.total else None


class ScanRunner:
    def __init__(
        self,
        settings: Settings,
        notifications: Notifications | None = None,
        mb_sync: MusicBrainzSync | None = None,
    ) -> None:
        self.settings = settings
        self.notifications = notifications
        self.mb_sync = mb_sync
        if mb_sync is not None:
            mb_sync.rescan = self.rescan
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._progress: ScanProgress | None = None
        self._phase_started = 0.0
        self._started_at: float | None = None
        self._result: ScanResult | None = None
        self._error: str | None = None
        self._paused = False

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Hold off scans while files are renamed. Raises RuntimeError if a scan is running or
        another rename holds it."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A scan is running; try again when it finishes.")
            if self._paused:
                raise RuntimeError("Another rename is in progress.")
            self._paused = True
        try:
            yield
        finally:
            with self._lock:
                self._paused = False

    def start(self, *, full: bool = False, automatic: bool = False, reason: str = "manual") -> bool:
        """Start a scan unless one is already running (or renames are in progress). Returns
        whether one was started. `automatic`: nobody pressed a button (the schedule, or a
        rescan after background lookups), so MusicBrainz lookups are left to the background
        trickle instead of all starting at once. `reason`: why it runs (db.SCAN_REASONS)."""
        with self._lock:
            if self._paused or (self._thread is not None and self._thread.is_alive()):
                return False
            self._progress = None
            self._started_at = time.time()
            self._error = None
            self._thread = threading.Thread(
                target=self._run,
                kwargs={"full": full, "automatic": automatic, "reason": reason},
                name="tagdrifter-scan",
                daemon=True,
            )
            self._thread.start()
            return True

    def rescan(self) -> bool:
        """Apply freshly fetched MusicBrainz data: an automatic scan, so it doesn't start
        another round of lookups."""
        return self.start(automatic=True, reason="musicbrainz")

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def status(self) -> ScanStatus:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return ScanStatus(
                running=running,
                progress=self._progress if running else None,
                phase_elapsed=time.monotonic() - self._phase_started if running else 0.0,
                started_at=self._started_at,
                result=self._result,
                error=self._error,
            )

    def _on_progress(self, progress: ScanProgress) -> None:
        with self._lock:
            if self._progress is None or self._progress.phase != progress.phase:
                self._phase_started = time.monotonic()
            self._progress = progress

    def _run(self, full: bool, automatic: bool = False, reason: str = "manual") -> None:
        log.info("Scan started (%s)", "full" if full else "incremental")
        try:
            with Store(self.settings.database_path) as store:
                result = run_scan(self.settings, store, self._on_progress, full=full, reason=reason)
        except Exception as exc:  # reported in the UI; run_scan already marked the scan failed
            log.exception("Scan failed")
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            if self.notifications is not None:
                self.notifications.scan_failed(self._error)
            return
        log.info(
            "Scan #%d finished in %.0fs: %d files, %d read; %d open issues (+%d, -%d)",
            result.scan_id,
            result.duration,
            result.files_total,
            result.files_read,
            result.issues_open,
            result.issues_new,
            result.issues_resolved,
        )
        with self._lock:
            self._result = result
        if self.notifications is not None:
            self.notifications.scan_finished(
                result.scan_id, result.issues_new, result.issues_resolved
            )
        if self.mb_sync is not None:
            try:
                self.mb_sync.after_scan(automatic=automatic)
            except Exception:
                log.exception("Couldn't start MusicBrainz lookups")
