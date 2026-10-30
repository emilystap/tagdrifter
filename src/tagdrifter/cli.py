"""Command line interface: `tagdrifter scan | issues | checks | scans | serve`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

from . import __version__
from .checks import REGISTRY
from .config import Settings, load_settings
from .db import Store
from .scanner import ScanProgress, run_scan


class StatusLine:
    """A self-overwriting status line on a terminal; sparse plain lines when piped."""

    def __init__(self) -> None:
        self.tty = sys.stderr.isatty()
        self.interval = 0.2 if self.tty else 10.0
        self._last = 0.0
        self._width = 0

    def update(self, text: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if (not force or not self.tty) and now - self._last < self.interval:
            return
        self._last = now
        if self.tty:
            try:
                columns = os.get_terminal_size(sys.stderr.fileno()).columns
            except OSError:
                columns = 120
            text = text[: max(20, columns - 1)]
            sys.stderr.write("\r" + text.ljust(self._width))
            sys.stderr.flush()
            self._width = len(text)
        else:
            print(text, file=sys.stderr, flush=True)

    def clear(self) -> None:
        if self.tty and self._width:
            sys.stderr.write("\r" + " " * self._width + "\r")
            sys.stderr.flush()
            self._width = 0


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _progress_printer(status: StatusLine):
    phase_started: dict[str, float] = {}
    last_phase = [""]

    def on_progress(p: ScanProgress) -> None:
        now = time.monotonic()
        phase_started.setdefault(p.phase, now)
        changed = p.phase != last_phase[0]
        last_phase[0] = p.phase
        elapsed = now - phase_started[p.phase]
        if p.phase == "discover":
            text = f"Discovering… {p.done:,} audio files ({fmt_duration(elapsed)}) · {p.current}"
        elif p.phase == "read":
            if p.total == 0:
                text = "Reading tags… nothing changed since the last scan"
            else:
                rate = p.done / elapsed if elapsed > 0 else 0
                eta = fmt_duration((p.total - p.done) / rate) if rate else "?"
                text = (
                    f"Reading tags {p.done:,}/{p.total:,} ({p.done / p.total:.0%}) · "
                    f"{rate:.0f}/s · ETA {eta} · {p.current}"
                )
        elif p.phase == "check":
            text = f"Checking {p.done:,}/{p.total:,} artists · {p.current}"
        else:
            text = "Saving results…"
        status.update(text, force=changed or p.done == p.total)

    return on_progress


# --- Commands ---------------------------------------------------------------------------


def cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    print(
        f"Scanning {settings.library_path} "
        f"(case {'sensitive' if settings.case_sensitive else 'insensitive'}, "
        f"database {settings.database_path})",
        file=sys.stderr,
    )
    status = StatusLine()
    with Store(settings.database_path) as store:
        try:
            result = run_scan(settings, store, _progress_printer(status), full=args.full)
        except KeyboardInterrupt:
            status.clear()
            print(
                "Scan cancelled. Tags read so far are saved; the next scan resumes from there.",
                file=sys.stderr,
            )
            return 130
        status.clear()
        print(
            f"Scan #{result.scan_id} finished in {fmt_duration(result.duration)}: "
            f"{result.files_total:,} audio files, {result.files_read:,} read, "
            f"{result.files_removed:,} removed since last scan."
        )
        print(
            f"Issues: {result.issues_open:,} open "
            f"({result.issues_new:,} new, {result.issues_resolved:,} resolved)."
        )
        for err in result.check_errors[:10]:
            print(f"  check error: {err}", file=sys.stderr)
        _print_counts(store)
    return 0


def _print_counts(store: Store) -> None:
    counts = store.open_issue_counts()
    if not counts:
        return
    by_check: Counter[str] = Counter()
    severity: dict[str, str] = {}
    for (check_id, sev), n in counts.items():
        by_check[check_id] += n
        severity[check_id] = sev
    width = max(len(c) for c in by_check)
    print()
    for check_id, n in sorted(by_check.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {check_id:<{width}}  {n:>6,}  {severity[check_id]}")


def cmd_issues(settings: Settings, args: argparse.Namespace) -> int:
    with Store(settings.database_path) as store:
        rows = store.open_issues(
            check_id=args.check,
            path_prefix=args.path,
            severity=args.severity,
            mbid_tagged=None if args.mbid is None else args.mbid == "yes",
            limit=None if args.json else args.limit,
        )
        if args.json:
            json.dump(
                [
                    {
                        "id": r.id,
                        "check": r.check_id,
                        "severity": r.severity,
                        "subject": r.subject,
                        "message": r.message,
                        "details": r.details,
                        "mbid_tagged": r.mbid_tagged,
                        "first_seen_scan": r.first_seen_scan,
                    }
                    for r in rows
                ],
                sys.stdout,
                ensure_ascii=False,
                indent=2,
            )
            print()
            return 0
        if not rows:
            print("No open issues match.")
            return 0
        current = None
        for r in rows:
            if r.check_id != current:
                current = r.check_id
                print(f"\n== {current} ==")
            untrusted = " (no MBIDs)" if r.mbid_tagged is False else ""
            print(f"[{r.severity}] {r.subject}{untrusted}\n    {r.message}")
        if args.limit and len(rows) == args.limit:
            print(
                f"\n(showing first {args.limit}; use --limit 0 for all, or filter with "
                "--check/--path/--severity/--mbid)"
            )
    return 0


def cmd_checks(settings: Settings, args: argparse.Namespace) -> int:
    width = max(len(c) for c in REGISTRY)
    for check_id, cls in sorted(REGISTRY.items()):
        state = "disabled" if check_id in settings.disabled_checks else "enabled "
        print(
            f"{check_id:<{width}}  {state}  {cls.scope.value:<7}  {cls.severity.value:<7}  "
            f"{cls.description}"
        )
    return 0


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn  # imported lazily: only the web UI needs it

    from .web import create_app

    try:
        app = create_app(settings)
    except ValueError as exc:  # e.g. an invalid scan schedule
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Show the app's own log lines (scheduled scans, scan results) next to uvicorn's.
    logging.basicConfig(
        level=args.log_level.upper(), format="%(levelname)-9s %(name)s: %(message)s"
    )
    if args.log_level != "debug":
        logging.getLogger("apscheduler").setLevel(logging.WARNING)  # job bookkeeping noise
    print(
        f"Tag Drifter UI on http://{args.host}:{args.port} "
        f"(library {settings.library_path}, database {settings.database_path})",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_scans(settings: Settings, args: argparse.Namespace) -> int:
    with Store(settings.database_path) as store:
        rows = store.recent_scans(args.limit)
    if not rows:
        print("No scans yet.")
    for r in rows:
        print(
            f"#{r['id']:<4} {r['started_at']}  {r['status']:<9}  files {r['files_total']:>6,}  "
            f"read {r['files_read']:>6,}  issues {r['issues_open']:>5,} "
            f"(+{r['issues_new']}, -{r['issues_resolved']})"
        )
    return 0


# --- Entry point ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tagdrifter", description="Music library drift monitor.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path, help="config.toml (default: ./config.toml)")
    parser.add_argument("--library", type=Path, help="library root (overrides config)")
    parser.add_argument("--db", type=Path, help="SQLite database path (overrides config)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan the library and record issues")
    scan.add_argument("--full", action="store_true", help="re-read every file, ignoring the cache")
    scan.add_argument("--workers", type=int, help="parallel file reads")
    case = scan.add_mutually_exclusive_group()
    case.add_argument("--case-sensitive", dest="case_sensitive", action="store_true", default=None)
    case.add_argument("--case-insensitive", dest="case_sensitive", action="store_false")
    scan.set_defaults(func=cmd_scan)

    issues = sub.add_parser("issues", help="list open issues from the last scan")
    issues.add_argument("--check", help="only this check id (see `tagdrifter checks`)")
    issues.add_argument("--path", help="only subjects under this library-relative path")
    issues.add_argument("--severity", choices=["error", "warning", "info"])
    issues.add_argument(
        "--mbid",
        choices=["yes", "no"],
        help="only issues whose track/album has (yes) or lacks (no) release and recording MBIDs",
    )
    issues.add_argument("--limit", type=int, default=100, help="max rows (0 = all)")
    issues.add_argument("--json", action="store_true", help="machine-readable output")
    issues.set_defaults(func=cmd_issues)

    checks = sub.add_parser("checks", help="list available checks")
    checks.set_defaults(func=cmd_checks)

    scans = sub.add_parser("scans", help="show recent scan history")
    scans.add_argument("--limit", type=int, default=20)
    scans.set_defaults(func=cmd_scans)

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 in Docker)")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"]
    )
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config, library_path=args.library, database_path=args.db)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "workers", None):
        settings = replace(settings, workers=args.workers)
    if getattr(args, "case_sensitive", None) is not None:
        settings = replace(settings, case_sensitive=args.case_sensitive)
    if getattr(args, "limit", None) == 0:
        args.limit = None
    try:
        return args.func(settings, args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
