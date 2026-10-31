"""Library browser (7a): album list and search from the file cache, and the release page."""

import pytest
from fastapi.testclient import TestClient

from tagdrifter.core.mbrelease import MbRelease, credit_text
from tagdrifter.db import Store
from tagdrifter.library import album_detail, album_summaries, search_albums
from tagdrifter.web import create_app

from .conftest import build_flac, good_tags, scan, settings_for
from .test_musicbrainz import MAENARAWANAI, fixture

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
