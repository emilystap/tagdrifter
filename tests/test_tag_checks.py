"""The local tag checks from roadmap 6a: MBIDs, naming tags, artist counts, duplicates, gaps."""

from pathlib import PurePosixPath

import pytest

from tagdrifter.checks import AlbumContext
from tagdrifter.checks.builtin_tags import (
    AlbumArtistMbidCount,
    ArtistMbidCount,
    CoverArt,
    DuplicateTracks,
    MissingMbids,
    MissingTracks,
    NamingTags,
    RecordingMbid,
    ReplayGain,
    RequiredTags,
)
from tagdrifter.config import Settings

from .test_checks import track_ctx

A1 = "11111111-1111-4111-8111-111111111111"
A2 = "55555555-5555-4555-8555-555555555555"
FULL_MBIDS = {
    "musicbrainz_albumid": "22222222-2222-4222-8222-222222222222",
    "musicbrainz_releasegroupid": "33333333-3333-4333-8333-333333333333",
    "musicbrainz_releasetrackid": "66666666-6666-4666-8666-666666666666",
    "musicbrainz_artistid": A1,
    "musicbrainz_albumartistid": A1,
}


def album(*tracks, path: str = "Pixies/Doolittle") -> AlbumContext:
    return AlbumContext(PurePosixPath(path), tracks, (), (), Settings())


def song(n: int, disc: int = 1, **tags):
    """Track n of a release, as Picard tags it."""
    defaults = {
        "musicbrainz_albumid": FULL_MBIDS["musicbrainz_albumid"],
        "musicbrainz_releasetrackid": f"66666666-6666-4666-8666-{disc:06d}{n:06d}",
        "tracknumber": str(n),
        "discnumber": str(disc),
    }
    return track_ctx(f"Pixies/Doolittle/{disc}-{n:02d}.flac", **(defaults | tags))


def test_recording_mbid() -> None:
    (issue,) = RecordingMbid().run(track_ctx(title="T"))
    assert "musicbrainz_trackid" in issue.message and issue.severity == "error"
    assert not list(RecordingMbid().run(track_ctx(musicbrainz_trackid=A1)))


def test_missing_mbids_names_each_tag() -> None:
    assert not list(MissingMbids().run(track_ctx(**FULL_MBIDS)))
    tags = dict(FULL_MBIDS)
    del tags["musicbrainz_releasetrackid"], tags["musicbrainz_albumartistid"]
    (issue,) = MissingMbids().run(track_ctx(**tags))
    assert issue.message == "Missing MusicBrainz IDs: release track, album artist"
    assert issue.details["tags"] == ["musicbrainz_releasetrackid", "musicbrainz_albumartistid"]


def test_standalone_recording_is_flagged() -> None:
    # A recording MBID but no release: listed deliberately (owner's choice).
    (issue,) = MissingMbids().run(track_ctx(musicbrainz_trackid=A1, musicbrainz_artistid=A1))
    assert issue.details["missing"] == ["release", "release group", "release track", "album artist"]


def test_naming_tags_explain_fallbacks() -> None:
    complete = {
        "albumartist": "Pixies",
        "artist": "Pixies",
        "album": "Doolittle",
        "title": "Debaser",
        "tracknumber": "1",
        "originaldate": "1989",
    }
    assert not list(NamingTags().run(track_ctx(**complete)))
    (issue,) = NamingTags().run(track_ctx(artist="Pixies", tracknumber="1"))
    assert issue.details["missing"] == ["album artist", "album", "title", "date"]
    assert 'the album folder becomes "Unknown Album"' in issue.message
    # Disc numbers only matter on multi-disc releases.
    (issue,) = NamingTags().run(track_ctx(**complete, totaldiscs="2"))
    assert issue.details["missing"] == ["disc number"]


def test_required_tags_empty_by_default() -> None:
    assert not list(RequiredTags().run(track_ctx()))


def test_artist_mbid_count_both_directions() -> None:
    collab = {"artists": ["ROSÉ", "Bruno Mars"], "musicbrainz_artistid": [A1, A2]}
    assert not list(ArtistMbidCount().run(track_ctx(**collab)))
    # Joined into one value by some taggers: still two MBIDs.
    assert not list(
        ArtistMbidCount().run(
            track_ctx(artists=["ROSÉ", "Bruno Mars"], musicbrainz_artistid=f"{A1}; {A2}")
        )
    )
    (issue,) = ArtistMbidCount().run(
        track_ctx(artists=["ROSÉ", "Bruno Mars"], musicbrainz_artistid=A1)
    )
    assert issue.message == "2 artist names but 1 MBIDs"
    (issue,) = ArtistMbidCount().run(track_ctx(artists="ROSÉ", musicbrainz_artistid=[A1, A2]))
    assert issue.message == "1 artist name but 2 MBIDs"


def test_artist_mbid_count_without_names_tag() -> None:
    # One MBID needs no ARTISTS tag to be unambiguous; several do.
    assert not list(ArtistMbidCount().run(track_ctx(artist="Pixies", musicbrainz_artistid=A1)))
    (issue,) = ArtistMbidCount().run(
        track_ctx(artist="ROSÉ & Bruno Mars", musicbrainz_artistid=[A1, A2])
    )
    assert issue.message == "2 artist MBIDs but no ARTISTS tag naming them"
    assert not list(ArtistMbidCount().run(track_ctx(artists="Pixies")))  # tags.mbids's job


def test_album_artist_mbid_count_reports_once_per_album() -> None:
    tags = {"albumartist": "Kander & Ebb", "albumartists": "Musical cast"}
    ctx = album(
        song(1, **tags, musicbrainz_albumartistid=[A1, A2]),
        song(2, **tags, musicbrainz_albumartistid=[A1, A2]),
    )
    (issue,) = AlbumArtistMbidCount().run(ctx)
    assert issue.subject == "Pixies/Doolittle"
    assert issue.message == "1 album artist name but 2 MBIDs (album artist 'Kander & Ebb')"
    assert not list(AlbumArtistMbidCount().run(album(song(1, musicbrainz_albumartistid=A1))))
    (issue,) = AlbumArtistMbidCount().run(
        album(song(1, albumartist="ROSÉ & Bruno Mars", musicbrainz_albumartistid=[A1, A2]))
    )
    assert issue.message == (
        "2 album artist MBIDs but no ALBUMARTISTS tag naming them "
        "(album artist 'ROSÉ & Bruno Mars')"
    )


def test_duplicate_tracks() -> None:
    assert not list(DuplicateTracks().run(album(song(1), song(2), song(1, disc=2))))
    copy = track_ctx(
        "Pixies/Doolittle/1-01 (copy).flac",
        musicbrainz_albumid=FULL_MBIDS["musicbrainz_albumid"],
        musicbrainz_releasetrackid="66666666-6666-4666-8666-000001000001",
        tracknumber="1",
        discnumber="1",
    )
    (issue,) = DuplicateTracks().run(album(song(1), song(2), copy))
    assert issue.details["duplicates"] == [["1-01 (copy).flac", "1-01.flac"]]  # one group
    assert issue.severity == "error"


def test_duplicate_positions_in_different_releases_are_not_duplicates() -> None:
    other = track_ctx(
        "Pixies/Doolittle/other.flac",
        musicbrainz_albumid="77777777-7777-4777-8777-777777777777",
        tracknumber="1",
    )
    assert not list(DuplicateTracks().run(album(song(1), other)))


def test_missing_tracks() -> None:
    complete = [song(n, totaltracks="3") for n in (1, 2, 3)]
    assert not list(MissingTracks().run(album(*complete)))
    (issue,) = MissingTracks().run(album(*(song(n, totaltracks="12") for n in (1, 2, 3, 5))))
    assert issue.message == "4 of 12 tracks (missing 4, 6-12)"
    assert issue.severity == "info"
    # "n/N" track numbers, and a whole disc missing.
    tracks = [
        song(1, tracknumber="1/2", totaldiscs="2"),
        song(2, tracknumber="2/2", totaldiscs="2"),
    ]
    (issue,) = MissingTracks().run(album(*tracks))
    assert issue.message == "Disc 2 missing"
    # Discs numbered past TOTALDISCS (a medium Picard skipped): both counted discs are there.
    tracks = [song(1, disc=2, totaldiscs="2"), song(1, disc=3, totaldiscs="2")]
    assert not list(MissingTracks().run(album(*tracks)))
    # Unknown totals: nothing to compare with.
    assert not list(MissingTracks().run(album(song(1), song(3))))


def cover(n: int, *pictures: str):
    return song(n, **{"~pictures": list(pictures)})


FRONT = "3 image/jpeg 1200x1200"


def test_cover_art_missing() -> None:
    assert not list(CoverArt().run(album(cover(1, FRONT), cover(2, FRONT))))
    (issue,) = CoverArt().run(album(cover(1), cover(2)))
    assert issue.message == "All tracks have no embedded cover"
    (issue,) = CoverArt().run(album(cover(1, FRONT), cover(2), cover(3)))
    assert issue.message == "2 of 3 tracks have no embedded cover"
    assert issue.details["files"] == {"no embedded cover": ["1-02.flac", "1-03.flac"]}
    (issue,) = CoverArt().run(album(cover(1)))
    assert issue.message == "The track has no embedded cover"


def test_cover_art_skips_files_read_before_pictures_were_recorded() -> None:
    assert not list(CoverArt().run(album(song(1), song(2))))  # no ~pictures: unknown
    (issue,) = CoverArt().run(album(song(1), cover(2)))
    assert issue.message == "The track has no embedded cover"


def test_cover_art_front_cover() -> None:
    back = "4 image/jpeg 1200x1200"
    (issue,) = CoverArt().run(album(cover(1, back), cover(2, back, FRONT)))
    assert issue.message == "1 of 2 tracks has no picture marked front cover"
    assert not list(CoverArt({"require_front_cover": False}).run(album(cover(1, back))))


def test_cover_art_min_size() -> None:
    small = "3 image/jpeg 300x300"
    tracks = album(cover(1, small), cover(2, small), cover(3, "3 image/png 0x0"))
    assert not list(CoverArt().run(tracks))  # off by default
    (issue,) = CoverArt({"min_size": 500}).run(tracks)
    assert issue.message == "2 of 3 tracks have a cover smaller than 500 px (300x300)"


CONSISTENT = ReplayGain({"expect": "consistent"})


def gain(n: int, track: str | None = "-7.00 dB", album_gain: str | None = "-6.50 dB", **tags):
    if track:
        tags["replaygain_track_gain"] = track
    if album_gain:
        tags["replaygain_album_gain"] = album_gain
    return song(n, **tags)


def test_replaygain_consistent_albums_pass() -> None:
    assert not list(CONSISTENT.run(album(song(1), song(2))))
    assert not list(CONSISTENT.run(album(gain(1), gain(2))))
    # Track gain alone is complete too, and a single track can't disagree with itself.
    assert not list(CONSISTENT.run(album(gain(1, album_gain=None), gain(2, album_gain=None))))
    assert not list(CONSISTENT.run(album(gain(1, track=None, album_gain=None))))


def test_replaygain_some_tracks_without() -> None:
    (issue,) = CONSISTENT.run(album(gain(1), song(2), song(3)))
    assert issue.message == "2 of 3 tracks have no track gain; 2 of 3 tracks have no album gain"
    assert issue.severity == "info"
    assert issue.details["missing"]["track gain"] == ["1-02.flac", "1-03.flac"]
    (issue,) = CONSISTENT.run(album(gain(1), gain(2, album_gain=None)))
    assert issue.message == "1 of 2 tracks has no album gain"


def test_replaygain_counts_r128_tags() -> None:
    tracks = album(gain(1, album_gain=None), song(2, r128_track_gain="0"))
    assert not list(CONSISTENT.run(tracks))


def test_replaygain_album_gain_differs() -> None:
    # Rounding isn't a difference.
    assert not list(CONSISTENT.run(album(gain(1), gain(2, album_gain="-6.5000 dB"))))
    (issue,) = CONSISTENT.run(album(gain(1), gain(2, album_gain="+1.25 dB")))
    assert issue.message == "Album gain differs between tracks (-6.50 to +1.25 dB)"


def test_replaygain_album_gain_may_differ_between_discs_and_releases() -> None:
    other_disc = song(1, disc=2, replaygain_track_gain="-7 dB", replaygain_album_gain="-3 dB")
    assert not list(CONSISTENT.run(album(gain(1), other_disc)))
    other_release = gain(2, album_gain="-3.00 dB", musicbrainz_albumid=A2)
    assert not list(CONSISTENT.run(album(gain(1), other_release)))


def test_replaygain_invalid_values() -> None:
    tracks = album(gain(1), gain(2, album_gain="NaN dB"), gain(3, album_gain="inf dB"))
    (issue,) = CONSISTENT.run(tracks)
    assert issue.message == "2 of 3 tracks have an invalid album gain (NaN dB, inf dB)"
    assert issue.details["invalid"]["album gain"] == {
        "1-02.flac": "NaN dB",
        "1-03.flac": "inf dB",
    }
    (issue,) = CONSISTENT.run(album(gain(1, track="NaN dB")))
    assert issue.message == "The track has an invalid track gain (NaN dB)"


def test_replaygain_flags_any_gain_tag_by_default() -> None:
    check = ReplayGain()
    assert not list(check.run(album(song(1), song(2, **{"volume level (replaygain)": "-7"}))))
    (issue,) = check.run(
        album(gain(1, replaygain_track_peak="0.9"), song(2, r128_album_gain="0"), song(3))
    )
    assert issue.message == (
        "2 of 3 tracks have gain tags: r128_album_gain, replaygain_album_gain, "
        "replaygain_track_gain, replaygain_track_peak"
    )
    assert issue.details["files"] == ["1-01.flac", "1-02.flac"]
    (issue,) = check.run(album(gain(1), gain(2)))
    assert issue.message.startswith("All tracks have gain tags: ")
    (issue,) = check.run(album(gain(1, album_gain=None), gain(2, album_gain=None)))
    assert issue.message == "All tracks have gain tags: replaygain_track_gain"


def test_replaygain_default_names_invalid_values() -> None:
    (issue,) = ReplayGain().run(album(gain(1, album_gain="NaN dB"), gain(2)))
    assert issue.message == (
        "All tracks have gain tags: replaygain_album_gain, replaygain_track_gain; "
        "1 of 2 tracks has an invalid album gain (NaN dB)"
    )
    assert issue.details["invalid"] == {"album gain": {"1-01.flac": "NaN dB"}}


def test_replaygain_rejects_an_unknown_expect() -> None:
    with pytest.raises(ValueError, match="expect"):
        list(ReplayGain({"expect": "all"}).run(album(song(1))))
