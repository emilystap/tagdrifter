import struct

import pytest
from mutagen.id3 import COMM, ID3, TIPL, TIT2, TMCL, TPE1, TRCK, TXXX, UFID

from tagdrifter.core import read_metadata
from tagdrifter.core.id3 import id3_tags, image_size
from tagdrifter.core.metadata import EmbeddedPicture, read_tags

from .conftest import MBIDS, build_flac, build_mp3, good_tags, picard_id3, scan, settings_for


def test_picard_mp3_reads_like_flac(tmp_path) -> None:
    path = build_mp3(tmp_path / "a.mp3", picard_id3())
    tags = read_tags(path)
    assert tags["albumartist"] == ("Pixies",)
    assert tags["title"] == ("Debaser",)
    assert tags["tracknumber"] == ("1",)
    assert tags["totaltracks"] == ("15",)
    assert tags["discnumber"] == ("1",)
    assert tags["totaldiscs"] == ("1",)
    assert tags["date"] == ("1989",)
    for key in ("musicbrainz_trackid", "musicbrainz_albumid", "musicbrainz_albumartistid"):
        assert tags[key] == (MBIDS[key],)

    meta = read_metadata(path)
    assert meta.is_mbid_tagged
    assert meta.length_ms == pytest.approx(2606, abs=30)  # 100 frames of 26.1 ms
    assert meta.pictures == (EmbeddedPicture(3, "image/jpeg", 600, 400),)


def test_other_frames() -> None:
    id3 = ID3()
    for frame in [
        TXXX(encoding=3, desc="ARTISTS", text=["Adele", "Erroll Garner"]),
        TXXX(encoding=3, desc="originalyear", text="1992"),
        COMM(encoding=3, lang="eng", desc="", text="hello"),
        COMM(encoding=3, lang="eng", desc="MusicBrainz Album Comment", text="deluxe"),
        TIPL(encoding=3, people=[["producer", "Steve Albini"], ["DJ-mix", "X"]]),
        TMCL(encoding=3, people=[["guitar", "Joey Santiago"]]),
        UFID(owner="http://example.com", data=b"other"),
    ]:
        id3.add(frame)
    assert dict(id3_tags(id3)) == {
        "ARTISTS": ["Adele", "Erroll Garner"],
        "originalyear": ["1992"],
        "comment": ["hello"],
        "comment:MusicBrainz Album Comment": ["deluxe"],
        "producer": ["Steve Albini"],
        "djmixer": ["X"],
        "performer:guitar": ["Joey Santiago"],
    }


def test_id3v23_splits_mbid_lists_but_not_names(tmp_path) -> None:
    ids = "11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222"
    path = build_mp3(
        tmp_path / "a.mp3",
        picard_id3(
            TPE1=TPE1(encoding=1, text="AC/DC"),
            **{
                "TXXX:MusicBrainz Artist Id": TXXX(
                    encoding=1, desc="MusicBrainz Artist Id", text=ids
                )
            },
        ),
        v2_version=3,
    )
    tags = read_tags(path)
    assert tags["artist"] == ("AC/DC",)
    assert tags["musicbrainz_artistid"] == tuple(ids.split("/"))


def test_untagged_mp3(tmp_path) -> None:
    meta = read_metadata(build_mp3(tmp_path / "a.mp3"))
    assert not meta.is_mbid_tagged
    assert meta.pictures == ()


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 284, 291), (284, 291)),
        (b"GIF89a" + struct.pack("<HH", 32, 16), (32, 16)),
        (b"\xff\xd8\xff\xc2\x00\x11\x08" + struct.pack(">HH", 10, 20) + b"\x00" * 9, (20, 10)),
        (b"\xff\xd8\xff\xe0\x00\x02\xff\xd9", (0, 0)),
        (b"RIFF....WEBP", (0, 0)),
        (b"", (0, 0)),
    ],
)
def test_image_size(data: bytes, size: tuple[int, int]) -> None:
    assert image_size(data) == size


def test_scan_treats_mp3_like_flac(tmp_path) -> None:
    lib = tmp_path / "music"
    folder = lib / "Pixies/Doolittle"
    build_flac(folder / "Pixies_Doolittle_01_Debaser.flac", good_tags(TOTALTRACKS="3"))
    tame = picard_id3(TIT2=TIT2(encoding=3, text="Tame"), TRCK=TRCK(encoding=3, text="2/3"))
    build_mp3(folder / "Pixies_Doolittle_02_Tame.mp3", tame)
    (folder / "Pixies_Doolittle_02_Tame.lrc").write_text("[00:00] la")
    wave = picard_id3(
        TIT2=TIT2(encoding=3, text="Wave of Mutilation"), TRCK=TRCK(encoding=3, text="3/3")
    )
    build_mp3(folder / "03 Wave of Mutilation.mp3", wave)

    result, issues = scan(settings_for(lib, tmp_path))
    assert result.files_total == 3
    assert {key for key in issues if not key[0].startswith("album.")} == {
        ("path.track_location", "Pixies/Doolittle/03 Wave of Mutilation.mp3")
    }
    issue = issues["path.track_location", "Pixies/Doolittle/03 Wave of Mutilation.mp3"]
    assert issue.details["expected"] == (
        "Pixies/Doolittle/Pixies_Doolittle_03_Wave of Mutilation.mp3"
    )


def test_replaygain_txxx_frames_keep_the_tag_name(tmp_path) -> None:
    frames = {"TXXX:REPLAYGAIN_ALBUM_GAIN": TXXX(desc="REPLAYGAIN_ALBUM_GAIN", text="-6.50 dB")}
    meta = read_metadata(build_mp3(tmp_path / "a.mp3", picard_id3(**frames)))
    assert meta.tags["replaygain_album_gain"] == ("-6.50 dB",)
