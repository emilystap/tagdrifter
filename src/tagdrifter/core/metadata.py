"""Track metadata: the only module in core that touches audio files.

`TrackMeta.from_tags` is pure and takes a plain mapping, so naming and placement logic can be
tested without any audio files. `read_metadata` is the thin mutagen adapter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePath

from .sanitize import nfc

RE_LEADING_INT = re.compile(r"^\s*(\d+)")
RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Tag keys checked, in priority order, for each logical field.
ALBUM_ARTIST_KEYS = ("albumartist", "album artist")
ALBUM_ARTIST_SORT_KEYS = ("albumartistsort", "artistsort")
ALBUM_ARTISTS_KEYS = ("albumartists", "album_artists")
ARTIST_KEYS = ("artist",)
ARTIST_SORT_KEYS = ("artistsort",)
ARTISTS_KEYS = ("artists",)
ALBUM_KEYS = ("album",)
TITLE_KEYS = ("title",)
DISAMBIGUATION_KEYS = (
    "albumversion",
    "comment:musicbrainz album comment",
    "musicbrainz_albumcomment",
    "releasecomment",
    "comment:disambiguation",
    "albumdisambiguation",
    "discogs_release_notes",
    "edition",
)
RELEASE_MBID_KEYS = ("musicbrainz_albumid", "musicbrainz album id")
RECORDING_MBID_KEYS = ("musicbrainz_trackid", "musicbrainz track id")
DATE_KEYS = ("date", "year", "originaldate", "originalyear")
CATALOG_KEYS = ("catalognumber", "catalog number")
COUNTRY_KEYS = ("releasecountry", "musicbrainz album release country")
TRACK_NUMBER_KEYS = ("tracknumber",)
DISC_NUMBER_KEYS = ("discnumber",)
TOTAL_DISCS_KEYS = ("disctotal", "totaldiscs")

# The audio length in ms, stored alongside the tags (0 = unknown). Not a possible Vorbis
# comment name ("~" is outside the allowed range), so it can't clash with a real tag.
LENGTH_KEY = "~length"
# Embedded pictures, one value each: "<type> <mime> <width>x<height>" (type 3 = front cover);
# present and empty when the file has none.
PICTURES_KEY = "~pictures"
# Pseudo-tags the current reader records: a cached file without one of them is read again.
DERIVED_KEYS = (LENGTH_KEY, PICTURES_KEY)
FRONT_COVER = 3

Tags = Mapping[str, tuple[str, ...]]


def normalize_tags(
    raw: Mapping[str, object] | Iterable[tuple[str, object]],
) -> dict[str, tuple[str, ...]]:
    """Lower-case keys and coerce every value to a tuple of NFC strings.

    Accepts a dict (str -> str | list[str]) or mutagen-style (key, value) pairs, which may
    repeat a key for multi-value Vorbis comments.
    """
    items = raw.items() if isinstance(raw, Mapping) else raw
    out: dict[str, list[str]] = {}
    for key, value in items:
        values = value if isinstance(value, list | tuple) else [value]
        bucket = out.setdefault(str(key).lower(), [])
        bucket.extend(nfc(str(v)) for v in values if v is not None)
    return {k: tuple(v) for k, v in out.items()}


def first_tag(tags: Tags, keys: Iterable[str], default: str = "") -> str:
    """First non-empty value among candidate keys."""
    for key in keys:
        for value in tags.get(key, ()):
            if value.strip():
                return value.strip()
    return default


def tag_list(tags: Tags, keys: Iterable[str]) -> tuple[str, ...]:
    """Multi-value or semicolon-delimited list tag (e.g. 'artists'), from the first key present."""
    for key in keys:
        result = [part.strip() for entry in tags.get(key, ()) for part in entry.split(";")]
        result = [p for p in result if p]
        if result:
            return tuple(result)
    return ()


def is_mbid(value: str) -> bool:
    """Whether a value is a well-formed MusicBrainz ID (a UUID)."""
    return bool(RE_UUID.match(value.strip()))


def parse_index(value: str, default: int = 1) -> int:
    """Leading integer of values like '3' or '3/12'; `default` when absent or unparsable."""
    match = RE_LEADING_INT.match(value or "")
    return int(match.group(1)) if match else default


@dataclass(frozen=True, slots=True)
class TrackMeta:
    """Tag-derived facts about one track. Raw values are kept un-defaulted; the properties
    apply the same fallbacks the v1 scanner used."""

    path: PurePath
    albumartist: str = ""
    albumartist_sort: str = ""
    albumartists: tuple[str, ...] = ()
    artist: str = ""
    artist_sort: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    title: str = ""
    disambiguation: str = ""
    release_mbid: str = ""
    date: str = ""
    catalog_number: str = ""
    release_country: str = ""
    track_number: int = 1
    disc_number: int = 1
    total_discs: int = 1
    tags: Tags = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_tags(cls, path: PurePath, raw_tags: Mapping[str, object]) -> TrackMeta:
        tags = normalize_tags(raw_tags)
        return cls(
            path=path,
            albumartist=first_tag(tags, ALBUM_ARTIST_KEYS),
            albumartist_sort=first_tag(tags, ALBUM_ARTIST_SORT_KEYS),
            albumartists=tag_list(tags, ALBUM_ARTISTS_KEYS),
            artist=first_tag(tags, ARTIST_KEYS),
            artist_sort=first_tag(tags, ARTIST_SORT_KEYS),
            artists=tag_list(tags, ARTISTS_KEYS),
            album=first_tag(tags, ALBUM_KEYS),
            title=first_tag(tags, TITLE_KEYS),
            disambiguation=first_tag(tags, DISAMBIGUATION_KEYS),
            release_mbid=first_tag(tags, RELEASE_MBID_KEYS),
            date=first_tag(tags, DATE_KEYS),
            catalog_number=first_tag(tags, CATALOG_KEYS),
            release_country=first_tag(tags, COUNTRY_KEYS),
            track_number=parse_index(first_tag(tags, TRACK_NUMBER_KEYS)),
            disc_number=parse_index(first_tag(tags, DISC_NUMBER_KEYS)),
            total_discs=parse_index(first_tag(tags, TOTAL_DISCS_KEYS)),
            tags=tags,
        )

    # --- Fallback-resolved views (mirror v1 build_track_target) -------------------------

    @property
    def effective_album_artist(self) -> str:
        return self.albumartist or self.artist or "Unknown Artist"

    @property
    def effective_album_artist_sort(self) -> str:
        return self.albumartist_sort or self.albumartist

    @property
    def effective_track_artist(self) -> str:
        return self.artist or self.albumartist or "Unknown Artist"

    @property
    def effective_track_artist_sort(self) -> str:
        return self.artist_sort or self.effective_track_artist

    @property
    def effective_album(self) -> str:
        return self.album or "Unknown Album"

    @property
    def effective_title(self) -> str:
        return self.title or "Unknown Track"

    @property
    def release_year(self) -> str:
        return self.date[:4] if len(self.date) >= 4 and self.date[:4].isdigit() else ""

    @property
    def is_multi_disc(self) -> bool:
        return self.total_discs > 1 or self.disc_number > 1

    def has_tag(self, key: str) -> bool:
        return any(v.strip() for v in self.tags.get(key.lower(), ()))

    @property
    def length_ms(self) -> int | None:
        """The audio length, when the file was read by a version that records it."""
        value = parse_index(first_tag(self.tags, (LENGTH_KEY,)), 0)
        return value or None

    @property
    def pictures(self) -> tuple[EmbeddedPicture, ...] | None:
        """Embedded pictures, or None when the file was read by a version that didn't record
        them (so a check can't tell)."""
        if PICTURES_KEY not in self.tags:
            return None
        return tuple(p for v in self.tags[PICTURES_KEY] if (p := EmbeddedPicture.parse(v)))

    @property
    def is_mbid_tagged(self) -> bool:
        """Has valid release and recording MBIDs, i.e. was most likely tagged by Picard.

        Until MusicBrainz itself is consulted, such tracks' tags are trusted as the source of
        truth (including letter case) over their paths.
        """
        return is_mbid(self.release_mbid) and is_mbid(first_tag(self.tags, RECORDING_MBID_KEYS))


@dataclass(frozen=True, slots=True)
class EmbeddedPicture:
    type: int  # ID3v2 APIC picture type; 3 = front cover
    mime: str
    width: int  # 0 = not recorded by the tagger
    height: int

    def describe(self) -> str:
        return f"{self.type} {self.mime} {self.width}x{self.height}"

    @classmethod
    def parse(cls, value: str) -> EmbeddedPicture | None:
        match = re.fullmatch(r"(\d+) (\S*) (\d+)x(\d+)", value)
        if not match:
            return None
        kind, mime, width, height = match.groups()
        return cls(int(kind), mime, int(width), int(height))

    @property
    def is_front_cover(self) -> bool:
        return self.type == FRONT_COVER


class MetadataReadError(Exception):
    """Raised when a file's tags cannot be read."""


def read_tags(path: PurePath) -> dict[str, tuple[str, ...]]:
    """Read a file's tags as {lower-case key: values}. Raises MetadataReadError on failure.

    FLAC uses the built-in reader, which skips embedded artwork. Other formats fall back to
    mutagen.
    """
    if path.suffix.lower() == ".flac":
        from .flac import FlacError, read_flac

        try:
            info = read_flac(path)
        except (FlacError, OSError) as exc:
            raise MetadataReadError(f"{path}: {exc}") from exc
        pictures = [
            EmbeddedPicture(p.type, p.mime, p.width, p.height).describe() for p in info.pictures
        ]
        return normalize_tags(
            [*info.comments, (LENGTH_KEY, str(info.length_ms or 0)), (PICTURES_KEY, pictures)]
        )

    from mutagen import File as MutagenFile  # imported lazily; only needed for non-FLAC
    from mutagen import MutagenError

    try:
        audio = MutagenFile(path)
    except (MutagenError, OSError) as exc:
        raise MetadataReadError(f"{path}: {exc}") from exc
    if audio is None:
        raise MetadataReadError(f"{path}: unrecognized audio format")
    # Vorbis comments (Ogg/Opus) are (key, value) pairs; ID3/MP4 will need a key-mapping
    # adapter here when those formats are supported.
    tags = audio.tags or []
    pairs = tags if isinstance(tags, list) else list(tags.items())
    length = getattr(getattr(audio, "info", None), "length", 0) or 0
    derived = [(LENGTH_KEY, str(round(length * 1000)))]
    if hasattr(audio, "pictures"):  # FLAC-style pictures (e.g. Ogg FLAC); else unknown
        pics = [EmbeddedPicture(p.type, p.mime, p.width, p.height) for p in audio.pictures]
        derived.append((PICTURES_KEY, [p.describe() for p in pics]))
    return normalize_tags([*pairs, *derived])


def read_metadata(path: PurePath) -> TrackMeta:
    """Read a file's tags into a TrackMeta. Raises MetadataReadError on failure."""
    return TrackMeta.from_tags(path, read_tags(path))
