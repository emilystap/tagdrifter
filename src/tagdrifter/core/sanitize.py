"""String sanitization and artist-name normalization.

Everything here is pure (no filesystem access) and safe to cache.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Lookalike substitutes for characters Windows/SMB can't store, so a name reads back as its
# tag. Replacement for illegal path slashes (/ or \): Unicode Division Slash (U+2215).
SLASH_REPLACEMENT = "∕"
COLON_REPLACEMENT = "꞉"  # U+A789 Modifier Letter Colon
CHAR_REPLACEMENTS = {
    ":": COLON_REPLACEMENT,
    "*": "∗",  # U+2217 Asterisk Operator
    "|": "∣",  # U+2223 Divides
    "?": "？",  # U+FF1F Fullwidth Question Mark
    "<": "‹",  # U+2039 Single Left-Pointing Angle Quotation Mark
    ">": "›",  # U+203A Single Right-Pointing Angle Quotation Mark
}
# A leading or trailing dot in a folder/file name: Windows/SMB drop trailing dots, and a
# leading dot hides the entry (and matches the default `.*` ignore pattern).
EDGE_DOT_REPLACEMENT = "․"  # U+2024 One Dot Leader

# Substitutes mapped back to what they stand for, for comparing names with tag values.
SUBSTITUTES_TO_ASCII = str.maketrans(
    {v: k for k, v in CHAR_REPLACEMENTS.items()}
    | {SLASH_REPLACEMENT: "/", EDGE_DOT_REPLACEMENT: ".", "″": '"', "“": '"', "”": '"'}
)
CHAR_REPLACEMENTS_TABLE = str.maketrans(CHAR_REPLACEMENTS)

RE_SLASHES = re.compile(r"[\\/]")
RE_INCH_QUOTE = re.compile(r'(\d)"')
RE_PAIRED_QUOTES = re.compile(r'"([^"]+)"')
RE_WHITESPACE = re.compile(r"\s+")
RE_NON_WORD = re.compile(r"[^\w\s]")
RE_COLLAB_SPLIT = re.compile(r"\s*[/\\∕,]\s*|\s+(?:&|\+)\s+")
RE_FEAT_SUFFIX = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)

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
    """Make a tag value safe for use inside a path component (see docs/naming-conventions.md).

    Characters Windows/SMB can't store become lookalikes, so the name still reads as the tag.
    Leading/trailing dots are kept: they only matter at the edges of a whole folder or file
    name, which `protect_edges` handles once the name is assembled.
    """
    if not text:
        return ""

    text = nfc(text)
    raw_stripped = text.strip()
    if raw_stripped and set(raw_stripped) == {"."}:
        return "(period)"  # a dots-only name would be hidden or dropped entirely

    cleaned = RE_SLASHES.sub(slash_replacement, text)
    cleaned = RE_INCH_QUOTE.sub(r"\1″", cleaned)
    cleaned = RE_PAIRED_QUOTES.sub(r"“\1”", cleaned).replace('"', "”")
    cleaned = cleaned.translate(CHAR_REPLACEMENTS_TABLE)
    sanitized = RE_WHITESPACE.sub(" ", cleaned).strip()
    return sanitized or "(unknown)"


def protect_edges(name: str) -> str:
    """Replace leading/trailing dots of an assembled folder or file name with lookalikes."""
    core = name.strip(".")
    if not core:
        return name
    lead = len(name) - len(name.lstrip("."))
    trail = len(name) - len(name.rstrip("."))
    return EDGE_DOT_REPLACEMENT * lead + core + EDGE_DOT_REPLACEMENT * trail


def unprotect_edges(name: str) -> str:
    """Undo `protect_edges`, e.g. to reuse a folder name inside a longer name."""
    core = name.strip(EDGE_DOT_REPLACEMENT)
    if not core:
        return name
    lead = len(name) - len(name.lstrip(EDGE_DOT_REPLACEMENT))
    trail = len(name) - len(name.rstrip(EDGE_DOT_REPLACEMENT))
    return "." * lead + core + "." * trail


def restore_substitutes(name: str) -> str:
    """Map substitute characters in a name back to the ASCII they stand for."""
    return name.translate(SUBSTITUTES_TO_ASCII)


def sanitize_folder(text: str) -> str:
    """A whole folder name from one tag value (e.g. an artist folder)."""
    return protect_edges(sanitize(text))


@lru_cache(maxsize=8192)
def normalize_artist_name(name: str) -> str:
    """Comparison key for artist names; resolves stylizations like Ke$ha -> kesha."""
    if not name:
        return ""
    # Substitutes back to ASCII first: some (꞉) count as letters and would survive RE_NON_WORD.
    normalized = restore_substitutes(nfc(name)).replace("$", "s")
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
