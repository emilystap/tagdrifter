import pytest

from tagdrifter.core.flac import FlacError, read_vorbis_comments

from .conftest import build_flac


def test_reads_comments(tmp_path) -> None:
    path = build_flac(tmp_path / "a.flac", {"ARTIST": "Pixies", "ARTISTS": ["A", "B"]})
    assert read_vorbis_comments(path) == [("ARTIST", "Pixies"), ("ARTISTS", "A"), ("ARTISTS", "B")]


@pytest.mark.parametrize("picture_first", [True, False])
def test_skips_cover_art(tmp_path, picture_first) -> None:
    path = build_flac(
        tmp_path / "a.flac",
        {"TITLE": "Debaser"},
        picture_bytes=3_000_000,
        picture_first=picture_first,
    )
    assert read_vorbis_comments(path) == [("TITLE", "Debaser")]


def test_value_containing_equals_and_unicode(tmp_path) -> None:
    path = build_flac(tmp_path / "a.flac", {"TITLE": "x=y ∕ “Heroes”"})
    assert read_vorbis_comments(path) == [("TITLE", "x=y ∕ “Heroes”")]


def test_no_comment_block(tmp_path) -> None:
    assert read_vorbis_comments(build_flac(tmp_path / "a.flac", None)) == []


def test_id3_prefixed_flac(tmp_path) -> None:
    path = build_flac(tmp_path / "a.flac", {"ALBUM": "Doolittle"}, id3_prefix=True)
    assert read_vorbis_comments(path) == [("ALBUM", "Doolittle")]


def test_not_flac(tmp_path) -> None:
    path = tmp_path / "a.flac"
    path.write_bytes(b"definitely not flac")
    with pytest.raises(FlacError):
        read_vorbis_comments(path)


def test_truncated(tmp_path) -> None:
    path = build_flac(tmp_path / "a.flac", {"ALBUM": "Doolittle"})
    path.write_bytes(path.read_bytes()[:50])
    with pytest.raises(FlacError):
        read_vorbis_comments(path)


def test_agrees_with_mutagen(tmp_path) -> None:
    mutagen_flac = pytest.importorskip("mutagen.flac")
    path = build_flac(
        tmp_path / "a.flac",
        {"ALBUMARTIST": "Ke$ha", "ARTISTS": ["Ke$ha", "3OH!3"]},
        picture_bytes=1000,
    )
    expected = [(k, v) for k, v in mutagen_flac.FLAC(path).tags]
    assert read_vorbis_comments(path) == expected


def test_length_from_streaminfo(tmp_path) -> None:
    from tagdrifter.core.flac import read_flac

    info = read_flac(build_flac(tmp_path / "a.flac", {"TITLE": "x"}, length_ms=189_000))
    assert info.length_ms == 189_000 and info.comments == [("TITLE", "x")]
    assert read_flac(build_flac(tmp_path / "b.flac", None)).length_ms is None  # 0 samples


def test_read_tags_records_length(tmp_path) -> None:
    from pathlib import PurePath

    from tagdrifter.core import TrackMeta, read_tags

    path = build_flac(tmp_path / "a.flac", {"TITLE": "x"}, length_ms=61_500, picture_bytes=500)
    tags = read_tags(path)
    assert tags["~length"] == ("61500",)
    assert TrackMeta.from_tags(PurePath("a.flac"), tags).length_ms == 61_500
