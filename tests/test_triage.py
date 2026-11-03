import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tagdrifter.db import Store
from tagdrifter.renamer import RenameError, undo_batch
from tagdrifter.triage import (
    BLOCKED,
    CONFIRM,
    READY,
    WAITING,
    confirm_album,
    move_album,
    plan_triage,
    validate_settings,
)

from .conftest import build_flac, good_tags, scan, settings_for
from .test_renamer import listing

DEBASER = "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac"
TAME = "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac"


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    lib = tmp_path / "libraries" / "music"
    build_flac(
        lib / "Pixies/Surfer Rosa/Pixies_Surfer Rosa_01_Bone Machine.flac",
        good_tags(
            ALBUM="Surfer Rosa",
            TITLE="Bone Machine",
            musicbrainz_albumid=str(uuid.uuid4()),
        ),
    )
    return lib


@pytest.fixture
def triage(tmp_path: Path) -> Path:
    path = tmp_path / "libraries" / "triage"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def settings(lib: Path, triage: Path, tmp_path: Path):
    settings = settings_for(lib, tmp_path, triage_path=triage, triage_settle=0)
    scan(settings)
    return settings


def plan(settings, **kw):
    with Store(settings.database_path) as store:
        return plan_triage(settings, store, **kw)


def only(settings, **kw):
    albums = plan(settings, **kw).albums
    assert len(albums) == 1, albums
    return albums[0]


def doolittle(triage: Path, folder: str = "new rip") -> None:
    build_flac(triage / folder / "track01.flac", good_tags(TOTALTRACKS="2"))
    build_flac(
        triage / folder / "track02.flac", good_tags(TITLE="Tame", TRACKNUMBER="2", TOTALTRACKS="2")
    )
    (triage / folder / "track01.lrc").write_text("[00:00] la")
    (triage / folder / "cover.jpg").write_bytes(b"jpg")
    (triage / folder / "Scans").mkdir()
    (triage / folder / "Scans" / "back.jpg").write_bytes(b"jpg")


def test_moves_a_clean_album_with_its_files_and_undoes_it(settings, lib, triage) -> None:
    doolittle(triage)
    album = only(settings)
    assert album.status == READY, (album.problems, album.issues, album.reasons)
    assert album.label == "Pixies: Doolittle"
    assert album.target == "Pixies/Doolittle"

    with Store(settings.database_path) as store:
        result = move_album(settings, store, album)
        assert listing(triage) == []
        assert not (triage / "new rip").exists()
        assert triage.is_dir()
        assert sorted(p for p in listing(lib) if "Doolittle" in p) == [
            "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac",
            "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.lrc",
            "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac",
            "Pixies/Doolittle/Scans/back.jpg",
            "Pixies/Doolittle/cover.jpg",
        ]
        batch = store.rename_batch(result.batch_id)
        assert (batch["origin"], batch["status"]) == ("triage", "applied")

        undo_batch(settings, store, result.batch_id)
    assert not (lib / "Pixies/Doolittle").exists()
    assert listing(triage) == [
        "new rip/Scans/back.jpg",
        "new rip/cover.jpg",
        "new rip/track01.flac",
        "new rip/track01.lrc",
        "new rip/track02.flac",
    ]


def test_recently_changed_folders_wait(settings, triage) -> None:
    doolittle(triage)
    album = only(replace(settings, triage_settle=300))
    assert album.status == WAITING
    assert "changed in the last 5 min" in album.waiting[0]
    later = time.time_ns() + 301 * 10**9
    settled = replace(settings, triage_settle=300)
    assert only(settled, now_ns=later).status == READY


def test_files_without_the_tags_naming_needs_are_blocked(settings, triage) -> None:
    tags = good_tags()
    del tags["musicbrainz_trackid"]
    build_flac(triage / "x" / "a.flac", tags)
    build_flac(triage / "y" / "b.flac", good_tags(TITLE="", musicbrainz_albumid=str(uuid.uuid4())))
    (triage / "z").mkdir()
    (triage / "z" / "c.flac").write_bytes(b"not flac")
    albums = {a.files[0]: a for a in plan(settings).albums}
    assert all(a.status == BLOCKED for a in albums.values())
    assert albums["x/a.flac"].problems == [
        "x/a.flac: missing a valid recording MBID (musicbrainz_trackid)."
    ]
    assert albums["y/b.flac"].problems == ["y/b.flac: missing title."]
    assert "can't be read" in albums["z/c.flac"].problems[0]


def test_warnings_need_confirmation_and_info_does_not(settings, triage) -> None:
    # Only track 1 of 2: album.missing_tracks is info, so it still moves on its own.
    build_flac(triage / "a" / "01.flac", good_tags(TOTALTRACKS="2"))
    album = only(settings)
    assert album.status == READY
    assert [n.check_id for n in album.notes] == ["album.missing_tracks"]

    # No date: tags.naming is a warning.
    build_flac(triage / "a" / "02.flac", good_tags(TITLE="Tame", TRACKNUMBER="2", DATE=""))
    album = only(settings)
    assert album.status == CONFIRM
    assert [(f.check_id, f.where) for f in album.issues] == [("tags.naming", "a/02.flac")]

    with Store(settings.database_path) as store:
        with pytest.raises(RenameError, match="changed since"):
            confirm_album(settings, store, album.key, "0" * 16)
        confirm_album(settings, store, album.key, album.fingerprint)
    assert (settings.library_path / DEBASER).is_file()
    assert (settings.library_path / TAME).is_file()


def test_never_overwrites_and_never_splits_a_release(settings, lib, triage) -> None:
    build_flac(lib / DEBASER.replace("Debaser", "debaser"), good_tags())
    build_flac(triage / "a" / "01.flac", good_tags())
    album = only(settings)
    assert album.status == BLOCKED
    assert "Pixies_Doolittle_01_debaser.flac already exists" in album.problems[0]

    # The release is in the library under another folder name: it isn't split.
    (lib / DEBASER.replace("Debaser", "debaser")).rename(lib / "Pixies/Surfer Rosa/x.flac")
    scan(settings)
    album = only(settings)
    assert album.status == BLOCKED
    assert "already in the library at Pixies/Surfer Rosa" in album.problems[0]


def test_adds_tracks_to_the_release_in_the_library(settings, lib, triage) -> None:
    build_flac(lib / DEBASER, good_tags(TOTALTRACKS="2"))
    scan(settings)
    build_flac(triage / "tame.flac", good_tags(TITLE="Tame", TRACKNUMBER="2", TOTALTRACKS="2"))
    album = only(settings)
    assert album.status == READY, (album.problems, album.issues)
    assert album.remarks == ["Adds 1 track to Pixies/Doolittle, which has 1 already."]
    assert [(m.src, m.dst) for m in album.moves] == [("tame.flac", TAME)]


def test_other_files_stay_when_a_folder_holds_two_releases(settings, triage) -> None:
    build_flac(triage / "mixed" / "a.flac", good_tags())
    other = str(uuid.uuid4())
    build_flac(
        triage / "mixed" / "b.flac",
        good_tags(ALBUM="Surfer Rosa 2", TITLE="Gigantic", musicbrainz_albumid=other),
    )
    (triage / "mixed" / "cover.jpg").write_bytes(b"jpg")
    albums = plan(settings).albums
    assert len(albums) == 2
    for album in albums:
        assert album.status == READY
        assert {m.kind for m in album.moves} == {"file"}


def test_waits_for_the_musicbrainz_lookup(settings, triage) -> None:
    build_flac(triage / "a" / "01.flac", good_tags())
    looked_up = replace(settings, musicbrainz_enabled=True)
    album = only(looked_up)
    assert album.status == WAITING
    assert album.needs_lookup


def test_moving_needs_one_filesystem_and_write_access(settings, triage, monkeypatch) -> None:
    real = os.access
    monkeypatch.setattr(os, "access", lambda p, mode: p != triage and real(p, mode))
    status = plan(settings).status
    assert status.ok
    assert status.warnings == ["The triage folder is read-only here, so nothing can be moved."]


def test_validate_settings(settings, lib, tmp_path) -> None:
    for bad in (lib, lib / "triage", lib.parent, Path(lib.anchor)):
        with pytest.raises(ValueError):
            validate_settings(replace(settings, triage_path=bad))
    validate_settings(settings)


# --- Web UI and the background watcher --------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.sent = []

    def describe(self) -> str:
        return "recorder"

    def send(self, notification) -> str:
        self.sent.append(notification)
        return "ok"


def test_watcher_moves_ready_albums_and_announces_the_rest_once(settings, lib, triage) -> None:
    from tagdrifter.notify import Notifications
    from tagdrifter.web.runner import ScanRunner
    from tagdrifter.web.triage import TriageWatcher

    doolittle(triage)
    build_flac(
        triage / "odd" / "x.flac",
        good_tags(ALBUM="Bossanova", DATE="", musicbrainz_albumid=str(uuid.uuid4())),
    )
    recorder = Recorder()
    notifications = Notifications(settings, recorder)
    runner = ScanRunner(settings, notifications)
    watcher = TriageWatcher(settings, runner, notifications)

    run = watcher.run()
    runner.wait()
    notifications.wait()
    assert [label for label, _ in run.moved] == ["Pixies: Doolittle"]
    assert run.rescanning
    assert (lib / DEBASER).is_file()
    with Store(settings.database_path) as store:
        assert store.recent_scans(1)[0]["reason"] == "triage"
    [message] = [n for n in recorder.sent if "triage" in n.title]
    assert "Moved 1 album" in message.message
    assert message.title == "Tag Drifter: 1 album in triage needs you"
    assert "**Pixies: Bossanova**: needs confirmation" in message.message

    watcher.run()  # nothing new to tell
    notifications.wait()
    assert len([n for n in recorder.sent if "triage" in n.title]) == 1


def test_web_confirm_and_undo(settings, lib, triage) -> None:
    from fastapi.testclient import TestClient

    from tagdrifter.web import create_app

    build_flac(triage / "a" / "01.flac", good_tags(DATE=""))
    app = create_app(replace(settings, triage_interval=0))
    with TestClient(app) as client:
        page = client.get("/triage")
        assert page.status_code == 200 and 'href="/triage"' in page.text
        body = client.get("/triage/plan").text
        assert "Needs confirmation (1)" in body and "Move anyway" in body
        key = re.search(r'name="key" value="([^"]+)"', body)[1]
        fingerprint = re.search(r'name="fingerprint" value="([^"]+)"', body)[1]

        stale = client.post("/triage/move", data={"key": key, "fingerprint": "0" * 16})
        assert "changed since it was shown" in stale.text
        moved = client.post("/triage/move", data={"key": key, "fingerprint": fingerprint})
        assert "moved" in moved.text and "Pixies/Doolittle" in moved.text
        assert moved.headers.get("HX-Trigger") == "scan-started"
        app.state.runner.wait()
        assert (lib / DEBASER).is_file()

        renames = client.get("/renames").text
        assert "from triage" in renames
        batch = re.search(r'action="/renames/(\d+)/undo"', renames)[1]
        assert "triage/a/01.flac" in client.get(f"/renames/{batch}").text
        client.post(f"/renames/{batch}/undo")
        app.state.runner.wait()
    assert listing(triage) == ["a/01.flac"]
    assert not (lib / "Pixies/Doolittle").exists()


def test_web_process_now(settings, lib, triage) -> None:
    from fastapi.testclient import TestClient

    from tagdrifter.web import create_app

    doolittle(triage)
    app = create_app(replace(settings, triage_interval=0))
    with TestClient(app) as client:
        body = client.post("/triage/run").text
        app.state.runner.wait()
    assert "Just now" in body and "Pixies: Doolittle" in body
    assert (lib / DEBASER).is_file()


def test_web_triage_is_off_without_a_folder(settings) -> None:
    from fastapi.testclient import TestClient

    from tagdrifter.web import create_app

    with TestClient(create_app(replace(settings, triage_path=None))) as client:
        assert 'href="/triage"' not in client.get("/").text
        assert client.get("/triage").status_code == 404
        assert client.post("/triage/run").status_code == 404
        assert client.get("/renames").status_code == 404


def test_cli_triage(settings, lib, triage, capsys, monkeypatch) -> None:
    from tagdrifter import cli

    key = "66666666-6666-4666-8666-666666666666"
    doolittle(triage)
    build_flac(
        triage / "odd" / "x.flac", good_tags(ALBUM="Bossanova", DATE="", musicbrainz_albumid=key)
    )
    argv = ["--db", str(settings.database_path), "--library", str(lib), "triage"]
    monkeypatch.setenv("TAGDRIFTER_TRIAGE", str(triage))
    monkeypatch.setenv("TAGDRIFTER_TRIAGE_SETTLE", "0")

    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "confirm  Pixies: Bossanova -> Pixies/Bossanova" in out
    assert "ready    Pixies: Doolittle -> Pixies/Doolittle" in out
    assert not (lib / DEBASER).exists()

    assert cli.main([*argv, "--apply", "--no-scan"]) == 0
    assert "moved   Pixies: Doolittle -> Pixies/Doolittle" in capsys.readouterr().out
    assert (lib / DEBASER).is_file()

    assert cli.main([*argv, "--apply", "--confirm", key, "--no-scan"]) == 0
    assert listing(triage) == []
