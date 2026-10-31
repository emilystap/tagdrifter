"""Bulk renames (roadmap 5f): several albums planned and applied together, one journal batch
per album, from the web UI and the CLI."""

import re
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tagdrifter import cli, renamer
from tagdrifter.db import IssueFilter, Store
from tagdrifter.renamer import RenameError
from tagdrifter.web import create_app

from .conftest import scan, settings_for
from .test_renamer import ALBUM, listing, track

ROSA = "Pixies/surfer rosa (old)"
ROSA_TRACK = "Pixies/Surfer Rosa/Pixies_Surfer Rosa_01_Bone Machine.flac"


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    """Doolittle in a misnamed folder (as in test_renamer)."""
    lib = tmp_path / "music"
    track(lib, f"{ALBUM}/01 Debaser.flac")
    track(lib, f"{ALBUM}/02 Tame.flac", TITLE="Tame", TRACKNUMBER="2")
    return lib


@pytest.fixture
def settings(lib: Path, tmp_path: Path):
    return settings_for(lib, tmp_path)


@pytest.fixture
def two_albums(lib: Path) -> Path:
    """Doolittle plus a second release with a wrong folder name."""
    track(
        lib,
        f"{ROSA}/01 Bone Machine.flac",
        TITLE="Bone Machine",
        ALBUM="Surfer Rosa",
        DATE="1988",
        musicbrainz_albumid="55555555-5555-4555-8555-555555555555",
    )
    return lib


def plans_for(settings, *albums):
    with Store(settings.database_path) as store:
        return renamer.plan_albums(settings, store, albums)


def test_candidates_and_plans_keep_refusals_per_album(settings, two_albums) -> None:
    track(two_albums, "Pixies/untagged/x.flac", musicbrainz_trackid="")
    scan(settings)
    with Store(settings.database_path) as store:
        assert store.rename_candidates() == [ALBUM, ROSA]  # the untagged album isn't offered
        assert store.rename_candidates(IssueFilter(text="surfer")) == [ROSA]
    plans = plans_for(settings, ROSA, ALBUM, "Pixies/untagged", ROSA)
    assert [(p.album, p.ok) for p in plans] == [
        (ROSA, True),
        (ALBUM, True),
        ("Pixies/untagged", False),
    ]


def test_albums_moving_into_the_same_folder_are_refused(settings, lib) -> None:
    track(lib, "Pixies/doolittle copy/03 Wave.flac", TITLE="Wave", TRACKNUMBER="3")
    scan(settings)
    first, second = plans_for(settings, ALBUM, "Pixies/doolittle copy")
    assert first.ok
    assert second.problems == [
        f"{ALBUM} also moves tracks into the same folder; rename them one at a time."
    ]


def test_one_batch_per_album_and_a_failure_skips_only_its_album(settings, two_albums) -> None:
    scan(settings)
    doolittle, _ = plans_for(settings, ALBUM, ROSA)
    seen = []
    with Store(settings.database_path) as store:
        result = renamer.apply_albums(
            settings,
            store,
            [(ALBUM, doolittle.fingerprint), (ROSA, "stale")],
            progress=lambda done, total, album: seen.append((done, total, album)),
        )
        assert [b["album"] for b in store.rename_batches()] == [ALBUM]
    assert [a for a, _ in result.applied] == [ALBUM] and result.moved == 3
    assert result.failed == [(ROSA, "The folder changed since the preview; preview it again.")]
    assert result.partial is None and not result.skipped
    assert seen == [(0, 2, ALBUM), (1, 2, ROSA), (2, 2, "")]
    files = listing(two_albums)
    assert "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac" in files
    assert f"{ROSA}/01 Bone Machine.flac" in files  # untouched


def test_stop_between_albums(settings, two_albums) -> None:
    scan(settings)
    stop = threading.Event()
    stop.set()
    plans = [(p.album, p.fingerprint) for p in plans_for(settings, ALBUM, ROSA)]
    with Store(settings.database_path) as store:
        result = renamer.apply_albums(settings, store, plans, stop=stop)
    assert result.skipped == [ALBUM, ROSA] and not result.applied


def test_a_half_moved_album_stops_the_run(settings, two_albums, monkeypatch) -> None:
    scan(settings)
    plans = [(p.album, p.fingerprint) for p in plans_for(settings, ALBUM, ROSA)]

    def stuck(*args):
        raise RenameError("Rolling back also failed", partial=True)

    monkeypatch.setattr(renamer, "apply_plan", stuck)
    with Store(settings.database_path) as store:
        result = renamer.apply_albums(settings, store, plans)
    assert result.partial == ALBUM and result.skipped == [ROSA]


# --- Web UI -----------------------------------------------------------------------------


def test_web_preview_apply_and_undo_one_album(settings, two_albums) -> None:
    settings = replace(settings, fix_enabled=True)
    scan(settings)
    before = listing(two_albums)
    with TestClient(create_app(settings)) as client:
        page = client.get("/issues").text
        assert "all 2 albums with renames matching these filters" in page
        assert f'name="album" value="{ROSA}" form="bulk-form"' in page

        picked = client.post("/rename/bulk/preview", data={"album": [ROSA]}).text
        assert "1 album ready to rename" in picked and ALBUM not in picked

        preview = client.post("/rename/bulk/preview", data={"all": "1", "f_artist": "Pixies"})
        assert "2 albums ready to rename" in preview.text
        assert "3 tracks, 2 folders renamed" in preview.text
        plans = re.findall(r'name="plan" value="([^"]+)"', preview.text)
        assert [p.split(" ", 1)[1] for p in plans] == [ALBUM, ROSA]
        bare = client.get("/rename/preview", params={"album": ROSA, "bare": 1}).text
        assert "Bone Machine" in bare and "rename-actions" not in bare

        started = client.post("/rename/bulk/apply", data={"plan": plans})
        assert "bulk-status" in started.text
        client.app.state.bulk.wait(10)
        done = client.get("/rename/bulk/status", params={"watching": 1})
        assert "2 albums renamed" in done.text
        assert done.headers.get("HX-Trigger") == "scan-started"
        client.app.state.runner.wait(10)
        assert "2 albums renamed" in client.get("/renames").text  # the outcome stays visible
        assert ROSA_TRACK in listing(two_albums)

        with Store(settings.database_path) as store:
            batches = store.rename_batches()
        assert sorted(b["album"] for b in batches) == [ALBUM, ROSA]
        rosa = next(b for b in batches if b["album"] == ROSA)
        client.post(f"/renames/{rosa['id']}/undo")  # undo one album only
        client.app.state.runner.wait(10)
    files = listing(two_albums)
    assert f"{ROSA}/01 Bone Machine.flac" in files
    assert "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac" in files
    assert files != before


def test_web_refusals(settings, two_albums) -> None:
    settings = replace(settings, fix_enabled=True)
    scan(settings)
    with TestClient(create_app(settings)) as client:
        assert "No albums selected" in client.post("/rename/bulk/preview", data={}).text
        assert "Nothing selected" in client.post("/rename/bulk/apply", data={}).text
        with client.app.state.runner.paused():
            busy = client.post("/rename/bulk/apply", data={"plan": [f"abc {ALBUM}"]})
        assert "Another rename is in progress" in busy.text


def test_web_bulk_is_off_by_default(settings, two_albums) -> None:
    scan(settings)
    with TestClient(create_app(settings)) as client:
        assert "bulk-form" not in client.get("/issues").text
        assert client.post("/rename/bulk/preview", data={"all": "1"}).status_code == 404
        assert client.post("/rename/bulk/apply", data={}).status_code == 404


# --- CLI --------------------------------------------------------------------------------


def test_cli_rename(settings, two_albums, capsys, monkeypatch) -> None:
    scan(settings)
    db = ["--library", str(settings.library_path), "--db", str(settings.database_path)]
    before = listing(two_albums)
    assert cli.main([*db, "rename", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"ok    {ROSA} -> Pixies/Surfer Rosa (1 track)" in out
    assert "2 albums to rename (3 tracks), 0 skipped." in out
    assert listing(two_albums) == before

    assert cli.main([*db, "rename", "--yes"]) == 2
    assert "renaming is off" in capsys.readouterr().err

    monkeypatch.setenv("TAGDRIFTER_FIX", "true")
    assert cli.main([*db, "rename", "--album", ROSA, "--yes", "--no-scan"]) == 0
    assert "Renamed 1 album (2 changes); 0 failed, 0 not attempted." in capsys.readouterr().out
    files = listing(two_albums)
    assert ROSA_TRACK in files
    assert f"{ALBUM}/02 Tame.flac" in files  # not selected
