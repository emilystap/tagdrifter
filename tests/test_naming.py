from tagdrifter.core import compute_target, resolve_release_collisions

from .conftest import make_meta


def test_single_disc_filename() -> None:
    meta = make_meta(
        "Pixies/Surfer Rosa/x.flac",
        albumartist="Pixies",
        artist="Pixies",
        album="Surfer Rosa",
        title="Where Is My Mind?",
        tracknumber="7/13",
        discnumber="1/1",
    )
    target = compute_target(meta, "Pixies")
    assert (
        str(target.relative_path)
        == "Pixies/Surfer Rosa/Pixies_Surfer Rosa_07_Where Is My Mind.flac"
    )


def test_multi_disc_filename() -> None:
    meta = make_meta(
        artist="Pixies", album="Doolittle", title="Debaser", tracknumber="1", discnumber="2"
    )
    assert compute_target(meta, "Pixies").filename == "Pixies_Doolittle_02-01_Debaser.flac"


def test_album_folder_with_disambiguation() -> None:
    meta = make_meta(artist="Wolfmother", album="Wolfmother", albumversion="Deluxe Edition")
    assert compute_target(meta, "Wolfmother").album_folder == "Wolfmother (Deluxe Edition)"


def test_primary_artist_from_artists_tag() -> None:
    meta = make_meta(
        albumartist="Adele",
        artist="Adele feat. The Raconteurs",
        artists="Adele; The Raconteurs",
        album="21",
        title="T",
        tracknumber="1",
    )
    assert compute_target(meta, "Adele").track_artist == "Adele"


def test_primary_artist_strips_feat_without_artists_tag() -> None:
    meta = make_meta(artist="Someone ft. Other", album="A", title="T")
    assert compute_target(meta, "Various Artists").track_artist == "Someone"


def test_folder_spelling_wins_for_stylized_artist() -> None:
    meta = make_meta(albumartist="Ke$ha", artist="Ke$ha", album="Animal", title="Tik Tok")
    assert compute_target(meta, "Kesha").filename.startswith("Kesha_Animal_")


def test_sort_tag_resolves_stylization_on_compilation() -> None:
    meta = make_meta(
        albumartist="Various Artists", artist="Ke$ha", artistsort="Kesha", album="Hits", title="T"
    )
    assert compute_target(meta, "Various Artists").track_artist == "Kesha"


def test_inverted_sort_tag_is_not_used() -> None:
    meta = make_meta(artist="The Cure", artistsort="Cure, The", album="Hits", title="T")
    assert compute_target(meta, "Various Artists").track_artist == "The Cure"


def test_split_album_keeps_each_track_artist() -> None:
    folder = "Bikini Kill & Huggy Bear"
    bk = make_meta(artist="Bikini Kill", albumartist=folder, album="Yeah Yeah Yeah Yeah", title="A")
    hb = make_meta(artist="Huggy Bear", albumartist=folder, album="Yeah Yeah Yeah Yeah", title="B")
    assert compute_target(bk, folder).track_artist == "Bikini Kill"
    assert compute_target(hb, folder).track_artist == "Huggy Bear"


def test_empty_sort_tag_is_ignored() -> None:
    # v1 treated an empty ARTISTSORT as authoritative and produced "_Album_01_T.flac".
    meta = make_meta(artist="...", artistsort="", album="Album", title="T")
    assert compute_target(meta, "Various Artists").filename == "(period)_Album_01_T.flac"


def test_extension_follows_source_file() -> None:
    meta = make_meta("A/B/x.FLAC", artist="A", album="B", title="T")
    assert compute_target(meta, "A").filename.endswith(".flac")


# --- Release collisions -----------------------------------------------------------------


def _release(folder: str, mbid: str, date: str = "", count: int = 1, **extra: str):
    return [
        make_meta(
            f"Hole/{folder}/t{i}.flac",
            artist="Hole",
            album="Pretend We're Dead",
            title=f"T{i}",
            tracknumber=str(i + 1),
            musicbrainz_albumid=mbid,
            date=date,
            **extra,
        )
        for i in range(count)
    ]


def _resolve(*releases):
    metas = [m for r in releases for m in r]
    items = [(m, compute_target(m, "Hole")) for m in metas]
    return resolve_release_collisions(items)


def test_collision_by_year() -> None:
    targets, notices = _resolve(_release("a", "mbid-a", "1992"), _release("b", "mbid-b", "1993"))
    assert {t.album_folder for t in targets} == {
        "Pretend We're Dead (1992)",
        "Pretend We're Dead (1993)",
    }
    assert len(notices) == 2


def test_collision_by_year_and_track_count() -> None:
    targets, _ = _resolve(
        _release("a", "mbid-a", "1992", count=1), _release("b", "mbid-b", "1992", count=2)
    )
    assert {t.album_folder for t in targets} == {
        "Pretend We're Dead (1992, 1 tracks)",
        "Pretend We're Dead (1992, 2 tracks)",
    }


def test_collision_by_catalog_then_mbid() -> None:
    targets, _ = _resolve(
        _release("a", "aaaaaaaa-1", "1992", catalognumber="CAT-1"),
        _release("b", "bbbbbbbb-2", "1992", catalognumber="CAT-2"),
    )
    assert {t.album_folder for t in targets} == {
        "Pretend We're Dead (1992, CAT-1)",
        "Pretend We're Dead (1992, CAT-2)",
    }

    targets, _ = _resolve(_release("a", "aaaaaaaa-1", "1992"), _release("b", "bbbbbbbb-2", "1992"))
    assert {t.album_folder for t in targets} == {
        "Pretend We're Dead (1992)",
        "Pretend We're Dead (1992 [bbbbbbbb])",
    }


def test_no_collision_for_single_release_across_folders() -> None:
    # Same MBID split across two folders is one release: no renaming.
    a = _release("a", "same", "1992")
    b = _release("b", "same", "1992")
    targets, notices = _resolve(a, b)
    assert notices == []
    assert {t.album_folder for t in targets} == {"Pretend We're Dead"}


def test_collision_without_mbid_uses_source_folder() -> None:
    _, notices = _resolve(_release("a", "", "1992"), _release("b", "", "1993"))
    assert len(notices) == 2


def test_collision_ignores_case() -> None:
    a = _release("a", "mbid-a", "1992")
    b = [
        make_meta(
            "Hole/b/t.flac",
            artist="Hole",
            album="pretend we're dead",
            title="T",
            musicbrainz_albumid="mbid-b",
            date="1993",
        )
    ]
    _, notices = _resolve(a, b)
    assert len(notices) == 2
