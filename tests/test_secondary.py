"""Secondary library (secondary.py, core/secondary.py): the plan that keeps a hard-linked copy
of the library in step, exclusions, and the exclusions proposed from an existing copy."""

from dataclasses import replace
from pathlib import Path

import pytest

from tagdrifter import secondary as secondary_module
from tagdrifter.config import load_settings
from tagdrifter.core.secondary import (
    AudioKeys,
    Exclusion,
    FileEntry,
    Move,
    Stray,
    Tree,
    audio_keys,
    excluded_files,
    exclusion_for,
    normalize_extension,
    plan_sync,
    propose_exclusions,
    propose_extensions,
)
from tagdrifter.db import Store
from tagdrifter.secondary import MARKER, analyze, check, validate_settings, walk

from .conftest import build_flac, good_tags, settings_for

A1 = "aaaaaaaa-0000-4000-8000-000000000001"
A2 = "aaaaaaaa-0000-4000-8000-000000000002"
R1 = "bbbbbbbb-0000-4000-8000-000000000001"
R2 = "bbbbbbbb-0000-4000-8000-000000000002"
T1 = "cccccccc-0000-4000-8000-000000000001"


def tree(files: dict[str, int], *, nlink: int = 2, dirs: set[str] | None = None) -> Tree:
    """A Tree from {path: inode}; folders are derived unless given."""
    t = Tree(files={p: FileEntry(ino, nlink, 1) for p, ino in files.items()})
    t.dirs = dirs if dirs is not None else {
        "/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)
    }  # fmt: skip
    return t


# --- Plan ---------------------------------------------------------------------------------


def test_plan_follows_renames_and_new_files() -> None:
    main = tree({"A/New Name/01.flac": 1, "A/New Name/cover.jpg": 2, "B/X/01.flac": 3})
    secondary = tree({"A/Old Name/01.flac": 1, "A/Old Name/cover.jpg": 2})
    plan = plan_sync(main, secondary, excluded=set())
    assert plan.moves == [
        Move("A/Old Name/01.flac", "A/New Name/01.flac"),
        Move("A/Old Name/cover.jpg", "A/New Name/cover.jpg"),
    ]
    assert plan.links == ["B/X/01.flac"]
    assert plan.rmdirs == ["A/Old Name"]
    assert plan.unchanged == 0 and not plan.unlinks and not plan.strays
    assert plan.changes == 4


def test_plan_in_step() -> None:
    files = {"A/X/01.flac": 1, "A/X/02.flac": 2}
    plan = plan_sync(tree(files), tree(files), excluded=set())
    assert plan.changes == 0 and plan.unchanged == 2


def test_plan_unlinks_excluded_and_duplicate_links() -> None:
    main = tree({"A/X/01.flac": 1, "B/Y/01.flac": 2})
    secondary = tree({"A/X/01.flac": 1, "A/X/copy of 01.flac": 1, "B/Y/01.flac": 2})
    plan = plan_sync(main, secondary, excluded={"B/Y/01.flac"})
    assert plan.unlinks == [("A/X/copy of 01.flac", "duplicate"), ("B/Y/01.flac", "excluded")]
    assert plan.rmdirs == ["B/Y", "B"]  # deepest first


def test_plan_never_removes_files_that_arent_links() -> None:
    main = tree({"A/X/01.flac": 1, "A/X/02.flac": 2})
    secondary = tree({"A/X/02.flac": 9, "A/X/notes.txt": 8, "A/Mine/song.flac": 7})
    secondary.files["A/X/notes.txt"] = FileEntry(8, 3, 1)  # linked somewhere else
    secondary.files["A/Mine/song.flac"] = FileEntry(7, 1, 1)
    secondary.files["A/X/02.flac"] = FileEntry(9, 1, 1)  # replaced in the library, say
    plan = plan_sync(main, secondary, excluded=set())
    assert plan.links == ["A/X/01.flac", "A/X/02.flac"]
    # The stale copy in the way is listed as replaced, with whether it's the only copy.
    assert plan.replaces == [Stray("A/X/02.flac", only_copy=True)]
    assert plan.strays == [
        Stray("A/Mine/song.flac", only_copy=True),
        Stray("A/X/notes.txt", only_copy=False),
    ]
    assert not plan.unlinks and not plan.rmdirs  # A/Mine keeps its stray


def test_plan_blocked_paths_and_ignored_names() -> None:
    main = tree({"A/X/01.flac": 1, "B/cover.jpg": 2})
    secondary = tree({"A/X": 5}, dirs={"A", "B", "B/cover.jpg", "C"})
    secondary.files["A/X"] = FileEntry(5, 1, 1)
    secondary.ignored_in = {"C"}
    plan = plan_sync(main, secondary, excluded=set())
    assert plan.blocked == [
        ("A/X/01.flac", "A/X is a file, not a folder"),
        ("B/cover.jpg", "a folder is in the way"),
    ]
    assert not plan.links
    assert plan.rmdirs == ["B/cover.jpg", "B"]  # not C: it holds an ignored name


# --- Exclusions ---------------------------------------------------------------------------


def keys(artist: str = A1, release: str = R1, track: str = "") -> AudioKeys:
    return AudioKeys(artist=artist, release=release, track=track)


def test_audio_keys_from_tags() -> None:
    tags = {
        "albumartist": ("Pixies",),
        "artist": ("Pixies",),
        "album": ("Doolittle",),
        "title": ("Debaser",),
        "musicbrainz_albumartistid": (A1.upper(), A2),
        "musicbrainz_albumid": (R1,),
        "musicbrainz_releasetrackid": ("not-an-mbid",),
    }
    k = audio_keys(tags)
    assert (k.artist, k.release, k.track) == (f"{A1};{A2}", R1, "")
    assert (k.artist_label, k.release_label, k.track_label) == (
        "Pixies",
        "Pixies - Doolittle",
        "Pixies - Debaser",
    )
    assert audio_keys(None) == AudioKeys()


def test_excluded_files_follow_their_audio() -> None:
    audio = {
        "A/X/01.flac": keys(track=T1),
        "A/X/02.flac": keys(),
        "A/Y/01.flac": keys(release=R2),
        "B/Z/01.flac": keys(artist=A2, release=""),
    }
    files = [
        *audio,
        "A/X/01.lrc",  # sidecar of an excluded track
        "A/X/02.lrc",
        "A/X/cover.jpg",  # the album isn't wholly excluded
        "A/Y/cover.jpg",
        "A/Y/Scans/back.jpg",  # follows A/Y, the nearest folder with audio
        "A/artist.jpg",  # A still has A/X
        "B/Z/cover.jpg",
        "B/artist.jpg",
    ]
    rules = [
        Exclusion("track", T1, ""),
        Exclusion("release", R2, ""),
        Exclusion("path", "B", ""),
    ]
    assert excluded_files(files, audio, rules, (".lrc",)) == {
        "A/X/01.flac",
        "A/X/01.lrc",
        "A/Y/01.flac",
        "A/Y/cover.jpg",
        "A/Y/Scans/back.jpg",
        "B/Z/01.flac",
        "B/Z/cover.jpg",
        "B/artist.jpg",
    }
    # An artist rule takes the artist folder's own files too; a path rule can name one file.
    rules = [Exclusion("artist", A1, ""), Exclusion("path", "B/Z/cover.jpg", "")]
    assert excluded_files(files, audio, rules, (".lrc",)) == {
        p for p in files if p.startswith("A/")
    } | {"B/Z/cover.jpg"}


def test_exclusion_for_prefers_a_shared_mbid() -> None:
    audio = {
        "A/X/01.flac": AudioKeys(artist=A1, release=R1, track=T1, release_label="A - X"),
        "A/X/02.flac": AudioKeys(artist=A1, release=R1),
        "A/Y/01.flac": AudioKeys(artist=A1, release=R2),
    }
    x = ["A/X/01.flac", "A/X/02.flac"]
    assert exclusion_for("release", x, audio, "A/X") == Exclusion("release", R1, "A - X")
    assert exclusion_for("artist", x, audio, "A") == Exclusion("artist", A1, "A")  # no label
    # Two releases, or a file without the key: by path
    assert exclusion_for("release", audio, audio, "A") == Exclusion("path", "A", "A")
    assert exclusion_for("track", ["A/X/02.flac"], audio, "A/X/02.flac").kind == "path"


# --- Bootstrap ----------------------------------------------------------------------------


def test_proposals_broadest_first() -> None:
    audio = {
        # Artist A1: only in this folder, all missing -> one album artist rule
        "Gone/X/01.flac": AudioKeys(A1, R1, artist_label="Gone"),
        "Gone/Y/01.flac": AudioKeys(A1, R2),
        # Artist A2: one album missing (two discs) -> one release rule; one track missing
        "Kept/Album/CD1/01.flac": AudioKeys(A2, "r3"),
        "Kept/Album/CD2/01.flac": AudioKeys(A2, "r3", release_label="Kept - Album"),
        "Kept/Other/01.flac": AudioKeys(A2, "r4", T1, track_label="Kept - Song"),
        "Kept/Other/02.flac": AudioKeys(A2, "r4"),
        # No MBIDs -> path rules
        "Untagged/Demo/01.flac": AudioKeys(),
        "Kept/Bits/01.flac": AudioKeys(),
        "Kept/Bits/02.flac": AudioKeys(),
    }
    missing = [p for p in audio if p not in ("Kept/Other/02.flac", "Kept/Bits/02.flac")]
    proposals = propose_exclusions(audio, missing, excluded=set())
    assert [(p.exclusion, p.files) for p in proposals] == [
        (Exclusion("artist", A1, "Gone"), 2),
        (Exclusion("path", "Untagged", "Untagged"), 1),
        (Exclusion("release", "r3", "Kept - Album"), 2),
        (Exclusion("path", "Kept/Bits/01.flac", "Kept/Bits/01.flac"), 1),
        (Exclusion("track", T1, "Kept - Song"), 1),
    ]
    assert proposals[2].folder == "Kept/Album/CD1"


def test_proposals_never_cover_linked_files() -> None:
    # The artist's MBID also tags a linked album elsewhere, so its folder gets a path rule.
    audio = {"Old Name/X/01.flac": AudioKeys(A1, R1), "New Name/Y/01.flac": AudioKeys(A1, R2)}
    proposals = propose_exclusions(audio, ["Old Name/X/01.flac"], excluded=set())
    assert [p.exclusion for p in proposals] == [Exclusion("path", "Old Name", "Old Name")]
    # Already excluded files count as gone, and aren't proposed again.
    proposals = propose_exclusions(audio, ["Old Name/X/01.flac"], {"New Name/Y/01.flac"})
    assert [p.exclusion.kind for p in proposals] == ["artist"]
    assert propose_exclusions(audio, [], {"New Name/Y/01.flac"}) == []


# --- Settings and checks ------------------------------------------------------------------


def test_secondary_settings(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[secondary]\npath = "/libraries/secondary"\n', encoding="utf-8")
    assert load_settings(cfg, env={}).secondary_path == Path("/libraries/secondary")
    env = {"TAGDRIFTER_SECONDARY": "/x"}
    assert load_settings(cfg, env=env).secondary_path == Path("/x")
    assert load_settings(env={}).secondary_path is None


def test_secondary_must_not_overlap_the_library(tmp_path) -> None:
    lib = tmp_path / "libraries" / "music"
    for bad, why in [
        (lib, "the library itself"),
        (lib / "secondary", "inside the library"),
        (tmp_path / "libraries", "contains the library"),
        (Path(Path.cwd().anchor), "filesystem root"),
    ]:
        with pytest.raises(ValueError, match=why):
            validate_settings(settings_for(lib, tmp_path, secondary_path=bad))
    validate_settings(settings_for(lib, tmp_path, secondary_path=tmp_path / "libraries" / "sec"))
    validate_settings(settings_for(lib, tmp_path))


def test_check_needs_the_marker(tmp_path) -> None:
    lib, sec = tmp_path / "music", tmp_path / "secondary"
    lib.mkdir()
    settings = settings_for(lib, tmp_path, secondary_path=sec)
    assert "isn't a folder" in check(settings).problems[0]
    sec.mkdir()
    (problem,) = check(settings).problems
    assert MARKER in problem
    (sec / MARKER).touch()
    assert check(settings).ok


# --- On disk ------------------------------------------------------------------------------


def test_walk_records_links(libraries) -> None:
    sec = walk(libraries.secondary_path, libraries.ignore_patterns, skip=frozenset({MARKER}))
    assert sorted(sec.files) == [f"Pixies/Doolitle/01 Debaser.{e}" for e in ("flac", "lrc")]
    main = walk(libraries.library_path, libraries.ignore_patterns)
    entry = sec.files["Pixies/Doolitle/01 Debaser.flac"]
    assert entry.nlink == 2 and entry.ino == main.files["Pixies/Doolittle/01 Debaser.flac"].ino


def test_analyze_end_to_end(libraries) -> None:
    with Store(libraries.database_path) as store:
        result = analyze(libraries, store)
        assert result.status.ok and result.unscanned == 0
        plan = result.plan
        assert plan.moves == [
            Move("Pixies/Doolitle/01 Debaser.flac", "Pixies/Doolittle/01 Debaser.flac"),
            Move("Pixies/Doolitle/01 Debaser.lrc", "Pixies/Doolittle/01 Debaser.lrc"),
        ]
        assert plan.links == ["Other/Elsewhere/01 Song.flac", "Pixies/Doolittle/cover.jpg"]
        assert plan.rmdirs == ["Pixies/Doolitle"]
        # The missing artist is proposed, and so is .jpg: no cover is in the secondary.
        jpg, proposal = result.proposals
        assert (jpg.exclusion, jpg.files) == (Exclusion("extension", ".jpg", ".jpg"), 1)
        assert proposal.exclusion.kind == "artist" and proposal.exclusion.label == "Other"

        assert store.add_secondary_exclusions([proposal.exclusion]) == 1
        assert store.add_secondary_exclusions([proposal.exclusion]) == 0
        result = analyze(libraries, store)
        assert result.plan.links == ["Pixies/Doolittle/cover.jpg"]
        assert [p.exclusion.kind for p in result.proposals] == ["extension"]
        assert result.excluded == 1
        (view,) = result.rules
        assert (view.files, view.folder) == (1, "Other/Elsewhere")

        assert store.delete_secondary_exclusion(view.rule.id)
        assert not store.secondary_exclusions()


def test_unscanned_files_only_follow_path_rules(libraries) -> None:
    lib = libraries.library_path
    build_flac(lib / "Other/Elsewhere/02 New.flac", good_tags())
    with Store(libraries.database_path) as store:
        result = analyze(libraries, store)
    assert result.unscanned == 1
    assert "Other/Elsewhere/02 New.flac" in result.plan.links


def test_file_type_exclusions() -> None:
    audio = {"A/X/01.flac": keys(), "A/X/02.flac": keys()}
    files = [*audio, "A/X/01.lrc", "A/X/notes.TXT", "A/X/cover.jpg", "A/X/02.lrc"]
    rules = [Exclusion("extension", ".lrc", ""), Exclusion("extension", ".txt", "")]
    # Sidecars of included tracks too; matching ignores case.
    assert excluded_files(files, audio, rules, (".lrc", ".txt")) == {
        "A/X/01.lrc",
        "A/X/02.lrc",
        "A/X/notes.TXT",
    }
    assert normalize_extension(" LRC ") == normalize_extension(".lrc") == ".lrc"
    for bad in ("", ".", "a/b", "tar.gz", ".l rc"):
        with pytest.raises(ValueError):
            normalize_extension(bad)


def test_file_type_proposals() -> None:
    audio = {"A/X/01.flac": keys(), "A/Y/01.flac": keys()}
    files = [*audio, "A/X/01.lrc", "A/Y/01.lrc", "A/X/cover.jpg", "A/Y/cover.jpg", "README"]
    missing = ["A/X/01.lrc", "A/Y/01.lrc", "A/X/cover.jpg", "README", "A/Y/01.flac"]
    proposals = propose_extensions(files, audio, missing, excluded=set())
    # Not .jpg (one is linked), not audio, not files without an extension.
    assert [(p.exclusion, p.files) for p in proposals] == [
        (Exclusion("extension", ".lrc", ".lrc"), 2)
    ]
    assert propose_extensions(files, audio, ["A/X/01.lrc"], {"A/Y/01.lrc"})[0].files == 1


def test_mount_points(tmp_path, monkeypatch) -> None:
    info = tmp_path / "mountinfo"
    info.write_text(
        "22 1 0:21 / / rw - ext4 /dev/sda1 rw\n"
        "30 22 0:40 /media/music_libraries /libraries rw - zfs pool/media rw\n"
        "31 22 0:40 /media/other /my\\040libs rw - zfs pool/media rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(secondary_module, "MOUNTINFO", info)
    mounts = secondary_module._mounts()
    assert mounts == ["/", "/libraries", "/my libs"]
    assert secondary_module._mount_point(Path("/libraries/music/A"), mounts) == "/libraries"
    assert secondary_module._mount_point(Path("/librariesX"), mounts) == "/"


def test_separate_mounts_are_a_warning(libraries, monkeypatch) -> None:
    lib, sec = (p.resolve().as_posix() for p in (libraries.library_path, libraries.secondary_path))
    monkeypatch.setattr(secondary_module, "_mounts", lambda: ["/", lib, sec])
    status = check(libraries)
    assert status.ok and "separate mounts" in status.warnings[0]
    monkeypatch.setattr(secondary_module, "_mounts", lambda: ["/", lib.rsplit("/", 1)[0]])
    assert not any("mounts" in w for w in check(libraries).warnings)


# --- Web and CLI --------------------------------------------------------------------------


def test_secondary_page(libraries) -> None:
    from fastapi.testclient import TestClient

    from tagdrifter.web import create_app

    with TestClient(create_app(libraries), follow_redirects=False) as c:
        assert 'href="/secondary"' in c.get("/").text
        assert "Reading both libraries" in c.get("/secondary").text
        page = c.get("/secondary/analysis").text
        assert "Proposed exclusions" in page and "Other" in page and ".jpg" in page
        assert "Doolitle/01 Debaser.flac" in page  # a move
        fingerprint = page.split('name="fingerprint" value="')[1].split('"')[0]

        response = c.post("/secondary/exclusions/propose", data={"fingerprint": "stale"})
        assert "changed+since+the+preview" in response.headers["location"]
        response = c.post("/secondary/exclusions/propose", data={"fingerprint": fingerprint})
        assert response.status_code == 303 and response.headers["location"].endswith("saved=2")
        assert "Saved 2 exclusions." in c.get(response.headers["location"]).text
        page = c.get("/secondary/analysis").text
        assert "Proposed exclusions" not in page and "/delete" in page

        with Store(libraries.database_path) as store:
            artist, _jpg = store.secondary_exclusions()  # sorted by kind
        c.post(f"/secondary/exclusions/{artist.id}/delete")
        assert "Proposed exclusions" in c.get("/secondary/analysis").text

        response = c.post("/secondary/exclusions/extension", data={"extension": "LRC"})
        assert response.headers["location"].endswith("saved=1")
        response = c.post("/secondary/exclusions/extension", data={"extension": "a/b"})
        assert "not+a+file+extension" in response.headers["location"]
        with Store(libraries.database_path) as store:
            kinds = [(r.kind, r.key) for r in store.secondary_exclusions()]
        assert kinds == [("extension", ".jpg"), ("extension", ".lrc")]
        page = c.get("/secondary/analysis").text
        assert "Pixies/Doolitle/01 Debaser.lrc" in page  # now unlinked: excluded


def test_album_exclusions(libraries) -> None:
    from tagdrifter.secondary import album_exclusions, excluded_by_folder

    with Store(libraries.database_path) as store:
        assert album_exclusions(store, "Pixies") is None  # no audio directly in it
        album = album_exclusions(store, "/Pixies/Doolittle/")
        assert album is not None and album.excluded == 0 and not album.rules
        assert (album.release.rule.kind, album.release.saved) == ("release", False)
        assert album.release.rule.label == "Pixies - Doolittle"
        assert (album.artist.rule.kind, album.artist.rule.label, album.artist.files) == (
            "artist",
            "Pixies",
            1,  # Other has its own album artist MBID
        )
        (track,) = album.tracks.values()
        assert track.rule.kind == "track" and track.rule.label == "Pixies - Debaser"
        assert excluded_by_folder(store) == {}

        store.add_secondary_exclusions([album.release.rule])
        album = album_exclusions(store, "Pixies/Doolittle")
        assert album.excluded == 1 and album.release.saved and album.release.rule.id
        # Other/Elsewhere is tagged with the same release MBID, so the rule covers it too.
        (view,) = album.rules
        assert (view.rule, view.files) == (album.release.rule, 2)
        assert album.by_track == {"Pixies/Doolittle/01 Debaser.flac": [album.release.rule]}
        assert excluded_by_folder(store) == {"Pixies/Doolittle": 1, "Other/Elsewhere": 1}


def test_library_exclusion_toggles(libraries) -> None:
    from fastapi.testclient import TestClient

    from tagdrifter.web import create_app

    url = "/library/album?folder=Pixies%2FDoolittle"
    with TestClient(create_app(libraries), follow_redirects=False) as c:
        page = c.get(url).text
        assert 'id="secondary"' in page and "Every track of this album belongs" in page
        assert "Exclude every album by Pixies (1 track)" in page
        assert "Include again" not in page

        form = {"folder": "Pixies/Doolittle", "kind": "release"}
        response = c.post("/library/exclude", data=form)
        assert response.headers["location"] == url + "#secondary"
        page = c.get(url).text
        assert "This album is left out of the secondary library." in page
        assert "Include again" in page and "Exclude this album" not in page
        assert ">excluded</span>" in c.get("/library").text
        assert "Pixies/Doolitle/01 Debaser.flac" in c.get("/secondary/analysis").text  # unlink

        with Store(libraries.database_path) as store:
            (rule,) = store.secondary_exclusions()
        response = c.post(f"/secondary/exclusions/{rule.id}/delete", data={"next": url})
        assert response.headers["location"] == url
        assert c.post(f"/secondary/exclusions/{rule.id}/delete").headers["location"] == (
            "/secondary"
        )

        path = "Pixies/Doolittle/01 Debaser.flac"
        response = c.post("/library/exclude", data={**form, "kind": "track", "path": path})
        assert response.headers["location"] == url + "#tracks"
        with Store(libraries.database_path) as store:
            assert [r.kind for r in store.secondary_exclusions()] == ["track"]
        bad = [{**form, "kind": "nope"}, {**form, "kind": "track"}, {**form, "folder": "Pixies"}]
        for data in bad:
            assert c.post("/library/exclude", data=data).status_code == 404

    off = replace(libraries, secondary_path=None)
    with TestClient(create_app(off)) as c:
        assert 'id="secondary"' not in c.get(url).text
        assert c.post("/library/exclude", data=form).status_code == 404


def test_secondary_page_problems_and_off(library, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from tagdrifter.web import create_app

    settings = settings_for(library, tmp_path, secondary_path=tmp_path / "secondary")
    with TestClient(create_app(settings)) as c:
        assert "a folder (or isn" in c.get("/secondary/analysis").text
    with TestClient(create_app(settings_for(library, tmp_path))) as c:
        assert 'href="/secondary"' not in c.get("/").text
        assert c.get("/secondary").status_code == 404
    with pytest.raises(ValueError, match="inside the library"):
        create_app(settings_for(library, tmp_path, secondary_path=library / "x"))


def test_secondary_cli(libraries, capsys, monkeypatch) -> None:
    from tagdrifter.cli import main

    monkeypatch.setenv("TAGDRIFTER_SECONDARY", str(libraries.secondary_path))
    args = ["--library", str(libraries.library_path), "--db", str(libraries.database_path)]
    assert main([*args, "secondary"]) == 0
    out = capsys.readouterr().out
    assert "Move (renamed in the library) (2):" in out and "Link (2):" in out
    assert "secondary --propose" in out
    assert main([*args, "secondary", "--propose"]) == 0
    out = capsys.readouterr().out
    assert "Album artist  Other  (1 files, Other/Elsewhere)" in out
    assert "File type     .jpg  (1 files, anywhere)" in out
    assert main([*args, "secondary", "--propose", "--save", "--yes"]) == 0
    assert "Saved 2 exclusions." in capsys.readouterr().out
    assert main([*args, "secondary"]) == 0
    assert "Link (" not in capsys.readouterr().out
    assert main([*args, "secondary", "--exclude-type", "lrc", "--exclude-type", ".TXT"]) == 0
    assert "Saved 2 exclusions (.lrc, .txt)." in capsys.readouterr().out
    assert main([*args, "secondary", "--exclude-type", "a b"]) == 2
    assert main([*args, "secondary"]) == 0
    assert "Pixies/Doolitle/01 Debaser.lrc  (excluded)" in capsys.readouterr().out


def test_the_marker_can_be_turned_off(tmp_path) -> None:
    lib, sec = tmp_path / "music", tmp_path / "secondary"
    lib.mkdir()
    sec.mkdir()
    settings = settings_for(lib, tmp_path, secondary_path=sec)
    assert MARKER in check(settings).problems[0]
    assert "TAGDRIFTER_SECONDARY_MARKER=false" in check(settings).problems[0]
    assert check(replace(settings, secondary_marker=False)).ok
    cfg = tmp_path / "config.toml"
    cfg.write_text("[secondary]\nmarker = false\n", encoding="utf-8")
    assert load_settings(cfg, env={}).secondary_marker is False
    env = {"TAGDRIFTER_SECONDARY_MARKER": "false"}
    assert load_settings(env=env).secondary_marker is False
    assert load_settings(env={}).secondary_marker is True
