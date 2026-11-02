"""Writing the secondary library: the guarded write path and applying a sync plan.

`SecondaryWriter` is the only code that changes anything on disk for the secondary. It can:

- link a library file into the secondary (a new hard link; never a copy),
- move a link within the secondary (link to the new name, then unlink the old one),
- unlink a file in the secondary, only after checking it's the file the plan saw (inode) and,
  unless approved for that file, that another link to it exists (link count above 1),
- create and remove (empty) folders in the secondary.

It never opens a file for writing and never changes the library: library paths are only ever
link sources. Every path is relative and checked component by component (no "..", no
symlinks): on Linux through directory file descriptors opened with O_NOFOLLOW, so a symlink
swapped in between the check and the write can't redirect it either. Destinations never
overwrite: link() fails when the name exists.

`apply_sync` re-plans, refuses unless the plan is the previewed one (`plan_fingerprint`),
and applies it: unlinks, replaces, moves, links, then empty folders. It stops at the first
error. That's always safe to retry: the plan is recomputed from the two trees each time.
Each change is journaled (`secondary_syncs` / `secondary_ops`). There's no undo: every change
but removing a file that isn't a link to a library file can be redone by syncing again (after
fixing an exclusion, say), and those removals need approval file by file.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .core.secondary import Move, Tree, plan_fingerprint
from .db import Store
from .secondary import Analysis, analyze, check_marker

# On Linux, walk paths with directory file descriptors (no symlink can redirect a write).
USE_FDS = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and {os.open, os.link, os.unlink, os.mkdir, os.rmdir, os.stat} <= os.supports_dir_fd
)
DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
TEMP_SUFFIX = ".tagdrifter-move"  # a link parked while moves swap names
_LOCK = threading.Lock()  # one sync at a time in this process


class SyncError(Exception):
    """A sync was refused or stopped; the message says why."""


class UnsafePath(SyncError):
    pass


def _parts(rel: str) -> tuple[str, ...]:
    parts = tuple(rel.split("/"))
    if not rel or rel.startswith("/") or ("\\" in rel and os.name == "nt"):
        raise UnsafePath(f"not a relative path: {rel!r}")
    if any(p in ("", ".", "..") or "\0" in p for p in parts) or not parts:
        raise UnsafePath(f"not a plain relative path: {rel!r}")
    return parts


def _is_link_like(st: os.stat_result) -> bool:
    """A symlink, or on Windows a junction or other reparse point."""
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & reparse)


class SecondaryWriter:
    """Hard links from `library` into `secondary`, and nothing else (see the module doc)."""

    def __init__(self, library: Path, secondary: Path) -> None:
        self.library = Path(os.path.realpath(library))
        self.secondary = Path(os.path.realpath(secondary))
        self.created_dirs: list[str] = []  # by the last operation, for the journal

    # --- Path resolution --------------------------------------------------------------

    @contextmanager
    def _parent(
        self, root: Path, rel: str, *, create: bool = False
    ) -> Iterator[tuple[int | None, Path, str]]:
        """(dir fd or None, parent folder path, name) for `rel` under `root`, refusing
        symlinks on the way; with `create`, missing folders are made (in the secondary)."""
        parts = _parts(rel)
        folders, name = parts[:-1], parts[-1]
        made: list[str] = []
        if USE_FDS:
            fd = os.open(root, DIR_FLAGS)
            try:
                for i, part in enumerate(folders):
                    try:
                        child = os.open(part, DIR_FLAGS, dir_fd=fd)
                    except FileNotFoundError:
                        if not create:
                            raise
                        os.mkdir(part, dir_fd=fd)
                        made.append("/".join(folders[: i + 1]))
                        child = os.open(part, DIR_FLAGS, dir_fd=fd)
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                            where = "/".join(folders[: i + 1])
                            raise UnsafePath(f"{where} isn't a folder") from exc
                        raise
                    os.close(fd)
                    fd = child
                self.created_dirs += made
                yield fd, root.joinpath(*folders), name
            finally:
                os.close(fd)
            return
        path = root
        for i, part in enumerate(folders):
            path = path / part
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(path)
                made.append("/".join(folders[: i + 1]))
                continue
            if _is_link_like(st) or not stat.S_ISDIR(st.st_mode):
                raise UnsafePath(f"{'/'.join(folders[: i + 1])} isn't a folder")
        self.created_dirs += made
        yield None, path, name

    def _lstat(self, fd: int | None, folder: Path, name: str) -> os.stat_result:
        if fd is not None:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        return os.lstat(folder / name)

    def _expect(self, st: os.stat_result, ino: int, where: str) -> None:
        if not stat.S_ISREG(st.st_mode) or _is_link_like(st):
            raise SyncError(f"{where} isn't a regular file")
        if st.st_ino != ino:
            raise SyncError(f"{where} changed since the plan was made")

    # --- Operations -------------------------------------------------------------------

    def link(self, library_rel: str, dst: str, ino: int) -> None:
        """Hard-link library file `library_rel` (inode `ino`) at `dst` in the secondary."""
        with self._parent(self.library, library_rel) as (sfd, sfolder, sname):
            self._expect(self._lstat(sfd, sfolder, sname), ino, library_rel)
            with self._parent(self.secondary, dst, create=True) as (dfd, dfolder, dname):
                self._link(sfd, sfolder, sname, dfd, dfolder, dname)

    def _link(self, sfd, sfolder: Path, sname: str, dfd, dfolder: Path, dname: str) -> None:
        if sfd is not None:
            os.link(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd, follow_symlinks=False)
        else:
            os.link(sfolder / sname, dfolder / dname)

    def move(self, src: str, dst: str, ino: int) -> None:
        """Move the secondary link at `src` (inode `ino`) to `dst`, never overwriting."""
        with self._parent(self.secondary, src) as (sfd, sfolder, sname):
            self._expect(self._lstat(sfd, sfolder, sname), ino, src)
            with self._parent(self.secondary, dst, create=True) as (dfd, dfolder, dname):
                self._link(sfd, sfolder, sname, dfd, dfolder, dname)
            self._unlink(sfd, sfolder, sname)

    def unlink(self, rel: str, ino: int, *, last_copy_ok: bool = False) -> None:
        """Remove `rel` from the secondary if it's still inode `ino` and (unless approved)
        another link to it exists."""
        with self._parent(self.secondary, rel) as (fd, folder, name):
            st = self._lstat(fd, folder, name)
            self._expect(st, ino, rel)
            if st.st_nlink <= 1 and not last_copy_ok:
                raise SyncError(f"{rel} is the only copy of its data; not removed")
            self._unlink(fd, folder, name)

    def _unlink(self, fd, folder: Path, name: str) -> None:
        if fd is not None:
            os.unlink(name, dir_fd=fd)
        else:
            os.unlink(folder / name)

    def exists(self, rel: str) -> bool:
        try:
            with self._parent(self.secondary, rel) as (fd, folder, name):
                self._lstat(fd, folder, name)
        except (FileNotFoundError, UnsafePath):
            return False
        return True

    def rmdir(self, rel: str) -> bool:
        """Remove an empty folder; False (and nothing done) if it isn't empty."""
        with self._parent(self.secondary, rel) as (fd, folder, name):
            try:
                if fd is not None:
                    os.rmdir(name, dir_fd=fd)
                else:
                    if _is_link_like(os.lstat(folder / name)):
                        raise UnsafePath(f"{rel} isn't a folder")
                    os.rmdir(folder / name)
            except OSError as exc:
                gone = isinstance(exc, FileNotFoundError)
                if gone or exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    return False
                raise
        return True


# --- Applying a plan ----------------------------------------------------------------------


@dataclass
class SyncResult:
    sync_id: int
    applied: dict[str, int] = field(default_factory=dict)  # kind -> count
    skipped: list[str] = field(default_factory=list)  # why, per skipped change
    error: str | None = None

    @property
    def changes(self) -> int:
        return sum(v for k, v in self.applied.items() if k != "mkdir")


def _explain(exc: OSError, what: str) -> str:
    hint = {
        errno.EXDEV: "the secondary and the library are on different mounts",
        errno.EROFS: "the secondary is mounted read-only",
        errno.EPERM: (
            "not permitted; to hard-link a file, Tag Drifter's user must own it or be able to "
            "read and write it (fs.protected_hardlinks)"
        ),
        errno.EACCES: "permission denied",
    }.get(exc.errno or 0)
    return f"{what}: {hint or exc.strerror or exc}"


def apply_sync(
    settings: Settings,
    store: Store,
    fingerprint: str,
    *,
    replace_only_copies: bool = False,
    trigger: str = "manual",
    progress: Callable[[int, int], None] | None = None,
) -> SyncResult:
    """Apply the current plan if it's the one previewed (`fingerprint`). Replacing a file
    that's the only copy of its data needs `replace_only_copies`; without it those paths are
    skipped. Raises SyncError when refused; a failure partway is in the result's `error`."""
    if not settings.secondary_sync:
        raise SyncError("Syncing is off; set TAGDRIFTER_SECONDARY_SYNC=true.")
    if not _LOCK.acquire(blocking=False):
        raise SyncError("A sync is already running.")
    try:
        analysis = analyze(settings, store)
        _refuse_unless_ready(analysis, fingerprint)
        return _apply(settings, store, analysis, replace_only_copies, trigger, progress)
    finally:
        _LOCK.release()


def _refuse_unless_ready(analysis: Analysis, fingerprint: str) -> None:
    status = analysis.status
    if not status.ok:
        raise SyncError(" ".join(status.problems))
    if status.warnings:
        raise SyncError(" ".join(status.warnings))
    if analysis.plan is None or plan_fingerprint(analysis.plan) != fingerprint:
        raise SyncError("The libraries changed since the preview; review the changes again.")


def _apply(
    settings: Settings,
    store: Store,
    analysis: Analysis,
    replace_only_copies: bool,
    trigger: str,
    progress: Callable[[int, int], None] | None,
) -> SyncResult:
    plan, main, sec = analysis.plan, analysis.main, analysis.secondary
    assert plan is not None and main is not None and sec is not None
    assert settings.secondary_path is not None
    try:
        check_marker(settings)  # once more, right before writing
    except ValueError as exc:
        raise SyncError(str(exc)) from exc
    writer = SecondaryWriter(settings.library_path, settings.secondary_path)
    result = SyncResult(store.start_secondary_sync(trigger))

    skip = {s.path for s in plan.replaces if s.only_copy and not replace_only_copies}
    result.skipped += [f"{p}: only copy of its data, not replaced" for p in sorted(skip)]
    total = plan.changes - 2 * len(skip)
    done = 0

    def record(kind: str, path: str, src: str = "") -> None:
        nonlocal done
        for folder in writer.created_dirs:
            store.record_secondary_op(result.sync_id, "mkdir", folder)
            result.applied["mkdir"] = result.applied.get("mkdir", 0) + 1
        writer.created_dirs.clear()
        store.record_secondary_op(result.sync_id, kind, path, src)
        result.applied[kind] = result.applied.get(kind, 0) + 1
        done += 1
        if progress:
            progress(done, total)

    current = ""
    try:
        for path, why in plan.unlinks:
            current = f"unlink {path}"
            writer.unlink(path, sec.files[path].ino)
            record("unlink", path, why)
        for stray in plan.replaces:
            if stray.path in skip:
                continue
            current = f"replace {stray.path}"
            writer.unlink(stray.path, sec.files[stray.path].ino, last_copy_ok=stray.only_copy)
            record("replace", stray.path)
        current = "moves"
        _apply_moves(writer, [m for m in plan.moves if m.dst not in skip], sec, record)
        for path in plan.links:
            if path in skip:
                continue
            current = f"link {path}"
            writer.link(path, path, main.files[path].ino)
            record("link", path, path)
        for folder in plan.rmdirs:
            current = f"rmdir {folder}"
            if writer.rmdir(folder):
                record("rmdir", folder)
    except OSError as exc:
        result.error = _explain(exc, current)
    except SyncError as exc:
        result.error = f"{current}: {exc}"
    store.finish_secondary_sync(
        result.sync_id, "failed" if result.error else "applied", result.error
    )
    return result


def _apply_moves(
    writer: SecondaryWriter, moves: list[Move], sec: Tree, record: Callable[..., None]
) -> None:
    """Moves whose destination is taken by a link that moves away too wait for it; a cycle
    (two links swapping names) parks one under a temporary name first."""
    pending = [(m.src, m.dst, sec.files[m.src].ino) for m in moves]
    while pending:
        waiting = []
        for src, dst, ino in pending:
            if writer.exists(dst):
                waiting.append((src, dst, ino))
                continue
            writer.move(src, dst, ino)
            record("move", dst, src)
        if len(waiting) == len(pending):  # no progress: a cycle, or something in the way
            sources = {s for s, _, _ in waiting}
            blocked = next((d for _, d, _ in waiting if d not in sources), None)
            if blocked is not None:
                raise SyncError(f"{blocked} is in the way")
            src, dst, ino = waiting[0]  # a cycle: park one link to break it
            parked = src + TEMP_SUFFIX
            if writer.exists(parked):
                raise SyncError(f"{parked} is in the way")
            writer.move(src, parked, ino)
            waiting[0] = (parked, dst, ino)
        pending = waiting
