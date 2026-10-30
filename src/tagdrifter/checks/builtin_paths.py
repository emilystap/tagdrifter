"""Built-in checks about where files live and what they are called."""

from __future__ import annotations

from collections.abc import Iterable

from ..core import PathMatch, compare_paths, find_case_collisions, is_mismatch, path_key
from .base import (
    AlbumContext,
    ArtistContext,
    Check,
    Issue,
    LibraryContext,
    Scope,
    Severity,
    TrackContext,
    register,
)


@register
class TrackLocation(Check):
    id = "path.track_location"
    scope = Scope.TRACK
    description = "Track folder or filename differs from the naming convention."

    def run(self, ctx: TrackContext) -> Iterable[Issue]:
        expected = ctx.target.relative_path
        match = compare_paths(ctx.rel_path, expected)
        if not is_mismatch(match, case_sensitive=ctx.settings.case_sensitive):
            return
        folder_ok = compare_paths(ctx.rel_path.parent, expected.parent) is PathMatch.EXACT
        name_ok = compare_paths(ctx.rel_path.name, expected.name) is PathMatch.EXACT
        if match is PathMatch.CASE_ONLY:
            what = "Letter case differs from the expected path"
        elif not folder_ok and not name_ok:
            what = "Wrong album folder and filename"
        elif not folder_ok:
            what = "Wrong album folder"
        else:
            what = "Wrong filename"
        yield self.issue(
            ctx.rel_path,
            f"{what}; expected {expected}",
            expected=str(expected),
            folder_ok=folder_ok,
            filename_ok=name_ok,
            case_only=match is PathMatch.CASE_ONLY,
        )


@register
class OrphanSidecar(Check):
    id = "path.orphan_sidecar"
    scope = Scope.ALBUM
    severity = Severity.INFO
    description = "Lyrics/sidecar file (.lrc, .txt) without an audio file of the same name."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        cs = ctx.settings.case_sensitive
        stems = {path_key(t.rel_path.stem, case_sensitive=cs) for t in ctx.tracks}
        stems |= {path_key(u.rel_path.stem, case_sensitive=cs) for u in ctx.unreadable}
        for name in ctx.other_files:
            suffix = name[name.rfind(".") :].lower() if "." in name else ""
            if suffix not in ctx.settings.sidecar_extensions:
                continue
            stem = name[: -len(suffix)]
            if path_key(stem, case_sensitive=cs) not in stems:
                yield self.issue(
                    ctx.rel_path / name, "No audio file with a matching name in this folder"
                )


def _collision_issues(check: Check, parent: str, names: Iterable[str]) -> Iterable[Issue]:
    groups = find_case_collisions(names)
    if groups:
        listed = "; ".join(" / ".join(g) for g in groups)
        yield check.issue(
            parent or ".",
            f"Names differ only by letter case (SMB/Windows clients may show only one): {listed}",
            groups=groups,
        )


@register
class FileCaseCollision(Check):
    id = "path.case_collision_files"
    scope = Scope.ALBUM
    severity = Severity.ERROR
    description = "Files in one folder whose names differ only by letter case."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        names = [t.rel_path.name for t in ctx.tracks]
        names += [u.rel_path.name for u in ctx.unreadable]
        names += list(ctx.other_files)
        yield from _collision_issues(self, str(ctx.rel_path), names)


@register
class AlbumCaseCollision(Check):
    id = "path.case_collision_albums"
    scope = Scope.ARTIST
    severity = Severity.ERROR
    description = "Album folders under one artist whose names differ only by letter case."

    def run(self, ctx: ArtistContext) -> Iterable[Issue]:
        yield from _collision_issues(self, str(ctx.rel_path), ctx.subfolders)


@register
class ArtistCaseCollision(Check):
    id = "path.case_collision_artists"
    scope = Scope.LIBRARY
    severity = Severity.ERROR
    description = "Artist folders whose names differ only by letter case."

    def run(self, ctx: LibraryContext) -> Iterable[Issue]:
        yield from _collision_issues(self, "", ctx.artist_folders)


@register
class LooseFile(Check):
    id = "path.loose_file"
    scope = Scope.LIBRARY
    description = "Audio file sitting directly in the library root instead of an artist folder."

    def run(self, ctx: LibraryContext) -> Iterable[Issue]:
        for name in ctx.loose_files:
            yield self.issue(name, "Audio file is not inside an artist/album folder")
