"""A secondary library kept in step with the library by hard links (core/secondary.py plans).

The secondary is a folder with the library's structure, made of hard links to its files minus
excluded artists, releases, tracks or paths, for a music server or another reader that can't
exclude anything itself. Links take no space, and tag edits made in place show in both.

This module only reads (roadmap: applying the plan comes next). Safety rules, checked here:

- The secondary is set explicitly (`secondary_path`) and is neither the library, inside it,
  nor around it, nor a filesystem root (`validate_settings`, at startup).
- Its root holds a marker file (MARKER) that you create by hand, so a path pointing at the
  wrong folder does nothing.
- Both are on one filesystem (`st_dev`); links between two mounts fail (EXDEV) even when they
  are the same filesystem, so applying will also need them under one mount.
- Walks never follow symlinks.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .config import Settings
from .core.secondary import (
    AudioKeys,
    Exclusion,
    FileEntry,
    Proposal,
    SyncPlan,
    Tree,
    audio_keys,
    excluded_files,
    plan_sync,
    propose_exclusions,
    rule_matches,
)
from .db import Store

MARKER = ".tagdrifter-secondary"
MOUNTINFO = Path("/proc/self/mountinfo")


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(path))


def validate_settings(settings: Settings) -> None:
    """Refuse a secondary path that overlaps the library or is a filesystem root (raises
    ValueError). Nothing to check when no secondary is set."""
    if settings.secondary_path is None:
        return
    secondary = _resolved(settings.secondary_path)
    library = _resolved(settings.library_path)
    if secondary == Path(secondary.anchor):
        raise ValueError(f"secondary path is a filesystem root: {settings.secondary_path}")
    if secondary == library:
        raise ValueError("secondary path is the library itself")
    if secondary.is_relative_to(library):
        raise ValueError(f"secondary path is inside the library: {settings.secondary_path}")
    if library.is_relative_to(secondary):
        raise ValueError(f"secondary path contains the library: {settings.secondary_path}")


@dataclass
class Status:
    problems: list[str] = field(default_factory=list)  # nothing can be planned
    warnings: list[str] = field(default_factory=list)  # fine for a preview, not for applying

    @property
    def ok(self) -> bool:
        return not self.problems


def _mount_point(path: Path, mounts: list[str]) -> str:
    text = str(path)
    return max(
        (m for m in mounts if text == m or text.startswith(m.rstrip("/") + "/")),
        key=len,
        default="/",
    )


def _mounts() -> list[str]:
    """Mount points from /proc/self/mountinfo (Linux); [] elsewhere."""
    try:
        lines = MOUNTINFO.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    points = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4:
            # Octal escapes for space, tab, newline and backslash
            points.append(re.sub(r"\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
    return points


def check(settings: Settings) -> Status:
    """Whether the secondary can be planned (problems) and later written (warnings)."""
    status = Status()
    secondary, library = settings.secondary_path, settings.library_path
    if secondary is None:
        status.problems.append("No secondary library is set.")
        return status
    if not secondary.is_dir():
        status.problems.append(f"{secondary} isn't a folder (or isn't mounted).")
        return status
    if not library.is_dir():
        status.problems.append(f"The library {library} isn't a folder (or isn't mounted).")
        return status
    if not (secondary / MARKER).is_file():
        status.problems.append(
            f"{secondary} has no {MARKER} file. Create an empty file with that name in it to "
            "confirm it's the secondary library (a safeguard against a wrong path)."
        )
    if secondary.stat().st_dev != library.stat().st_dev:
        status.problems.append(
            "The secondary and the library are on different filesystems, so they can't share "
            "files by hard links."
        )
    if status.problems:
        return status
    mounts = _mounts()
    if mounts and _mount_point(_resolved(secondary), mounts) != _mount_point(
        _resolved(library), mounts
    ):
        status.warnings.append(
            "The secondary and the library are separate mounts. Links can't cross mounts, "
            "even of one filesystem, so mount their common parent folder once instead."
        )
    if not os.access(secondary, os.W_OK):
        status.warnings.append("The secondary is read-only here, which is fine for a preview.")
    return status


def walk(root: Path, ignore: tuple[str, ...], *, skip: frozenset[str] = frozenset()) -> Tree:
    """Every file and folder below `root` (no symlink following), minus ignored names and the
    root-level names in `skip`."""
    tree = Tree()
    stack = [PurePosixPath()]
    while stack:
        rel = stack.pop()
        with os.scandir(root.joinpath(*rel.parts)) as entries:
            for entry in entries:
                if (not rel.parts and entry.name in skip) or any(
                    fnmatchcase(entry.name, pat) for pat in ignore
                ):
                    if rel.parts:
                        tree.ignored_in.add(str(rel))
                    continue
                path = str(rel / entry.name)
                if entry.is_dir(follow_symlinks=False):
                    tree.dirs.add(path)
                    stack.append(rel / entry.name)
                elif entry.is_file(follow_symlinks=False):
                    # DirEntry.stat() has no inode or link count on Windows.
                    st = entry.stat(follow_symlinks=False) if os.name != "nt" else None
                    st = st or os.lstat(entry.path)
                    tree.files[path] = FileEntry(st.st_ino, st.st_nlink, st.st_size, st.st_mtime_ns)
                else:
                    tree.other.append(path)
    return tree


@dataclass(frozen=True)
class RuleView:
    rule: Exclusion
    files: int  # audio files it matches now
    folder: str  # the first one's folder ('' = none)


@dataclass
class Analysis:
    status: Status
    plan: SyncPlan | None = None
    rules: list[RuleView] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    main_files: int = 0
    excluded: int = 0
    # Audio files that changed or appeared since the last scan: their tags (and so their
    # MBID rules) aren't known, so only path rules apply to them
    unscanned: int = 0
    main_other: int = 0  # symlinks etc. in the library: not linked


def analyze(settings: Settings, store: Store) -> Analysis:
    """Walk both trees and work out the sync plan, how each exclusion applies, and the
    exclusions that would describe what the secondary leaves out now. Reads only."""
    status = check(settings)
    rules = store.secondary_exclusions()
    analysis = Analysis(status, rules=[RuleView(r, 0, "") for r in rules])
    if not status.ok:
        return analysis
    assert settings.secondary_path is not None
    main = walk(settings.library_path, settings.ignore_patterns)
    secondary = walk(settings.secondary_path, settings.ignore_patterns, skip=frozenset({MARKER}))

    cache = store.load_file_cache()
    audio: dict[str, AudioKeys] = {}
    exts = settings.audio_extensions
    for path, entry in main.files.items():
        if PurePosixPath(path).suffix.lower() not in exts:
            continue
        cached = cache.get(path)
        if cached is None or (cached.size, cached.mtime_ns) != (entry.size, entry.mtime_ns):
            analysis.unscanned += 1
            audio[path] = AudioKeys()
        else:
            audio[path] = audio_keys(cached.tags)
    excluded = excluded_files(main.files, audio, rules, settings.sidecar_extensions)
    plan = plan_sync(main, secondary, excluded)

    views = []
    for rule in rules:
        matched = sorted(p for p, keys in audio.items() if rule_matches(rule, p, keys))
        folder = str(PurePosixPath(matched[0]).parent) if matched else ""
        views.append(RuleView(rule, len(matched), folder))
    missing = [p for p in plan.links if p in audio]
    analysis.plan = plan
    analysis.rules = views
    analysis.proposals = propose_exclusions(audio, missing, excluded)
    analysis.main_files = len(main.files)
    analysis.excluded = len(excluded)
    analysis.main_other = len(main.other)
    return analysis
