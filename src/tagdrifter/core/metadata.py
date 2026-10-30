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
DATE_KEYS = ("date", "year", "originaldate", "originalyear")
CATALOG_KEYS = ("catalognumber", "catalog number")
COUNTRY_KEYS = ("releasecountry", "musicbrainz album release country")
TRACK_NUMBER_KEYS = ("tracknumber",)
DISC_NUMBER_KEYS = ("discnumber",)
TOTAL_DISCS_KEYS = ("disctotal", "totaldiscs")

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


class MetadataReadError(Exception):
    """Raised when a file's tags cannot be read."""


def read_metadata(path: PurePath) -> TrackMeta:
    """Read tags from an audio file via mutagen. Raises MetadataReadError on failure."""
    from mutagen import File as MutagenFile  # local import keeps pure code import-light
    from mutagen import MutagenError

    try:
        audio = MutagenFile(path)
    except (MutagenError, OSError) as exc:
        raise MetadataReadError(f"{path}: {exc}") from exc
    if audio is None:
        raise MetadataReadError(f"{path}: unrecognized audio format")
    # Vorbis comments (FLAC/Ogg) are a list of (key, value) pairs; other formats will need
    # a key-mapping adapter here when support is added.
    tags = audio.tags or []
    pairs = tags if isinstance(tags, list) else list(tags.items())
    return TrackMeta.from_tags(path, dict(normalize_tags(pairs)))
