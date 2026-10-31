"""Minimal FLAC tag reader.

Reads the STREAMINFO block (for the exact length) and the VORBIS_COMMENT block, and *seeks
past* everything else, so embedded cover art (PICTURE blocks, often several MB) is never
read. Over SMB or on spinning disks this is far faster than a general-purpose tag library,
which loads every metadata block.

Format reference: https://xiph.org/flac/format.html#metadata_block
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from os import PathLike

FLAC_MAGIC = b"fLaC"
ID3_MAGIC = b"ID3"
BLOCK_STREAMINFO = 0
BLOCK_VORBIS_COMMENT = 4
STREAMINFO_SIZE = 34
MAX_COMMENT_BLOCK = 16 * 1024 * 1024  # 16 MiB guard against corrupt length fields


class FlacError(Exception):
    """The file is not a readable FLAC stream."""


def _skip_id3v2(f) -> None:
    """Some taggers prepend an ID3v2 tag to FLAC files; skip it if present."""
    head = f.read(10)
    if len(head) == 10 and head[:3] == ID3_MAGIC:
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]  # syncsafe int
        footer = 10 if head[5] & 0x10 else 0
        f.seek(10 + size + footer)
    else:
        f.seek(0)


@dataclass(frozen=True)
class FlacInfo:
    comments: list[tuple[str, str]] = field(default_factory=list)  # (KEY, value), file order
    length_ms: int | None = None  # None when STREAMINFO doesn't give the sample count


def read_vorbis_comments(path: str | PathLike[str]) -> list[tuple[str, str]]:
    """Return the (KEY, value) pairs of a FLAC file's Vorbis comment block, in file order.

    Returns an empty list for a valid FLAC file without a comment block.
    Raises FlacError for anything that is not a well-formed FLAC metadata header.
    """
    return read_flac(path).comments


def read_flac(path: str | PathLike[str]) -> FlacInfo:
    """The Vorbis comments and the length (from STREAMINFO, which always comes first).
    Raises FlacError for anything that is not a well-formed FLAC metadata header."""
    length_ms: int | None = None
    with open(path, "rb") as f:
        _skip_id3v2(f)
        if f.read(4) != FLAC_MAGIC:
            raise FlacError("missing fLaC marker")

        while True:
            header = f.read(4)
            if len(header) < 4:
                raise FlacError("truncated metadata block header")
            is_last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            length = int.from_bytes(header[1:4], "big")

            if block_type == BLOCK_STREAMINFO and length >= STREAMINFO_SIZE:
                data = f.read(length)
                if len(data) < length:
                    raise FlacError("truncated STREAMINFO block")
                length_ms = _length_ms(data)
                if is_last:
                    return FlacInfo([], length_ms)
                continue

            if block_type == BLOCK_VORBIS_COMMENT:
                if length > MAX_COMMENT_BLOCK:
                    raise FlacError(f"implausible comment block size ({length} bytes)")
                data = f.read(length)
                if len(data) < length:
                    raise FlacError("truncated comment block")
                return FlacInfo(_parse_vorbis_comment(data), length_ms)

            if block_type == 127:
                raise FlacError("invalid metadata block type")
            if is_last:
                return FlacInfo([], length_ms)
            f.seek(length, 1)  # skip PICTURE, PADDING, SEEKTABLE, ...


def _length_ms(streaminfo: bytes) -> int | None:
    """Sample rate (20 bits) and total samples (36 bits) are packed into bytes 10-17, after
    the block and frame sizes; a total of 0 means unknown."""
    packed = int.from_bytes(streaminfo[10:18], "big")
    sample_rate = packed >> 44
    total_samples = packed & ((1 << 36) - 1)
    if not sample_rate or not total_samples:
        return None
    return round(total_samples * 1000 / sample_rate)


def _parse_vorbis_comment(data: bytes) -> list[tuple[str, str]]:
    try:
        (vendor_len,) = struct.unpack_from("<I", data, 0)
        offset = 4 + vendor_len
        (count,) = struct.unpack_from("<I", data, offset)
        offset += 4
        pairs: list[tuple[str, str]] = []
        for _ in range(count):
            (entry_len,) = struct.unpack_from("<I", data, offset)
            offset += 4
            entry = data[offset : offset + entry_len]
            if len(entry) < entry_len:
                raise FlacError("truncated comment entry")
            offset += entry_len
            key, sep, value = entry.decode("utf-8", errors="replace").partition("=")
            if sep:
                pairs.append((key, value))
        return pairs
    except struct.error as exc:
        raise FlacError(f"malformed comment block: {exc}") from exc
