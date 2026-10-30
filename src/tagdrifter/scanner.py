"""Library scan: discover files, read changed tags, run checks, persist results.

Incremental: a file's tags are re-read only when its size or modification time changed
since the last scan (or with full=True). Checks always run over the whole library, because
some (release collisions, placement) depend on a file's neighbours; they are cheap.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .checks import (
    AlbumContext,
    ArtistContext,
    Check,
    Issue,
    LibraryContext,
    Scope,
    Severity,
    TrackContext,
    UnreadableFile,
    build_checks,
)
from .config import Settings
from .core import (
    MetadataReadError,
    TrackMeta,
    compute_target,
    read_tags,
    resolve_release_collisions,
)
from .db import Store

SAVE_BATCH = 200  # flush freshly read tags this often, so an interrupted scan keeps its work


# --- Progress reporting -----------------------------------------------------------------


@dataclass(frozen=True)
class ScanProgress:
    phase: str  # discover | read | check | save
    done: int
    total: int  # 0 when unknown (discovery)
    current: str = ""


ProgressCallback = Callable[[ScanProgress], None]


@dataclass
class ScanResult:
    scan_id: int
    files_total: int = 0
    files_read: int = 0
    files_removed: int = 0
    issues_open: int = 0
    issues_new: int = 0
    issues_resolved: int = 0
    check_errors: list[str] = field(default_factory=list)
    duration: float = 0.0


# --- Discovery --------------------------------------------------------------------------


@dataclass
class DirEntryInfo:
    rel: PurePosixPath
    audio: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (size, mtime_ns)
    other_files: list[str] = field(default_factory=list)
    subdirs: list[str] = field(default_factory=list)


def discover(
    root: Path, settings: Settings, progress: ProgressCallback | None = None
) -> dict[PurePosixPath, DirEntryInfo]:
    """Walk the library (no symlink following). Returns {relative dir: contents}."""
    ignore = settings.ignore_patterns
    audio_exts = settings.audio_extensions
    dirs: dict[PurePosixPath, DirEntryInfo] = {}
    stack = [PurePosixPath()]
    files_seen = 0
    while stack:
        rel = stack.pop()
        info = dirs[rel] = DirEntryInfo(rel)
        with os.scandir(root.joinpath(*rel.parts)) as entries:
            for entry in entries:
                if any(fnmatchcase(entry.name, pat) for pat in ignore):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    info.subdirs.append(entry.name)
                    stack.append(rel / entry.name)
                elif entry.is_file(follow_symlinks=False):
                    if os.path.splitext(entry.name)[1].lower() in audio_exts:
                        st = entry.stat(follow_symlinks=False)
                        info.audio[entry.name] = (st.st_size, st.st_mtime_ns)
                        files_seen += 1
                    else:
                        info.other_files.append(entry.name)
        info.subdirs.sort()
        info.other_files.sort()
        if progress:
            progress(ScanProgress("discover", files_seen, 0, str(rel)))
    return dirs


# --- Scan -------------------------------------------------------------------------------


def _read_one(root: Path, rel: str) -> tuple[dict[str, tuple[str, ...]] | None, str | None]:
    try:
        return read_tags(root / rel), None
    except MetadataReadError as exc:
        return None, str(exc).split(": ", 1)[-1]


def run_scan(
    settings: Settings,
    store: Store,
    progress: ProgressCallback | None = None,
    *,
    full: bool = False,
) -> ScanResult:
    """Scan the library and record results. KeyboardInterrupt marks the scan cancelled
    (tags read so far are kept) and re-raises."""
    root = settings.library_path
    if not root.is_dir():
        raise FileNotFoundError(f"library path not found or not a directory: {root}")

    started = time.monotonic()
    result = ScanResult(scan_id=store.start_scan(str(root)))
    try:
        _scan(settings, store, root, result, progress, full)
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
        store.finish_scan(
            result.scan_id,
            status,
            error=repr(exc) if status == "failed" else None,
            files_total=result.files_total,
            files_read=result.files_read,
        )
        raise
    result.duration = time.monotonic() - started
    return result


def _scan(
    settings: Settings,
    store: Store,
    root: Path,
    result: ScanResult,
    progress: ProgressCallback | None,
    full: bool,
) -> None:
    def report(phase: str, done: int, total: int, current: str = "") -> None:
        if progress:
            progress(ScanProgress(phase, done, total, current))

    # 1. Discover
    dirs = discover(root, settings, progress)
    audio: dict[str, tuple[int, int]] = {
        str(info.rel / name): stat for info in dirs.values() for name, stat in info.audio.items()
    }
    result.files_total = len(audio)

    # 2. Read tags of new/changed files
    cache = store.load_file_cache()
    removed = cache.keys() - audio.keys()
    store.delete_files(removed)
    result.files_removed = len(removed)

    tags_by_path: dict[str, dict[str, tuple[str, ...]] | None] = {}
    errors: dict[str, str] = {}
    to_read: list[str] = []
    for rel, (size, mtime) in audio.items():
        cached = cache.get(rel)
        if full or cached is None or (cached.size, cached.mtime_ns) != (size, mtime):
            to_read.append(rel)
        else:
            tags_by_path[rel] = cached.tags
            if cached.error:
                errors[rel] = cached.error

    report("read", 0, len(to_read))
    pending: list[tuple[str, int, int, dict | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=max(1, settings.workers)) as pool:
        futures = {pool.submit(_read_one, root, rel): rel for rel in to_read}
        try:
            for done, future in enumerate(as_completed(futures), 1):
                rel = futures[future]
                tags, error = future.result()
                tags_by_path[rel] = tags
                if error:
                    errors[rel] = error
                pending.append((rel, *audio[rel], tags, error))
                result.files_read = done
                if len(pending) >= SAVE_BATCH:
                    store.save_files(pending)
                    pending.clear()
                report("read", done, len(to_read), rel)
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            store.save_files(pending)

    # 3. Run checks
    issues = list(_run_checks(settings, dirs, tags_by_path, errors, result, report))

    # 4. Save
    report("save", 0, 1)
    result.issues_open, result.issues_new, result.issues_resolved = store.sync_issues(
        result.scan_id, issues
    )
    store.finish_scan(
        result.scan_id,
        "completed",
        files_total=result.files_total,
        files_read=result.files_read,
        files_removed=result.files_removed,
        issues_open=result.issues_open,
        issues_new=result.issues_new,
        issues_resolved=result.issues_resolved,
    )
    report("save", 1, 1)


def _run_checks(
    settings: Settings,
    dirs: dict[PurePosixPath, DirEntryInfo],
    tags_by_path: dict[str, dict[str, tuple[str, ...]] | None],
    errors: dict[str, str],
    result: ScanResult,
    report: Callable[..., None],
) -> Iterable[Issue]:
    checks = build_checks(settings)
    by_scope: dict[Scope, list[Check]] = defaultdict(list)
    for check in checks:
        by_scope[check.scope].append(check)

    def run(scope: Scope, ctx, subject: PurePosixPath | str) -> Iterable[Issue]:
        for check in by_scope[scope]:
            try:
                yield from check.run(ctx)
            except Exception as exc:  # a buggy check must not abort the scan
                result.check_errors.append(f"{check.id} on {subject}: {exc!r}")
                yield Issue(
                    "scanner.check_error",
                    Severity.ERROR,
                    f"{subject} [{check.id}]",
                    f"Check {check.id} crashed: {exc!r}",
                )

    root_info = dirs[PurePosixPath()]
    yield from run(
        Scope.LIBRARY,
        LibraryContext(tuple(root_info.subdirs), tuple(sorted(root_info.audio)), settings),
        "",
    )

    # Group album directories (any dir with audio, below the root) by artist folder.
    albums_by_artist: dict[str, list[DirEntryInfo]] = defaultdict(list)
    for rel, info in dirs.items():
        if rel.parts and info.audio:
            albums_by_artist[rel.parts[0]].append(info)

    artists = sorted(set(root_info.subdirs) | albums_by_artist.keys())
    for done, artist in enumerate(artists, 1):
        report("check", done, len(artists), artist)
        album_infos = sorted(albums_by_artist.get(artist, []), key=lambda i: str(i.rel))

        # Expected paths for every readable track under this artist, then collision fixes.
        readable: list[tuple[PurePosixPath, TrackMeta]] = []
        for info in album_infos:
            for name in sorted(info.audio):
                rel = info.rel / name
                tags = tags_by_path.get(str(rel))
                if tags is not None:
                    readable.append((rel, TrackMeta.from_tags(rel, tags)))
        targets, _ = resolve_release_collisions(
            [(meta, compute_target(meta, artist)) for _, meta in readable]
        )
        track_ctx = {
            rel: TrackContext(rel, meta, target, settings)
            for (rel, meta), target in zip(readable, targets, strict=True)
        }

        album_contexts = []
        for info in album_infos:
            tracks = tuple(
                track_ctx[info.rel / n] for n in sorted(info.audio) if info.rel / n in track_ctx
            )
            unreadable = tuple(
                UnreadableFile(info.rel / n, errors.get(str(info.rel / n), "unreadable"))
                for n in sorted(info.audio)
                if info.rel / n not in track_ctx
            )
            album = AlbumContext(info.rel, tracks, unreadable, tuple(info.other_files), settings)
            album_contexts.append(album)
            for track in tracks:
                yield from run(Scope.TRACK, track, track.rel_path)
            yield from run(Scope.ALBUM, album, album.rel_path)

        artist_info = dirs.get(PurePosixPath(artist))
        subfolders = tuple(artist_info.subdirs) if artist_info else ()
        yield from run(
            Scope.ARTIST,
            ArtistContext(PurePosixPath(artist), tuple(album_contexts), subfolders, settings),
            artist,
        )
