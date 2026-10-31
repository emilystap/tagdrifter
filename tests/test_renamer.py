import os
import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tagdrifter import renamer
from tagdrifter.db import Store
from tagdrifter.renamer import RenameError, apply_plan, plan_album, undo_batch
from tagdrifter.web import create_app

from .conftest import build_flac, good_tags, scan, settings_for


def listing(root: Path) -> list[str]:
    """Every file below root, as sorted '/'-separated relative paths (real on-disk case)."""
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            out.append(Path(dirpath, name).relative_to(root).as_posix())
    return sorted(out)


def track(lib: Path, rel: str, **overrides: str) -> Path:
    return build_flac(lib / rel, good_tags(**overrides))


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    lib = tmp_path / "music"
    # Wrong folder name and filenames; a cover image and lyrics that should travel along.
    track(lib, "Pixies/doolittle (old)/01 Debaser.flac")
    track(lib, "Pixies/doolittle (old)/02 Tame.flac", TITLE="Tame", TRACKNUMBER="2")
    (lib / "Pixies/doolittle (old)/01 Debaser.lrc").write_text("[00:00] la")
    (lib / "Pixies/doolittle (old)/01 Debaser.txt").write_text("la")
    (lib / "Pixies/doolittle (old)/cover.jpg").write_bytes(b"jpg")
    return lib


@pytest.fixture
def settings(lib: Path, tmp_path: Path):
    return settings_for(lib, tmp_path)


def plan(settings, album: str):
    with Store(settings.database_path) as store:
        return plan_album(settings, store, album)


def apply(settings, album: str):
    with Store(settings.database_path) as store:
        p = plan_album(settings, store, album)
        assert p.ok, p.problems
        return apply_plan(settings, store, album, p.fingerprint)


def test_renames_the_folder_and_tracks_and_undoes_it(settings, lib) -> None:
    scan(settings)
    before = listing(lib)
    p = plan(settings, "Pixies/doolittle (old)")
    assert p.ok
    assert [(m.kind, m.src, m.dst) for m in p.moves] == [
        ("folder", "Pixies/doolittle (old)", "Pixies/Doolittle"),
        (
            "file",
            "Pixies/Doolittle/01 Debaser.flac",
            "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac",
        ),
        (
            "sidecar",
            "Pixies/Doolittle/01 Debaser.lrc",
            "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.lrc",
        ),
        (
            "sidecar",
            "Pixies/Doolittle/01 Debaser.txt",
            "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.txt",
        ),
        ("file", "Pixies/Doolittle/02 Tame.flac", "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac"),
    ]

    result = apply(settings, "Pixies/doolittle (old)")
    assert listing(lib) == [
        "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac",
        "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.lrc",
        "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.txt",
        "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac",
        "Pixies/Doolittle/cover.jpg",
    ]
    _, issues = scan(settings)
    assert not [k for k in issues if k[0] == "path.track_location"]

    with Store(settings.database_path) as store:
        assert store.rename_batch(result.batch_id)["status"] == "applied"
        assert undo_batch(settings, store, result.batch_id) == 5
        assert store.rename_batch(result.batch_id)["status"] == "undone"
    assert listing(lib) == before


def test_case_only_rename(settings, lib, tmp_path) -> None:
    lib2 = tmp_path / "music2"
    track(lib2, "Pixies/Doolittle/Pixies_Doolittle_01_debaser.flac")
    settings = settings_for(lib2, tmp_path / "other")
    scan(settings)
    apply(settings, "Pixies/Doolittle")
    assert listing(lib2) == ["Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac"]


def test_tracks_move_one_by_one_into_an_existing_folder(settings, lib) -> None:
    track(
        lib,
        "Pixies/Doolittle/Pixies_Doolittle_03_Wave of Mutilation.flac",
        TITLE="Wave of Mutilation",
        TRACKNUMBER="3",
    )
    scan(settings)
    p = plan(settings, "Pixies/doolittle (old)")
    assert p.ok and p.moves[0].kind == "file"
    apply(settings, "Pixies/doolittle (old)")
    files = listing(lib)
    assert "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.lrc" in files
    # The cover stays behind, so the old folder is kept.
    assert "Pixies/doolittle (old)/cover.jpg" in files


def test_old_folder_removed_when_empty_and_restored_on_undo(settings, lib) -> None:
    (lib / "Pixies/doolittle (old)/cover.jpg").unlink()
    track(lib, "Pixies/Doolittle/Pixies_Doolittle_03_Wave.flac", TITLE="Wave", TRACKNUMBER="3")
    scan(settings)
    result = apply(settings, "Pixies/doolittle (old)")
    assert result.removed_dirs == 1
    assert not (lib / "Pixies/doolittle (old)").exists()
    with Store(settings.database_path) as store:
        undo_batch(settings, store, result.batch_id)
    assert (lib / "Pixies/doolittle (old)/01 Debaser.flac").exists()


def test_refuses_untagged_changed_and_conflicting_albums(settings, lib) -> None:
    track(lib, "Pixies/Bossa/x.flac", ALBUM="Bossa Nova", musicbrainz_trackid="")
    scan(settings)
    assert "no valid release/recording MBIDs" in " ".join(plan(settings, "Pixies/Bossa").problems)

    build_flac(lib / "Pixies/doolittle (old)/02 Tame.flac", good_tags(TITLE="Tame!"))
    assert "changed since the last scan" in " ".join(
        plan(settings, "Pixies/doolittle (old)").problems
    )

    scan(settings)
    # Same title and number twice: two tracks would get one name.
    build_flac(lib / "Pixies/doolittle (old)/02 Tame.flac", good_tags())
    scan(settings)
    assert "would both become" in " ".join(plan(settings, "Pixies/doolittle (old)").problems)


def test_never_overwrites(settings, lib) -> None:
    track(lib, "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac", TITLE="Other")
    scan(settings)
    problems = " ".join(plan(settings, "Pixies/doolittle (old)").problems)
    assert "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac already exists" in problems


def test_apply_refuses_a_stale_preview_and_rolls_back_failures(settings, lib, monkeypatch) -> None:
    scan(settings)
    with (
        Store(settings.database_path) as store,
        pytest.raises(RenameError, match="changed since the preview"),
    ):
        apply_plan(settings, store, "Pixies/doolittle (old)", "0" * 16)

    # A move fails halfway: the moves already made are reversed.
    before = listing(lib)
    real_rename = renamer._rename

    def flaky(root, src, dst):
        if src.endswith("02 Tame.flac"):
            raise OSError("disk on fire")
        real_rename(root, src, dst)

    monkeypatch.setattr(renamer, "_rename", flaky)
    with Store(settings.database_path) as store:
        p = plan_album(settings, store, "Pixies/doolittle (old)")
        with pytest.raises(RenameError, match=r"disk on fire\. Nothing was changed"):
            apply_plan(settings, store, "Pixies/doolittle (old)", p.fingerprint)
        assert store.rename_batches()[0]["status"] == "failed"
    assert listing(lib) == before


def test_rejects_paths_outside_the_library(settings) -> None:
    scan(settings)
    for album in ("../x", "/etc", "Pixies", ""):
        assert plan(settings, album).problems


# --- Web UI -----------------------------------------------------------------------------

ALBUM = "Pixies/doolittle (old)"


def test_web_rename_is_off_by_default(settings) -> None:
    scan(settings)
    with TestClient(create_app(settings)) as client:
        assert "Preview renames" not in client.get("/issues").text
        assert 'href="/renames"' not in client.get("/").text
        assert client.get("/rename/preview", params={"album": ALBUM}).status_code == 404
        assert client.post("/rename/apply", data={"album": ALBUM}).status_code == 404


def test_web_preview_apply_and_undo(settings, lib) -> None:
    settings = replace(settings, fix_enabled=True)
    scan(settings)
    before = listing(lib)
    with TestClient(create_app(settings)) as client:
        assert "Preview renames" in client.get("/issues").text
        preview = client.get("/rename/preview", params={"album": ALBUM}).text
        assert "Rename 2 tracks and the folder" in preview
        fingerprint = re.search(r'name="fingerprint" value="(\w+)"', preview)[1]

        stale = client.post("/rename/apply", data={"album": ALBUM, "fingerprint": "x"})
        assert "Nothing renamed" in stale.text and listing(lib) == before

        applied = client.post("/rename/apply", data={"album": ALBUM, "fingerprint": fingerprint})
        assert "5 changes made" in applied.text
        assert applied.headers.get("HX-Trigger") == "scan-started"
        assert "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac" in listing(lib)
        client.app.state.runner.wait(10)

        page = client.get("/renames").text
        assert ALBUM in page and "applied" in page
        assert "Tame.flac</span>" in client.get("/renames/1").text
        undone = client.post("/renames/1/undo")
        assert undone.status_code == 200 and "undone" in undone.text
        client.app.state.runner.wait(10)
    assert listing(lib) == before


def test_web_shows_why_a_rename_is_refused(settings, lib) -> None:
    settings = replace(settings, fix_enabled=True)
    track(lib, "Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac", TITLE="Other")
    scan(settings)
    with TestClient(create_app(settings)) as client:
        preview = client.get("/rename/preview", params={"album": ALBUM}).text
        assert "Can’t rename this folder" in preview and "already exists" in preview
