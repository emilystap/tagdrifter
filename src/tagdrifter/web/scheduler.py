"""Automatic scans on a cron schedule while the web app runs (APScheduler)."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .runner import ScanRunner

log = logging.getLogger(__name__)

OFF_VALUES = frozenset({"", "off", "none", "disabled", "false", "0"})


RE_NUMERIC_DOW = re.compile(r"^(\*|\d+(?:-\d+)?)(?:/(\d+))?$")


def _cron_weekdays(field: str) -> str:
    """Translate a standard cron weekday field (0 or 7 = Sunday) for APScheduler 3.

    APScheduler 3 numbers weekdays from 0 = Monday, so `* * * * 1` would otherwise run on
    Tuesdays, and steps (`*/2`) would pick different days. Numeric parts are expanded to an
    explicit list in APScheduler's numbering; day names mean the same to both and pass through.
    """
    if field == "*":
        return field
    days: set[int] = set()  # standard numbering, 0 = Sunday
    names = []
    for part in field.split(","):
        m = RE_NUMERIC_DOW.match(part)
        if not m:
            names.append(part)
            continue
        base, step = m[1], int(m[2] or 1)
        if base == "*":
            lo, hi = 0, 6
        elif "-" in base:
            lo, hi = map(int, base.split("-"))
        else:
            lo = int(base)
            hi = 7 if m[2] else lo  # "5/2" means from 5 to the end of the week
        if not (0 <= lo <= hi <= 7) or step < 1:
            raise ValueError(f"invalid weekday {part!r}")
        days |= {d % 7 for d in range(lo, hi + 1, step)}
    aps_days = sorted((d - 1) % 7 for d in days)  # Sunday 0 -> 6, Monday 1 -> 0, ...
    return ",".join([*map(str, aps_days), *names])


def parse_schedule(expression: str) -> CronTrigger | None:
    """A trigger for a 5-field cron expression in the local time zone (TZ), or None for "off"."""
    if expression.strip().lower() in OFF_VALUES:
        return None
    try:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(f"expected 5 fields, got {len(fields)}")
        minute, hour, day, month, weekday = fields
        return CronTrigger(
            minute=minute, hour=hour, day=day, month=month, day_of_week=_cron_weekdays(weekday)
        )
    except (ValueError, IndexError) as exc:
        raise ValueError(
            f"invalid scan schedule {expression!r} (expected a cron expression such as "
            f'"0 4 * * *", or "off"): {exc}'
        ) from exc


class ScanScheduler:
    """Starts a scan via the ScanRunner whenever the schedule fires. A run that comes due while
    a scan is already going (e.g. one started with "Scan now") is skipped."""

    def __init__(self, runner: ScanRunner, expression: str) -> None:
        self.runner = runner
        self.expression = expression
        self.trigger = parse_schedule(expression)
        self._scheduler = BackgroundScheduler(daemon=True)

    @property
    def enabled(self) -> bool:
        return self.trigger is not None

    def start(self) -> None:
        if self.trigger is None:
            log.info("Automatic scans are off")
            return
        self._scheduler.add_job(
            self._fire,
            self.trigger,
            id="scan",
            max_instances=1,
            coalesce=True,  # after downtime, catch up with one scan rather than several
            misfire_grace_time=3600,
        )
        self._scheduler.start()
        log.info("Automatic scans on %r; next at %s", self.expression, self.next_run())

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def next_run(self) -> datetime | None:
        job = self._scheduler.get_job("scan") if self._scheduler.running else None
        return job.next_run_time if job else None

    def _fire(self) -> None:
        if not self.runner.start(automatic=True, reason="schedule"):
            log.info("Scheduled scan skipped: a scan is already running")
