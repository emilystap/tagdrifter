from pathlib import PurePosixPath

import pytest

from tagdrifter.checks import AlbumContext
from tagdrifter.checks.builtin_tags import AlbumPlacement
from tagdrifter.config import Settings
from tagdrifter.core import PlacementIssueType, album_artist_matches_folder, audit_album_placement
from tagdrifter.core.mbrelease import MbRelease

from .conftest import make_meta
from .test_checks import track_ctx

T = PlacementIssueType


@pytest.mark.parametrize(
    ("folder", "tags", "expected"),
    [
        ("Pixies", {"albumartist": "Pixies"}, None),
        ("Kesha", {"albumartist": "Ke$ha"}, None),
        ("AC∕DC", {"albumartist": "AC/DC"}, None),
        ("Various Artists", {"albumartist": "Various Artists"}, None),
        (
            "Bikini Kill & Huggy Bear",
            {"albumartist": "Bikini Kill / Huggy Bear"},
            T.ARTIST_FOLDER_SANITIZATION,
        ),
        ("Bikini Kill & Huggy Bear", {"albumartist": "Bikini Kill & Huggy Bear"}, None),
        ("Pixies", {"albumartist": "Various Artists"}, T.COMPILATION_IN_ARTIST_FOLDER),
        ("Various Artists", {"albumartist": "Pixies"}, T.ARTIST_ALBUM_IN_VA_FOLDER),
        (
            "Bikini Kill",
            {"albumartist": "Bikini Kill & Huggy Bear"},
            T.COLLAB_UNDER_SINGLE_CONTRIBUTOR,
        ),
        ("Bikini Kill & Huggy Bear", {"albumartist": "Bikini Kill"}, T.SOLO_UNDER_COLLAB_FOLDER),
        ("Pixies", {"albumartist": "Breeders"}, T.ARTIST_NAME_MISMATCH),
        ("AC-DC", {"albumartist": "AC/DC"}, T.ARTIST_FOLDER_SANITIZATION),
        ("Pixies", {"artist": "Pixies"}, None),  # falls back to track artist
    ],
)
def test_audit_album_placement(folder, tags, expected) -> None:
    issue = audit_album_placement(folder, [make_meta(**tags)])
    assert (issue.issue_type if issue else None) == expected


def test_case_only_folder_difference_depends_on_setting() -> None:
    tracks = [make_meta(albumartist="Pixies")]
    issue = audit_album_placement("pixies", tracks, case_sensitive=True)
    assert issue is not None and issue.issue_type == T.ARTIST_FOLDER_CASE
    assert issue.expected_artist_folder == "Pixies"
    assert audit_album_placement("pixies", tracks, case_sensitive=False) is None


def test_case_only_match_against_sort_name_expects_that_spelling() -> None:
    tracks = [make_meta(albumartist="The Beatles", albumartistsort="Beatles, The")]
    issue = audit_album_placement("beatles, the", tracks)
    assert issue is not None and issue.issue_type == T.ARTIST_FOLDER_CASE
    assert issue.expected_artist_folder == "Beatles, The"


def test_expected_folder_and_unique_artists() -> None:
    tracks = [
        make_meta(albumartist="Various Artists", artist="A"),
        make_meta(albumartist="Various Artists", artist="B"),
    ]
    issue = audit_album_placement("Pixies", tracks)
    assert issue.expected_artist_folder == "Various Artists"
    assert issue.unique_track_artists == 2


def test_no_tags_returns_none() -> None:
    assert audit_album_placement("Pixies", [make_meta()]) is None


def test_album_artist_matches_folder() -> None:
    assert album_artist_matches_folder(make_meta(albumartist="Ke$ha"), "Kesha")
    assert album_artist_matches_folder(
        make_meta(albumartists="Bikini Kill; Huggy Bear", albumartist="BK + HB"),
        "Bikini Kill & Huggy Bear",
    )
    assert not album_artist_matches_folder(make_meta(albumartist="Pixies"), "Breeders")


# --- The artist's current name in MusicBrainz -------------------------------------------

CRASH = "d93a2f75-7c5d-46bf-8970-5881184e894f"


def former_name_release(*credits: tuple[str, str, str]) -> dict:
    """A release credited to artists under a former name (credited, current, joinphrase), as
    CRASH is to "Charli XCX", now Charli xcx. Picard's "variations only" keeps such credits."""
    return {
        "id": CRASH,
        "title": "CRASH",
        "media": [],
        "artist-credit": [
            {
                "name": credited,
                "joinphrase": join,
                "artist": {
                    "id": f"{i}0000000-0000-4000-8000-000000000000",
                    "name": current,
                    "sort-name": current,
                },
            }
            for i, (credited, current, join) in enumerate(credits, 1)
        ],
    }


def placement_issues(folder: str, albumartist: str, release: dict | None, **options):
    tags = {"albumartist": albumartist, "musicbrainz_albumid": CRASH}
    album = AlbumContext(
        PurePosixPath(f"{folder}/CRASH"),
        (track_ctx(f"{folder}/CRASH/01.flac", **tags),),
        (),
        (),
        Settings(),
        {CRASH: MbRelease.from_json(release)} if release else {},
    )
    return list(AlbumPlacement(options).run(album))


def test_folder_follows_the_artists_current_name() -> None:
    crash = former_name_release(("Charli XCX", "Charli xcx", ""))
    # Filed with the artist's other albums: fine, though the tags keep the former name.
    assert placement_issues("Charli xcx", "Charli XCX", crash) == []
    (issue,) = placement_issues("Charli XCX", "Charli XCX", crash)
    assert issue.details["issue_type"] == T.ARTIST_FOLDER_CASE
    assert issue.details["expected_artist_folder"] == "Charli xcx"
    assert issue.message == (
        "Artist Folder Letter Case Mismatch: album artist is 'Charli xcx' in MusicBrainz now "
        "(tagged 'Charli XCX'), expected folder 'Charli xcx'"
    )
    # A former name that isn't just letter case.
    b52s = former_name_release(("The B-52's", "The B-52s", ""))
    (issue,) = placement_issues("The B-52's", "The B-52's", b52s)
    # Names compare without punctuation, so it's the spelling that's off, not the artist.
    assert issue.details["issue_type"] == T.ARTIST_FOLDER_SANITIZATION
    assert issue.details["expected_artist_folder"] == "The B-52s"
    assert placement_issues("The B-52's", "The B-52's", b52s, artist_folder="album_artist") == []


def test_collaboration_under_current_names() -> None:
    split = former_name_release(("Charli XCX", "Charli xcx", " & "), ("Troye", "Troye Sivan", ""))
    assert placement_issues("Charli xcx & Troye Sivan", "Charli XCX & Troye", split) == []
    (issue,) = placement_issues("Charli xcx", "Charli XCX & Troye", split)
    assert issue.details["issue_type"] == T.COLLAB_UNDER_SINGLE_CONTRIBUTOR


def test_folder_by_tags_when_asked_or_not_looked_up() -> None:
    crash = former_name_release(("Charli XCX", "Charli xcx", ""))
    strict = {"artist_folder": "album_artist"}
    assert placement_issues("Charli XCX", "Charli XCX", crash, **strict) == []
    (issue,) = placement_issues("Charli xcx", "Charli XCX", crash, **strict)
    assert issue.message.startswith("Artist Folder Letter Case Mismatch: tagged album artist is")
    assert issue.details["current_album_artist"] == ""
    # Release not looked up yet: by the tags.
    assert placement_issues("Charli XCX", "Charli XCX", None) == []
    with pytest.raises(ValueError, match="artist_folder"):
        placement_issues("Charli xcx", "Charli XCX", crash, artist_folder="tags")
