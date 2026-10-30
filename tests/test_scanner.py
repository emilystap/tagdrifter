import os

import pytest

from tagdrifter.config import Settings
from tagdrifter.db import Store
from tagdrifter.scanner import run_scan

from .conftest import build_flac, good_tags, scan, settings_for


def test_full_scan_finds_expected_issues(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path)
    result, issues = scan(settings)

    assert result.files_total == 5  # ignored folders excluded
    assert result.files_read == 5
    assert set(issues) == {
        ("path.track_location", "Pixies/Doolittle/02 Tame.flac"),
        ("tags.required", "Pixies/Doolittle/02 Tame.flac"),
        ("tags.invalid_mbid", "Pixies/Doolittle/02 Tame.flac"),
        ("file.unreadable", "Pixies/Doolittle/broken.flac"),
        ("path.orphan_sidecar", "Pixies/Doolittle/old name.lrc"),
        ("album.placement", "Pixies/Hits"),
        ("path.loose_file", "loose.flac"),
    }
    loc = issues[("path.track_location", "Pixies/Doolittle/02 Tame.flac")]
    assert loc.details["expected"] == "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac"
    assert issues[("tags.required", "Pixies/Doolittle/02 Tame.flac")].details["missing"] == [
        "musicbrainz_releasegroupid"
    ]
    assert result.issues_new == len(issues) and result.issues_resolved == 0


def test_rescan_reads_only_changed_files_and_resolves_issues(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path)
    scan(settings)

    result, issues = scan(settings)
    assert result.files_read == 0
    assert result.issues_new == 0 and result.issues_resolved == 0

    # Fix the misnamed track (rename) and the broken file (delete).
    doolittle = library / "Pixies/Doolittle"
    os.rename(doolittle / "02 Tame.flac", doolittle / "Pixies_Doolittle_02_Tame.flac")
    (doolittle / "broken.flac").unlink()

    result, issues = scan(settings)
    assert result.files_read == 1  # only the renamed file is new to the cache
    assert result.files_removed == 2
    assert ("path.track_location", "Pixies/Doolittle/02 Tame.flac") not in issues
    assert ("file.unreadable", "Pixies/Doolittle/broken.flac") not in issues
    # Same tag problems, now reported against the new path
    assert ("tags.required", "Pixies/Doolittle/Pixies_Doolittle_02_Tame.flac") in issues
    assert result.issues_resolved == 4  # location, unreadable, + required/mbid at old path


def test_changed_file_is_reread(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path)
    scan(settings)
    target = library / "Pixies/Hits/Pixies_Hits_01_Song.flac"
    build_flac(target, good_tags(ALBUMARTIST="Pixies", ALBUM="Hits", TITLE="Song"))
    os.utime(target, ns=(1, 1_000_000_000))  # guarantee a different mtime

    result, issues = scan(settings)
    assert result.files_read == 1
    assert ("album.placement", "Pixies/Hits") not in issues


def test_full_flag_rereads_everything(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path)
    scan(settings)
    result, _ = scan(settings, full=True)
    assert result.files_read == 5


def test_disabled_checks_and_options(library, tmp_path) -> None:
    settings = settings_for(
        library,
        tmp_path,
        disabled_checks=frozenset({"path.loose_file", "path.orphan_sidecar"}),
        check_options={"tags.required": {"tags": ["title"]}},
    )
    _, issues = scan(settings)
    checks = {check for check, _ in issues}
    assert "path.loose_file" not in checks
    assert "path.orphan_sidecar" not in checks
    assert "tags.required" not in checks


def test_case_handling(tmp_path) -> None:
    lib = tmp_path / "music"
    build_flac(lib / "Pixies/Doolittle/pixies_doolittle_01_debaser.flac", good_tags())

    _, issues = scan(settings_for(lib, tmp_path, case_sensitive=True))
    issue = issues[("path.track_location", "Pixies/Doolittle/pixies_doolittle_01_debaser.flac")]
    assert issue.details["case_only"] is True

    _, issues = scan(settings_for(lib, tmp_path, case_sensitive=False))
    assert not any(c == "path.track_location" for c, _ in issues)


@pytest.mark.skipif(os.name == "nt", reason="needs a case-sensitive filesystem")
def test_case_collisions(tmp_path) -> None:
    lib = tmp_path / "music"
    build_flac(lib / "Pixies/Doolittle/a.flac", good_tags())
    probe = lib / "Pixies/doolittle"
    probe.mkdir()
    if (lib / "Pixies/Doolittle").samefile(probe):
        pytest.skip("filesystem is case-insensitive")
    build_flac(probe / "a.flac", good_tags())
    build_flac(lib / "pixies/X/a.flac", good_tags())

    _, issues = scan(settings_for(lib, tmp_path))
    assert ("path.case_collision_albums", "Pixies") in issues
    assert ("path.case_collision_artists", ".") in issues


def test_missing_library_raises(tmp_path) -> None:
    with Store(":memory:") as store, pytest.raises(FileNotFoundError):
        run_scan(Settings(library_path=tmp_path / "nope"), store)


def test_buggy_check_does_not_abort_scan(library, tmp_path, monkeypatch) -> None:
    from tagdrifter.checks import builtin_paths

    def boom(self, ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(builtin_paths.LooseFile, "run", boom)
    result, issues = scan(settings_for(library, tmp_path))
    assert result.check_errors
    assert any(check == "scanner.check_error" for check, _ in issues)
    assert ("path.track_location", "Pixies/Doolittle/02 Tame.flac") in issues


def test_issues_record_mbid_status(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path)
    _, issues = scan(settings)

    # 02 Tame's recording MBID is malformed, so neither it nor its album counts as tagged.
    assert issues[("path.track_location", "Pixies/Doolittle/02 Tame.flac")].mbid_tagged is False
    assert issues[("file.unreadable", "Pixies/Doolittle/broken.flac")].mbid_tagged is False
    assert issues[("album.placement", "Pixies/Hits")].mbid_tagged is True
    assert issues[("path.loose_file", "loose.flac")].mbid_tagged is None

    with Store(settings.database_path) as store:
        tagged = {i.subject for i in store.open_issues(mbid_tagged=True)}
        untagged = {i.subject for i in store.open_issues(mbid_tagged=False)}
    assert tagged == {"Pixies/Hits"}
    assert "loose.flac" in untagged and "Pixies/Hits" not in untagged
