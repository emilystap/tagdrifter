from __future__ import annotations

import struct
from pathlib import Path, PurePosixPath

import pytest

from tagdrifter.core import TrackMeta


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
    picture = (6, b"\x00" * picture_bytes) if picture_bytes else None
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
