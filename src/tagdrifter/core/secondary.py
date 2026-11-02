"""Planning for a secondary library: a copy of the library made of hard links to its files,
minus excluded artists, releases, tracks or paths (secondary.py walks the trees).

A hard link shares its inode with the library's file, so the plan needs no state of its own:
a secondary file whose inode belongs to a library file at another path was renamed there and
moves to match; a library file with no link anywhere in the secondary gets one. Exclusions
are keyed on MusicBrainz IDs where the tags have them, so renaming an excluded album doesn't
bring it back; a path rule covers files without them.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .metadata import is_mbid

# Exclusion kinds, in the order the bootstrap proposes them.
EXCLUSION_KINDS = {
    "artist": "Album artist",  # key: the album artist MBIDs, lower-case, joined with ";"
    "release": "Release",  # key: release MBID
    "track": "Track",  # key: release track MBID
    "path": "Path",  # key: a library-relative folder or file; matches everything under it
}


@dataclass(frozen=True)
class Exclusion:
    kind: str
    key: str
    label: str
    id: int | None = None


@dataclass(frozen=True)
class FileEntry:
    """A regular file as `lstat` sees it."""

    ino: int
    nlink: int
    size: int
    mtime_ns: int = 0


@dataclass
class Tree:
    """One walk of a library or secondary root ('/'-separated relative paths)."""

    files: dict[str, FileEntry] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)  # every folder below the root
    # Folders holding ignored names (never removed: rmdir would fail, and they're not ours)
    ignored_in: set[str] = field(default_factory=set)
    other: list[str] = field(default_factory=list)  # symlinks, sockets, ...: left alone


@dataclass(frozen=True)
class AudioKeys:
    """What exclusion rules match an audio file by ('' = no valid MBID)."""

    artist: str = ""
    release: str = ""
    track: str = ""
    artist_label: str = ""
    release_label: str = ""
    track_label: str = ""


def _mbid(tags: Mapping[str, Sequence[str]], key: str) -> str:
    value = next(iter(tags.get(key, ())), "").strip().lower()
    return value if is_mbid(value) else ""


def audio_keys(tags: Mapping[str, Sequence[str]] | None) -> AudioKeys:
    """Exclusion keys and labels from a file's tags (None = not read yet: no keys)."""
    if not tags:
        return AudioKeys()

    def first(key: str) -> str:
        return next((v for v in tags.get(key, ()) if v.strip()), "").strip()

    artists = [v.strip().lower() for v in tags.get("musicbrainz_albumartistid", ())]
    album_artist = first("albumartist") or first("artist")
    return AudioKeys(
        artist=";".join(artists) if artists and all(map(is_mbid, artists)) else "",
        release=_mbid(tags, "musicbrainz_albumid"),
        track=_mbid(tags, "musicbrainz_releasetrackid"),
        artist_label=album_artist,
        release_label=" - ".join(filter(None, (album_artist, first("album")))),
        track_label=" - ".join(filter(None, (first("artist"), first("title")))),
    )


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def _ancestors(path: str) -> Iterable[str]:
    """Folders containing `path`, innermost first, excluding the root."""
    for parent in PurePosixPath(path).parents:
        if parent.parts:
            yield str(parent)


def rule_matches(rule: Exclusion, path: str, keys: AudioKeys) -> bool:
    if rule.kind == "path":
        return _under(path, rule.key)
    return bool(rule.key) and getattr(keys, rule.kind, "") == rule.key


def excluded_files(
    files: Iterable[str],
    audio: Mapping[str, AudioKeys],
    rules: Sequence[Exclusion],
    sidecar_extensions: Sequence[str],
) -> set[str]:
    """Library files the rules leave out of the secondary.

    Audio files (the keys of `audio`) match rules directly. Any other file matches a path
    rule; a sidecar (same name as a track, with a sidecar extension) follows its track;
    everything else (cover images, scans, ...) follows the nearest folder above it with audio
    files below: it's left out when all of that folder's audio is.
    """
    files = list(files)
    excluded = {p for p, keys in audio.items() if any(rule_matches(r, p, keys) for r in rules)}
    below: dict[str, int] = defaultdict(int)
    excluded_below: dict[str, int] = defaultdict(int)
    stems: dict[tuple[str, str], str] = {}
    for path in audio:
        pure = PurePosixPath(path)
        stems[str(pure.parent), pure.stem] = path
        for folder in ("", *_ancestors(path)):
            below[folder] += 1
            excluded_below[folder] += path in excluded
    sidecars = {e.lower() for e in sidecar_extensions}
    no_keys = AudioKeys()
    for path in files:
        if path in audio:
            continue
        if any(r.kind == "path" and rule_matches(r, path, no_keys) for r in rules):
            excluded.add(path)
            continue
        pure = PurePosixPath(path)
        track = stems.get((str(pure.parent), pure.stem))
        if track is not None and pure.suffix.lower() in sidecars:
            if track in excluded:
                excluded.add(path)
            continue
        folder = next((f for f in (*_ancestors(path), "") if below.get(f)), None)
        if folder is not None and excluded_below[folder] == below[folder]:
            excluded.add(path)
    return excluded


# --- Sync plan ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    src: str  # where the link is in the secondary now
    dst: str


@dataclass(frozen=True)
class Stray:
    """A secondary file that isn't a link to any library file."""

    path: str
    only_copy: bool  # its link count is 1: removing it deletes the data for good


@dataclass
class SyncPlan:
    links: list[str] = field(default_factory=list)  # library files to link (same path)
    moves: list[Move] = field(default_factory=list)  # links whose file was renamed
    unlinks: list[tuple[str, str]] = field(default_factory=list)  # (path, "excluded"|"duplicate")
    # Files in the way of a link or move: a different file at that path, replaced by it
    replaces: list[Stray] = field(default_factory=list)
    strays: list[Stray] = field(default_factory=list)  # left alone
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (path, why): not linked
    rmdirs: list[str] = field(default_factory=list)  # left empty; deepest first
    other: list[str] = field(default_factory=list)  # symlinks etc. in the secondary
    unchanged: int = 0

    @property
    def changes(self) -> int:
        return (
            len(self.links) + len(self.moves) + len(self.unlinks) + len(self.replaces)
            + len(self.rmdirs)
        )  # fmt: skip


def plan_sync(main: Tree, secondary: Tree, excluded: set[str]) -> SyncPlan:
    """What makes `secondary` a copy of `main` minus `excluded`, by hard links (both trees
    must be on one filesystem, so equal inode numbers mean the same file)."""
    plan = SyncPlan(other=sorted(secondary.other))
    desired = {p: e.ino for p, e in main.files.items() if p not in excluded}
    main_inodes: dict[int, list[str]] = defaultdict(list)
    for path, entry in main.files.items():
        main_inodes[entry.ino].append(path)

    def in_place(path: str) -> bool:
        entry = secondary.files.get(path)
        return entry is not None and desired.get(path) == entry.ino

    needed = sorted(p for p in desired if not in_place(p))
    plan.unchanged = len(desired) - len(needed)
    spare: dict[int, list[str]] = defaultdict(list)  # links not where they belong
    strays: dict[str, Stray] = {}
    for path in sorted(secondary.files):
        entry = secondary.files[path]
        if in_place(path):
            continue
        if entry.ino in main_inodes:
            spare[entry.ino].append(path)
        else:
            strays[path] = Stray(path, only_copy=entry.nlink <= 1)

    # A wanted path below a stray file, or where a folder is, can't be created.
    stray_files = set(strays)
    for path in needed:
        if path in secondary.dirs:
            plan.blocked.append((path, "a folder is in the way"))
        elif blocker := next((a for a in _ancestors(path) if a in stray_files), None):
            plan.blocked.append((path, f"{blocker} is a file, not a folder"))
    blocked = {p for p, _ in plan.blocked}

    for path in needed:
        if path in blocked:
            continue
        candidates = spare.get(desired[path])
        if candidates:
            # Prefer a link with the same name (only its folder changed), else the first.
            name = PurePosixPath(path).name
            pick = next((c for c in candidates if PurePosixPath(c).name == name), candidates[0])
            candidates.remove(pick)
            plan.moves.append(Move(pick, path))
        else:
            plan.links.append(path)
        if path in strays:
            plan.replaces.append(strays.pop(path))
    moved_away = {m.src for m in plan.moves}
    for ino, paths in spare.items():
        reason = "excluded" if all(p in excluded for p in main_inodes[ino]) else "duplicate"
        plan.unlinks += [(p, reason) for p in paths if p not in moved_away]
    plan.unlinks.sort()
    plan.strays = sorted(strays.values(), key=lambda s: s.path)

    # Folders with nothing left below them go too, unless they hold ignored names.
    kept = {*plan.links, *(m.dst for m in plan.moves), *strays, *plan.other}
    kept |= {p for p in secondary.files if in_place(p)}
    keep_dirs = set(secondary.ignored_in)
    for path in kept | secondary.ignored_in:
        keep_dirs.update(_ancestors(path))
    plan.rmdirs = sorted(
        secondary.dirs - keep_dirs, key=lambda d: (-len(PurePosixPath(d).parts), d)
    )
    return plan


# --- Bootstrap ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    exclusion: Exclusion
    files: int  # audio files it covers
    folder: str  # where they are (the first one's folder)


def propose_exclusions(
    audio: Mapping[str, AudioKeys], missing: Iterable[str], excluded: set[str]
) -> list[Proposal]:
    """Exclusions that describe what the secondary leaves out today: `missing` are the audio
    files with no link in it that no rule excludes yet (`excluded`).

    Broadest first: an artist folder whose audio is all missing becomes an album artist rule
    (if its files share album artist MBIDs that no linked file has), else a path rule; then
    album folders become release rules (or path rules); then single tracks become track rules
    (or path rules). A rule never covers a file that's linked."""
    missing = set(missing) & set(audio)
    gone = missing | (excluded & set(audio))
    proposals: list[Proposal] = []
    covered: set[str] = set()

    def by_key(kind: str) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for path, keys in audio.items():
            if getattr(keys, kind):
                groups[getattr(keys, kind)].append(path)
        return groups

    def propose(kind: str, paths: list[str], folder: str) -> None:
        """Exclude `paths` (all gone, some missing) by their shared key, else by folder."""
        keys = {getattr(audio[p], kind) for p in paths}
        key = next(iter(keys)) if len(keys) == 1 else ""
        if key and all(p in gone for p in groups[kind].get(key, ())):
            matched = groups[kind][key]
            labels = (getattr(audio[p], f"{kind}_label") for p in sorted(matched))
            rule = Exclusion(kind, key, next(filter(None, labels), folder))
        else:
            rule = Exclusion("path", folder, folder)
            matched = [p for p in audio if _under(p, folder)]
        news = sorted(p for p in matched if p in missing and p not in covered)
        if news:
            covered.update(matched)
            proposals.append(Proposal(rule, len(news), str(PurePosixPath(news[0]).parent)))

    groups = {kind: by_key(kind) for kind in ("artist", "release", "track")}
    folders: dict[str, list[str]] = defaultdict(list)
    tops: dict[str, list[str]] = defaultdict(list)
    for path in audio:
        parts = PurePosixPath(path).parts
        folders[str(PurePosixPath(path).parent)].append(path)
        if len(parts) > 1:
            tops[parts[0]].append(path)

    for top in sorted(tops):
        paths = tops[top]
        if all(p in gone for p in paths) and not covered.issuperset(paths):
            propose("artist", paths, top)
    for folder in sorted(folders):
        paths = [p for p in folders[folder] if p not in covered]
        if folder != "." and paths and all(p in gone for p in folders[folder]):
            propose("release", paths, folder)
    for path in sorted(missing - covered):
        propose("track", [path], path)
    return proposals


def proposals_fingerprint(proposals: Iterable[Proposal]) -> str:
    """Identifies a list of proposals, so saving them can check nothing changed since."""
    text = "\n".join(sorted(f"{p.exclusion.kind} {p.exclusion.key}" for p in proposals))
    return hashlib.sha256(text.encode()).hexdigest()[:16]
