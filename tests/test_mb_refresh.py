"""Refresh an album's releases from MusicBrainz on request (6b-3), and cache pruning."""

import argparse
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tagdrifter import cli
from tagdrifter.db import Store
from tagdrifter.musicbrainz import refresh_releases
from tagdrifter.web import ScanRunner, create_app
from tagdrifter.web.mbsync import MusicBrainzSync

from .conftest import build_flac, good_tags, scan, settings_for
from .test_musicbrainz import client_with

FOLDER = "Pixies/Doolittle"
RELEASE = good_tags()["musicbrainz_albumid"]
OTHER = "9d0b1b6d-4a3a-4e04-9f39-7d8d1c6b9a01"  # sorts after RELEASE
DOOLITTLE = {"id": RELEASE, "title": "Doolittle", "media": []}


def album(tmp_path, **settings_kw):
    lib = tmp_path / "music"
    build_flac(lib / FOLDER / "01 Debaser.flac", good_tags())
    settings = settings_for(lib, tmp_path, **settings_kw)
    scan(settings)
    return settings


def ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="seconds")


def set_fetched(settings, mbid: str, fetched_at: str) -> None:
    with Store(settings.database_path) as store, store.conn:
        store.conn.execute(
            "UPDATE mb_releases SET fetched_at = ? WHERE mbid = ?", (fetched_at, mbid)
        )


# --- refresh_releases -------------------------------------------------------------------


def test_refresh_skips_releases_fetched_minutes_ago(tmp_path) -> None:
    settings = album(tmp_path)
    with Store(settings.database_path) as store:
        store.save_mb_release(RELEASE, DOOLITTLE, "2099-01-01")  # fetched just now
        client, opener = client_with()
        result = refresh_releases(store, client, [RELEASE.upper()])
        assert (result.skipped, result.fetched, opener.requests) == (1, 0, [])

        set_fetched(settings, RELEASE, ago(minutes=10))
        client, opener = client_with((200, DOOLITTLE | {"title": "Doolittle (fixed)"}), (404, None))
        result = refresh_releases(store, client, [RELEASE, OTHER, "not-an-mbid"])
        assert (result.fetched, result.not_found, result.skipped) == (1, 1, 0)
        assert len(opener.requests) == 2
        cached = store.mb_releases([RELEASE, OTHER])
        assert cached[RELEASE]["title"] == "Doolittle (fixed)" and cached[OTHER] is None
        # A forced refresh doesn't move the background refresh date earlier than normal.
        (row,) = store.conn.execute(
            "SELECT refresh_after FROM mb_releases WHERE mbid = ?", (RELEASE,)
        )
        assert row[0] > ago(days=-40)


def test_refresh_stops_at_the_first_error(tmp_path) -> None:
    settings = album(tmp_path)
    with Store(settings.database_path) as store:
        client, opener = client_with((500, None))
        result = refresh_releases(store, client, [RELEASE, OTHER])
        assert result.error and len(opener.requests) == 1 and not result.changed


# --- Pruning ----------------------------------------------------------------------------


def test_prune_releases_no_longer_in_the_library(tmp_path) -> None:
    settings = album(tmp_path)
    with Store(settings.database_path) as store:
        store.save_mb_release(RELEASE, DOOLITTLE, "2099-01-01")
        store.save_mb_release(OTHER, None, "2099-01-01")  # e.g. the album was retagged
        assert store.prune_mb_cache() == 1
        assert list(store.mb_releases()) == [RELEASE]

        # Most of the cache orphaned at once looks like a missing library: keep it.
        for i in range(30):
            store.save_mb_release(f"{i:08d}-0000-4000-8000-000000000000", None, "2099-01-01")
        assert store.prune_mb_cache() == 0
        assert len(store.mb_releases()) == 31


def test_prune_does_nothing_without_a_library(tmp_path) -> None:
    with Store(tmp_path / "td.db") as store:
        store.save_mb_release(OTHER, None, "2099-01-01")
        assert store.prune_mb_cache() == 0


# --- Release page -----------------------------------------------------------------------


def app_with(settings, *answers):
    client, opener = client_with(*answers)
    sync = MusicBrainzSync(settings, client)
    runner = ScanRunner(settings, mb_sync=sync)
    return TestClient(create_app(settings, runner)), runner, sync, opener


def test_refresh_button_refetches_and_rescans(tmp_path) -> None:
    settings = album(tmp_path, musicbrainz_enabled=True)
    with Store(settings.database_path) as store:
        store.save_mb_release(RELEASE, DOOLITTLE, "2099-01-01")
    set_fetched(settings, RELEASE, ago(days=3))
    c, runner, sync, opener = app_with(settings, (200, DOOLITTLE))
    with c:
        page = c.get("/library/album", params={"folder": FOLDER}).text
        assert "Refresh from MusicBrainz" in page and "Looked up 3 days ago" in page
        assert not opener.requests

        response = c.post(f"/library/refresh?folder={FOLDER}")
        assert response.status_code == 200
        assert "Refreshed 1 release. Rescanning…" in response.text
        assert response.headers["HX-Trigger"] == "scan-started"
        runner.wait(30)
        assert len(opener.requests) == 1
        with Store(settings.database_path) as store:
            assert len(store.recent_scans()) == 2  # the first scan, then the rescan
        assert not sync.status().running

        # Clicking again straight away asks MusicBrainz nothing and doesn't rescan.
        again = c.post(f"/library/refresh?folder={FOLDER}")
        assert "Looked up minutes ago" in again.text and "HX-Trigger" not in again.headers
        assert len(opener.requests) == 1

        assert c.post("/library/refresh?folder=Pixies/Bossanova").status_code == 404


def test_refresh_failure_pauses_lookups(tmp_path) -> None:
    settings = album(tmp_path, musicbrainz_enabled=True)
    c, _, sync, opener = app_with(settings, (500, None))
    with c:
        text = c.post(f"/library/refresh?folder={FOLDER}").text
        assert "Stopped: MusicBrainz answered 500" in text and "rename-problem" in text
        assert sync.status().paused_until is not None
        text = c.post(f"/library/refresh?folder={FOLDER}").text
        assert "paused after an error" in text and len(opener.requests) == 1


def test_no_refresh_button_when_lookups_are_off(tmp_path) -> None:
    settings = album(tmp_path)
    with TestClient(create_app(settings)) as c:
        assert (
            "Refresh from MusicBrainz"
            not in c.get("/library/album", params={"folder": FOLDER}).text
        )
        assert "MusicBrainz lookups are off." in c.post(f"/library/refresh?folder={FOLDER}").text


# --- CLI --------------------------------------------------------------------------------


def test_cli_refresh(tmp_path, monkeypatch, capsys) -> None:
    settings = album(tmp_path, musicbrainz_enabled=True)
    client, opener = client_with((200, DOOLITTLE), (404, None))
    monkeypatch.setattr(cli, "MusicBrainzClient", lambda **_: client)
    args = argparse.Namespace(target=[FOLDER, OTHER.upper()], no_scan=True)
    assert cli.cmd_refresh(settings, args) == 0
    assert len(opener.requests) == 2
    assert "1 looked up, 1 not found" in capsys.readouterr().out

    args = argparse.Namespace(target=["Pixies/Bossanova"], no_scan=True)
    assert cli.cmd_refresh(settings, args) == 2
    off = settings_for(tmp_path / "music", tmp_path)
    assert cli.cmd_refresh(off, argparse.Namespace(target=[FOLDER], no_scan=True)) == 2
