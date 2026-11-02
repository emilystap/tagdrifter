"""Syncing the secondary library (secondary_write.py): the guarded write path and applying a
plan. On Linux (CI) the writer goes through directory file descriptors; elsewhere through
checked paths, so both run somewhere."""

import os
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tagdrifter.core.secondary import Exclusion, plan_fingerprint
from tagdrifter.db import Store
from tagdrifter.secondary import MARKER, analyze, walk
from tagdrifter.secondary_write import SecondaryWriter, SyncError, UnsafePath, apply_sync
from tagdrifter.web import create_app


@pytest.fixture
def writer(tmp_path) -> tuple[SecondaryWriter, Path, Path]:
    lib, sec = tmp_path / "music", tmp_path / "secondary"
    (lib / "A/X").mkdir(parents=True)
    (lib / "A/X/01.flac").write_bytes(b"audio")
    sec.mkdir()
    return SecondaryWriter(lib, sec), lib, sec


def ino(path: Path) -> int:
    return os.lstat(path).st_ino


# --- The writer ---------------------------------------------------------------------------


def test_link_makes_a_hard_link_and_its_folders(writer) -> None:
    w, lib, sec = writer
    w.link("A/X/01.flac", "A/X/01.flac", ino(lib / "A/X/01.flac"))
    assert ino(sec / "A/X/01.flac") == ino(lib / "A/X/01.flac")
    assert os.lstat(lib / "A/X/01.flac").st_nlink == 2
    assert w.created_dirs == ["A", "A/X"]
    with pytest.raises(FileExistsError):  # never overwrites
        w.link("A/X/01.flac", "A/X/01.flac", ino(lib / "A/X/01.flac"))
    with pytest.raises(SyncError, match="changed since the plan"):
        w.link("A/X/01.flac", "A/X/other.flac", ino(lib / "A/X/01.flac") + 1)


def test_move_and_unlink(writer) -> None:
    w, lib, sec = writer
    n = ino(lib / "A/X/01.flac")
    w.link("A/X/01.flac", "Old/01.flac", n)
    w.move("Old/01.flac", "A/X/01.flac", n)
    assert not (sec / "Old/01.flac").exists() and ino(sec / "A/X/01.flac") == n
    assert w.rmdir("A") is False  # not empty: left alone
    assert w.rmdir("Old") is True
    w.unlink("A/X/01.flac", n)
    assert (lib / "A/X/01.flac").read_bytes() == b"audio"  # the library keeps it


def test_unlink_never_removes_an_only_copy_unasked(writer) -> None:
    w, _, sec = writer
    (sec / "mine.flac").write_bytes(b"x")
    with pytest.raises(SyncError, match="only copy"):
        w.unlink("mine.flac", ino(sec / "mine.flac"))
    with pytest.raises(SyncError, match="changed since the plan"):
        w.unlink("mine.flac", ino(sec / "mine.flac") + 1, last_copy_ok=True)
    w.unlink("mine.flac", ino(sec / "mine.flac"), last_copy_ok=True)
    assert not (sec / "mine.flac").exists()


@pytest.mark.parametrize("bad", ["", "/abs.flac", "../music/A/X/01.flac", "A/../B", "./A", "A//B"])
def test_unsafe_paths(writer, bad) -> None:
    w, lib, _ = writer
    with pytest.raises(UnsafePath):
        w.link("A/X/01.flac", bad, ino(lib / "A/X/01.flac"))


def test_symlinks_are_never_followed(writer, tmp_path) -> None:
    w, lib, sec = writer
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, sec / "A", target_is_directory=True)
    except OSError:
        pytest.skip("can't create symlinks here")
    with pytest.raises(UnsafePath):
        w.link("A/X/01.flac", "A/01.flac", ino(lib / "A/X/01.flac"))
    assert not list(outside.iterdir())
    os.symlink(lib / "A/X/01.flac", sec / "link.flac")
    with pytest.raises(SyncError, match="regular file"):
        w.unlink("link.flac", ino(lib / "A/X/01.flac"))


# --- Applying a plan ----------------------------------------------------------------------


def synced(settings) -> object:
    return replace(settings, secondary_sync=True)


def preview(settings) -> str:
    with Store(settings.database_path) as store:
        return plan_fingerprint(analyze(settings, store).plan)


def test_apply_brings_the_secondary_in_step(libraries) -> None:
    settings = synced(libraries)
    with Store(settings.database_path) as store:
        result = apply_sync(settings, store, preview(settings))
        assert result.error is None
        assert result.applied == {"move": 2, "mkdir": 3, "link": 2, "rmdir": 1}
        assert result.changes == 5
        after = analyze(settings, store).plan
        assert after.changes == 0 and after.unchanged == 4
        (row,) = store.secondary_syncs()
        assert (row["status"], row["changes"], row["trigger"]) == ("applied", 5, "manual")
        kinds = [(op["kind"], op["path"]) for op in store.secondary_ops(row["id"])]
    sec = settings.secondary_path
    assert ("move", "Pixies/Doolittle/01 Debaser.flac") in kinds
    assert ("rmdir", "Pixies/Doolitle") in kinds
    assert not (sec / "Pixies/Doolitle").exists()
    assert (sec / ".DS_Store").exists() and (sec / MARKER).exists()
    lib = settings.library_path
    assert ino(sec / "Other/Elsewhere/01 Song.flac") == ino(lib / "Other/Elsewhere/01 Song.flac")


def test_apply_refusals(libraries) -> None:
    with Store(libraries.database_path) as store:
        with pytest.raises(SyncError, match="Syncing is off"):
            apply_sync(libraries, store, preview(libraries))
        settings = synced(libraries)
        with pytest.raises(SyncError, match="changed since the preview"):
            apply_sync(settings, store, "stale")
        fingerprint = preview(settings)
        (settings.secondary_path / MARKER).unlink()
        with pytest.raises(SyncError, match=MARKER):
            apply_sync(settings, store, fingerprint)
        assert not store.secondary_syncs()


def test_apply_unlinks_exclusions_and_keeps_the_library(libraries) -> None:
    settings = synced(libraries)
    with Store(settings.database_path) as store:
        store.add_secondary_exclusions([Exclusion("extension", ".lrc", ".lrc")])
        result = apply_sync(settings, store, preview(settings))
    assert result.error is None and result.applied["unlink"] == 1
    sec, lib = settings.secondary_path, settings.library_path
    assert not list(sec.rglob("*.lrc"))
    assert (lib / "Pixies/Doolittle/01 Debaser.lrc").exists()


def test_only_copies_are_replaced_only_when_approved(libraries) -> None:
    settings = synced(libraries)
    stale = settings.secondary_path / "Pixies/Doolittle/cover.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"an old cover")  # where the library's cover belongs
    with Store(settings.database_path) as store:
        result = apply_sync(settings, store, preview(settings))
        assert result.error is None and "replace" not in result.applied
        assert result.skipped == ["Pixies/Doolittle/cover.jpg: only copy of its data, not replaced"]
        assert stale.read_bytes() == b"an old cover"
        result = apply_sync(settings, store, preview(settings), replace_only_copies=True)
    assert result.error is None and result.applied == {"replace": 1, "link": 1}
    assert ino(stale) == ino(settings.library_path / "Pixies/Doolittle/cover.jpg")


def test_swapped_names_go_through_a_temporary_name(libraries) -> None:
    settings = synced(libraries)
    lib, sec = settings.library_path, settings.secondary_path
    with Store(settings.database_path) as store:
        apply_sync(settings, store, preview(settings))
        a, b, tmp = (lib / "Pixies/Doolittle" / n for n in ("01 Debaser.flac", "cover.jpg", "t"))
        a.rename(tmp)
        b.rename(a)
        tmp.rename(b)  # the two library files swapped names
        result = apply_sync(settings, store, preview(settings))
        assert result.error is None and result.applied == {"move": 2}
        assert analyze(settings, store).plan.changes == 0
    assert ino(sec / "Pixies/Doolittle/cover.jpg") == ino(b)
    assert not list(sec.rglob("*.tagdrifter-move"))


def test_a_failure_stops_the_sync_and_a_retry_finishes_it(libraries, monkeypatch) -> None:
    settings = synced(libraries)
    calls = []
    real = SecondaryWriter.link

    def flaky(self, *args):
        calls.append(args)
        if len(calls) == 1:
            raise PermissionError(1, "Operation not permitted")
        return real(self, *args)

    monkeypatch.setattr(SecondaryWriter, "link", flaky)
    with Store(settings.database_path) as store:
        result = apply_sync(settings, store, preview(settings))
        assert "protected_hardlinks" in result.error
        assert store.secondary_syncs()[0]["status"] == "failed"
        result = apply_sync(settings, store, preview(settings))
        assert result.error is None
        assert analyze(settings, store).plan.changes == 0


def test_walk_after_sync_matches_the_library(libraries) -> None:
    settings = synced(libraries)
    with Store(settings.database_path) as store:
        apply_sync(settings, store, preview(settings))
    main = walk(settings.library_path, settings.ignore_patterns)
    sec = walk(settings.secondary_path, settings.ignore_patterns, skip=frozenset({MARKER}))
    assert {p: e.ino for p, e in main.files.items()} == {p: e.ino for p, e in sec.files.items()}


# --- Web and CLI --------------------------------------------------------------------------


def test_sync_from_the_page(libraries) -> None:
    with TestClient(create_app(libraries)) as c:
        page = c.get("/secondary/analysis").text
        assert "Syncing is off" in page and 'action="/secondary/sync"' not in page
    settings = synced(libraries)
    with TestClient(create_app(settings), follow_redirects=False) as c:
        page = c.get("/secondary/analysis").text
        fingerprint = page.split('name="fingerprint" value="')[2].split('"')[0]
        assert "Sync 5 changes" in page
        response = c.post("/secondary/sync", data={"fingerprint": "stale"})
        assert "changed+since+the+preview" in response.headers["location"]
        response = c.post("/secondary/sync", data={"fingerprint": fingerprint})
        assert response.headers["location"] == "/secondary?synced=1"
        assert "5 changes made" in c.get(response.headers["location"]).text
        page = c.get("/secondary/analysis").text
        assert "the secondary is in step" in page and "Recent syncs" in page
        ops = c.get("/secondary/syncs/1").text
        assert "Debaser.flac" in ops and "Other/Elsewhere/01 Song.flac" in ops
        assert ">mkdir<" not in ops


def test_sync_from_the_cli(libraries, capsys, monkeypatch) -> None:
    from tagdrifter.cli import main

    monkeypatch.setenv("TAGDRIFTER_SECONDARY", str(libraries.secondary_path))
    args = ["--library", str(libraries.library_path), "--db", str(libraries.database_path)]
    assert main([*args, "secondary", "--apply", "--yes"]) == 2
    assert "syncing is off" in capsys.readouterr().err
    monkeypatch.setenv("TAGDRIFTER_SECONDARY_SYNC", "true")
    assert main([*args, "secondary", "--apply", "--yes"]) == 0
    assert "Sync #1: 2 move, 2 link, 1 rmdir." in capsys.readouterr().out
    assert main([*args, "secondary"]) == 0
    assert "the secondary is in step" in capsys.readouterr().out


def test_sync_without_the_marker(libraries) -> None:
    settings = replace(synced(libraries), secondary_marker=False)
    (settings.secondary_path / MARKER).unlink()
    with Store(settings.database_path) as store:
        result = apply_sync(settings, store, preview(settings))
        assert result.error is None and analyze(settings, store).plan.changes == 0
