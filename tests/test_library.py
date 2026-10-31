"""Library browser (7a): album list and search from the file cache, and the release page;
other releases in the release group ranked by fit (6d)."""

import pytest
from fastapi.testclient import TestClient

from tagdrifter.core.mbrelease import MbRelease, credit_text
from tagdrifter.db import Store
from tagdrifter.library import (
    album_detail,
    album_summaries,
    compare_release_group,
    search_albums,
)
from tagdrifter.web import create_app
from tagdrifter.web.mbsync import MusicBrainzSync
from tagdrifter.web.runner import ScanRunner

from .conftest import build_flac, good_tags, scan, settings_for
from .test_musicbrainz import MAENARAWANAI, client_with, fixture

HX = {"HX-Request": "true", "HX-Target": "results"}
AG_FOLDER = "ATARASHII GAKKO!/Maenarawanai"


@pytest.fixture
def settings(library, tmp_path):
    return settings_for(library, tmp_path)


@pytest.fixture
def client(settings):
    scan(settings)
    with TestClient(create_app(settings)) as c:
        yield c


def text(response) -> str:
    assert response.status_code == 200, response.text
    return " ".join(response.text.split())


# --- Album list -----------------------------------------------------------------------------


def test_album_summaries_from_the_file_cache(settings) -> None:
    scan(settings)
    with Store(settings.database_path) as store:
        albums = {a.folder: a for a in album_summaries(store)}
    # loose.flac sits in the library root: not an album.
    assert list(albums) == ["Pixies/Doolittle", "Pixies/Hits"]
    doolittle = albums["Pixies/Doolittle"]
    assert (doolittle.artist, doolittle.year) == ("Pixies", "1989")
    assert doolittle.titles == ("Doolittle",)
    assert (doolittle.tracks, doolittle.unreadable) == (3, 1)
    assert doolittle.issues > 0
    assert albums["Pixies/Hits"].artist == "Various Artists"

    found = search_albums(list(albums.values()), "pixies HITS")
    assert [a.folder for a in found] == ["Pixies/Hits"]
    release_id = doolittle.release_ids[0]
    assert [a.folder for a in search_albums(list(albums.values()), release_id.upper())] == [
        "Pixies/Doolittle",
        "Pixies/Hits",  # good_tags() gives every file the same release MBID
    ]
    assert search_albums(list(albums.values()), "nevermind") == []


def test_library_page_and_search(client) -> None:
    page = text(client.get("/library"))
    assert 'href="/library" aria-current="page"' in page
    assert "1–2 of 2 albums" in page
    assert 'href="/library/album?folder=Pixies/Doolittle"' in page
    assert "1 unreadable" in page

    partial = text(client.get("/library", params={"q": "hits"}, headers=HX))
    assert "<html" not in partial
    assert "1–1 of 1 album" in partial and "(2 in the library)" in partial
    assert "Pixies/Doolittle" not in partial
    assert "No albums match." in text(client.get("/library", params={"q": "zzz"}, headers=HX))


def test_library_before_any_scan(settings) -> None:
    with TestClient(create_app(settings)) as c:
        assert "No albums yet" in text(c.get("/library"))


# --- Release page ---------------------------------------------------------------------------


def test_album_page_shows_tracks_tags_and_issues(client) -> None:
    page = text(client.get("/library/album", params={"folder": "Pixies/Doolittle"}))
    assert '<h1 class="album-title">Doolittle</h1>' in page
    assert "Debaser" in page and "Tame" in page
    assert "Unreadable:" in page and "broken.flac" in page
    # 02 Tame.flac has no release group MBID: an album tag that differs between tracks.
    assert '<tr class="differs"> <th scope="row"><code>musicbrainz_releasegroupid</code>' in page
    assert '(not set)</span></mark> <span class="muted small">1 track</span>' in page
    assert 'id="issues"' in page and "path.track_location" in page
    assert "https://musicbrainz.org/release/" in page
    assert "MusicBrainz lookups are off" in page  # not looked up, lookups disabled
    # Track tags: what isn't shared, per track.
    assert "<code>musicbrainz_releasetrackid</code>" in page


def test_album_page_unknown_folder(client) -> None:
    response = client.get("/library/album", params={"folder": "Pixies/Bossanova"})
    assert response.status_code == 404
    assert "No audio files in" in response.text


def test_issue_cards_link_to_the_library(client) -> None:
    page = text(client.get("/issues"))
    assert 'href="/library/album?folder=Pixies/Doolittle"' in page
    assert 'href="/library/album?folder=loose.flac"' not in page


def test_musicbrainz_tracklist_beside_the_files(tmp_path) -> None:
    release = MbRelease.from_json(fixture("maenarawanai"))
    lib = tmp_path / "music"
    for position in (1, 2):
        t = release.track_at(1, position)
        tags = {
            "musicbrainz_albumid": MAENARAWANAI,
            "musicbrainz_releasetrackid": t.id,
            "musicbrainz_trackid": t.recording_id,
            "tracknumber": str(position),
            "discnumber": "1",
            "title": (t.title_alias or t.title) + (" (edit)" if position == 2 else ""),
            "album": "Maenarawanai",
            "artist": credit_text(t.artists),
            "albumartist": "ATARASHII GAKKO!",
        }
        build_flac(lib / AG_FOLDER / f"{position:02d}.flac", tags, length_ms=t.length_ms + 9000)
    settings = settings_for(lib, tmp_path)
    scan(settings)
    with Store(settings.database_path) as store:
        store.save_mb_release(MAENARAWANAI, fixture("maenarawanai"), "2099-01-01")
        album = album_detail(store, AG_FOLDER)

    (view,) = album.releases
    rows = view.rows
    assert len(rows) == len(release.tracks)  # every MusicBrainz track; the 2 files matched
    assert [r.file.name if r.file else None for r in rows[:3]] == ["01.flac", "02.flac", None]
    assert not rows[0].title_differs and rows[1].title_differs
    assert rows[0].length_diff_s == 9
    assert album.release_group_ids == (release.release_group_id,)

    with TestClient(create_app(settings)) as c:
        page = text(c.get("/library/album", params={"folder": AG_FOLDER}))
    assert "MusicBrainz tracklist" in page and "No file" in page
    assert "<mark>+9 s</mark>" in page
    assert ' (edit)</mark> <span class="muted">in the tags</span>' in page
    assert f"https://musicbrainz.org/release-group/{release.release_group_id}" in page


def test_release_not_looked_up_or_unknown(tmp_path) -> None:
    lib = tmp_path / "music"
    build_flac(lib / "Pixies/Doolittle/01.flac", good_tags())
    settings = settings_for(lib, tmp_path, musicbrainz_enabled=True)
    scan(settings)
    with TestClient(create_app(settings)) as c:
        page = text(c.get("/library/album", params={"folder": "Pixies/Doolittle"}))
        assert "Not looked up yet" in page
        with Store(settings.database_path) as store:
            (release_id,) = store.library_release_ids()
            store.save_mb_release(release_id, None, "2099-01-01")
        page = text(c.get("/library/album", params={"folder": "Pixies/Doolittle"}))
        assert "MusicBrainz has no release with this MBID" in page


# --- Other releases in the release group (6d) -----------------------------------------------

CAKE_GROUP = "34cc7616-0e7e-34aa-a42d-bc9c17755f18"
CAKE_2023 = "045161e0-16c7-4278-873e-5b649303ac21"  # Deluxe Edition, 15 tracks (tagged)
CAKE_1998 = "d0c81325-1494-4afe-84be-3466f3858ccc"  # US CD, 13 tracks: the files' lengths
CAKE_FOLDER = "CAKE/Prolonging the Magic"


def cake_album(tmp_path, **settings_kw):
    """Tracks 9, 11 and 14, tagged with the 2023 release but with the 1998 CD's lengths
    (14 isn't on the CD), like the owner's files."""
    group = fixture("prolonging_the_magic_group")["releases"]
    new, old = (
        MbRelease.from_json(next(r for r in group if r["id"] == mbid))
        for mbid in (CAKE_2023, CAKE_1998)
    )
    lib = tmp_path / "music"
    for position in (9, 11, 14):
        t = new.track_at(1, position)
        length = (old.track_at(1, position) or t).length_ms
        tags = {
            "musicbrainz_albumid": CAKE_2023,
            "musicbrainz_releasegroupid": CAKE_GROUP,
            "musicbrainz_releasetrackid": t.id,
            "musicbrainz_trackid": t.recording_id,
            "tracknumber": str(position),
            "discnumber": "1",
            "title": t.title,
            "album": "Prolonging the Magic",
            "artist": "CAKE",
            "albumartist": "CAKE",
        }
        build_flac(lib / CAKE_FOLDER / f"{position:02d}.flac", tags, length_ms=length)
    settings = settings_for(lib, tmp_path, **settings_kw)
    scan(settings)
    return settings


def test_releases_ranked_by_how_their_lengths_fit(tmp_path) -> None:
    settings = cake_album(tmp_path)
    with Store(settings.database_path) as store:
        album = album_detail(store, CAKE_FOLDER)
        assert compare_release_group(store, album, CAKE_GROUP, 3000) is None  # not looked up
        store.save_mb_release_group(CAKE_GROUP, fixture("prolonging_the_magic_group"))
        comparison = compare_release_group(store, album, CAKE_GROUP, 3000)

    assert (comparison.files, comparison.release_count) == (3, 4)
    best, *rest = comparison.fits
    # The 1998 CD: tracks 9 and 11 fit (11 matched by position: a different recording),
    # track 14 isn't on it.
    assert best.release.id == CAKE_1998 and not best.tagged
    assert (best.fits, best.off, best.unmatched, best.no_file) == (2, 0, 1, 11)
    assert best.formats == "CD" and best.raw("country") == "US"
    tagged = next(f for f in rest if f.tagged)
    assert tagged.release.id == CAKE_2023
    assert (tagged.fits, tagged.off, tagged.unmatched) == (1, 2, 0)
    assert tagged.worst_ms > 20_000  # Let Me Go: 3:30 vs 3:56
    assert [f.fits for f in comparison.fits] == sorted(
        (f.fits for f in comparison.fits), reverse=True
    )


def test_compare_with_other_releases_on_the_album_page(tmp_path) -> None:
    settings = cake_album(tmp_path, musicbrainz_enabled=True)
    client, opener = client_with((200, fixture("prolonging_the_magic_group")))
    runner = ScanRunner(settings, mb_sync=MusicBrainzSync(settings, client))
    lookup = f"/library/release-group?folder={CAKE_FOLDER}&id={CAKE_GROUP}"
    with TestClient(create_app(settings, runner)) as c:
        page = text(c.get("/library/album", params={"folder": CAKE_FOLDER}))
        assert f'id="group-{CAKE_GROUP}"' in page and "Compare with other releases" in page
        assert not opener.requests  # nothing is looked up until someone asks

        section = text(c.post(lookup))
        (request,) = opener.requests
        assert f"release?release-group={CAKE_GROUP}&inc=recordings+media" in request.full_url
        assert "<html" not in section
        assert section.index(CAKE_1998) < section.index(CAKE_2023)
        assert "2 of 3</strong>" in section and "tagged</span>" in section
        assert "Look up again" in section

        # Again within minutes: nothing sent, the cached lookup is shown.
        again = text(c.post(lookup))
        assert len(opener.requests) == 1 and "Looked up a few minutes ago" in again
        # The album page shows the cached comparison without asking MusicBrainz.
        page = text(c.get("/library/album", params={"folder": CAKE_FOLDER}))
        assert "2 of 3</strong>" in page and len(opener.requests) == 1

        assert (
            c.post(f"/library/release-group?folder={CAKE_FOLDER}&id={CAKE_2023}").status_code == 404
        )


def test_failed_lookup_pauses_lookups(tmp_path) -> None:
    settings = cake_album(tmp_path, musicbrainz_enabled=True)
    client, opener = client_with((500, None))
    sync = MusicBrainzSync(settings, client)
    with TestClient(create_app(settings, ScanRunner(settings, mb_sync=sync))) as c:
        section = text(c.post(f"/library/release-group?folder={CAKE_FOLDER}&id={CAKE_GROUP}"))
        assert "MusicBrainz answered 500" in section and "Compare with other releases" in section
        assert sync.status().paused_until is not None
        section = text(c.post(f"/library/release-group?folder={CAKE_FOLDER}&id={CAKE_GROUP}"))
        assert "paused after an error" in section and len(opener.requests) == 1


def test_no_lookup_button_when_lookups_are_off(tmp_path) -> None:
    settings = cake_album(tmp_path)
    with TestClient(create_app(settings)) as c:
        page = text(c.get("/library/album", params={"folder": CAKE_FOLDER}))
        assert "group-" + CAKE_GROUP not in page
        section = text(c.post(f"/library/release-group?folder={CAKE_FOLDER}&id={CAKE_GROUP}"))
        assert "MusicBrainz lookups are off." in section
