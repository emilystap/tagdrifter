"""MusicBrainz text checks (6b-2): album, track titles and artist credits, against the
recorded Maenarawanai release (an artist credited by its English alias, translated titles)."""

from pathlib import PurePosixPath

import pytest

from tagdrifter.checks import AlbumContext
from tagdrifter.checks.builtin_musicbrainz import AlbumTitle, ArtistCredit, TrackTitle
from tagdrifter.config import Settings
from tagdrifter.core.mbrelease import MbCredit, MbRelease, credit_matches, credit_text, translated

from .test_checks import track_ctx
from .test_musicbrainz import MAENARAWANAI, fixture

AG = "12a43a61-e31a-4cac-92a6-c95ce9cdfe3c"  # 新しい学校のリーダーズ, alias ATARASHII GAKKO!
H_ZETTRIO = "91fe3bfa-8209-434a-9584-51e47557cf73"
RELEASE_GROUP = "83bd4df3-898e-496d-bef9-46981e008da5"


def release() -> MbRelease:
    return MbRelease.from_json(fixture("maenarawanai"))


def ag_track(position: int, **tags):
    """A file tagged by the owner's Picard (English translations) from the fixture."""
    t = release().track_at(1, position)
    defaults = {
        "musicbrainz_albumid": MAENARAWANAI,
        "musicbrainz_releasetrackid": t.id,
        "musicbrainz_trackid": t.recording_id,
        "musicbrainz_releasegroupid": RELEASE_GROUP,
        "musicbrainz_artistid": [c.artist_id for c in t.artists],
        "musicbrainz_albumartistid": AG,
        "tracknumber": str(position),
        "discnumber": "1",
        "title": t.title_alias or t.title,
        "album": "Maenarawanai",
        "artist": credit_text(t.artists),
        "artists": [c.picard_name for c in t.artists],
        "albumartist": "ATARASHII GAKKO!",
        "albumartists": "ATARASHII GAKKO!",
    }
    return track_ctx(f"ATARASHII GAKKO!/Maenarawanai/{position:02d}.flac", **(defaults | tags))


def ag_album(*tracks) -> AlbumContext:
    folder = PurePosixPath("ATARASHII GAKKO!/Maenarawanai")
    return AlbumContext(folder, tracks, (), (), Settings(), {MAENARAWANAI: release()})


CHECKS = (AlbumTitle(), TrackTitle(), ArtistCredit())


def issues(album: AlbumContext) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for check in CHECKS:
        for i in check.run(album):
            found.setdefault(check.id, []).append(f"{i.subject}: {i.message}")
    return found


# --- Parsing ----------------------------------------------------------------------------


def test_parse_credits_aliases_and_release_group() -> None:
    r = release()
    assert (r.release_group_id, r.release_group_title) == (RELEASE_GROUP, "マエナラワナイ")
    (ag,) = r.artists
    assert (ag.artist_id, ag.name, ag.credited, ag.alias) == (
        AG,
        "新しい学校のリーダーズ",
        "ATARASHII GAKKO!",
        "ATARASHII GAKKO!",
    )
    feat = r.track_at(1, 3)
    assert credit_text(feat.artists) == "ATARASHII GAKKO! feat. H ZETTRIO"
    assert feat.artists[1].artist_id == H_ZETTRIO
    # The recording alias "dokubana" is primary and en; "saisyu jinrui" isn't primary.
    assert r.track_at(1, 6).title_alias == "dokubana"
    assert r.track_at(1, 9).title_alias == ""


def alias(name: str, locale: str, *, primary: bool = True, type_: str = "Artist name") -> dict:
    return {"name": name, "locale": locale, "primary": primary, "type": type_}


def test_translated_follows_picard_locale_rules() -> None:
    assert translated([alias("A", "en", primary=False)]) == ""
    assert translated([alias("Fr", "fr"), alias("En", "en_US")]) == "En"  # language match
    assert translated([alias("Lang", "en"), alias("Full", "en")]) == "Lang"  # first wins
    assert (
        translated([alias("Search", "en", type_="Search hint"), alias("Name", "en")], "Artist name")
        == "Name"
    )
    assert translated(None) == ""


def test_credit_matches_any_name_per_artist() -> None:
    credit = (
        MbCredit("1", "新しい学校のリーダーズ", "ATARASHII GAKKO!", " feat. ", "ATARASHII GAKKO!"),
        MbCredit("2", "H ZETTRIO", "H ZETTRIO", ""),
    )
    assert credit_matches("ATARASHII GAKKO! feat. H ZETTRIO", credit)
    assert credit_matches("新しい学校のリーダーズ feat. H ZETTRIO", credit)
    assert not credit_matches("ATARASHII GAKKO! & H ZETTRIO", credit)
    assert not credit_matches("ATARASHII GAKKO!", credit)
    assert not credit_matches("anything", ())


# --- Checks -----------------------------------------------------------------------------


def test_picard_tagged_album_has_no_issues() -> None:
    album = ag_album(*(ag_track(n) for n in range(1, 11)))
    assert issues(album) == {}


def test_untranslated_or_standardized_names_are_accepted() -> None:
    album = ag_album(
        ag_track(
            3,
            artist="新しい学校のリーダーズ feat. H ZETTRIO",
            artists=["新しい学校のリーダーズ", "H ZETTRIO"],
            albumartist="新しい学校のリーダーズ",
        ),
        ag_track(1, title="sekigae Gattuso"),
    )
    assert issues(album) == {}


def test_track_title() -> None:
    album = ag_album(
        ag_track(1, title="Sekigae Gattuso"),
        ag_track(2, title="席替ガットゥーゾ"),  # the recording's title, not what Picard writes
        ag_track(4, title=""),  # missing: tags.naming's job
    )
    assert issues(album)["mb.track_title"] == [
        "ATARASHII GAKKO!/Maenarawanai/01.flac: Title 'Sekigae Gattuso' vs "
        "'sekigae Gattuso' in MusicBrainz (letter case)",
        "ATARASHII GAKKO!/Maenarawanai/02.flac: Title '席替ガットゥーゾ' vs "
        "'kimiwaina’17' in MusicBrainz",
    ]


def test_track_from_another_release_is_left_to_track_mismatch() -> None:
    stray = ag_track(
        1, title="Something else", musicbrainz_releasetrackid="22222222-2222-4222-8222-222222222222"
    )
    assert "mb.track_title" not in issues(ag_album(stray))


def test_album_title_and_release_group() -> None:
    album = ag_album(
        ag_track(1, album="マエナラワナイ"),  # the release group's title, not the release's
        ag_track(2, album="マエナラワナイ"),
        ag_track(3, musicbrainz_releasegroupid="33333333-3333-4333-8333-333333333333"),
    )
    (issue,) = issues(album)["mb.album"]
    assert issue == (
        "ATARASHII GAKKO!/Maenarawanai: Album 'マエナラワナイ' vs 'Maenarawanai' in "
        "MusicBrainz; Release group MBID 33333333-3333-4333-8333-333333333333 vs "
        f"{RELEASE_GROUP} ('マエナラワナイ') in MusicBrainz"
    )


def test_track_artist_credit() -> None:
    album = ag_album(
        ag_track(3, artist="ATARASHII GAKKO! & H ZETTRIO"),
        ag_track(4, artists=["ATARASHII GAKKO!", "H ZETTRIO"], musicbrainz_artistid=[AG, AG]),
        ag_track(
            5,
            musicbrainz_artistid=[AG, H_ZETTRIO],
            artists=["H ZETTRIO", "ATARASHII GAKKO!"],
        ),
    )
    assert issues(album)["mb.artist_credit"] == [
        "ATARASHII GAKKO!/Maenarawanai/03.flac: Artist 'ATARASHII GAKKO! & H ZETTRIO' vs "
        "'ATARASHII GAKKO! feat. H ZETTRIO' in MusicBrainz",
        "ATARASHII GAKKO!/Maenarawanai/04.flac: ARTISTS 'ATARASHII GAKKO!; H ZETTRIO' vs "
        "'ATARASHII GAKKO!' in MusicBrainz; Artist MBIDs: in a different order from MusicBrainz",
        "ATARASHII GAKKO!/Maenarawanai/05.flac: ARTISTS 'H ZETTRIO; ATARASHII GAKKO!' vs "
        "'ATARASHII GAKKO!' in MusicBrainz; Artist MBIDs: 1 not in the MusicBrainz credit",
    ]


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        ([H_ZETTRIO, AG], "in a different order from MusicBrainz"),
        ([AG], "none for H ZETTRIO"),
    ],
)
def test_artist_mbids(ids, message) -> None:
    (issue,) = issues(ag_album(ag_track(3, musicbrainz_artistid=ids)))["mb.artist_credit"]
    assert issue.endswith(f"Artist MBIDs: {message}")


def test_album_artist_reported_once_per_album() -> None:
    album = ag_album(*(ag_track(n, albumartist="Atarashii Gakko!") for n in (1, 2, 4)))
    assert issues(album)["mb.artist_credit"] == [
        "ATARASHII GAKKO!/Maenarawanai: Album artist 'Atarashii Gakko!' vs "
        "'ATARASHII GAKKO!' in MusicBrainz (letter case)"
    ]
