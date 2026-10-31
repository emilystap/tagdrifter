"""Built-in checks about tag contents."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from ..core import TrackMeta, audit_album_placement, is_mbid
from ..core.metadata import (
    ALBUM_ARTIST_KEYS,
    ALBUM_ARTISTS_KEYS,
    ARTISTS_KEYS,
    DATE_KEYS,
    RECORDING_MBID_KEYS,
    RELEASE_MBID_KEYS,
    parse_index,
)
from .base import AlbumContext, Check, Issue, Scope, Severity, TrackContext, register

# Extra tags to require, as "a|b" alternatives (any one present satisfies the requirement).
# Empty by default: MBIDs and the tags the naming rules use have their own checks.
DEFAULT_REQUIRED_TAGS: tuple[str, ...] = ()

# MBID tags besides the recording MBID: (label, tag keys).
MBID_TAGS = (
    ("release", RELEASE_MBID_KEYS),
    ("release group", ("musicbrainz_releasegroupid",)),
    ("release track", ("musicbrainz_releasetrackid",)),
    ("artist", ("musicbrainz_artistid",)),
    ("album artist", ("musicbrainz_albumartistid",)),
)

# Tags the naming rules read (core/naming.py): (label, tag keys, what happens without it).
NAMING_TAGS = (
    ("album artist", ALBUM_ARTIST_KEYS, "the artist folder falls back to the track artist"),
    ("track artist", ("artist",), "the filename falls back to the album artist"),
    ("album", ("album",), 'the album folder becomes "Unknown Album"'),
    ("title", ("title",), 'the filename becomes "Unknown Track"'),
    ("track number", ("tracknumber",), "the filename index defaults to 01"),
    ("date", DATE_KEYS, "releases with the same title can't be told apart by year"),
)
DISC_NUMBER_FALLBACK = "multi-disc filenames default to disc 1"
TOTAL_TRACKS_KEYS = ("totaltracks", "tracktotal")

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

RE_MULTI_SPLIT = re.compile(r"[;/,\s]+")

DEFAULT_CONSISTENT_TAGS = (
    "albumartist",
    "album",
    "musicbrainz_albumid",
    "musicbrainz_albumartistid",
)


def _has_any(meta: TrackMeta, keys: Iterable[str]) -> bool:
    return any(meta.has_tag(k) for k in keys)


def _mbids(meta: TrackMeta, key: str) -> list[str]:
    """A multi-valued MBID tag's values (Picard writes one per artist; some taggers join
    them into one value)."""
    return [p for v in meta.tags.get(key, ()) for p in RE_MULTI_SPLIT.split(v.strip()) if p]


def _names(meta: TrackMeta, keys: Iterable[str]) -> list[str]:
    """A multi-valued name tag's values (e.g. ARTISTS), without splitting inside a value."""
    for key in keys:
        values = [v.strip() for v in meta.tags.get(key, ()) if v.strip()]
        if values:
            return values
    return []


@register
class RecordingMbid(Check):
    id = "tags.recording_mbid"
    scope = Scope.TRACK
    severity = Severity.ERROR
    description = (
        "Track has no recording MBID (musicbrainz_trackid), which ListenBrainz uses to match "
        "scrobbles."
    )

    def run(self, ctx: TrackContext) -> Iterable[Issue]:
        if not _has_any(ctx.meta, RECORDING_MBID_KEYS):
            yield self.issue(
                ctx.rel_path,
                "Missing recording MBID (musicbrainz_trackid): scrobbles are matched by artist "
                "and title only",
            )


@register
class MissingMbids(Check):
    id = "tags.mbids"
    scope = Scope.TRACK
    description = (
        "Track is missing MusicBrainz IDs: release, release group, release track, artist or "
        "album artist."
    )

    def run(self, ctx: TrackContext) -> Iterable[Issue]:
        missing = [(label, keys[0]) for label, keys in MBID_TAGS if not _has_any(ctx.meta, keys)]
        if missing:
            yield self.issue(
                ctx.rel_path,
                f"Missing MusicBrainz IDs: {', '.join(label for label, _ in missing)}",
                missing=[label for label, _ in missing],
                tags=[tag for _, tag in missing],
            )


@register
class NamingTags(Check):
    id = "tags.naming"
    scope = Scope.TRACK
    description = "Track is missing tags the naming rules use, so its path falls back."

    def run(self, ctx: TrackContext) -> Iterable[Issue]:
        meta = ctx.meta
        missing = {
            label: fallback for label, keys, fallback in NAMING_TAGS if not _has_any(meta, keys)
        }
        if meta.total_discs > 1 and not meta.has_tag("discnumber"):
            missing["disc number"] = DISC_NUMBER_FALLBACK
        if missing:
            yield self.issue(
                ctx.rel_path,
                f"Missing: {', '.join(missing)} ({'; '.join(missing.values())})",
                missing=list(missing),
                fallbacks=missing,
            )


@register
class RequiredTags(Check):
    id = "tags.required"
    scope = Scope.TRACK
    description = 'Track is missing tags listed in [checks."tags.required"] (none by default).'

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
                invalid = [p for p in parts if not is_mbid(p)]
                if invalid:
                    bad.setdefault(key, []).extend(invalid)
        if bad:
            summary = ", ".join(f"{k}={v[0]!r}" for k, v in bad.items())
            yield self.issue(ctx.rel_path, f"Invalid MusicBrainz ID: {summary}", invalid=bad)


def _count_mismatch(names: list[str], ids: list[str], what: str, tag: str) -> str | None:
    """Why `names` (e.g. ARTISTS) and `ids` (e.g. MUSICBRAINZ_ARTISTID) don't pair up, if they
    don't. Without a names tag, a single MBID is unambiguous; several need their names."""
    if not ids:
        return None  # tags.mbids reports missing MBIDs
    if not names:
        if len(ids) > 1:
            return f"{len(ids)} {what} MBIDs but no {tag} tag naming them"
        return None
    if len(names) != len(ids):
        return f"{len(names)} {what} name{'s' * (len(names) != 1)} but {len(ids)} MBIDs"
    return None


@register
class ArtistMbidCount(Check):
    id = "tags.artist_mbid_count"
    scope = Scope.TRACK
    description = "Track's ARTISTS names and artist MBIDs differ in number."

    def run(self, ctx: TrackContext) -> Iterable[Issue]:
        names = _names(ctx.meta, ARTISTS_KEYS)
        ids = _mbids(ctx.meta, "musicbrainz_artistid")
        if reason := _count_mismatch(names, ids, "artist", "ARTISTS"):
            yield self.issue(ctx.rel_path, reason, names=names, mbids=ids)


@register
class AlbumArtistMbidCount(Check):
    id = "album.artist_mbid_count"
    scope = Scope.ALBUM
    description = "Album's ALBUMARTISTS names and album artist MBIDs differ in number."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        for t in ctx.tracks:  # album-level tags: the first track that shows it is enough
            names = _names(t.meta, ALBUM_ARTISTS_KEYS)
            ids = _mbids(t.meta, "musicbrainz_albumartistid")
            if reason := _count_mismatch(names, ids, "album artist", "ALBUMARTISTS"):
                yield self.issue(
                    ctx.rel_path,
                    f"{reason} (album artist {t.meta.albumartist!r})",
                    names=names,
                    mbids=ids,
                    albumartist=t.meta.albumartist,
                )
                return


@register
class DuplicateTracks(Check):
    id = "album.duplicate_tracks"
    scope = Scope.ALBUM
    severity = Severity.ERROR
    description = (
        "Two files in an album are the same track: same release track MBID, or same release, "
        "disc and track number."
    )

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        by_mbid: dict[str, list[str]] = defaultdict(list)
        by_position: dict[tuple[str, int, int], list[str]] = defaultdict(list)
        for t in ctx.tracks:
            name = t.rel_path.name
            for mbid in _mbids(t.meta, "musicbrainz_releasetrackid")[:1]:
                by_mbid[mbid.lower()].append(name)
            if t.meta.has_tag("tracknumber"):
                release = t.meta.release_mbid.lower()
                by_position[(release, t.meta.disc_number, t.meta.track_number)].append(name)
        groups = sorted(
            {tuple(sorted(names)) for names in [*by_mbid.values(), *by_position.values()]
             if len(names) > 1}
        )  # fmt: skip
        if groups:
            yield self.issue(
                ctx.rel_path,
                "Duplicate tracks: " + "; ".join(" = ".join(g) for g in groups),
                duplicates=[list(g) for g in groups],
            )


@register
class MissingTracks(Check):
    id = "album.missing_tracks"
    scope = Scope.ALBUM
    severity = Severity.INFO
    description = "Album has fewer tracks or discs than its TOTALTRACKS / TOTALDISCS tags say."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        releases: dict[str, list[TrackMeta]] = defaultdict(list)
        for t in ctx.tracks:
            releases[t.meta.release_mbid.lower()].append(t.meta)
        gaps: list[str] = []
        details: list[dict] = []
        for metas in releases.values():
            discs: dict[int, list[TrackMeta]] = defaultdict(list)
            for m in metas:
                discs[m.disc_number].append(m)
            total_discs = max(m.total_discs for m in metas)
            multi = total_discs > 1 or len(discs) > 1
            for disc, tracks in sorted(discs.items()):
                total = Counter(_total_tracks(m) for m in tracks).most_common(1)[0][0]
                have = {m.track_number for m in tracks}
                absent = [n for n in range(1, total + 1) if n not in have]
                if absent:
                    where = f"disc {disc}: " if multi else ""
                    gaps.append(f"{where}{len(have)} of {total} tracks (missing {_ranges(absent)})")
                    details.append({"disc": disc, "total": total, "missing": absent})
            # Only when the numbering fits the total: a release with a medium Picard didn't
            # tag can have discs 2-3 of TOTALDISCS 2, and both are there.
            absent_discs = [d for d in range(1, total_discs + 1) if d not in discs]
            if len(discs) < total_discs and max(discs) <= total_discs:
                gaps.append(f"disc{'s' * (len(absent_discs) > 1)} {_ranges(absent_discs)} missing")
                details.append({"total_discs": total_discs, "missing_discs": absent_discs})
        if gaps:
            message = "; ".join(gaps)
            yield self.issue(ctx.rel_path, message[0].upper() + message[1:], gaps=details)


@register
class CoverArt(Check):
    id = "album.cover_art"
    scope = Scope.ALBUM
    description = (
        "Tracks without an embedded cover, with pictures but none marked as the front cover, "
        "or (with min_size set) a front cover smaller than min_size pixels."
    )

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        min_size = int(self.options.get("min_size", 0))
        want_front = bool(self.options.get("require_front_cover", True))
        known = [t for t in ctx.tracks if t.meta.pictures is not None]
        problems: dict[str, list[str]] = {}
        sizes: set[str] = set()
        for t in known:
            pictures = t.meta.pictures or ()
            front = next((p for p in pictures if p.is_front_cover), None)
            if not pictures:
                problems.setdefault("no embedded cover", []).append(t.rel_path.name)
            elif front is None and want_front:
                problems.setdefault("no picture marked front cover", []).append(t.rel_path.name)
            cover = front or (pictures[0] if pictures else None)
            if cover and min_size and 0 < min(cover.width, cover.height) < min_size:
                problems.setdefault(f"a cover smaller than {min_size} px", []).append(
                    t.rel_path.name
                )
                sizes.add(f"{cover.width}x{cover.height}")
        if not problems:
            return
        parts = []
        for what, files in problems.items():
            if len(files) == len(known):
                count = "All tracks" if len(known) > 1 else "The track"
                verb = "have" if len(known) > 1 else "has"
            else:
                count = f"{len(files)} of {len(known)} tracks"
                verb = "has" if len(files) == 1 else "have"
            parts.append(f"{count} {verb} {what}")
        message = "; ".join(parts)
        if sizes:
            message += f" ({', '.join(sorted(sizes))})"
        yield self.issue(ctx.rel_path, message, files=problems)


def _total_tracks(meta: TrackMeta) -> int:
    """The disc's track count from TOTALTRACKS / TRACKTOTAL or a "3/12" track number; 0 if
    unknown."""
    for key in TOTAL_TRACKS_KEYS:
        for value in meta.tags.get(key, ()):
            if total := parse_index(value, 0):
                return total
    number = next(iter(meta.tags.get("tracknumber", ())), "")
    _, sep, total = number.partition("/")
    return parse_index(total, 0) if sep else 0


def _ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 5] -> "1-3, 5"."""
    spans: list[list[int]] = []
    for n in numbers:
        if spans and spans[-1][1] == n - 1:
            spans[-1][1] = n
        else:
            spans.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


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
