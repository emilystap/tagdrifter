"""Path comparison that respects the library's case sensitivity and Unicode normalization."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePath

from .sanitize import nfc


class PathMatch(StrEnum):
    EXACT = "exact"
    CASE_ONLY = "case_only"  # differ only by letter case
    DIFFERENT = "different"


def path_key(path: PurePath | str, *, case_sensitive: bool) -> str:
    """Comparison key for a path: NFC-normalized, case-folded when the filesystem ignores case."""
    text = nfc(str(path))
    return text if case_sensitive else text.casefold()


def compare_paths(current: PurePath | str, target: PurePath | str) -> PathMatch:
    a, b = nfc(str(current)), nfc(str(target))
    if a == b:
        return PathMatch.EXACT
    if a.casefold() == b.casefold():
        return PathMatch.CASE_ONLY
    return PathMatch.DIFFERENT


def is_mismatch(match: PathMatch, *, case_sensitive: bool) -> bool:
    """Whether a comparison result counts as a mismatch.

    On a case-sensitive library a case-only difference is a real difference (and a rename
    is needed). On a case-insensitive one it is ignored.
    """
    if match is PathMatch.EXACT:
        return False
    if match is PathMatch.CASE_ONLY:
        return case_sensitive
    return True


def find_case_collisions(names: Iterable[str]) -> list[list[str]]:
    """Groups of sibling names that differ only by case (or Unicode normalization).

    These coexist on a case-sensitive ZFS dataset but collide for SMB/Windows clients, which
    may show only one of them.
    """
    groups: dict[str, set[str]] = defaultdict(set)
    for name in names:
        groups[nfc(name).casefold()].add(name)
    return [sorted(g) for g in groups.values() if len(g) > 1]
