"""A MusicBrainz release, parsed from a cached web service lookup (pure; no I/O).

Only the fields the checks compare are pulled out; `raw` keeps the whole response for checks
that need more.

Translations follow the owner's Picard settings (artist names, album and track titles
translated to the `en` locale): Picard takes the *primary* alias whose locale matches, the
full locale first (`en_GB`), then the language (`en`), and never follows pseudo-releases.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .sanitize import nfc

TRANSLATION_LOCALES = ("en",)  # Picard's translation_locales (owner's setting)


def translated(aliases: Any, preferred_type: str = "") -> str:
    """The name Picard translates to from `aliases`, or "" when none fits: a primary alias
    for the first locale that has one (full locale match first, then the language), the
    alias type `preferred_type` (e.g. "Artist name") winning a tie."""
    primary = [a for a in aliases or () if a.get("primary") and a.get("locale") and a.get("name")]
    primary.sort(key=lambda a: a.get("type") != preferred_type)
    for exact in (True, False):
        for locale in TRANSLATION_LOCALES:
            want = locale if exact else locale.split("_")[0]
            for alias in primary:
                have = alias["locale"] if exact else alias["locale"].split("_")[0]
                if have == want:
                    return nfc(alias["name"])
    return ""


@dataclass(frozen=True)
class MbCredit:
    """One artist in an artist credit."""

    artist_id: str
    name: str  # the artist's own name
    credited: str  # the name on this credit (may be a variation)
    joinphrase: str  # text after this artist, e.g. " & " or " feat. "
    alias: str = ""  # Picard's translation (primary en alias), if any

    @property
    def names(self) -> tuple[str, ...]:
        """Names Picard may write for this artist: the credited name, the artist's own name
        (standardized variations) or the translation."""
        return tuple(dict.fromkeys(n for n in (self.credited, self.name, self.alias) if n))

    @property
    def picard_name(self) -> str:
        return self.alias or self.credited

    @classmethod
    def parse(cls, credit: Any) -> tuple[MbCredit, ...]:
        out = []
        for c in credit or ():
            artist = c.get("artist") or {}
            name = nfc(artist.get("name") or "")
            out.append(
                cls(
                    artist_id=(artist.get("id") or "").lower(),
                    name=name,
                    credited=nfc(c.get("name") or name),
                    joinphrase=nfc(c.get("joinphrase") or ""),
                    alias=translated(artist.get("aliases"), "Artist name"),
                )
            )
        return tuple(out)


def credit_text(credit: tuple[MbCredit, ...]) -> str:
    """The credit as Picard writes it (ARTIST / ALBUMARTIST)."""
    return "".join(c.picard_name + c.joinphrase for c in credit)


def credit_matches(value: str, credit: tuple[MbCredit, ...]) -> bool:
    """Whether `value` spells this credit, each artist by one of its names."""

    def rest(i: int, pos: int) -> bool:
        if i == len(credit):
            return pos == len(value)
        c = credit[i]
        return any(
            value.startswith(seg, pos) and rest(i + 1, pos + len(seg))
            for seg in (n + c.joinphrase for n in c.names)
        )

    return bool(credit) and rest(0, 0)


@dataclass(frozen=True)
class MbTrack:
    id: str  # release track MBID (Picard: musicbrainz_releasetrackid)
    disc: int  # medium position (Picard: discnumber)
    position: int  # position on the medium (Picard: tracknumber); `number` may be "A1"
    number: str
    title: str
    length_ms: int | None
    recording_id: str  # Picard: musicbrainz_trackid
    recording_title: str
    title_alias: str = ""  # the recording's translated title (Picard: translate_track_titles)
    artists: tuple[MbCredit, ...] = ()  # the track's artist credit


@dataclass(frozen=True)
class MbMedium:
    position: int
    format: str
    track_count: int
    tracks: tuple[MbTrack, ...]


@dataclass(frozen=True)
class MbRelease:
    id: str
    title: str
    media: tuple[MbMedium, ...]
    title_alias: str = ""  # the release's translated title (Picard: translate_album_titles)
    artists: tuple[MbCredit, ...] = ()  # the release's artist credit (album artists)
    release_group_id: str = ""
    release_group_title: str = ""
    raw: Mapping[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> MbRelease:
        media = []
        for m in data.get("media", ()):
            disc = int(m.get("position") or 1)
            tracks = []
            for t in m.get("tracks", ()):
                recording = t.get("recording") or {}
                tracks.append(
                    MbTrack(
                        id=t["id"].lower(),
                        disc=disc,
                        position=int(t.get("position") or 0),
                        number=str(t.get("number") or ""),
                        title=nfc(t.get("title") or recording.get("title", "")),
                        length_ms=t.get("length") or recording.get("length"),
                        recording_id=recording.get("id", "").lower(),
                        recording_title=nfc(recording.get("title", "")),
                        title_alias=translated(recording.get("aliases"), "Recording name"),
                        artists=MbCredit.parse(
                            t.get("artist-credit") or recording.get("artist-credit")
                        ),
                    )
                )
            media.append(
                MbMedium(disc, m.get("format") or "", int(m.get("track-count") or 0), tuple(tracks))
            )
        group = data.get("release-group") or {}
        return cls(
            id=data["id"].lower(),
            title=nfc(data.get("title", "")),
            media=tuple(media),
            title_alias=translated(data.get("aliases"), "Release name"),
            artists=MbCredit.parse(data.get("artist-credit")),
            release_group_id=(group.get("id") or "").lower(),
            release_group_title=nfc(group.get("title") or ""),
            raw=data,
        )

    @property
    def tracks(self) -> tuple[MbTrack, ...]:
        return tuple(t for m in self.media for t in m.tracks)

    def track_by_id(self, track_id: str) -> MbTrack | None:
        track_id = track_id.lower()
        return next((t for t in self.tracks if t.id == track_id), None)

    def track_at(self, disc: int, position: int) -> MbTrack | None:
        return next((t for t in self.tracks if (t.disc, t.position) == (disc, position)), None)

    def medium(self, disc: int) -> MbMedium | None:
        return next((m for m in self.media if m.position == disc), None)
