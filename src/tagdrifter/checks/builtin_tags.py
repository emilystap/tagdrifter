"""Built-in checks about tag contents."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..core import audit_album_placement
from .base import AlbumContext, Check, Issue, Scope, Severity, TrackContext, register

# Alternatives separated by "|": any one present satisfies the requirement.
DEFAULT_REQUIRED_TAGS = (
    "albumartist|album artist",
    "artist",
    "album",
    "title",
    "tracknumber",
    "date|year|originaldate",
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "musicbrainz_trackid",
)

# Tags holding MusicBrainz UUIDs. (musicbrainz_discid is not a UUID and is deliberately absent.)
DEFAULT_MBID_TAGS = (
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "musicbrainz_trackid",
    "musicbrainz_releasetrackid",
    "musicbrainz_workid",
)

RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
RE_MULTI_SPLIT = re.compile(r"[;/,\s]+")

DEFAULT_CONSISTENT_TAGS = (
    "albumartist",
    "album",
    "musicbrainz_albumid",
    "musicbrainz_albumartistid",
)


@register
class RequiredTags(Check):
    id = "tags.required"
    scope = Scope.TRACK
    description = "Track is missing tags that the library relies on (configurable)."

    def run(self, ctx: TrackContext) -> Iterable[Issue]:
        required = self.options.get("tags", DEFAULT_REQUIRED_TAGS)
        missing = [
            spec.split("|")[0]
            for spec in required
            if not any(ctx.meta.has_tag(alt.strip()) for alt in spec.split("|"))
        ]
        if missing:
            yield self.issue(ctx.rel_path, f"Missing: {', '.join(missing)}", missing=missing)


@register
class InvalidMbid(Check):
    id = "tags.invalid_mbid"
    scope = Scope.TRACK
    severity = Severity.ERROR
    description = "A MusicBrainz ID tag is present but is not a valid UUID."

    def run(self, ctx: TrackContext) -> Iterable[Issue]:
        bad: dict[str, list[str]] = {}
        for key in self.options.get("tags", DEFAULT_MBID_TAGS):
            for value in ctx.meta.tags.get(key, ()):
                parts = [p for p in RE_MULTI_SPLIT.split(value.strip()) if p]
                invalid = [p for p in parts if not RE_UUID.match(p)]
                if invalid:
                    bad.setdefault(key, []).extend(invalid)
        if bad:
            summary = ", ".join(f"{k}={v[0]!r}" for k, v in bad.items())
            yield self.issue(ctx.rel_path, f"Invalid MusicBrainz ID: {summary}", invalid=bad)


@register
class AlbumPlacement(Check):
    id = "album.placement"
    scope = Scope.ALBUM
    description = "Album is filed under an artist folder that does not match its album artist."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        if not ctx.is_directly_under_artist or not ctx.tracks:
            return
        result = audit_album_placement(
            ctx.artist_folder,
            [t.meta for t in ctx.tracks],
            case_sensitive=ctx.settings.case_sensitive,
        )
        if result:
            yield self.issue(
                ctx.rel_path,
                f"{result.issue_type.value}: tagged album artist is "
                f"{result.tagged_album_artist!r}, "
                f"expected folder {result.expected_artist_folder!r}",
                issue_type=result.issue_type.value,
                tagged_album_artist=result.tagged_album_artist,
                expected_artist_folder=result.expected_artist_folder,
                unique_track_artists=result.unique_track_artists,
            )


@register
class InconsistentAlbumTags(Check):
    id = "album.inconsistent_tags"
    scope = Scope.ALBUM
    description = "Tracks in one album folder disagree on album-level tags (configurable)."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        if len(ctx.tracks) < 2:
            return
        disagreements: dict[str, list[str]] = {}
        for key in self.options.get("tags", DEFAULT_CONSISTENT_TAGS):
            values = {"; ".join(t.meta.tags.get(key, ())) for t in ctx.tracks}
            if len(values) > 1:
                disagreements[key] = sorted(values)
        if disagreements:
            summary = ", ".join(f"{k} ({len(v)} values)" for k, v in disagreements.items())
            yield self.issue(ctx.rel_path, f"Tracks disagree on: {summary}", values=disagreements)


@register
class Unreadable(Check):
    id = "file.unreadable"
    scope = Scope.ALBUM
    severity = Severity.ERROR
    description = "Audio file whose tags could not be read (corrupt or not really that format)."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        for item in ctx.unreadable:
            yield self.issue(item.rel_path, f"Could not read tags: {item.error}")
