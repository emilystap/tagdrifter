"""Library browser (roadmap 7): look up any album, not just ones with issues.

Everything comes from the scan's file cache and the MusicBrainz release cache, so browsing
never touches the disk or the network. An album is a folder below an artist folder with audio
files in it, as the scanner sees it; loose files in the library root aren't albums.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from .core import TrackMeta, is_mbid
from .core.mbrelease import MbRelease, MbTrack
from .core.sanitize import nfc
from .db import Store
from .musicbrainz import normalize_isrc

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
TIMES = chr(0xD7)  # multiplication sign, for "2xCD" as MusicBrainz writes it


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
    fetched_at: str = ""  # when it was last looked up

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
    fetched_at = store.mb_release_fetched_at(release_ids)
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
                fetched_at.get(rid, ""),
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


# --- Other releases in the release group (6d) ------------------------------------------


@dataclass(frozen=True)
class ReleaseFit:
    """How well one release's tracklist fits an album's files, by track length."""

    release: MbRelease
    fits: int  # files matched to a track whose length is within the tolerance
    off: int  # files matched to a track whose length differs by more
    unknown: int  # files matched to a track MusicBrainz has no length for
    unmatched: int  # files with no track on this release
    no_file: int  # tracks on this release with no file
    worst_ms: int  # largest difference among matched files (0 if none)
    mean_ms: float  # mean absolute difference among matched files
    tagged: bool  # the files are tagged with this release
    fitting: frozenset[str] = frozenset()  # paths of the files that fit

    @property
    def label(self) -> str:
        """Title, then what tells it apart from its siblings: "Title (1998 · US · CD)"."""
        details = [self.raw("date"), self.raw("country"), self.formats]
        return f"{self.release.title} ({' · '.join(d for d in details if d)})"

    @property
    def url(self) -> str:
        return f"{MUSICBRAINZ_URL}/release/{self.release.id}"

    @property
    def track_count(self) -> int:
        return len(self.release.tracks)

    def raw(self, key: str) -> str:
        return str(self.release.raw.get(key) or "")

    @property
    def formats(self) -> str:
        """Media formats, e.g. "CD", "2xCD" (with a multiplication sign) or "CD + DVD"."""
        counts = Counter(m.format or "(unknown format)" for m in self.release.media)
        return " + ".join(f"{n}{TIMES}{f}" if n > 1 else f for f, n in counts.items())

    @property
    def sort_key(self) -> tuple:
        return (-self.fits, self.off, self.unmatched, self.no_file, self.mean_ms)


@dataclass(frozen=True)
class GroupComparison:
    release_group_id: str
    fetched_at: str
    release_count: int  # releases in the group (more than `fits` when the browse was cut off)
    fits: tuple[ReleaseFit, ...]  # best fit first
    tolerance_ms: int
    files: int  # readable files compared
    tracks: TrackSources | None = None  # track by track, when no release fits every file

    @property
    def url(self) -> str:
        return f"{MUSICBRAINZ_URL}/release-group/{self.release_group_id}"

    @property
    def tolerance_s(self) -> str:
        return f"{self.tolerance_ms / 1000:g}"


def compare_release_group(
    store: Store, album: AlbumDetail, release_group_id: str, tolerance_ms: int
) -> GroupComparison | None:
    """Rank the cached releases of a release group by how well their track lengths fit the
    album's files; None if the group hasn't been looked up."""
    cached = store.mb_release_group(release_group_id)
    if cached is None:
        return None
    data, fetched_at = cached
    tagged = {r.id for r in album.releases}
    files = album.readable
    fits = sorted(
        (
            fit_release(MbRelease.from_json(r), files, tolerance_ms, r["id"].lower() in tagged)
            for r in data.get("releases", ())
        ),
        key=lambda f: f.sort_key,
    )
    fits_all = bool(fits) and fits[0].fits == len(files)
    return GroupComparison(
        release_group_id=release_group_id,
        fetched_at=fetched_at,
        release_count=int(data.get("release-count") or len(fits)),
        fits=tuple(fits),
        tolerance_ms=tolerance_ms,
        files=len(files),
        tracks=None if fits_all or not fits else track_sources(store, files, fits, tolerance_ms),
    )


def fit_release(
    release: MbRelease, files: Sequence[TrackFile], tolerance_ms: int, tagged: bool = False
) -> ReleaseFit:
    """Match each file to one of the release's tracks (by release track MBID, else by
    recording MBID when it's on the release once, else by disc and track number) and compare
    lengths."""
    recordings = Counter(t.recording_id for t in release.tracks)
    by_recording = {t.recording_id: t for t in release.tracks if recordings[t.recording_id] == 1}
    used: set[str] = set()
    diffs: list[int] = []
    fitting: set[str] = set()
    unknown = unmatched = 0
    for f in files:
        candidates = (
            release.track_by_id(f.tag("musicbrainz_releasetrackid")),
            by_recording.get(f.tag("musicbrainz_trackid").lower()),
            release.track_at(f.meta.disc_number, f.meta.track_number)
            if f.meta.has_tag("tracknumber")
            else None,
        )
        track = next((t for t in candidates if t is not None and t.id not in used), None)
        if track is None:
            unmatched += 1
            continue
        used.add(track.id)
        if not track.length_ms or not f.meta.length_ms:
            unknown += 1
        else:
            diffs.append(abs(f.meta.length_ms - track.length_ms))
            if diffs[-1] <= tolerance_ms:
                fitting.add(f.path)
    return ReleaseFit(
        release=release,
        fits=sum(d <= tolerance_ms for d in diffs),
        off=sum(d > tolerance_ms for d in diffs),
        unknown=unknown,
        unmatched=unmatched,
        no_file=len(release.tracks) - len(used),
        worst_ms=max(diffs, default=0),
        mean_ms=sum(diffs) / len(diffs) if diffs else 0.0,
        tagged=tagged,
        fitting=frozenset(fitting),
    )


# --- Track by track (6d follow-up) -----------------------------------------------------
# When no release in the release group fits every file, the folder may have been put together
# from several releases. First, from the cached release group alone: which releases fit the
# files between them. Then, for files that fit none of them and only when someone asks
# (*Look up these tracks*): the other releases each file's recording is on (compilations,
# singles), and the recordings that share its ISRC.


@dataclass(frozen=True)
class Appearance:
    """A file's recording on a release outside the release group."""

    release_id: str
    title: str
    date: str
    country: str
    format: str
    number: str  # the track number as printed ("3", "A1")
    diff_ms: int | None  # file length minus the track's; None if MusicBrainz has no length
    fits: bool

    @property
    def url(self) -> str:
        return f"{MUSICBRAINZ_URL}/release/{self.release_id}"

    @property
    def details(self) -> str:
        return " · ".join(d for d in (self.date, self.country, self.format) if d)


@dataclass(frozen=True)
class IsrcRecording:
    """A recording with the same ISRC as a file."""

    id: str
    title: str
    disambiguation: str
    diff_ms: int | None  # file length minus the recording's
    fits: bool  # within the tolerance (a recording's length is typical, not exact)
    tagged: bool  # the file's own recording

    @property
    def url(self) -> str:
        return f"{MUSICBRAINZ_URL}/recording/{self.id}"


@dataclass(frozen=True)
class TrackSource:
    file: TrackFile
    release: ReleaseFit | None  # the release of the group this file is put down to, if any
    group_fits: int  # releases in the group the file fits
    cover_key: str  # the letter of that release in TrackSources.cover ("" if none)
    recording_id: str
    isrc: str
    looked_up: str = ""  # when its recording was looked up ("" = not yet)
    recording_found: bool = True
    elsewhere: tuple[Appearance, ...] = ()  # best first: those that fit, then the closest
    isrc_looked_up: bool = False
    isrc_recordings: tuple[IsrcRecording, ...] = ()

    @property
    def explained(self) -> bool:
        return self.release is not None

    @property
    def fits_elsewhere(self) -> tuple[Appearance, ...]:
        return tuple(a for a in self.elsewhere if a.fits)

    @property
    def other_isrc_recordings(self) -> tuple[IsrcRecording, ...]:
        return tuple(r for r in self.isrc_recordings if not r.tagged)

    @property
    def lookup_keys(self) -> tuple[tuple[str, str], ...]:
        keys = [("recording", self.recording_id)] if self.recording_id else []
        return tuple(keys + ([("isrc", self.isrc)] if self.isrc else []))

    @property
    def pending(self) -> bool:
        """Unexplained, and lookups that haven't happened yet could help."""
        return not self.explained and bool(
            (self.recording_id and not self.looked_up) or (self.isrc and not self.isrc_looked_up)
        )


@dataclass(frozen=True)
class TrackSources:
    cover: tuple[tuple[ReleaseFit, int], ...]  # releases that fit the files between them
    tracks: tuple[TrackSource, ...]  # in track order

    @property
    def keyed_cover(self) -> tuple[tuple[str, ReleaseFit, int], ...]:
        return tuple((cover_key(i), fit, n) for i, (fit, n) in enumerate(self.cover))

    @property
    def unexplained(self) -> tuple[TrackSource, ...]:
        return tuple(t for t in self.tracks if not t.explained)

    @property
    def pending(self) -> tuple[TrackSource, ...]:
        return tuple(t for t in self.tracks if t.pending)

    @property
    def lookup_keys(self) -> tuple[tuple[str, str], ...]:
        """What *Look up these tracks* asks MusicBrainz, in track order."""
        return tuple(k for t in self.unexplained for k in t.lookup_keys)


def track_sources(
    store: Store, files: Sequence[TrackFile], fits: Sequence[ReleaseFit], tolerance_ms: int
) -> TrackSources:
    """Put each file down to a release of the group it fits, using as few releases as
    possible (the one that fits most files, then the one that fits most of the rest, ...);
    for the files left over, what the cached per-track lookups say."""
    left = {f.path for f in files}
    cover: list[tuple[ReleaseFit, int]] = []
    release_of: dict[str, ReleaseFit] = {}
    while left:
        best = max(fits, key=lambda fit: len(fit.fitting & left))  # ties: the better fit
        covered = best.fitting & left
        if not covered:
            break
        cover.append((best, len(covered)))
        release_of.update(dict.fromkeys(covered, best))
        left -= covered
    keys = {id(fit): cover_key(i) for i, (fit, _) in enumerate(cover)}
    group_ids = {fit.release.id for fit in fits}
    tracks = []
    for f in files:
        recording_id = f.tag("musicbrainz_trackid").lower()
        tracks.append(
            TrackSource(
                file=f,
                release=release_of.get(f.path),
                cover_key=keys.get(id(release_of.get(f.path)), ""),
                group_fits=sum(f.path in fit.fitting for fit in fits),
                recording_id=recording_id if is_mbid(recording_id) else "",
                isrc=normalize_isrc(f.tag("isrc")),
            )
        )
    cached = store.mb_lookups(k for t in tracks if not t.explained for k in t.lookup_keys)
    for i, t in enumerate(tracks):
        if t.explained:
            continue
        recording = cached.get(("recording", t.recording_id))
        isrc = cached.get(("isrc", t.isrc))
        tracks[i] = replace(
            t,
            looked_up=recording[1] if recording else "",
            recording_found=recording is None or recording[0] is not None,
            elsewhere=_appearances(recording and recording[0], t.file, group_ids, tolerance_ms),
            isrc_looked_up=isrc is not None,
            isrc_recordings=_isrc_recordings(
                isrc and isrc[0], t.file, t.recording_id, tolerance_ms
            ),
        )
    return TrackSources(cover=tuple(cover), tracks=tuple(tracks))


def cover_key(index: int) -> str:
    """A, B, ... Z, then 27, 28, ..."""
    return chr(ord("A") + index) if index < 26 else str(index + 1)


def _diff(f: TrackFile, length_ms: object) -> int | None:
    if not isinstance(length_ms, int) or not length_ms or not f.meta.length_ms:
        return None
    return f.meta.length_ms - length_ms


def _appearances(
    data: dict | None, f: TrackFile, group_ids: set[str], tolerance_ms: int
) -> tuple[Appearance, ...]:
    """The releases outside the group that a recording lookup lists, closest length first."""
    found = []
    for release in (data or {}).get("releases") or ():
        release_id = str(release.get("id") or "").lower()
        if not release_id or release_id in group_ids:
            continue
        for medium in release.get("media") or ():
            for track in medium.get("tracks") or ():
                diff = _diff(f, track.get("length"))
                found.append(
                    Appearance(
                        release_id=release_id,
                        title=str(release.get("title") or ""),
                        date=str(release.get("date") or ""),
                        country=str(release.get("country") or ""),
                        format=str(medium.get("format") or ""),
                        number=str(track.get("number") or ""),
                        diff_ms=diff,
                        fits=diff is not None and abs(diff) <= tolerance_ms,
                    )
                )
    return tuple(
        sorted(found, key=lambda a: (a.diff_ms is None, abs(a.diff_ms or 0), a.date, a.title))
    )


def _isrc_recordings(
    data: dict | None, f: TrackFile, recording_id: str, tolerance_ms: int
) -> tuple[IsrcRecording, ...]:
    """The recordings an ISRC lookup lists."""
    found = []
    for r in (data or {}).get("recordings") or ():
        rid = str(r.get("id") or "").lower()
        if rid:
            diff = _diff(f, r.get("length"))
            found.append(
                IsrcRecording(
                    id=rid,
                    title=str(r.get("title") or ""),
                    disambiguation=str(r.get("disambiguation") or ""),
                    diff_ms=diff,
                    fits=diff is not None and abs(diff) <= tolerance_ms,
                    tagged=rid == recording_id,
                )
            )
    return tuple(found)


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
