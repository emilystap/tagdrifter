from __future__ import annotations

import struct
from pathlib import Path, PurePosixPath

import pytest

from tagdrifter.config import Settings
from tagdrifter.core import TrackMeta
from tagdrifter.db import Store
from tagdrifter.scanner import run_scan


def make_meta(path: str = "Artist/Album/track.flac", **tags: object) -> TrackMeta:
    """Build a TrackMeta from keyword tags. Pass keys with spaces or colons via a dict:
    make_meta(**{"album artist": "X"})."""
    return TrackMeta.from_tags(PurePosixPath(path), tags)


def _block(block_type: int, data: bytes, *, last: bool) -> bytes:
    return bytes([(0x80 if last else 0) | block_type]) + len(data).to_bytes(3, "big") + data


def _streaminfo() -> bytes:
    # min/max block size, min/max frame size, then sample rate(20b) channels-1(3b)
    # bps-1(5b) total samples(36b), then 16-byte MD5.
    packed = (44100 << 44) | (1 << 41) | (15 << 36)
    return struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16


def _vorbis_comment(tags: dict[str, str | list[str]]) -> bytes:
    vendor = b"tagdrifter-tests"
    entries = []
    for key, value in tags.items():
        for v in value if isinstance(value, list) else [value]:
            entries.append(f"{key}={v}".encode())
    out = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(entries))
    for e in entries:
        out += struct.pack("<I", len(e)) + e
    return out


def _picture(size: int) -> bytes:
    # Picture type, MIME, description, width/height/depth/colours, then the image data, padded
    # so the whole block is `size` bytes. Mutagen parses this structure rather than trusting the
    # block length, so it has to be well formed.
    mime = b"image/jpeg"
    header = struct.pack(">II", 3, len(mime)) + mime + struct.pack(">IIIII", 0, 500, 500, 24, 0)
    data_len = size - len(header) - 4
    if data_len < 0:
        raise ValueError(f"picture_bytes must be at least {len(header) + 4}")
    return header + struct.pack(">I", data_len) + b"\x00" * data_len


def build_flac(
    path: Path,
    tags: dict[str, str | list[str]] | None = None,
    *,
    picture_bytes: int = 0,
    picture_first: bool = True,
    id3_prefix: bool = False,
) -> Path:
    """Write a structurally valid, audio-less FLAC file.

    `picture_bytes` adds a PICTURE block of that size (before or after the comment block).
    """
    blocks: list[tuple[int, bytes]] = [(0, _streaminfo())]
    picture = (6, _picture(picture_bytes)) if picture_bytes else None
    comment = (4, _vorbis_comment(tags)) if tags is not None else None
    ordered = [picture, comment] if picture_first else [comment, picture]
    blocks += [b for b in ordered if b]
    body = b"".join(_block(t, d, last=i == len(blocks) - 1) for i, (t, d) in enumerate(blocks))

    prefix = b""
    if id3_prefix:
        size = 20
        syncsafe = bytes(
            [(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]
        )
        prefix = b"ID3\x04\x00\x00" + syncsafe + b"\x00" * size

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + b"fLaC" + body + b"\xff\xf8fake-audio-frames")
    return path


@pytest.fixture
def flac_factory(tmp_path: Path):
    """Create FLAC files under tmp_path with the given Vorbis comments."""

    def factory(relative: str, **tags: str | list[str]) -> Path:
        return build_flac(tmp_path / relative, dict(tags))

    return factory


# --- A small library with one example of most issues -----------------------------------

MBIDS = {
    "musicbrainz_artistid": "11111111-1111-4111-8111-111111111111",
    "musicbrainz_albumartistid": "11111111-1111-4111-8111-111111111111",
    "musicbrainz_albumid": "22222222-2222-4222-8222-222222222222",
    "musicbrainz_releasegroupid": "33333333-3333-4333-8333-333333333333",
    "musicbrainz_trackid": "44444444-4444-4444-8444-444444444444",
}


def good_tags(**overrides: str) -> dict[str, str]:
    tags = {
        "ALBUMARTIST": "Pixies",
        "ARTIST": "Pixies",
        "ALBUM": "Doolittle",
        "TITLE": "Debaser",
        "TRACKNUMBER": "1",
        "DISCNUMBER": "1",
        "TOTALDISCS": "1",
        "DATE": "1989",
        **MBIDS,
    }
    tags.update(overrides)
    return tags


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "music"
    # Correct track, with art and a matching lyrics file
    build_flac(
        lib / "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac", good_tags(), picture_bytes=50_000
    )
    (lib / "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.lrc").write_text("[00:00] la")
    # Misnamed track, missing an MBID, with a bad MBID
    tags = good_tags(TITLE="Tame", TRACKNUMBER="2", musicbrainz_trackid="not-a-uuid")
    del tags["musicbrainz_releasegroupid"]
    build_flac(lib / "Pixies/Doolittle/02 Tame.flac", tags)
    # Orphan lyrics, and an unreadable file
    (lib / "Pixies/Doolittle/old name.lrc").write_text("x")
    (lib / "Pixies/Doolittle/broken.flac").write_bytes(b"not flac")
    # Compilation filed under an artist
    build_flac(
        lib / "Pixies/Hits/Pixies_Hits_01_Song.flac",
        good_tags(ALBUMARTIST="Various Artists", ALBUM="Hits", TITLE="Song"),
    )
    # Loose file in the root, and ignored folders
    build_flac(lib / "loose.flac", good_tags())
    build_flac(lib / ".zfs/snapshot/x.flac", good_tags())
    build_flac(lib / "@eaDir/x.flac", good_tags())
    return lib


def scan(settings: Settings, **kw):
    with Store(settings.database_path) as store:
        result = run_scan(settings, store, **kw)
        issues = {(i.check_id, i.subject): i for i in store.open_issues()}
    return result, issues


def settings_for(lib: Path, tmp_path: Path, **kw) -> Settings:
    return Settings(library_path=lib, database_path=tmp_path / "db" / "td.db", workers=4, **kw)
