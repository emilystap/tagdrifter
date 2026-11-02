from __future__ import annotations

import os
import struct
import uuid
from pathlib import Path, PurePosixPath

import pytest

from tagdrifter.config import Settings
from tagdrifter.core import TrackMeta
from tagdrifter.db import Store
from tagdrifter.scanner import run_scan
from tagdrifter.secondary import MARKER


def make_meta(path: str = "Artist/Album/track.flac", **tags: object) -> TrackMeta:
    """Build a TrackMeta from keyword tags. Pass keys with spaces or colons via a dict:
    make_meta(**{"album artist": "X"})."""
    return TrackMeta.from_tags(PurePosixPath(path), tags)


def _block(block_type: int, data: bytes, *, last: bool) -> bytes:
    return bytes([(0x80 if last else 0) | block_type]) + len(data).to_bytes(3, "big") + data


def _streaminfo(length_ms: int = 0) -> bytes:
    # min/max block size, min/max frame size, then sample rate(20b) channels-1(3b)
    # bps-1(5b) total samples(36b), then 16-byte MD5.
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | (length_ms * 44100 // 1000)
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


def _picture(size: int, kind: int = 3, width: int = 500, height: int = 500) -> bytes:
    # Picture type, MIME, description, width/height/depth/colours, then the image data, padded
    # so the whole block is `size` bytes. Mutagen parses this structure rather than trusting the
    # block length, so it has to be well formed.
    mime = b"image/jpeg"
    header = (
        struct.pack(">II", kind, len(mime)) + mime + struct.pack(">IIIII", 0, width, height, 24, 0)
    )
    data_len = size - len(header) - 4
    if data_len < 0:
        raise ValueError(f"picture_bytes must be at least {len(header) + 4}")
    return header + struct.pack(">I", data_len) + b"\x00" * data_len


def build_flac(
    path: Path,
    tags: dict[str, str | list[str]] | None = None,
    *,
    picture_bytes: int = 64,
    picture_first: bool = True,
    id3_prefix: bool = False,
    length_ms: int = 0,
    picture: tuple[int, int, int] = (3, 500, 500),
    extra_pictures: tuple[tuple[int, int, int, int], ...] = (),
) -> Path:
    """Write a structurally valid, audio-less FLAC file.

    `picture_bytes` adds a PICTURE block of that size (before or after the comment block);
    by default a small front cover, as every file in the target library has one (0 = none).
    `picture` is its (type, width, height); `extra_pictures` adds (size, type, width, height)
    blocks at the end. `length_ms` sets the length STREAMINFO reports (0 = unknown).
    """
    blocks: list[tuple[int, bytes]] = [(0, _streaminfo(length_ms))]
    pic = (6, _picture(picture_bytes, *picture)) if picture_bytes else None
    comment = (4, _vorbis_comment(tags)) if tags is not None else None
    ordered = [pic, comment] if picture_first else [comment, pic]
    blocks += [b for b in ordered if b]
    blocks += [(6, _picture(*extra)) for extra in extra_pictures]
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


# One silent MPEG-1 Layer III frame: 128 kbit/s, 44.1 kHz, 417 bytes, 1152 samples.
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def build_mp3(
    path: Path, frames: list | None = None, *, audio_frames: int = 100, v2_version: int = 4
) -> Path:
    """Write an MP3 of `audio_frames` silent frames (about 26 ms each) with the given mutagen
    ID3 frames (None = no ID3 tag at all)."""
    from mutagen.id3 import ID3

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_FRAME * audio_frames)
    if frames is not None:
        id3 = ID3()
        for frame in frames:
            id3.add(frame)
        id3.save(path, v2_version=v2_version)
    return path


def picard_id3(**overrides: object) -> list:
    """ID3 frames as Picard writes good_tags() to an MP3, with a 600x400 JPEG front cover.
    Overrides replace a frame by its HashKey ("TIT2", "TXXX:ARTISTS"); None drops it."""
    from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, UFID

    jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 9
        + b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", 400, 600) + b"\x00" * 10
    )  # fmt: skip
    frames = {
        "TPE2": TPE2(encoding=3, text="Pixies"),
        "TPE1": TPE1(encoding=3, text="Pixies"),
        "TALB": TALB(encoding=3, text="Doolittle"),
        "TIT2": TIT2(encoding=3, text="Debaser"),
        "TRCK": TRCK(encoding=3, text="1/15"),
        "TPOS": TPOS(encoding=3, text="1/1"),
        "TDRC": TDRC(encoding=3, text="1989"),
        "UFID:http://musicbrainz.org": UFID(
            owner="http://musicbrainz.org", data=MBIDS["musicbrainz_trackid"].encode()
        ),
        "APIC:": APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=jpeg),
    }
    for key, desc in [
        ("musicbrainz_albumid", "MusicBrainz Album Id"),
        ("musicbrainz_releasegroupid", "MusicBrainz Release Group Id"),
        ("musicbrainz_artistid", "MusicBrainz Artist Id"),
        ("musicbrainz_albumartistid", "MusicBrainz Album Artist Id"),
    ]:
        frames[f"TXXX:{desc}"] = TXXX(encoding=3, desc=desc, text=MBIDS[key])
    track_id = "MusicBrainz Release Track Id"
    frames[f"TXXX:{track_id}"] = TXXX(encoding=3, desc=track_id, text=str(uuid.uuid4()))
    frames.update(overrides)
    return [f for f in frames.values() if f is not None]


@pytest.fixture
def flac_factory(tmp_path: Path):
    """Create FLAC files under tmp_path with the given Vorbis comments."""

    def factory(relative: str, **tags: str | list[str]) -> Path:
        return build_flac(tmp_path / relative, dict(tags))

    return factory


@pytest.fixture(autouse=True)
def _no_musicbrainz_requests(monkeypatch):
    """Tests must never call the real MusicBrainz API (a small non-profit with strict rate
    limits): clients without a fake opener fail instead."""

    def refuse(request, timeout=None):
        raise AssertionError(f"test tried to call MusicBrainz: {request.full_url}")

    monkeypatch.setattr("tagdrifter.musicbrainz._urlopen", refuse)


# --- A small library with one example of most issues -----------------------------------

MBIDS = {
    "musicbrainz_artistid": "11111111-1111-4111-8111-111111111111",
    "musicbrainz_albumartistid": "11111111-1111-4111-8111-111111111111",
    "musicbrainz_albumid": "22222222-2222-4222-8222-222222222222",
    "musicbrainz_releasegroupid": "33333333-3333-4333-8333-333333333333",
    "musicbrainz_trackid": "44444444-4444-4444-8444-444444444444",
}


def good_tags(**overrides: str) -> dict[str, str]:
    """Picard-like tags for Pixies – Doolittle track 1. Each call gets its own release track
    MBID, so separate files never look like duplicates of one track."""
    tags = {
        "musicbrainz_releasetrackid": str(uuid.uuid4()),
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


# --- A library and a secondary of hard links (tests/test_secondary*.py) ----------------


def link(lib: Path, sec: Path, rel: str, dst: str | None = None) -> None:
    target = sec / (dst or rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(lib / rel, target)


@pytest.fixture
def libraries(tmp_path):
    """A library of two artists and a secondary that has only Pixies, with Doolittle linked
    under its old folder name and its cover not linked."""
    lib, sec = tmp_path / "libraries" / "music", tmp_path / "libraries" / "secondary"
    build_flac(lib / "Pixies/Doolittle/01 Debaser.flac", good_tags())
    (lib / "Pixies/Doolittle/01 Debaser.lrc").write_text("[00:00] la")
    (lib / "Pixies/Doolittle/cover.jpg").write_bytes(b"jpg")
    other = "99999999-9999-4999-8999-999999999999"
    tags = good_tags(ALBUMARTIST="Other", ALBUM="Elsewhere", musicbrainz_albumartistid=other)
    build_flac(lib / "Other/Elsewhere/01 Song.flac", tags)
    sec.mkdir(parents=True)
    (sec / MARKER).touch()
    for name in ("01 Debaser.flac", "01 Debaser.lrc"):
        link(lib, sec, f"Pixies/Doolittle/{name}", f"Pixies/Doolitle/{name}")
    (sec / ".DS_Store").write_bytes(b"x")
    settings = settings_for(lib, tmp_path, secondary_path=sec)
    scan(settings)
    return settings
