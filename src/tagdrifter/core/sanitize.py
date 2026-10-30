"""String sanitization and artist-name normalization.

Everything here is pure (no filesystem access) and safe to cache.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Replacement for illegal path slashes (/ or \). Unicode Division Slash (U+2215).
SLASH_REPLACEMENT = "∕"

RE_SLASHES = re.compile(r"[\\/]")
RE_COLON_SPACE = re.compile(r"\s*:\s+")
RE_INCH_QUOTE = re.compile(r'(\d)"')
RE_PAIRED_QUOTES = re.compile(r'"([^"]+)"')
RE_ILLEGAL_CHARS = re.compile(r"[?<>]")
RE_WHITESPACE = re.compile(r"\s+")
RE_NON_WORD = re.compile(r"[^\w\s]")
RE_COLLAB_SPLIT = re.compile(r"\s*[/\\∕,]\s*|\s+(?:&|\+)\s+")
RE_FEAT_SUFFIX = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)

# Exact map for punctuation-only titles (Lidarr conventions).
PUNCTUATION_WORD_MAP: dict[str, str] = {
    ".": "(period)",
    "..": "(period)",
    "...": "(period)",
    "?": "(question mark)",
    "!": "(exclamation mark)",
    "*": "(asterisk)",
    ":": "(colon)",
    "/": "(slash)",
    "\\": "(backslash)",
    "|": "(pipe)",
    '"': "(quote)",
    "<": "(less than)",
    ">": "(greater than)",
}

VA_TAG_NAMES = frozenset({"various artists", "various", "soundtrack", "va"})
VA_FOLDER_NAMES = frozenset({"various artists", "various", "soundtrack", "soundtracks", "va"})
VARIOUS_ARTISTS_FOLDER = "Various Artists"


def nfc(text: str) -> str:
    """Normalize to Unicode NFC so visually identical names compare equal.

    Files written from macOS clients are often stored decomposed (NFD); tags are almost
    always composed (NFC). Without this, "Beyoncé" from a path and from a tag can differ.
    """
    return unicodedata.normalize("NFC", text)


@lru_cache(maxsize=8192)
def sanitize(text: str, slash_replacement: str = SLASH_REPLACEMENT) -> str:
    """Make a tag value safe for use as a path component (see docs/naming-conventions.md)."""
    if not text:
        return ""

    text = nfc(text)
    raw_stripped = text.strip()
    if raw_stripped in PUNCTUATION_WORD_MAP:
        return PUNCTUATION_WORD_MAP[raw_stripped]

    cleaned = RE_SLASHES.sub(slash_replacement, text)
    cleaned = RE_COLON_SPACE.sub(" - ", cleaned).replace(":", "-")
    cleaned = RE_INCH_QUOTE.sub(r"\1″", cleaned)
    cleaned = RE_PAIRED_QUOTES.sub(r"“\1”", cleaned).replace('"', "”")
    cleaned = cleaned.replace("*", "∗").replace("|", "∣")
    cleaned = RE_ILLEGAL_CHARS.sub("", cleaned)
    cleaned = RE_WHITESPACE.sub(" ", cleaned)
    sanitized = cleaned.strip(" .")

    if not sanitized:
        if set(raw_stripped) == {"."}:
            return "(period)"
        return "(unknown)"

    return sanitized


@lru_cache(maxsize=8192)
def normalize_artist_name(name: str) -> str:
    """Comparison key for artist names; resolves stylizations like Ke$ha -> kesha."""
    if not name:
        return ""
    normalized = nfc(name).replace("$", "s")
    return RE_NON_WORD.sub("", normalized).strip().lower()


@lru_cache(maxsize=8192)
def normalize_collaborators(artist_name: str) -> frozenset[str]:
    """Normalized artist tokens split across joiners (/, ∕, comma, &, +)."""
    if not artist_name:
        return frozenset()
    parts = RE_COLLAB_SPLIT.split(artist_name)
    return frozenset(norm for p in parts if (norm := normalize_artist_name(p)))


@lru_cache(maxsize=8192)
def strip_featured_artists(artist_name: str) -> str:
    """Remove trailing 'feat. ...', 'ft. ...', 'featuring ...' or 'with ...' credits."""
    if not artist_name:
        return ""
    return RE_FEAT_SUFFIX.sub("", artist_name).strip()


def extract_artist_set(artist_list: tuple[str, ...], fallback_credit: str = "") -> frozenset[str]:
    """Normalized artist set from an explicit multi-value tag, else split the display credit."""
    if artist_list:
        return frozenset(norm for a in artist_list if (norm := normalize_artist_name(a)))
    return normalize_collaborators(fallback_credit)


def is_various_artists_tag(value: str) -> bool:
    return value.strip().lower() in VA_TAG_NAMES


def is_various_artists_folder(name: str) -> bool:
    return name.strip().lower() in VA_FOLDER_NAMES
