"""ID3 frames (MP3) as the Vorbis-style tag names the rest of Tag Drifter reads.

Follows Picard's ID3 mapping, so an MP3 tagged by Picard reads back with the same keys as a
FLAC tagged by it: `TPE1` is `artist`, `TXXX:MusicBrainz Album Id` is `musicbrainz_albumid`,
the recording MBID is the `UFID` frame owned by http://musicbrainz.org, and `TRCK` "3/12" is
`tracknumber` 3 plus `totaltracks` 12. Pure: works on an already loaded mutagen `ID3`.
"""

from __future__ import annotations

import struct
from typing import Any

# Text and URL frames with a fixed name.
FRAMES = {
    "TIT1": "grouping",
    "TIT2": "title",
    "TIT3": "subtitle",
    "TALB": "album",
    "TSST": "discsubtitle",
    "TSRC": "isrc",
    "TPE1": "artist",
    "TPE2": "albumartist",
    "TPE3": "conductor",
    "TPE4": "remixer",
    "TEXT": "lyricist",
    "TCOM": "composer",
    "TENC": "encodedby",
    "TBPM": "bpm",
    "TKEY": "key",
    "TLAN": "language",
    "TCON": "genre",
    "TMED": "media",
    "TMOO": "mood",
    "TCOP": "copyright",
    "TPUB": "label",
    "TDOR": "originaldate",
    "TDRC": "date",
    "TSSE": "encodersettings",
    "TSOA": "albumsort",
    "TSOP": "artistsort",
    "TSOT": "titlesort",
    "TSO2": "albumartistsort",
    "TSOC": "composersort",
    "TOAL": "originalalbum",
    "TOPE": "originalartist",
    "TOFN": "originalfilename",
    "TCMP": "compilation",
    "MVNM": "movement",
    "GRP1": "grouping",
    "WCOP": "license",
    "WOAR": "website",
}
# "n/N" frames: (number key, total key).
NUMBERED_FRAMES = {
    "TRCK": ("tracknumber", "totaltracks"),
    "TPOS": ("discnumber", "totaldiscs"),
    "MVIN": ("movementnumber", "movementtotal"),
}
# TXXX descriptions Picard writes under another name (compared case-insensitively). Any
# other TXXX keeps its description as the name: TXXX:ARTISTS is `artists`.
FREETEXT = {
    "musicbrainz artist id": "musicbrainz_artistid",
    "musicbrainz album id": "musicbrainz_albumid",
    "musicbrainz album artist id": "musicbrainz_albumartistid",
    "musicbrainz album type": "releasetype",
    "musicbrainz album status": "releasestatus",
    "musicbrainz album release country": "releasecountry",
    "musicbrainz album comment": "musicbrainz_albumcomment",
    "musicbrainz release track id": "musicbrainz_releasetrackid",
    "musicbrainz release group id": "musicbrainz_releasegroupid",
    "musicbrainz work id": "musicbrainz_workid",
    "musicbrainz disc id": "musicbrainz_discid",
    "musicbrainz original album id": "musicbrainz_originalalbumid",
    "musicbrainz original artist id": "musicbrainz_originalartistid",
    "musicbrainz trm id": "musicbrainz_trmid",
    "musicip puid": "musicip_puid",
    "musicmagic fingerprint": "musicip_fingerprint",
    "acoustid id": "acoustid_id",
    "acoustid fingerprint": "acoustid_fingerprint",
}
# TIPL roles Picard names differently; other roles keep their name.
INVOLVED_ROLES = {"DJ-mix": "djmixer", "mix": "mixer"}
MUSICBRAINZ_UFID_OWNER = "http://musicbrainz.org"

Pair = tuple[str, list[str]]
Picture = tuple[int, str, int, int]  # (APIC type, MIME type, width, height)


def id3_tags(id3: Any, version: tuple[int, ...] = (2, 4, 0)) -> list[Pair]:
    """(name, values) pairs for a mutagen `ID3`'s frames (`None` = an untagged file).

    Frames with nothing to compare (PRIV, MCDI, pictures, ...) are left out; text frames
    without a Picard name keep their frame ID (`tlen`).

    ID3v2.3 has no multi-value text frames, so Picard joins values with "/" there. A UUID
    never contains one, so MusicBrainz ID lists are split again; names are left as they are
    ("AC/DC").
    """
    pairs: list[Pair] = []
    for frame in id3.values() if id3 is not None else ():
        pairs.extend(_frame_pairs(frame))
    if version < (2, 4):
        pairs = [(k, _split_ids(vs) if _is_id_list(k) else vs) for k, vs in pairs]
    return pairs


def id3_pictures(id3: Any) -> list[Picture]:
    """Every APIC frame's type, MIME type and image size (0x0 if the image can't be read)."""
    if id3 is None:
        return []
    return [
        (int(frame.type), frame.mime, *image_size(frame.data))
        for frame in id3.values()
        if frame.FrameID == "APIC"
    ]


def _frame_pairs(frame) -> list[Pair]:
    frame_id = frame.FrameID
    if frame_id == "TXXX":
        name = FREETEXT.get(frame.desc.lower(), frame.desc)
        return [(name, _texts(frame))]
    if frame_id == "UFID":
        if frame.owner != MUSICBRAINZ_UFID_OWNER:
            return []
        return [("musicbrainz_trackid", [frame.data.decode("ascii", "replace")])]
    if frame_id in ("COMM", "USLT"):
        base = "comment" if frame_id == "COMM" else "lyrics"
        return [(f"{base}:{frame.desc}" if frame.desc else base, _texts(frame))]
    if frame_id in NUMBERED_FRAMES:
        number_key, total_key = NUMBERED_FRAMES[frame_id]
        out: list[Pair] = []
        for text in _texts(frame):
            number, _, total = text.partition("/")
            out.append((number_key, [number.strip()]))
            if total.strip():
                out.append((total_key, [total.strip()]))
        return out
    if frame_id == "TIPL":
        return [(INVOLVED_ROLES.get(role, role.lower()), [name]) for role, name in frame.people]
    if frame_id == "TMCL":
        return [(f"performer:{role}", [name]) for role, name in frame.people]
    if frame_id in FRAMES:
        values = [frame.url] if frame_id.startswith("W") else _texts(frame)
        return [(FRAMES[frame_id], values)]
    if frame_id.startswith("T") and hasattr(frame, "text"):
        return [(frame_id.lower(), _texts(frame))]
    return []


def _texts(frame) -> list[str]:
    text = frame.text
    return [str(t) for t in (text if isinstance(text, list) else [text])]


def _is_id_list(key: str) -> bool:
    return key.startswith("musicbrainz_") and key.endswith("id")


def _split_ids(values: list[str]) -> list[str]:
    return [part.strip() for v in values for part in v.split("/") if part.strip()]


def image_size(data: bytes) -> tuple[int, int]:
    """Width and height of a PNG, JPEG or GIF image from its header; (0, 0) otherwise."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    return (0, 0)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Size from the first start-of-frame marker (SOF0-15, except DHT, JPG and DAC)."""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return (0, 0)
        marker = data[pos + 1]
        if marker == 0xFF:  # padding
            pos += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD8)):  # markers without a length
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 9 > len(data):
                return (0, 0)
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return (width, height)
        pos += 2 + length
    return (0, 0)
