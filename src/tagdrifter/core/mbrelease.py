"""A MusicBrainz release, parsed from a cached web service lookup (pure; no I/O).

Only the fields the checks compare are pulled out; `raw` keeps the whole response for checks
that need more (artist credits, aliases).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


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
    raw: Mapping[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> MbRelease:
        media = []
        for m in data.get("media", ()):
            disc = int(m.get("position") or 1)
            tracks = tuple(
                MbTrack(
                    id=t["id"].lower(),
                    disc=disc,
                    position=int(t.get("position") or 0),
                    number=str(t.get("number") or ""),
                    title=t.get("title") or t.get("recording", {}).get("title", ""),
                    length_ms=t.get("length") or t.get("recording", {}).get("length"),
                    recording_id=t.get("recording", {}).get("id", "").lower(),
                    recording_title=t.get("recording", {}).get("title", ""),
                )
                for t in m.get("tracks", ())
            )
            media.append(
                MbMedium(disc, m.get("format") or "", int(m.get("track-count") or 0), tracks)
            )
        return cls(data["id"].lower(), data.get("title", ""), tuple(media), data)

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
