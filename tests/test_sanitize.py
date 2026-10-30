import unicodedata

import pytest

from tagdrifter.core.sanitize import (
    normalize_artist_name,
    normalize_collaborators,
    sanitize,
    strip_featured_artists,
)


# Examples from the naming-conventions table.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AC/DC", "AC∕DC"),
        ("Song A / Song B", "Song A ∕ Song B"),
        ("Back\\Slash", "Back∕Slash"),
        ("Medley: Pick Up the Pieces", "Medley - Pick Up the Pieces"),
        ("4:44", "4-44"),
        ("Work B**ch", "Work B∗∗ch"),
        ('Blue Monday (12" Mix)', "Blue Monday (12″ Mix)"),
        ('"Heroes"', "“Heroes”"),
        ('Stray " quote', "Stray ” quote"),
        ("A | B", "A ∣ B"),
        ("Where Is My Mind?", "Where Is My Mind"),
        ("<Angle>", "Angle"),
        ("Album...", "Album"),
        ("  padded  ", "padded"),
        ("multiple   spaces", "multiple spaces"),
        (".", "(period)"),
        ("...", "(period)"),
        ("?", "(question mark)"),
        ("!", "(exclamation mark)"),
        ("*", "(asterisk)"),
        ("....", "(period)"),
        ("?? ", "(unknown)"),
        ("", ""),
    ],
)
def test_sanitize(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


def test_sanitize_normalizes_to_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "Beyoncé")
    assert sanitize(decomposed) == unicodedata.normalize("NFC", "Beyoncé")


@pytest.mark.parametrize(
    ("a", "b"),
    [("Ke$ha", "Kesha"), ("A$AP Rocky", "ASAP Rocky"), ("The B-52's", "The B52s")],
)
def test_normalize_artist_name_equates_stylizations(a: str, b: str) -> None:
    assert normalize_artist_name(a) == normalize_artist_name(b)


def test_normalize_collaborators() -> None:
    expected = frozenset({"bikini kill", "huggy bear"})
    assert normalize_collaborators("Bikini Kill & Huggy Bear") == expected
    assert normalize_collaborators("Bikini Kill / Huggy Bear") == expected
    assert normalize_collaborators("Bikini Kill ∕ Huggy Bear") == expected
    assert normalize_collaborators("Bikini Kill, Huggy Bear") == expected


@pytest.mark.parametrize(
    ("credit", "expected"),
    [
        ("Adele feat. Erroll Garner", "Adele"),
        ("Adele ft. The Raconteurs", "Adele"),
        ("Adele Featuring Someone", "Adele"),
        ("Adele with Someone", "Adele"),
        ("Bikini Kill", "Bikini Kill"),
    ],
)
def test_strip_featured_artists(credit: str, expected: str) -> None:
    assert strip_featured_artists(credit) == expected
