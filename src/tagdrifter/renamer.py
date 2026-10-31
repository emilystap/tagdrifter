"""Rename an album folder's tracks to their expected paths, with a journal for undo.

`plan_album` turns an album's open `path.track_location` issues into a list of moves and
the reasons it can't be applied; `apply_plan` performs it and `undo_batch` reverses it.
Safety rules (see CLAUDE.md, roadmap 5c):

- Only albums whose every audio file is readable and MBID-tagged (their tags are trusted).
- Every audio file must still match the last scan (size and mtime) when planning and again
  right before applying, so a rename never acts on tags it hasn't read.
- Never overwrite: a destination that exists (other than the source itself, for case-only
  renames) blocks the plan, and is checked again before each move.
- Every move is journaled as it happens, so a batch can be undone even after a failure.
- Moves stay inside the album's artist folder.

When all of an album's tracks go to one new folder that doesn't exist yet, the folder itself
is renamed (so artwork, scans and other files go with it); otherwise tracks move one by one
and the old folder is removed if it ends up empty. `.lrc`/`.txt` sidecars follow their track.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import Settings
from .core import PathMatch, TrackMeta, compare_paths, path_key
from .db import Store

TRACK_LOCATION = "path.track_location"


class RenameError(Exception):
    """A plan could not be applied or undone; the message says why. `partial` means files
    were left half-moved (a rollback or undo stopped partway)."""

    def __init__(self, message: str, *, partial: bool = False) -> None:
        super().__init__(message)
        self.partial = partial


@dataclass(frozen=True)
class Move:
    kind: str  # folder | file | sidecar
    src: str  # library-relative, '/'-separated
    dst: str


@dataclass
class RenamePlan:
    album: str
    moves: list[Move] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # why it can't be applied

    @property
    def ok(self) -> bool:
        return bool(self.moves) and not self.problems

    @property
    def fingerprint(self) -> str:
        """Identifies the exact moves, so applying can refuse a plan that changed since the
        preview was shown."""
        data = json.dumps([self.album, [(m.kind, m.src, m.dst) for m in self.moves]])
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    @property
    def track_count(self) -> int:
        return sum(1 for m in self.moves if m.kind == "file")


@dataclass(frozen=True)
class ApplyResult:
    batch_id: int
    moved: int  # journaled moves, including the folder and sidecars
    removed_dirs: int


def _safe_rel(text: str) -> PurePosixPath | None:
    rel = PurePosixPath(text)
    if not text or rel.is_absolute() or any(p in ("", ".", "..") for p in rel.parts):
        return None
    return rel


def _stem_and_ext(name: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(name)
    return stem, ext


def plan_album(settings: Settings, store: Store, album: str) -> RenamePlan:
    """The moves that bring `album` (a library-relative folder) in line with its tags."""
    plan = RenamePlan(album)
    rel = _safe_rel(album)
    root = settings.library_path
    if rel is None or len(rel.parts) < 2:
        plan.problems.append("Not an album folder.")
        return plan
    folder = root.joinpath(*rel.parts)
    if not folder.is_dir():
        plan.problems.append("The folder no longer exists; scan again.")
        return plan

    expected = {
        r.subject: r.details.get("expected")
        for r in store.open_issues(check_id=TRACK_LOCATION, path_prefix=album)
        if r.album == album and r.details.get("expected")
    }
    if not expected:
        plan.problems.append("No renames are proposed for this folder.")
        return plan

    # What is on disk now, compared with what the last scan read.
    audio: dict[str, tuple[int, int]] = {}
    sidecars: list[str] = []
    subdirs: list[str] = []
    for entry in os.scandir(folder):
        if entry.is_dir(follow_symlinks=False):
            subdirs.append(entry.name)
        elif entry.is_file(follow_symlinks=False):
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in settings.audio_extensions:
                st = entry.stat(follow_symlinks=False)
                audio[entry.name] = (st.st_size, st.st_mtime_ns)
            elif ext in settings.sidecar_extensions:
                sidecars.append(entry.name)
    cached = store.files_in_folder(album)
    for name, stat in sorted(audio.items()):
        path = f"{album}/{name}"
        entry = cached.get(path)
        if entry is None or (entry.size, entry.mtime_ns) != stat:
            plan.problems.append(f"{name} changed since the last scan; scan again.")
        elif entry.tags is None:
            plan.problems.append(f"{name} can't be read.")
        elif not TrackMeta.from_tags(PurePosixPath(path), entry.tags).is_mbid_tagged:
            plan.problems.append(f"{name} has no valid release/recording MBIDs.")
    for path in sorted(cached.keys() - {f"{album}/{n}" for n in audio}):
        plan.problems.append(f"{PurePosixPath(path).name} is gone since the last scan; scan again.")
    if plan.problems:
        return plan

    # Where each track goes; tracks without an issue stay put.
    targets: dict[str, PurePosixPath] = {}
    artist = rel.parts[0]
    for name in audio:
        path = f"{album}/{name}"
        target = _safe_rel(expected.get(path, path))
        if target is None or target.parts[0] != artist or len(target.parts) < 3:
            plan.problems.append(f"{name}: unexpected target {expected.get(path)}.")
            continue
        targets[name] = target
    seen: dict[str, str] = {}
    for name, target in sorted(targets.items()):
        key = path_key(target, case_sensitive=False)  # never two names that collide over SMB
        if key in seen:
            plan.problems.append(f"{seen[key]} and {name} would both become {target}.")
        seen[key] = name
    if plan.problems:
        return plan

    parents = {str(t.parent) for t in targets.values()}
    new_folder = parents.pop() if len(parents) == 1 else None
    nested_audio = any(
        PurePosixPath(p).parent != rel for p in store.files_under(album)
    )  # another album inside this folder would move with it
    move_folder = (
        new_folder is not None
        and new_folder != album
        and not nested_audio
        and _free(root, album, new_folder)
    )
    base = new_folder if move_folder else album
    if move_folder:
        plan.moves.append(Move("folder", album, new_folder))

    sidecar_stems = [
        (path_key(_stem_and_ext(n)[0], case_sensitive=settings.case_sensitive), n)
        for n in sorted(sidecars)
    ]
    for name in sorted(audio):
        target = targets[name]
        src = f"{base}/{name}"
        dst = str(target)
        if src == dst:
            continue
        if not move_folder and not _free(root, f"{album}/{name}", dst):
            plan.problems.append(f"{dst} already exists.")
            continue
        plan.moves.append(Move("file", src, dst))
        stem = path_key(_stem_and_ext(name)[0], case_sensitive=settings.case_sensitive)
        new_stem = _stem_and_ext(target.name)[0]
        for side_stem, side_name in sidecar_stems:
            if side_stem != stem:
                continue
            side_dst = str(target.parent / (new_stem + _stem_and_ext(side_name)[1]))
            if not move_folder and not _free(root, f"{album}/{side_name}", side_dst):
                plan.problems.append(f"{side_dst} already exists.")
                continue
            plan.moves.append(Move("sidecar", f"{base}/{side_name}", side_dst))
    if move_folder:
        # Inside the renamed folder, a file's new name must not be taken by a file that stays.
        staying = {path_key(n, case_sensitive=False) for n in os.listdir(folder)}
        staying -= {path_key(PurePosixPath(m.src).name, case_sensitive=False) for m in plan.moves}
        for m in plan.moves[1:]:
            if path_key(PurePosixPath(m.dst).name, case_sensitive=False) in staying:
                plan.problems.append(f"{m.dst} already exists.")
    if not plan.moves:
        plan.problems.append("Every track already has its expected path.")
    return plan


def _abs(root: Path, rel: str) -> Path:
    return root.joinpath(*PurePosixPath(rel).parts)


def _free(root: Path, src: str, dst: str) -> bool:
    """Whether `dst` can be created without overwriting anything but `src` itself."""
    target = _abs(root, dst)
    if not os.path.lexists(target):
        return True
    try:
        return os.path.samefile(_abs(root, src), target)  # case-only, case-insensitive FS
    except OSError:
        return False


def _rename(root: Path, src: str, dst: str) -> None:
    """Rename without overwriting. Case-only renames go through a hidden temporary name, which
    works on case-insensitive filesystems too (and scans ignore it if left behind)."""
    a, b = _abs(root, src), _abs(root, dst)
    if not os.path.lexists(a):
        raise RenameError(f"{src} no longer exists.")
    if compare_paths(src, dst) is PathMatch.CASE_ONLY:
        if os.path.lexists(b) and not _free(root, src, dst):
            raise RenameError(f"{dst} already exists.")
        temp = a.with_name(f".tagdrifter-{uuid.uuid4().hex[:8]}")
        os.rename(a, temp)
        try:
            os.rename(temp, b)
        except OSError:
            os.rename(temp, a)
            raise
        return
    if os.path.lexists(b):
        raise RenameError(f"{dst} already exists.")
    os.rename(a, b)


def apply_plan(settings: Settings, store: Store, album: str, fingerprint: str) -> ApplyResult:
    """Re-plan `album`, check it is the plan that was previewed, and perform it.

    On a failure the moves made so far are undone and RenameError is raised; the batch stays
    in the journal (status failed, or partial if the rollback failed too).
    """
    plan = plan_album(settings, store, album)
    if plan.problems:
        raise RenameError(" ".join(plan.problems))
    if plan.fingerprint != fingerprint:
        raise RenameError("The folder changed since the preview; preview it again.")

    root = settings.library_path
    batch = store.start_rename_batch(album)
    removed = 0
    try:
        for move in plan.moves:
            dst_dir = _abs(root, move.dst).parent
            if not dst_dir.exists():
                created = []
                for parent in reversed([dst_dir, *dst_dir.parents]):
                    if not parent.exists():
                        parent.mkdir()
                        created.append(parent)
                for d in created:
                    store.record_rename(batch, "mkdir", "", d.relative_to(root).as_posix())
            _rename(root, move.src, move.dst)
            store.record_rename(batch, move.kind, move.src, move.dst)
        old = _abs(root, album)
        if os.path.isdir(old) and not os.listdir(old) and plan.moves[0].kind != "folder":
            old.rmdir()
            store.record_rename(batch, "rmdir", album, "")
            removed += 1
    except (OSError, RenameError) as exc:
        message = str(exc) if isinstance(exc, RenameError) else f"{type(exc).__name__}: {exc}."
        try:
            undo_batch(settings, store, batch, status="failed", error=message)
        except RenameError as undo_exc:
            store.finish_rename_batch(batch, "partial", f"{message} Rollback failed: {undo_exc}")
            raise RenameError(
                f"{message} Rolling back also failed: {undo_exc}", partial=True
            ) from exc
        raise RenameError(f"{message} Nothing was changed.") from exc
    store.finish_rename_batch(batch, "applied")
    return ApplyResult(batch, len(plan.moves), removed)


def undo_batch(
    settings: Settings,
    store: Store,
    batch_id: int,
    *,
    status: str = "undone",
    error: str | None = None,
) -> int:
    """Reverse a batch's journaled moves, newest first. Returns how many were reversed.

    Each reversed move is marked in the journal, so an undo that stops halfway (e.g. a file
    was moved again since) can be retried after fixing the cause."""
    root = settings.library_path
    reversed_count = 0
    for move in store.rename_moves(batch_id, pending_only=True)[::-1]:
        try:
            if move["kind"] == "mkdir":
                path = _abs(root, move["dst"])
                if path.is_dir() and not os.listdir(path):
                    path.rmdir()
            elif move["kind"] == "rmdir":
                _abs(root, move["src"]).mkdir(exist_ok=True)
            else:
                _rename(root, move["dst"], move["src"])
        except (OSError, RenameError) as exc:
            store.finish_rename_batch(batch_id, "partial", f"Undo stopped: {exc}")
            raise RenameError(
                f"Undo stopped at {move['dst'] or move['src']}: {exc}", partial=True
            ) from exc
        store.mark_rename_undone(move["id"])
        reversed_count += 1
    store.finish_rename_batch(batch_id, status, error)
    return reversed_count


def library_writable(settings: Settings) -> bool:
    return os.access(settings.library_path, os.W_OK)
