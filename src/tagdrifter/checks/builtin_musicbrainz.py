"""Checks comparing tags with cached MusicBrainz releases (roadmap 6b).

They only read `AlbumContext.releases` (filled from the cache by the scanner when
`musicbrainz_enabled`); a release that hasn't been looked up yet is skipped, so these checks
never touch the network. Each runs once per album folder and reports per track where the
problem is a track's. Picard's tag mapping: musicbrainz_albumid = release,
musicbrainz_releasetrackid = track, musicbrainz_trackid = recording, discnumber = medium
position, tracknumber = position on the medium (not the printed number, e.g. "A1"),
totaltracks = the medium's track count, totaldiscs = the release's number of media.

The text checks (mb.album, mb.track_title, mb.artist_credit) compare exactly, letter case
included, as the owner's Picard writes them (no punctuation conversion), but accept Picard's
English translations and standardized artist names (core/mbrelease.py).
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core import TrackMeta
from ..core.mbrelease import MbCredit, MbRelease, MbTrack, credit_matches, credit_text
from ..core.metadata import (
    ALBUM_ARTIST_KEYS,
    ALBUM_ARTISTS_KEYS,
    ARTIST_KEYS,
    ARTISTS_KEYS,
    RECORDING_MBID_KEYS,
    first_tag,
    parse_index,
)
from .base import AlbumContext, Check, Issue, Scope, Severity, TrackContext, register
from .builtin_tags import _mbids, _names

TOTAL_TRACKS_KEYS = ("totaltracks", "tracktotal")
TOTAL_DISCS_KEYS = ("totaldiscs", "disctotal")
DEFAULT_TOLERANCE_SECONDS = 3


def _tag(meta: TrackMeta, *keys: str) -> str:
    return first_tag(meta.tags, keys)


def _locate(meta: TrackMeta, release: MbRelease) -> tuple[MbTrack | None, bool]:
    """The release's track this file is: by release track MBID, else by disc and position.
    Returns (track, found_by_id)."""
    track_id = _tag(meta, "musicbrainz_releasetrackid")
    if track_id:
        track = release.track_by_id(track_id)
        if track is not None:
            return track, True
    if meta.has_tag("tracknumber"):
        return release.track_at(meta.disc_number, meta.track_number), False
    return None, False


def _tracks_with_release(ctx: AlbumContext) -> Iterable[tuple[TrackContext, MbRelease]]:
    for t in ctx.tracks:
        release = ctx.release_of(t.meta)
        if release is not None:
            yield t, release


def _duration(ms: int) -> str:
    seconds = round(ms / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


@register
class ReleaseUnknown(Check):
    id = "mb.release_unknown"
    scope = Scope.ALBUM
    severity = Severity.ERROR
    description = "Release MBID that MusicBrainz doesn't have (deleted) or has merged into another."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        problems: dict[str, str] = {}
        for t in ctx.tracks:
            mbid = t.meta.release_mbid.lower()
            if not mbid or mbid not in ctx.releases or mbid in problems:
                continue
            release = ctx.releases[mbid]
            if release is None:
                problems[mbid] = f"Release {mbid} isn't in MusicBrainz (deleted?)"
            elif release.id != mbid:
                problems[mbid] = (
                    f"Release {mbid} was merged into {release.id} ({release.title!r}); "
                    "retag with Picard"
                )
        if problems:
            yield self.issue(ctx.rel_path, "; ".join(problems.values()), releases=problems)


@register
class TrackMismatch(Check):
    id = "mb.track_mismatch"
    scope = Scope.ALBUM
    description = (
        "Track's release track or recording MBID doesn't match the tagged release in "
        "MusicBrainz (a different version, or a stale tag)."
    )

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        for t, release in _tracks_with_release(ctx):
            meta = t.meta
            track_id = _tag(meta, "musicbrainz_releasetrackid").lower()
            recording_id = first_tag(meta.tags, RECORDING_MBID_KEYS).lower()
            track, by_id = _locate(meta, release)
            if track_id and not by_id:
                where = (
                    f"; at disc {track.disc} track {track.position} it has {track.title!r}"
                    if track is not None
                    else ""
                )
                yield self.issue(
                    t.rel_path,
                    f"Release track MBID isn't on {release.title!r} in MusicBrainz{where}",
                    release_track_mbid=track_id,
                    expected_release_track_mbid=track.id if track else None,
                )
            elif track is not None and recording_id and recording_id != track.recording_id:
                yield self.issue(
                    t.rel_path,
                    f"Recording MBID differs: MusicBrainz has {track.recording_id} "
                    f"({track.recording_title!r}) for disc {track.disc} track {track.position}",
                    recording_mbid=recording_id,
                    expected_recording_mbid=track.recording_id,
                )


@register
class Numbering(Check):
    id = "mb.numbering"
    scope = Scope.ALBUM
    description = "Track or disc number, or a total, differs from the release in MusicBrainz."

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        # Track and disc numbers are reported per track; totals are album facts (e.g. a track
        # removed from the release in MusicBrainz), so they're reported once, on the album.
        totals: dict[str, dict[str, int]] = {}
        for t, release in _tracks_with_release(ctx):
            track, by_id = _locate(t.meta, release)
            if track is None or not by_id:
                continue  # mb.track_mismatch's job
            medium = release.medium(track.disc)
            numbers = {
                "track number": (("tracknumber",), track.position),
                "disc number": (("discnumber",), track.disc),
            }
            wrong = _differences(t.meta, numbers)
            if wrong:
                yield self.issue(t.rel_path, _summary(wrong), differences=wrong)
            disc_totals = {
                f"disc {track.disc} total tracks": (
                    TOTAL_TRACKS_KEYS,
                    medium.track_count if medium else 0,
                ),
                "total discs": (TOTAL_DISCS_KEYS, len(release.media)),
            }
            for label, diff in _differences(t.meta, disc_totals).items():
                totals.setdefault(label, diff)
        if totals:
            if len({k for k in totals if k.startswith("disc ")}) == 1:  # say "total tracks"
                totals = {
                    k.split(" ", 2)[-1] if k.startswith("disc ") else k: v
                    for k, v in totals.items()
                }
            yield self.issue(ctx.rel_path, _summary(totals), differences=totals)


def _differences(
    meta: TrackMeta, expected: dict[str, tuple[tuple[str, ...], int]]
) -> dict[str, dict[str, int]]:
    wrong: dict[str, dict[str, int]] = {}
    for label, (keys, want) in expected.items():
        value = _tag(meta, *keys)
        if not value or not want:
            continue  # missing tags are tags.naming's job
        have = parse_index(value, 0)
        if have != want:
            wrong[label] = {"tagged": have, "musicbrainz": want}
    return wrong


def _summary(wrong: dict[str, dict[str, int]]) -> str:
    parts = (f"{label} {v['musicbrainz']} (tagged {v['tagged']})" for label, v in wrong.items())
    return "MusicBrainz has " + ", ".join(parts)


@register
class Duration(Check):
    id = "mb.duration"
    scope = Scope.ALBUM
    description = (
        "Track's audio length differs from MusicBrainz by more than a tolerance (default 3 s): "
        "often a different version (live, remaster, edit) or the wrong release."
    )

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        tolerance_ms = 1000 * float(
            self.options.get("tolerance_seconds", DEFAULT_TOLERANCE_SECONDS)
        )
        for t, release in _tracks_with_release(ctx):
            track, _ = _locate(t.meta, release)
            length = t.meta.length_ms
            if track is None or not track.length_ms or not length:
                continue
            diff = length - track.length_ms
            if abs(diff) > tolerance_ms:
                yield self.issue(
                    t.rel_path,
                    f"Length {_duration(length)} vs {_duration(track.length_ms)} in MusicBrainz "
                    f"({'+' if diff > 0 else '-'}{abs(diff) / 1000:.0f} s)",
                    length_ms=length,
                    musicbrainz_length_ms=track.length_ms,
                )


# --- Text: titles and artist credits (6b-2) ---------------------------------------------


def _vs(label: str, tagged: str, expected: str) -> str:
    case = " (letter case)" if tagged.casefold() == expected.casefold() else ""
    return f"{label} {tagged!r} vs {expected!r} in MusicBrainz{case}"


def _matched_track(meta: TrackMeta, release: MbRelease) -> MbTrack | None:
    """The release's track for this file, unless its release track MBID isn't on the release
    (mb.track_mismatch's job; comparing with whatever sits at its position would mislead)."""
    track, by_id = _locate(meta, release)
    if not by_id and _tag(meta, "musicbrainz_releasetrackid"):
        return None
    return track


@register
class AlbumTitle(Check):
    id = "mb.album"
    scope = Scope.ALBUM
    description = (
        "Album title or release group MBID differs from the release in MusicBrainz (English "
        "translations, as Picard makes them, are accepted)."
    )

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        problems: dict[tuple[str, str], str] = {}
        for t, release in _tracks_with_release(ctx):
            album = t.meta.album
            if album and album not in (release.title, release.title_alias):
                expected = release.title_alias or release.title
                problems.setdefault(("album", album), _vs("Album", album, expected))
            group = _tag(t.meta, "musicbrainz_releasegroupid").lower()
            if group and release.release_group_id and group != release.release_group_id:
                problems.setdefault(
                    ("group", group),
                    f"Release group MBID {group} vs {release.release_group_id} "
                    f"({release.release_group_title!r}) in MusicBrainz",
                )
        if problems:
            yield self.issue(ctx.rel_path, "; ".join(problems.values()))


@register
class TrackTitle(Check):
    id = "mb.track_title"
    scope = Scope.ALBUM
    description = (
        "Track title differs from MusicBrainz, letter case included (English translations, as "
        "Picard makes them, are accepted)."
    )

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        for t, release in _tracks_with_release(ctx):
            track = _matched_track(t.meta, release)
            title = t.meta.title
            if track is None or not title or title in (track.title, track.title_alias):
                continue
            expected = track.title_alias or track.title
            yield self.issue(
                t.rel_path, _vs("Title", title, expected), title=title, musicbrainz=expected
            )


def _credit_problems(
    meta: TrackMeta,
    credit: tuple[MbCredit, ...],
    *,
    label: str,
    name_keys: tuple[str, ...],
    names_keys: tuple[str, ...],
    names_tag: str,
    id_key: str,
) -> list[str]:
    """How a track's artist tags (e.g. ARTIST, ARTISTS, MUSICBRAINZ_ARTISTID) differ from an
    artist credit. Missing tags are other checks' business (tags.naming, tags.mbids)."""
    if not credit:
        return []
    problems = []
    value = first_tag(meta.tags, name_keys)
    if value and not credit_matches(value, credit):
        problems.append(_vs(label, value, credit_text(credit)))
    names = _names(meta, names_keys)
    if names and (
        len(names) != len(credit)
        or any(n not in c.names for n, c in zip(names, credit, strict=True))
    ):
        expected = "; ".join(c.picard_name for c in credit)
        problems.append(_vs(names_tag, "; ".join(names), expected))
    ids = [i.lower() for i in _mbids(meta, id_key)]
    wanted = [c.artist_id for c in credit]
    if ids and ids != wanted:
        missing = [c.picard_name for c in credit if c.artist_id not in ids]
        extra = len([i for i in ids if i not in wanted])
        parts = [f"none for {', '.join(missing)}"] if missing else []
        if extra:
            parts.append(f"{extra} not in the MusicBrainz credit")
        problems.append(
            f"{label} MBIDs: {'; '.join(parts) or 'in a different order from MusicBrainz'}"
        )
    return problems


@register
class ArtistCredit(Check):
    id = "mb.artist_credit"
    scope = Scope.ALBUM
    description = (
        "Artist or album artist names or MBIDs differ from the artist credits in MusicBrainz "
        "(per track for artists, once per album for album artists)."
    )

    def run(self, ctx: AlbumContext) -> Iterable[Issue]:
        album: dict[str, None] = {}  # album-level problems, in order, once each
        for t, release in _tracks_with_release(ctx):
            for problem in _credit_problems(
                t.meta,
                release.artists,
                label="Album artist",
                name_keys=ALBUM_ARTIST_KEYS,
                names_keys=ALBUM_ARTISTS_KEYS,
                names_tag="ALBUMARTISTS",
                id_key="musicbrainz_albumartistid",
            ):
                album.setdefault(problem)
            track = _matched_track(t.meta, release)
            if track is None:
                continue
            problems = _credit_problems(
                t.meta,
                track.artists,
                label="Artist",
                name_keys=ARTIST_KEYS,
                names_keys=ARTISTS_KEYS,
                names_tag="ARTISTS",
                id_key="musicbrainz_artistid",
            )
            if problems:
                yield self.issue(t.rel_path, "; ".join(problems))
        if album:
            yield self.issue(ctx.rel_path, "; ".join(album))
