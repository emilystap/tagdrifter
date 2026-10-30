from pathlib import PurePosixPath

import pytest

from tagdrifter.core import MetadataReadError, TrackMeta, parse_index, read_metadata

from .conftest import make_meta


@pytest.mark.parametrize(
    ("raw", "expected"), [("3", 3), ("3/12", 3), (" 07 ", 7), ("", 1), ("x", 1)]
)
def test_parse_index(raw: str, expected: int) -> None:
    assert parse_index(raw) == expected


def test_keys_are_case_insensitive_and_lists_supported() -> None:
    meta = TrackMeta.from_tags(
        PurePosixPath("a.flac"), {"ALBUMARTIST": ["Adele"], "Artists": ["Adele", "Erroll Garner"]}
    )
    assert meta.albumartist == "Adele"
    assert meta.artists == ("Adele", "Erroll Garner")


def test_semicolon_delimited_artists() -> None:
    assert make_meta(artists="Adele; The Raconteurs").artists == ("Adele", "The Raconteurs")


def test_fallbacks_match_v1() -> None:
    meta = make_meta(artist="Solo")
    assert meta.effective_album_artist == "Solo"
    assert meta.effective_track_artist == "Solo"
    assert meta.effective_album == "Unknown Album"
    assert meta.effective_title == "Unknown Track"

    empty = make_meta()
    assert empty.effective_album_artist == "Unknown Artist"
    assert empty.effective_track_artist == "Unknown Artist"


def test_album_artist_sort_falls_back_to_artistsort() -> None:
    assert make_meta(artistsort="Kesha").albumartist_sort == "Kesha"


def test_disambiguation_priority() -> None:
    meta = make_meta(edition="Deluxe", albumversion="Remastered")
    assert meta.disambiguation == "Remastered"
    meta = make_meta(**{"comment:MusicBrainz Album Comment": "MB comment", "edition": "Deluxe"})
    assert meta.disambiguation == "MB comment"


def test_release_year_and_multi_disc() -> None:
    meta = make_meta(date="1992-05-01", discnumber="2/2")
    assert meta.release_year == "1992"
    assert meta.is_multi_disc
    assert make_meta(date="n/a").release_year == ""
    assert make_meta(totaldiscs="2").is_multi_disc
    assert not make_meta().is_multi_disc


def test_empty_values_are_skipped() -> None:
    assert make_meta(albumartist=["", "Real"]).albumartist == "Real"
    assert not make_meta(albumartist="  ").has_tag("albumartist")


def test_read_metadata_from_real_flac(flac_factory) -> None:
    path = flac_factory(
        "Kesha/Animal/x.flac",
        ALBUMARTIST="Ke$ha",
        ARTISTS=["Ke$ha", "3OH!3"],
        ALBUM="Animal",
        TITLE="Blah Blah Blah",
        TRACKNUMBER="5",
        MUSICBRAINZ_ALBUMID="2d1e4c5a-0000-4000-8000-000000000000",
    )
    meta = read_metadata(path)
    assert meta.albumartist == "Ke$ha"
    assert meta.artists == ("Ke$ha", "3OH!3")
    assert meta.track_number == 5
    assert meta.release_mbid.startswith("2d1e4c5a")
    assert meta.has_tag("musicbrainz_albumid")


def test_read_metadata_rejects_non_audio(tmp_path) -> None:
    pytest.importorskip("mutagen")
    bogus = tmp_path / "not_audio.flac"
    bogus.write_bytes(b"definitely not flac")
    with pytest.raises(MetadataReadError):
        read_metadata(bogus)
