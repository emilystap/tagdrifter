"""Library browser (roadmap 7): look up any album, not just ones with issues.

Everything comes from the scan's file cache and the MusicBrainz release cache, so browsing
never touches the disk or the network. An album is a folder below an artist folder with audio
files in it, as the scanner sees it; loose files in the library root aren't albums.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from .core import TrackMeta, is_mbid
from .core.mbrelease import MbRelease, MbTrack
from .core.sanitize import nfc
from .db import Store

# Tags that should be the same on every track of a release (Picard's album-level tags). When
# they differ between an album's tracks, the release page highlights them.
ALBUM_TAGS = frozenset(
    {
        "album",
        "albumartist",
        "albumartists",
        "albumartistsort",
        "asin",
        "barcode",
        "catalognumber",
        "compilation",
        "date",
        "label",
        "media",
        "musicbrainz_albumartistid",
        "musicbrainz_albumid",
        "musicbrainz_releasegroupid",
        "originaldate",
        "originalyear",
        "releasecountry",
        "releasestatus",
        "releasetype",
        "script",
        "totaldiscs",
        "disctotal",
    }
)
# The tags people look for first, in this order; the rest follow alphabetically.
TAG_ORDER = (
    "title",
    "artist",
    "artists",
    "album",
    "albumartist",
    "albumartists",
    "date",
    "originaldate",
    "discnumber",
    "totaldiscs",
    "tracknumber",
    "totaltracks",
)
MUSICBRAINZ_URL = "https://musicbrainz.org"


def tag_sort_key(key: str) -> tuple[int, str]:
    return (TAG_ORDER.index(key), "") if key in TAG_ORDER else (len(TAG_ORDER), key)


# --- Album list ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlbumSummary:
    """One album folder in the library list."""

    folder: str
    artist: str  # the album artist tag, else the track artist, else the artist folder
    titles: tuple[str, ...]  # album titles in the tags (more than one = mixed tags)
    year: str
    release_ids: tuple[str, ...]  # lower-case release MBIDs in the tags
    tracks: int
    unreadable: int
    issues: int = 0

    @property
    def search_text(self) -> str:
        return nfc(" ".join((self.folder, self.artist, *self.titles, *self.release_ids))).casefold()


def album_summaries(store: Store) -> list[AlbumSummary]:
    """Every album folder, ordered by folder name (case-insensitive)."""
    by_folder: dict[str, list] = defaultdict(list)
    for row in store.library_file_rows():
        folder = row["path"].rpartition("/")[0]
        if folder:
            by_folder[folder].append(row)
    issues = store.open_issues_by_album()
    albums = []
    for folder, rows in by_folder.items():
        readable = [r for r in rows if not r["unreadable"]]

        def most_common(key: str, rows=readable) -> str:
            values = Counter(r[key] for r in rows if r[key])
            return values.most_common(1)[0][0] if values else ""

        titles = Counter(r["album"] for r in readable if r["album"])
        albums.append(
            AlbumSummary(
                folder=folder,
                artist=most_common("albumartist")
                or most_common("artist")
                or folder.split("/", 1)[0],
                titles=tuple(t for t, _ in titles.most_common()),
                year=most_common("date")[:4],
                release_ids=tuple(
                    sorted(
                        {
                            r["musicbrainz_albumid"].lower()
                            for r in readable
                            if r["musicbrainz_albumid"] and is_mbid(r["musicbrainz_albumid"])
                        }
                    )
                ),
                tracks=len(rows),
                unreadable=len(rows) - len(readable),
                issues=issues.get(folder, 0),
            )
        )
    return sorted(albums, key=lambda a: (a.folder.casefold(), a.folder))


def search_albums(albums: Sequence[AlbumSummary], query: str) -> list[AlbumSummary]:
    """Albums whose folder, artist, album title or release MBID contain every word of
    `query` (case-insensitive)."""
    words = nfc(query).casefold().split()
    return [a for a in albums if all(w in a.search_text for w in words)]


# --- Release page -------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackFile:
    path: str
    meta: TrackMeta | None  # None when the file couldn't be read
    error: str | None

    @property
    def name(self) -> str:
        return self.path.rpartition("/")[2]

    @property
    def release_id(self) -> str:
        value = self.meta.release_mbid.lower() if self.meta else ""
        return value if is_mbid(value) else ""

    def tag(self, key: str) -> str:
        return self.meta.tags.get(key, ("",))[0] if self.meta else ""


@dataclass(frozen=True)
class AlbumTag:
    """A tag shown once for the whole album, with each distinct value and how many tracks
    have it (an empty tuple = tracks without the tag)."""

    key: str
    values: tuple[tuple[tuple[str, ...], int], ...]

    @property
    def differs(self) -> bool:
        return len(self.values) > 1


@dataclass(frozen=True)
class TracklistRow:
    """A MusicBrainz track and the file that is it (either may be missing)."""

    track: MbTrack | None
    file: TrackFile | None

    @property
    def title_differs(self) -> bool:
        if not (self.track and self.file and self.file.meta and self.file.meta.title):
            return False
        return self.file.meta.title not in (self.track.title, self.track.title_alias)

    @property
    def length_diff_s(self) -> int:
        """File length minus MusicBrainz length, in whole seconds (0 if either is unknown)."""
        if not (self.track and self.track.length_ms and self.file and self.file.meta):
            return 0
        ms = self.file.meta.length_ms
        return round((ms - self.track.length_ms) / 1000) if ms else 0


@dataclass(frozen=True)
class ReleaseView:
    id: str
    cached: bool  # looked up at least once
    release: MbRelease | None  # None: not looked up yet, or MusicBrainz doesn't have it
    rows: tuple[TracklistRow, ...] = ()

    @property
    def url(self) -> str:
        return f"{MUSICBRAINZ_URL}/release/{self.id}"


@dataclass(frozen=True)
class AlbumDetail:
    folder: str
    tracks: tuple[TrackFile, ...]
    album_tags: tuple[AlbumTag, ...]
    track_tags: dict[str, tuple[tuple[str, tuple[str, ...]], ...]]  # by path: the rest
    releases: tuple[ReleaseView, ...]
    release_group_ids: tuple[str, ...]
    covers: tuple[tuple[str, int], ...]  # embedded picture descriptions, with track counts

    @property
    def artist_folder(self) -> str:
        return self.folder.split("/", 1)[0]

    @property
    def readable(self) -> tuple[TrackFile, ...]:
        return tuple(t for t in self.tracks if t.meta is not None)

    @property
    def length_ms(self) -> int:
        return sum(t.meta.length_ms or 0 for t in self.readable)


def album_detail(store: Store, folder: str) -> AlbumDetail | None:
    """One album folder's files, tags and cached MusicBrainz releases; None if the cache has
    no audio files directly in that folder."""
    folder = folder.strip("/")
    cached = store.files_in_folder(folder) if folder else {}
    if not cached:
        return None
    tracks = sorted(
        (
            TrackFile(
                path,
                TrackMeta.from_tags(PurePosixPath(path), entry.tags)
                if entry.tags is not None
                else None,
                entry.error,
            )
            for path, entry in cached.items()
        ),
        key=lambda t: (
            (t.meta.disc_number, t.meta.track_number) if t.meta else (1 << 30, 0),
            t.name.casefold(),
        ),
    )
    readable = [t for t in tracks if t.meta is not None]
    album_tags, track_tags = _split_tags(readable)

    release_ids = sorted({t.release_id for t in readable if t.release_id})
    data = store.mb_releases(release_ids)
    releases = []
    for rid in release_ids:
        release = MbRelease.from_json(data[rid]) if data.get(rid) else None
        releases.append(
            ReleaseView(
                rid,
                rid in data,
                release,
                _tracklist(release, [t for t in readable if t.release_id == rid])
                if release
                else (),
            )
        )
    group_ids = {
        v.lower() for t in readable for v in t.meta.tags.get("musicbrainz_releasegroupid", ())
    } | {r.release.release_group_id for r in releases if r.release}
    covers = Counter(
        ", ".join(p.describe() for p in t.meta.pictures or ()) or "none"
        for t in readable
        if t.meta.pictures is not None
    )
    return AlbumDetail(
        folder=folder,
        tracks=tuple(tracks),
        album_tags=album_tags,
        track_tags=track_tags,
        releases=tuple(releases),
        release_group_ids=tuple(sorted(g for g in group_ids if is_mbid(g))),
        covers=tuple(covers.most_common()),
    )


def _split_tags(
    tracks: Sequence[TrackFile],
) -> tuple[tuple[AlbumTag, ...], dict[str, tuple[tuple[str, tuple[str, ...]], ...]]]:
    """Album-level tags, and tags identical on every track, are shown once for the album;
    the rest per track. Pseudo-tags (audio length, pictures) are left out."""
    keys = {k for t in tracks for k in t.meta.tags if not k.startswith("~")}
    shared = []
    for key in sorted(keys, key=tag_sort_key):
        values = Counter(t.meta.tags.get(key, ()) for t in tracks)
        if key in ALBUM_TAGS or (len(values) == 1 and len(tracks) > 1):
            shared.append(AlbumTag(key, tuple(values.most_common())))
    shared_keys = {a.key for a in shared}
    per_track = {
        t.path: tuple(
            (k, t.meta.tags[k])
            for k in sorted(t.meta.tags, key=tag_sort_key)
            if k not in shared_keys and not k.startswith("~")
        )
        for t in tracks
    }
    return tuple(shared), per_track


def _tracklist(release: MbRelease, files: Sequence[TrackFile]) -> tuple[TracklistRow, ...]:
    """The release's tracks in order, each with its file (by release track MBID, else by
    disc and track number), then files that match no track."""
    by_id: dict[str, TrackFile] = {}
    by_position: dict[tuple[int, int], TrackFile] = {}
    for f in files:
        track_id = f.tag("musicbrainz_releasetrackid").lower()
        if track_id:
            by_id.setdefault(track_id, f)
        if f.meta and f.meta.has_tag("tracknumber"):
            by_position.setdefault((f.meta.disc_number, f.meta.track_number), f)
    rows, used = [], set()
    for track in release.tracks:
        f = by_id.get(track.id) or by_position.get((track.disc, track.position))
        if f is not None and f.path in used:
            f = None
        if f is not None:
            used.add(f.path)
        rows.append(TracklistRow(track, f))
    rows += [TracklistRow(None, f) for f in files if f.path not in used]
    return tuple(rows)
