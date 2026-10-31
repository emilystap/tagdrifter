"""Track by track (6d follow-up): which releases of the group fit an album's files between
them, and for files that fit none, recording and ISRC lookups on request."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tagdrifter.db import Store
from tagdrifter.library import album_detail, compare_release_group
from tagdrifter.musicbrainz import lookup_tracks, normalize_isrc
from tagdrifter.web import create_app
from tagdrifter.web.format import length_diff
from tagdrifter.web.mbsync import MusicBrainzSync
from tagdrifter.web.runner import ScanRunner

from .test_library import CAKE_1998, CAKE_2023, CAKE_FOLDER, CAKE_GROUP, cake_album, text
from .test_musicbrainz import client_with, fixture

LET_ME_GO = "235efc48-16f8-4b48-8688-bf88010c1a32"  # the recording track 11 is tagged with
LET_ME_GO_ISRC = "USCA19886895"
LET_ME_GO_SINGLE = "dcd9c114-80da-4b28-aa34-5ad9914097e4"  # 1999 US CD single, 3:55.066
LET_ME_GO_OTHER = "bbd1b7ad-01b0-469f-8ac6-667040a3051d"  # 3:30, also has the ISRC
TRACKS = f"/library/tracks?folder={CAKE_FOLDER}&id={CAKE_GROUP}"
GROUP = f"/library/release-group?folder={CAKE_FOLDER}&id={CAKE_GROUP}"


def odd_one_out(tmp_path, **kw):
    """Track 11 at 3:55.1 fits no release in the group within 0.5 s (they say 3:56 or 3:30),
    only the 1999 single outside it."""
    return cake_album(
        tmp_path,
        lengths={11: 235_100},
        check_options={"mb.duration": {"tolerance_seconds": 0.5}},
        **kw,
    )


def test_files_put_down_to_the_releases_that_fit_them(tmp_path) -> None:
    settings = cake_album(tmp_path)
    with Store(settings.database_path) as store:
        store.save_mb_release_group(CAKE_GROUP, fixture("prolonging_the_magic_group"))
        album = album_detail(store, CAKE_FOLDER)
        comparison = compare_release_group(store, album, CAKE_GROUP, 3000)
    sources = comparison.tracks
    # The 1998 CD fits tracks 9 and 11; only the 2023 Deluxe Edition has track 14.
    assert [(key, fit.release.id, n) for key, fit, n in sources.keyed_cover] == [
        ("A", CAKE_1998, 2),
        ("B", CAKE_2023, 1),
    ]
    by_number = {t.file.meta.track_number: t for t in sources.tracks}
    assert [by_number[n].cover_key for n in (9, 11, 14)] == ["A", "A", "B"]
    assert [by_number[n].group_fits for n in (9, 14)] == [1, 1]
    assert not sources.unexplained and not sources.lookup_keys  # nothing to ask MusicBrainz


def test_no_track_by_track_when_one_release_fits_every_file(tmp_path) -> None:
    settings = cake_album(tmp_path)
    with Store(settings.database_path) as store:
        store.save_mb_release_group(CAKE_GROUP, fixture("prolonging_the_magic_group"))
        album = album_detail(store, CAKE_FOLDER)
        # Generous enough for the Deluxe Edition's 26 s longer Let Me Go.
        assert compare_release_group(store, album, CAKE_GROUP, 30_000).tracks is None


def test_track_lookups_on_the_album_page(tmp_path) -> None:
    settings = odd_one_out(tmp_path, musicbrainz_enabled=True)
    client, opener = client_with(
        (200, fixture("prolonging_the_magic_group")),
        (200, fixture("let_me_go_recording")),
        (200, fixture("let_me_go_isrc")),
    )
    runner = ScanRunner(settings, mb_sync=MusicBrainzSync(settings, client))
    with TestClient(create_app(settings, runner)) as c:
        section = text(c.post(GROUP))
        assert "Track by track" in section and "Nothing in this release group" in section
        assert "Not looked up yet" in section and "Look up this track" in section
        assert len(opener.requests) == 1  # only the release group so far

        section = text(c.post(TRACKS))
        recording, isrc = (r.full_url for r in opener.requests[1:])
        assert f"/recording/{LET_ME_GO}?inc=releases+media&fmt=json" in recording
        assert isrc.endswith(f"/isrc/{LET_ME_GO_ISRC}?fmt=json")
        assert "<html" not in section and "Not looked up yet" not in section
        # The single fits; the 1998 CDs outside the trimmed group fixture don't (3:56).
        assert "On other releases:" in section
        assert f'href="https://musicbrainz.org/release/{LET_ME_GO_SINGLE}"' in section
        assert "1999 · US · CD · track 2 · +0.0 s" in section
        assert f"Same ISRC ({LET_ME_GO_ISRC})" in section
        assert f'href="https://musicbrainz.org/recording/{LET_ME_GO_OTHER}"' in section
        assert "+25 s" in section and "Look up again" in section

        # Again within minutes: nothing sent.
        again = text(c.post(TRACKS))
        assert len(opener.requests) == 3 and "Looked up a few minutes ago" in again
        # The album page shows what was found without asking.
        page = text(c.get("/library/album", params={"folder": CAKE_FOLDER}))
        assert "On other releases:" in page and len(opener.requests) == 3


def test_track_lookups_need_the_comparison_first(tmp_path) -> None:
    settings = odd_one_out(tmp_path, musicbrainz_enabled=True)
    client, opener = client_with()
    runner = ScanRunner(settings, mb_sync=MusicBrainzSync(settings, client))
    with TestClient(create_app(settings, runner)) as c:
        assert c.post(TRACKS).status_code == 409
        assert c.post(f"/library/tracks?folder={CAKE_FOLDER}&id={CAKE_2023}").status_code == 404
    assert not opener.requests


def test_failed_track_lookup_pauses_lookups(tmp_path) -> None:
    settings = odd_one_out(tmp_path, musicbrainz_enabled=True)
    with Store(settings.database_path) as store:
        store.save_mb_release_group(CAKE_GROUP, fixture("prolonging_the_magic_group"))
    client, opener = client_with((500, None))
    sync = MusicBrainzSync(settings, client)
    with TestClient(create_app(settings, ScanRunner(settings, mb_sync=sync))) as c:
        section = text(c.post(TRACKS))
        assert "MusicBrainz answered 500" in section and "Lookups are paused" in section
        assert sync.status().paused_until is not None
        section = text(c.post(TRACKS))
        assert "paused after an error" in section and len(opener.requests) == 1


def test_no_track_lookups_when_lookups_are_off(tmp_path) -> None:
    settings = odd_one_out(tmp_path)
    with Store(settings.database_path) as store:
        store.save_mb_release_group(CAKE_GROUP, fixture("prolonging_the_magic_group"))
    with TestClient(create_app(settings)) as c:
        page = text(c.get("/library/album", params={"folder": CAKE_FOLDER}))
        assert "Track by track" in page and "Look up this track" not in page
        assert "MusicBrainz lookups are off." in text(c.post(TRACKS))


# --- lookup_tracks ----------------------------------------------------------------------


def test_lookups_a_few_at_a_time_new_ones_first(tmp_path) -> None:
    keys = [
        ("recording", LET_ME_GO.upper()),
        ("isrc", "usca1-98-86895"),
        ("recording", LET_ME_GO_OTHER),
    ]
    client, opener = client_with(
        (200, fixture("let_me_go_recording")),
        (200, fixture("let_me_go_isrc")),
        (404, None),
        (200, fixture("let_me_go_recording")),
        (200, fixture("let_me_go_isrc")),
    )
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    with Store(tmp_path / "td.db") as store:
        first = lookup_tracks(store, client, keys, now=now, limit=2)
        assert (first.due, first.fetched, first.not_found) == (3, 2, 0)
        second = lookup_tracks(store, client, keys, now=now, limit=2)  # only the one left
        assert (second.due, second.fetched, second.not_found, second.skipped) == (1, 0, 1, 2)
        cached = store.mb_lookups(
            [("recording", LET_ME_GO), ("isrc", LET_ME_GO_ISRC), ("recording", LET_ME_GO_OTHER)]
        )
        assert cached[("recording", LET_ME_GO_OTHER)][0] is None  # not found, remembered
        assert cached[("isrc", LET_ME_GO_ISRC)][0]["isrc"] == LET_ME_GO_ISRC

        # All cached: looked up again only when someone asks after `min_age`. The cached
        # entries carry the real time they were saved, so "now" is measured from that.
        real_now = datetime.now(UTC)
        assert lookup_tracks(store, client, keys, now=real_now, min_age=600).due == 0
        later = lookup_tracks(store, client, keys, now=real_now + timedelta(hours=1), limit=2)
        assert (later.due, later.fetched) == (3, 2)
    assert len(opener.requests) == 5


def test_lookup_stops_at_the_first_error(tmp_path) -> None:
    client, opener = client_with((503, None), (503, None), (503, None))
    with Store(tmp_path / "td.db") as store:
        result = lookup_tracks(store, client, [("recording", LET_ME_GO), ("isrc", LET_ME_GO_ISRC)])
        assert result.error and result.fetched == 0
        assert not store.mb_lookups([("recording", LET_ME_GO)])
    assert all("/recording/" in r.full_url for r in opener.requests)  # never got to the ISRC


def test_isrcs_and_bad_keys() -> None:
    assert normalize_isrc(" usca1-98-86895 ") == LET_ME_GO_ISRC
    assert normalize_isrc("USCA1988689") == "" and normalize_isrc("12CA19886895") == ""
    client, opener = client_with()
    with pytest.raises(ValueError):
        client.isrc("nope")
    with pytest.raises(ValueError):
        client.recording("not-an-mbid")
    assert not opener.requests


def test_length_diff() -> None:
    assert length_diff(34) == "+0.0 s"
    assert length_diff(-307) == "-0.3 s"
    assert length_diff(25_100) == "+25 s"
    assert length_diff(None) == "–"
