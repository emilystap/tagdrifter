from __future__ import annotations

import struct
from pathlib import Path, PurePosixPath

import pytest

from tagdrifter.core import TrackMeta


def make_meta(path: str = "Artist/Album/track.flac", **tags: object) -> TrackMeta:
    """Build a TrackMeta from keyword tags. Pass keys with spaces or colons via a dict:
    make_meta(**{"album artist": "X"})."""
    return TrackMeta.from_tags(PurePosixPath(path), tags)


def write_minimal_flac(path: Path) -> Path:
    """Write a structurally valid, audio-less FLAC file (STREAMINFO block only)."""
    # STREAMINFO: min/max block size, min/max frame size, then 20-bit sample rate,
    # 3-bit channels-1, 5-bit bps-1, 36-bit total samples, then 16-byte MD5.
    sample_rate, channels, bps, total = 44100, 2, 16, 0
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | total
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big")
    streaminfo += b"\x00" * 16
    header = bytes([0x80]) + len(streaminfo).to_bytes(3, "big")  # last-block flag, type 0
    path.write_bytes(b"fLaC" + header + streaminfo)
    return path


@pytest.fixture
def flac_factory(tmp_path: Path):
    """Create real FLAC files with the given Vorbis comments."""
    FLAC = pytest.importorskip("mutagen.flac").FLAC

    def factory(relative: str, **tags: str | list[str]) -> Path:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        write_minimal_flac(path)
        audio = FLAC(path)
        audio.add_tags()
        for key, value in tags.items():
            audio[key] = value
        audio.save()
        return path

    return factory
