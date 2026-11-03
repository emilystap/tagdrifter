"""Triage: move newly tagged files from a drop folder into the library, at their expected paths.

Files dropped in the triage folder (`triage_path`) are grouped into albums by release MBID
(files without one are grouped by folder) and planned like this:

- **waiting**: a folder's files changed in the last `triage_settle` seconds (still being
  copied or tagged), or, with MusicBrainz lookups on, the release isn't looked up yet.
- **blocked**: something makes a move impossible or unsafe: an unreadable file, tags the
  naming rules need are missing (valid release and recording MBIDs, album artist or artist,
  album, title, track number, and disc number on multi-disc releases), a target that already
  exists (never overwritten), two files with one target, the release already in the library
  in another folder, or a folder that would collide over SMB.
- **confirm**: the checks found issues of warning severity or worse, run over the album as it
  would be in the library (its tracks there plus the new ones), minus the path checks
  (`path.*`, `album.placement`), which triage settles by choosing the path. Or the album's
  folder name would clash with another release in the library, which then gets a rename
  proposal. These move once someone confirms the exact plan (its `fingerprint`).
- **ready**: everything else moves on its own (info-level issues don't hold anything up).

The artist folder is the album artist's, as `album.placement` expects it (the artist's current
MusicBrainz name once the release is looked up, by default); an existing folder the album would
be correctly placed in is used as it is spelled. Tracks are named by the naming rules, sidecars
(`.lrc`, `.txt`) follow their track, and when every audio file at or below a triage folder
belongs to the album, the folder's other files (artwork, logs, cue sheets) and its folders
without audio move into the album folder too, unless a name is taken there. Emptied triage
folders are removed (never the triage folder itself).

Moves are renames (never copies), so the triage folder must be on the library's filesystem and
mount. Each album is one batch in the rename journal (origin 'triage'), rolled back on failure
and undone from the Renames page, which moves the files back into the triage folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .checks import AlbumContext, Issue, Scope, Severity, TrackContext, build_checks
from .checks.builtin_tags import current_album_artist
from .config import Settings
from .core import (
    MetadataReadError,
    TrackMeta,
    audit_album_placement,
    compute_target,
    expected_artist_folder,
    is_mbid,
    path_key,
    read_tags,
    resolve_release_collisions,
)
from .core.mbrelease import MbRelease
from .core.metadata import ALBUM_ARTIST_KEYS, RECORDING_MBID_KEYS, first_tag
from .db import Store
from .renamer import RenameError, move_path, undo_batch
from .secondary import Status, _mount_point, _mounts, _resolved

READY, CONFIRM, BLOCKED, WAITING = "ready", "confirm", "blocked", "waiting"
STATUS_ORDER = (CONFIRM, BLOCKED, WAITING, READY)
# Checks about where files live: triage decides that, so they never hold an album up.
SKIPPED_CHECKS = ("path.", "album.placement")
ORIGIN = "triage"
# Where triage tracks pretend to live while their targets are worked out alongside the
# library's ("." keeps it apart from every library folder, which scans never start with).
STAGING = PurePosixPath(".triage")
# Tags a track needs before triage will name it: (label, any of these keys).
NEEDED_TAGS = (
    ("album artist or artist", (*ALBUM_ARTIST_KEYS, "artist")),
    ("album", ("album",)),
    ("title", ("title",)),
    ("track number", ("tracknumber",)),
)

TagCache = dict[tuple[str, int, int], tuple[dict[str, tuple[str, ...]] | None, str | None]]


@dataclass(frozen=True)
class Move:
    kind: str  # file | sidecar | other | folder
    src: str  # triage-relative, '/'-separated
    dst: str  # library-relative


@dataclass(frozen=True)
class Finding:
    """An issue the checks found, with the triage file (or the album) it is about."""

    check_id: str
    severity: str
    where: str
    message: str


@dataclass
class TriageAlbum:
    key: str  # release MBID (lower-case), or "folder:<triage folder>" without one
    label: str  # "Album artist: Album", or the folder
    files: list[str] = field(default_factory=list)  # triage-relative audio files
    target: str = ""  # library-relative album folder
    moves: list[Move] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # blocked: why it can't move
    waiting: list[str] = field(default_factory=list)  # why it isn't decided yet
    issues: list[Finding] = field(default_factory=list)  # warning or worse: needs a yes
    reasons: list[str] = field(default_factory=list)  # other reasons it needs a yes
    notes: list[Finding] = field(default_factory=list)  # info-level issues
    remarks: list[str] = field(default_factory=list)  # worth knowing, holds nothing up
    needs_lookup: bool = False  # waiting for its release's MusicBrainz lookup

    @property
    def release_mbid(self) -> str:
        return "" if self.key.startswith("folder:") else self.key

    @property
    def status(self) -> str:
        if self.problems:
            return BLOCKED
        if self.waiting:
            return WAITING
        if self.issues or self.reasons:
            return CONFIRM
        return READY

    @property
    def fingerprint(self) -> str:
        """Identifies the moves and what needed confirming, so a confirmation only applies to
        the plan that was shown."""
        data = json.dumps(
            [
                self.key,
                self.target,
                [(m.kind, m.src, m.dst) for m in self.moves],
                sorted((f.check_id, f.where) for f in self.issues),
                self.reasons,
            ]
        )
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    @property
    def track_count(self) -> int:
        return sum(1 for m in self.moves if m.kind == "file")


@dataclass
class TriagePlan:
    albums: list[TriageAlbum] = field(default_factory=list)
    status: Status = field(default_factory=Status)

    def by_status(self, status: str) -> list[TriageAlbum]:
        return [a for a in self.albums if a.status == status]

    def album(self, key: str) -> TriageAlbum | None:
        return next((a for a in self.albums if a.key == key), None)


@dataclass(frozen=True)
class MoveResult:
    batch_id: int
    target: str
    moved: int  # journaled moves: tracks, sidecars and other files
    removed_dirs: int


# --- Settings and filesystem checks -----------------------------------------------------


def validate_settings(settings: Settings) -> None:
    """Refuse a triage folder that overlaps the library or the secondary library, or is a
    filesystem root (raises ValueError). Nothing to check when no triage folder is set."""
    if settings.triage_path is None:
        return
    triage = _resolved(settings.triage_path)
    if triage == Path(triage.anchor):
        raise ValueError(f"triage path is a filesystem root: {settings.triage_path}")
    others = [("library", settings.library_path)]
    if settings.secondary_path is not None:
        others.append(("secondary library", settings.secondary_path))
    for name, other in others:
        other = _resolved(other)
        if triage == other:
            raise ValueError(f"triage path is the {name} itself")
        if triage.is_relative_to(other):
            raise ValueError(f"triage path is inside the {name}: {settings.triage_path}")
        if other.is_relative_to(triage):
            raise ValueError(f"triage path contains the {name}: {settings.triage_path}")


def check(settings: Settings) -> Status:
    """Whether triage can be planned (problems) and whether files can be moved (warnings)."""
    status = Status()
    triage, library = settings.triage_path, settings.library_path
    if triage is None:
        status.problems.append("No triage folder is set.")
        return status
    if not triage.is_dir():
        status.problems.append(f"{triage} isn't a folder (or isn't mounted).")
    if not library.is_dir():
        status.problems.append(f"The library {library} isn't a folder (or isn't mounted).")
    if status.problems:
        return status
    if triage.stat().st_dev != library.stat().st_dev:
        status.warnings.append(
            "The triage folder and the library are on different filesystems. Triage only "
            "moves files (it never copies them), so put the triage folder next to the library."
        )
    else:
        mounts = _mounts()
        if mounts and _mount_point(_resolved(triage), mounts) != _mount_point(
            _resolved(library), mounts
        ):
            status.warnings.append(
                "The triage folder and the library are separate mounts. Files can't be moved "
                "across mounts, even of one filesystem, so mount their common parent folder "
                "once instead."
            )
    for name, path in (("triage folder", triage), ("library", library)):
        if not os.access(path, os.W_OK):
            status.warnings.append(f"The {name} is read-only here, so nothing can be moved.")
    return status


# --- Planning ---------------------------------------------------------------------------


@dataclass
class _Folder:
    rel: PurePosixPath
    audio: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (size, mtime_ns)
    other: list[str] = field(default_factory=list)  # other files directly in it
    subdirs: list[str] = field(default_factory=list)
    newest_ns: int = 0  # newest mtime of any file at or below it


def _walk(root: Path, ignore: tuple[str, ...], audio_exts: tuple[str, ...]) -> dict:
    """Every folder below `root` (no symlink following), minus ignored names."""
    folders: dict[PurePosixPath, _Folder] = {}
    order: list[PurePosixPath] = []
    stack = [PurePosixPath()]
    while stack:
        rel = stack.pop()
        info = folders[rel] = _Folder(rel)
        order.append(rel)
        with os.scandir(root.joinpath(*rel.parts)) as entries:
            for entry in entries:
                if any(fnmatchcase(entry.name, pat) for pat in ignore):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    info.subdirs.append(entry.name)
                    stack.append(rel / entry.name)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    info.newest_ns = max(info.newest_ns, st.st_mtime_ns)
                    if os.path.splitext(entry.name)[1].lower() in audio_exts:
                        info.audio[entry.name] = (st.st_size, st.st_mtime_ns)
                    else:
                        info.other.append(entry.name)
    for rel in reversed(order):  # children before parents
        if rel.parts:
            parent = folders[rel.parent]
            parent.newest_ns = max(parent.newest_ns, folders[rel].newest_ns)
    return folders


def _read(root: Path, rel: str, stat: tuple[int, int], cache: TagCache | None):
    key = (rel, *stat)
    hit = cache.get(key) if cache is not None else None  # shared by threads: one lookup
    if hit is not None:
        return hit
    try:
        result = read_tags(root.joinpath(*PurePosixPath(rel).parts)), None
    except MetadataReadError as exc:
        result = None, str(exc).split(": ", 1)[-1]
    if cache is not None:
        cache[key] = result
    return result


def _parsed_releases(store: Store, mbids: Iterable[str]) -> dict[str, MbRelease | None]:
    return {
        mbid: MbRelease.from_json(data) if data is not None else None
        for mbid, data in store.mb_releases(mbids).items()
    }


def _label(metas: list[TrackMeta], fallback: str) -> str:
    meta = next((m for m in metas if m.album), None)
    if meta is None:
        return fallback
    return f"{meta.effective_album_artist}: {meta.album}"


def plan_triage(
    settings: Settings,
    store: Store,
    *,
    now_ns: int | None = None,
    cache: TagCache | None = None,
) -> TriagePlan:
    """What triage would do with every album in the triage folder now. Reads tags (through
    `cache`, keyed by path, size and mtime) but changes nothing."""
    plan = TriagePlan(status=check(settings))
    if not plan.status.ok:
        return plan
    assert settings.triage_path is not None
    root = settings.triage_path
    now_ns = time.time_ns() if now_ns is None else now_ns
    settle_ns = settings.triage_settle * 1_000_000_000
    folders = _walk(root, settings.ignore_patterns, settings.audio_extensions)

    # Group audio by release; folders still changing wait as a whole, unread.
    groups: dict[str, TriageAlbum] = {}
    metas: dict[str, TrackMeta] = {}
    for rel, info in sorted(folders.items()):
        if not info.audio:
            continue
        folder_key = f"folder:{rel}"
        if now_ns - info.newest_ns < settle_ns:
            album = groups.setdefault(folder_key, TriageAlbum(folder_key, str(rel) or "(top)"))
            album.files += [str(rel / n) for n in sorted(info.audio)]
            album.waiting.append(
                f"{rel or 'The triage folder'}: files changed in the last "
                f"{_seconds(settings.triage_settle)}."
            )
            continue
        for name, stat in sorted(info.audio.items()):
            path = str(rel / name)
            tags, error = _read(root, path, stat, cache)
            if tags is None:
                album = groups.setdefault(folder_key, TriageAlbum(folder_key, str(rel) or "(top)"))
                album.files.append(path)
                album.problems.append(f"{name} can't be read: {error}.")
                continue
            meta = metas[path] = TrackMeta.from_tags(STAGING / path, tags)
            key = meta.release_mbid.lower() if is_mbid(meta.release_mbid) else folder_key
            album = groups.setdefault(key, TriageAlbum(key, str(rel) or "(top)"))
            album.files.append(path)

    planner = _Planner(settings, store, root, folders, metas, now_ns)
    for album in groups.values():
        if not album.problems and not album.waiting:
            planner.plan(album)
        else:
            album.label = _label([metas[f] for f in album.files if f in metas], album.label)
    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    plan.albums = sorted(groups.values(), key=lambda a: (order[a.status], a.label.casefold()))
    return plan


def _seconds(n: int) -> str:
    return f"{n // 60} min" if n >= 120 and n % 60 == 0 else f"{n} s"


class _Planner:
    """Plans albums one at a time, sharing what they all need from the library."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        root: Path,
        folders: dict[PurePosixPath, _Folder],
        metas: Mapping[str, TrackMeta],
        now_ns: int,
    ) -> None:
        self.settings = settings
        self.now_ns = now_ns
        self.store = store
        self.root = root
        self.folders = folders
        self.metas = metas
        self.library = settings.library_path
        self.artist_folders = sorted(
            e.name
            for e in os.scandir(self.library)
            if e.is_dir(follow_symlinks=False)
            and not any(fnmatchcase(e.name, p) for p in settings.ignore_patterns)
        )
        self.checks = [c for c in build_checks(settings) if not c.id.startswith(SKIPPED_CHECKS)]
        self._release_folders: dict[str, set[str]] | None = None
        self.claimed: dict[str, str] = {}  # library path key -> triage album, across albums

    def release_folders(self) -> dict[str, set[str]]:
        """{release MBID: library folders holding its files}, from the scan's cache."""
        if self._release_folders is None:
            out: dict[str, set[str]] = defaultdict(set)
            for path, tags in self.store.file_tags(["musicbrainz_albumid"]).items():
                mbid = next(iter((tags or {}).get("musicbrainz_albumid", ())), "").lower()
                if mbid:
                    out[mbid].add(str(PurePosixPath(path).parent))
            self._release_folders = out
        return self._release_folders

    def plan(self, album: TriageAlbum) -> None:
        tracks = [self.metas[f] for f in album.files]
        album.label = _label(tracks, album.label)
        changing = [
            r
            for r in self._owned(album)
            if self.now_ns - self.folders[r].newest_ns < self.settings.triage_settle * 1_000_000_000
        ]
        if changing:
            album.waiting.append(
                f"{changing[0]}: files changed in the last {_seconds(self.settings.triage_settle)}."
            )
            return

        # 1. Tags the naming rules need.
        for path, meta in zip(album.files, tracks, strict=True):
            missing = [label for label, keys in NEEDED_TAGS if not any(map(meta.has_tag, keys))]
            if meta.total_discs > 1 and not meta.has_tag("discnumber"):
                missing.append("disc number")
            if not is_mbid(first_tag(meta.tags, RECORDING_MBID_KEYS)):
                missing.insert(0, "a valid recording MBID (musicbrainz_trackid)")
            if not is_mbid(meta.release_mbid):
                missing.insert(0, "a valid release MBID (musicbrainz_albumid)")
            if missing:
                album.problems.append(f"{path}: missing {', '.join(missing)}.")
        if album.problems:
            return

        # 2. The release from MusicBrainz, for the artist folder and the mb.* checks.
        releases = _parsed_releases(self.store, [album.key])
        if self.settings.musicbrainz_enabled and album.key not in releases:
            album.needs_lookup = True
            album.waiting.append("Waiting for its MusicBrainz lookup.")
            return

        # 3. The artist folder.
        current = current_album_artist(
            releases.get(album.key), self.settings.options_for("album.placement")
        )
        artist = self._artist_folder(tracks, current, album)
        if artist is None:
            return

        # 4. Track targets, worked out with the library tracks already in that artist folder.
        library_tracks = [
            TrackMeta.from_tags(PurePosixPath(path), entry.tags)
            for path, entry in sorted(self.store.files_under(artist).items())
            if entry.tags is not None
        ]
        alone, _ = resolve_release_collisions(
            [(m, compute_target(m, artist)) for m in library_tracks]
        )
        together, _ = resolve_release_collisions(
            [(m, compute_target(m, artist)) for m in library_tracks + tracks]
        )
        renamed = sorted(
            {
                str(new.album_relative_path)
                for old, new in zip(alone, together, strict=False)
                if old.album_folder != new.album_folder
            }
        )
        if renamed:
            album.reasons.append(
                f"Another release is also named {PurePosixPath(renamed[0]).name}, so both get "
                "the year or another detail added, and the library one gets a rename proposal."
            )
        targets = together[len(library_tracks) :]
        folders = {str(t.album_relative_path) for t in targets}
        if len(folders) != 1:
            album.problems.append(f"Its tracks would go to {len(folders)} folders.")
            return
        album.target = folders.pop()

        # 5. The release already in the library: only in this very folder.
        elsewhere = sorted(self.release_folders().get(album.key, set()) - {album.target})
        if elsewhere:
            album.problems.append(
                f"This release is already in the library at {elsewhere[0]}. Rename that album "
                "first (or move these files by hand), so the release isn't split."
            )
            return
        existing = self.store.files_in_folder(album.target)
        if existing:
            album.remarks.append(
                f"Adds {len(tracks)} track{'s' * (len(tracks) != 1)} to {album.target}, which "
                f"has {len(existing)} already."
            )

        # 6. Moves, never overwriting anything.
        self._moves(album, [str(t.relative_path) for t in targets])
        if album.problems:
            return

        # 7. The checks, over the album as it would be in the library.
        self._run_checks(album, tracks, targets, existing, releases)

    def _artist_folder(self, tracks: list[TrackMeta], current, album: TriageAlbum) -> str | None:
        primary = next((t for t in tracks if t.albumartist or t.artist), tracks[0])
        expected = expected_artist_folder(
            current.name if current else primary.albumartist or primary.artist
        )
        cs = self.settings.case_sensitive

        def fits(folder: str) -> bool:
            return audit_album_placement(folder, tracks, case_sensitive=cs, current=current) is None

        if expected in self.artist_folders:
            return expected
        # An existing folder the album would be correctly placed in (another accepted
        # spelling: the sort name, a letter-case variant on a case-insensitive library).
        fitting = [f for f in self.artist_folders if fits(f)]
        if fitting:
            return fitting[0]
        key = path_key(expected, case_sensitive=False)
        clash = next(
            (f for f in self.artist_folders if path_key(f, case_sensitive=False) == key), None
        )
        if clash:
            album.problems.append(
                f"The artist folder would be {expected}, but the library has {clash}, and "
                "names differing only in letter case collide over SMB. Fix that folder first."
            )
            return None
        if not fits(expected):  # never expected: audit_album_placement chose it
            album.problems.append(f"No artist folder fits its album artist ({expected}).")
            return None
        return expected

    def _taken(self, rel: str) -> str | None:
        """What a library path collides with: an existing name in its folder that differs
        at most in letter case, or a path claimed by an earlier album of this run."""
        path = PurePosixPath(rel)
        key = path_key(path, case_sensitive=False)
        if key in self.claimed:
            return self.claimed[key]
        folder = self.library.joinpath(*path.parent.parts)
        try:
            names = os.listdir(folder)
        except (FileNotFoundError, NotADirectoryError):
            return None
        want = path_key(path.name, case_sensitive=False)
        return next(
            (str(path.parent / n) for n in names if path_key(n, case_sensitive=False) == want),
            None,
        )

    def _claim(self, album: TriageAlbum, rel: str) -> None:
        self.claimed[path_key(rel, case_sensitive=False)] = f"{album.label} (also in triage)"

    def _moves(self, album: TriageAlbum, targets: list[str]) -> None:
        cs = self.settings.case_sensitive
        seen: dict[str, str] = {}
        for src, dst in zip(album.files, targets, strict=True):
            key = path_key(dst, case_sensitive=False)
            if key in seen:
                album.problems.append(f"{seen[key]} and {src} would both become {dst}.")
                continue
            seen[key] = src
            if taken := self._taken(dst):
                album.problems.append(f"{taken} already exists; it's never overwritten.")
                continue
            album.moves.append(Move("file", src, dst))
            # Sidecars with the track's name follow it.
            src_path, dst_path = PurePosixPath(src), PurePosixPath(dst)
            stem = path_key(src_path.stem, case_sensitive=cs)
            for name in self.folders[src_path.parent].other:
                base, ext = os.path.splitext(name)
                if ext.lower() not in self.settings.sidecar_extensions:
                    continue
                if path_key(base, case_sensitive=cs) != stem:
                    continue
                side = str(dst_path.with_name(dst_path.stem + ext))
                if taken := self._taken(side):
                    album.problems.append(f"{taken} already exists; it's never overwritten.")
                    continue
                album.moves.append(Move("sidecar", str(src_path.parent / name), side))
        if album.problems:
            return
        for move in album.moves:
            self._claim(album, move.dst)

        # Other files and audio-less folders of folders that hold only this album.
        moved = {m.src for m in album.moves}
        for rel in self._owned(album):
            info = self.folders[rel]
            candidates = [("other", n) for n in info.other if str(rel / n) not in moved]
            candidates += [("folder", d) for d in info.subdirs if not self._has_audio(rel / d)]
            for kind, name in candidates:
                dst = f"{album.target}/{name}"
                if taken := self._taken(dst):
                    album.remarks.append(f"{rel / name} stays in triage: {taken} already exists.")
                    continue
                album.moves.append(Move(kind, str(rel / name), dst))
                self._claim(album, dst)

    def _has_audio(self, rel: PurePosixPath) -> bool:
        return any(f.audio for r, f in self.folders.items() if r == rel or r.is_relative_to(rel))

    def _owned(self, album: TriageAlbum) -> list[PurePosixPath]:
        """Triage folders (never the triage folder itself) where every audio file at or below
        belongs to the album: its folders, and their parents up to the first that holds other
        audio too."""
        mine = set(album.files)
        owned: set[PurePosixPath] = set()
        for rel in {PurePosixPath(f).parent for f in album.files}:
            while rel.parts and rel not in owned and self._only(rel, mine):
                owned.add(rel)
                rel = rel.parent
        return sorted(owned)

    def _only(self, rel: PurePosixPath, mine: set[str]) -> bool:
        """Whether every audio file at or below a triage folder belongs to the album."""
        return all(
            str(r / name) in mine
            for r, f in self.folders.items()
            if r == rel or r.is_relative_to(rel)
            for name in f.audio
        )

    def _run_checks(self, album, tracks, targets, existing, releases) -> None:
        settings = self.settings
        where: dict[str, str] = {}
        contexts: list[TrackContext] = []
        for src, meta, target in zip(album.files, tracks, targets, strict=True):
            rel = target.relative_path
            where[str(rel)] = src
            moved = TrackMeta.from_tags(rel, meta.tags)
            contexts.append(TrackContext(rel, moved, target, settings))
        artist = PurePosixPath(album.target).parts[0]
        library_ctx = []
        for path, entry in sorted(existing.items()):
            if entry.tags is None:
                continue
            meta = TrackMeta.from_tags(PurePosixPath(path), entry.tags)
            library_ctx.append(
                TrackContext(PurePosixPath(path), meta, compute_target(meta, artist), settings)
            )
        album_ctx = AlbumContext(
            PurePosixPath(album.target),
            tuple(sorted(library_ctx + contexts, key=lambda t: str(t.rel_path))),
            (),
            (),
            settings,
            releases,
        )
        found: list[Issue] = []
        for check in self.checks:
            try:
                if check.scope is Scope.TRACK:
                    for ctx in contexts:
                        found += check.run(ctx)
                elif check.scope is Scope.ALBUM:
                    found += check.run(album_ctx)
            except Exception as exc:  # a buggy check must hold the album, not crash triage
                album.issues.append(
                    Finding(check.id, Severity.ERROR, album.label, f"Check crashed: {exc!r}")
                )
        for issue in found:
            if issue.subject in where:
                place = where[issue.subject]
            elif issue.subject == album.target:
                place = "album"
            elif PurePosixPath(issue.subject).is_relative_to(album.target):
                continue  # about a track already in the library: on the Issues page
            else:
                place = issue.subject
            finding = Finding(issue.check_id, issue.severity, place, issue.message)
            if issue.severity is Severity.INFO:
                album.notes.append(finding)
            else:
                album.issues.append(finding)


# --- Moving -----------------------------------------------------------------------------


def move_album(settings: Settings, store: Store, album: TriageAlbum) -> MoveResult:
    """Move a planned album into the library, journaled as one rename batch. On a failure
    the moves made so far are undone and RenameError is raised (partial=True if that failed
    too). The caller decides whether the album may move (`status`)."""
    assert settings.triage_path is not None
    if not album.moves:
        raise RenameError("Nothing to move.")
    triage, library = settings.triage_path, settings.library_path
    batch = store.start_rename_batch(album.target, origin=ORIGIN)
    removed = 0
    try:
        for move in album.moves:
            dst_dir = library.joinpath(*PurePosixPath(move.dst).parent.parts)
            if not dst_dir.exists():
                created = []
                for parent in reversed([dst_dir, *dst_dir.parents]):
                    if not parent.exists():
                        parent.mkdir()
                        created.append(parent)
                for d in created:
                    store.record_rename(batch, "mkdir", "", d.relative_to(library).as_posix())
            move_path(triage, move.src, library, move.dst)
            store.record_rename(batch, move.kind, move.src, move.dst)
        # Remove the triage folders this emptied, deepest first (never the triage folder).
        folders = {p for m in album.moves for p in PurePosixPath(m.src).parents if p.parts}
        for rel in sorted(folders, key=lambda p: len(p.parts), reverse=True):
            path = triage.joinpath(*rel.parts)
            if path.is_dir() and not os.listdir(path):
                path.rmdir()
                store.record_rename(batch, "rmdir", str(rel), "")
                removed += 1
    except (OSError, RenameError) as exc:
        message = str(exc) if isinstance(exc, RenameError) else f"{type(exc).__name__}: {exc}."
        try:
            undo_batch(settings, store, batch, status="failed", error=message)
        except RenameError as undo_exc:
            store.finish_rename_batch(batch, "partial", f"{message} Rollback failed: {undo_exc}")
            raise RenameError(
                f"{message} Moving the files back also failed: {undo_exc}", partial=True
            ) from exc
        raise RenameError(f"{message} Nothing was moved.") from exc
    store.finish_rename_batch(batch, "applied")
    return MoveResult(batch, album.target, len(album.moves), removed)


def confirm_album(
    settings: Settings,
    store: Store,
    key: str,
    fingerprint: str,
    *,
    cache: TagCache | None = None,
) -> MoveResult:
    """Move an album someone confirmed: re-plan the triage folder and move the album if it is
    still the plan they saw (ready, or needing confirmation). Raises RenameError otherwise."""
    plan = plan_triage(settings, store, cache=cache)
    if not plan.status.ok:
        raise RenameError(" ".join(plan.status.problems))
    if plan.status.warnings:
        raise RenameError(" ".join(plan.status.warnings))
    album = plan.album(key)
    if album is None:
        raise RenameError("That album is no longer in the triage folder.")
    if album.status == BLOCKED:
        raise RenameError(" ".join(album.problems))
    if album.status == WAITING:
        raise RenameError(" ".join(album.waiting))
    if album.fingerprint != fingerprint:
        raise RenameError("The album changed since it was shown; look at it again.")
    return move_album(settings, store, album)
