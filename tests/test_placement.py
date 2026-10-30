import pytest

from tagdrifter.core import PlacementIssueType, album_artist_matches_folder, audit_album_placement

from .conftest import make_meta

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
    assert issue is not None and issue.issue_type == T.ARTIST_FOLDER_SANITIZATION
    assert audit_album_placement("pixies", tracks, case_sensitive=False) is None


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
