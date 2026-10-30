"""Jinja filters and display helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from difflib import SequenceMatcher

from markupsafe import Markup, escape

PLACEHOLDER = chr(0x2013)  # en dash, for missing values


def parse_ts(value: str | datetime | None) -> datetime | None:
    """Parse a stored ISO timestamp (naive ones are UTC); datetimes pass through."""
    if not value:
        return None
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def duration(seconds: float | None) -> str:
    if seconds is None:
        return PLACEHOLDER
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def scan_duration(row) -> str:
    start, end = parse_ts(row["started_at"]), parse_ts(row["finished_at"])
    return duration((end - start).total_seconds()) if start and end else PLACEHOLDER


def ago(value: str | None, now: datetime | None = None) -> str:
    ts = parse_ts(value)
    if ts is None:
        return "never"
    seconds = ((now or datetime.now(UTC)) - ts).total_seconds()
    for limit, unit, size in ((60, "s", 1), (3600, "min", 60), (86400, "h", 3600)):
        if seconds < limit:
            return "just now" if unit == "s" else f"{int(seconds // size)} {unit} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def timestamp(value: str | datetime | None) -> Markup:
    """A <time> element; a small script in base.html rewrites it in the viewer's local time."""
    ts = parse_ts(value)
    if ts is None:
        return Markup(PLACEHOLDER)
    return Markup('<time datetime="{}">{}</time>').format(
        ts.isoformat(), ts.strftime("%Y-%m-%d %H:%M UTC")
    )


def diff_marks(current: str, expected: str) -> tuple[Markup, Markup]:
    """Both strings as HTML, with the characters that differ wrapped in <mark>.

    Case-only and lookalike-character differences (e.g. `:` vs U+A789) are otherwise hard to spot.
    """
    a_out: list[str] = []
    b_out: list[str] = []
    matcher = SequenceMatcher(None, current, expected, autojunk=False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        a_part, b_part = escape(current[a1:a2]), escape(expected[b1:b2])
        if op == "equal":
            a_out.append(a_part)
            b_out.append(b_part)
        else:
            if a_part:
                a_out.append(Markup("<mark>{}</mark>").format(a_part))
            if b_part:
                b_out.append(Markup("<mark>{}</mark>").format(b_part))
    return Markup("").join(a_out), Markup("").join(b_out)


def expected_location(check_id: str, subject: str, details: dict) -> tuple[str, str] | None:
    """(current, expected) for issues that propose a different path, else None."""
    if check_id == "path.track_location" and details.get("expected"):
        return subject, details["expected"]
    if check_id == "album.placement" and details.get("expected_artist_folder"):
        _, _, rest = subject.partition("/")
        expected = details["expected_artist_folder"]
        return subject, f"{expected}/{rest}" if rest else expected
    return None
