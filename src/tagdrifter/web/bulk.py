"""Applies a previewed bulk rename (many albums) in a background thread, with progress.

Scans are held off for the whole run (`ScanRunner.paused`), each album is its own journal
batch (renamer.apply_albums), and one rescan follows at the end. Only one bulk rename runs at
a time, and a single-album rename can't start while it does (they share the pause).
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass

from ..config import Settings
from ..db import Store
from ..notify import Notifications
from ..renamer import BulkResult, apply_albums
from .runner import ScanRunner

log = logging.getLogger(__name__)

RESULT_SHOWN = 30 * 60  # seconds a finished run's outcome stays on the Issues/Renames pages


@dataclass(frozen=True)
class BulkStatus:
    running: bool
    done: int = 0
    total: int = 0
    current: str = ""  # the album being renamed
    stopping: bool = False
    result: BulkResult | None = None  # the last finished run
    error: str | None = None  # an unexpected failure of the last run
    finished_at: float | None = None

    @property
    def fraction(self) -> float | None:
        return self.done / self.total if self.total else None

    @property
    def shown(self) -> bool:
        """Worth showing: running, or finished in the last RESULT_SHOWN seconds (so the
        outcome survives the page reload after the rescan)."""
        recent = self.finished_at is not None and time.time() - self.finished_at < RESULT_SHOWN
        return self.running or recent


class BulkRenamer:
    def __init__(
        self, settings: Settings, runner: ScanRunner, notifications: Notifications | None = None
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.notifications = notifications
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._done = 0
        self._total = 0
        self._current = ""
        self._result: BulkResult | None = None
        self._error: str | None = None
        self._finished_at: float | None = None

    def start(self, plans: list[tuple[str, str]]) -> None:
        """Start renaming (album, fingerprint) plans. Raises RuntimeError when a scan or
        another rename is running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A bulk rename is already running.")
            pause = ExitStack()
            pause.enter_context(self.runner.paused())  # raises if a scan or rename runs
            self._stop.clear()
            self._done, self._total, self._current = 0, len(plans), ""
            self._result = self._error = None
            self._thread = threading.Thread(
                target=self._run, args=(plans, pause), name="tagdrifter-rename", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop after the album being renamed (it's never left half-done on purpose)."""
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def status(self) -> BulkStatus:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return BulkStatus(
                running=running,
                done=self._done,
                total=self._total,
                current=self._current if running else "",
                stopping=running and self._stop.is_set(),
                result=self._result,
                error=self._error,
                finished_at=self._finished_at,
            )

    def _progress(self, done: int, total: int, album: str) -> None:
        with self._lock:
            self._done, self._total, self._current = done, total, album

    def _run(self, plans: list[tuple[str, str]], pause: ExitStack) -> None:
        result: BulkResult | None = None
        error: str | None = None
        with pause:
            log.info("Bulk rename started: %d albums", len(plans))
            try:
                with Store(self.settings.database_path) as store:
                    result = apply_albums(
                        self.settings, store, plans, progress=self._progress, stop=self._stop
                    )
            except Exception as exc:  # reported in the UI; never take down the app
                log.exception("Bulk rename failed")
                error = f"{type(exc).__name__}: {exc}"
        if result is not None:
            log.info(
                "Bulk rename finished: %d albums renamed, %d failed, %d not attempted",
                len(result.applied),
                len(result.failed),
                len(result.skipped),
            )
            if result.partial and self.notifications is not None:
                why = dict(result.failed)[result.partial]
                self.notifications.rename_failed(result.partial, why)
        with self._lock:
            self._result, self._error, self._finished_at = result, error, time.time()
        if result is None or result.applied:
            self.runner.start()  # one rescan for everything that moved
