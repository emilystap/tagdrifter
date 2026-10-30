"""Runs one library scan at a time in a background thread and exposes its progress."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ..config import Settings
from ..db import Store
from ..scanner import ScanProgress, ScanResult, run_scan


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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._progress: ScanProgress | None = None
        self._phase_started = 0.0
        self._started_at: float | None = None
        self._result: ScanResult | None = None
        self._error: str | None = None

    def start(self, *, full: bool = False) -> bool:
        """Start a scan unless one is already running. Returns whether one was started."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._progress = None
            self._started_at = time.time()
            self._error = None
            self._thread = threading.Thread(
                target=self._run, kwargs={"full": full}, name="tagdrifter-scan", daemon=True
            )
            self._thread.start()
            return True

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

    def _run(self, full: bool) -> None:
        try:
            with Store(self.settings.database_path) as store:
                result = run_scan(self.settings, store, self._on_progress, full=full)
        except Exception as exc:  # reported in the UI; run_scan already marked the scan failed
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            return
        with self._lock:
            self._result = result
