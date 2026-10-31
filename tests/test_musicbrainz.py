"""MusicBrainz client, release cache, sync and mb.* checks. No test touches the network: the
client gets a fake opener, and releases come from recorded fixtures."""

import gzip
import io
import json
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

from tagdrifter.checks import AlbumContext
from tagdrifter.checks.builtin_musicbrainz import Duration, Numbering, ReleaseUnknown, TrackMismatch
from tagdrifter.config import Settings, load_settings
from tagdrifter.core.mbrelease import MbRelease
from tagdrifter.db import Store
from tagdrifter.musicbrainz import (
    MAX_BROWSE_PAGES,
    MIN_INTERVAL,
    RELEASE_INC,
    USER_AGENT,
    MusicBrainzClient,
    MusicBrainzError,
    RateLimiter,
    ReleaseNotFound,
    SyncResult,
    retry_after_seconds,
    sync_releases,
    user_agent,
)
from tagdrifter.web import ScanRunner, create_app
from tagdrifter.web.mbsync import MusicBrainzSync

from .conftest import build_flac, good_tags, scan, settings_for
from .test_checks import track_ctx

FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"
MAENARAWANAI = "bbbc8eb5-c0ed-4497-8418-ba851f18d328"
BRAT = "b2176814-eccb-4652-9de2-d719e17f6952"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# --- Client -----------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, gzipped: bool = True) -> None:
        super().__init__(gzip.compress(body) if gzipped else body)
        self.headers = {"Content-Encoding": "gzip"} if gzipped else {}


class FakeOpener:
    """Answers queued (status, body) pairs and records the requests."""

    def __init__(self, *answers, clock: FakeClock | None = None, latency: float = 0) -> None:
        self.answers = list(answers)
        self.requests = []
        self.clock, self.latency = clock, latency  # simulated time each answer takes

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.clock is not None:
            self.clock.now += self.latency
        status, body, *headers = self.answers.pop(0)
        if status != 200:
            hdrs = headers[0] if headers else {}
            raise urllib.error.HTTPError(request.full_url, status, "x", hdrs, io.BytesIO(b""))
        return FakeResponse(json.dumps(body).encode())


def client_with(
    *answers, clock: FakeClock | None = None, latency: float = 0
) -> tuple[MusicBrainzClient, FakeOpener]:
    clock = clock or FakeClock()
    opener = FakeOpener(*answers, clock=clock, latency=latency)
    limiter = RateLimiter(MIN_INTERVAL, clock=clock, sleep=clock.sleep)
    return MusicBrainzClient(limiter=limiter, opener=opener, backoff=(15, 60)), opener


def test_release_request() -> None:
    client, opener = client_with((200, {"id": MAENARAWANAI}))
    assert client.release(MAENARAWANAI.upper())["id"] == MAENARAWANAI
    (request,) = opener.requests
    assert request.full_url == (
        f"https://musicbrainz.org/ws/2/release/{MAENARAWANAI}?inc={RELEASE_INC}&fmt=json"
    )
    assert request.get_header("User-agent") == USER_AGENT
    assert "( https://github.com/emilystap/tagdrifter )" in USER_AGENT
    assert request.get_header("Accept-encoding") == "gzip"
    with pytest.raises(ValueError):
        client.release("not-an-mbid")  # never sent


def test_rate_limiter_spaces_requests() -> None:
    clock = FakeClock()
    client, _ = client_with(*[(200, {"id": "x"})] * 3, clock=clock)
    for _ in range(3):
        client.release(MAENARAWANAI)
    assert MIN_INTERVAL >= 1.5
    assert clock.slept == [MIN_INTERVAL, MIN_INTERVAL]  # the first goes at once


def test_rate_limiter_counts_from_the_response() -> None:
    # A slow answer doesn't use up the gap: the next request still waits the full interval
    # after it, so jitter on the way can't squeeze two requests under a second apart.
    clock = FakeClock()
    client, _ = client_with(*[(200, {"id": "x"})] * 2, clock=clock, latency=2.0)
    client.release(MAENARAWANAI)
    client.release(MAENARAWANAI)
    assert clock.slept == [MIN_INTERVAL]  # counted from start to start, it would be none


def test_not_found_and_errors() -> None:
    client, _ = client_with((404, None))
    with pytest.raises(ReleaseNotFound):
        client.release(MAENARAWANAI)
    client, _ = client_with((500, None))
    with pytest.raises(MusicBrainzError, match="answered 500"):
        client.release(MAENARAWANAI)


def test_503_backs_off_then_gives_up() -> None:
    clock = FakeClock()
    client, _ = client_with((503, None), (200, {"id": "ok"}), clock=clock)
    assert client.release(MAENARAWANAI) == {"id": "ok"}
    assert clock.slept == [15.0]  # the pause, before the retry
    clock = FakeClock()
    client, opener = client_with((503, None), (503, None), (503, None), clock=clock)
    with pytest.raises(MusicBrainzError, match="503"):
        client.release(MAENARAWANAI)
    assert len(opener.requests) == 3 and clock.slept == [15.0, 60.0]


# --- Cache and sync ---------------------------------------------------------------------


def store_with_files(tmp_path, *release_ids: str) -> Store:
    store = Store(tmp_path / "td.db")
    rows = [
        (f"A/{i}/1.flac", 1, 1, {"musicbrainz_albumid": [mbid]}, None)
        for i, mbid in enumerate(release_ids)
    ]
    store.save_files([*rows, ("A/x/2.flac", 1, 1, {"musicbrainz_albumid": ["junk"]}, None)])
    return store


def test_cache_round_trip(tmp_path) -> None:
    with store_with_files(tmp_path, MAENARAWANAI, BRAT.upper()) as store:
        assert store.library_release_ids() == {MAENARAWANAI, BRAT}
        store.save_mb_release(BRAT, fixture("brat"), "2027-01-01")
        store.save_mb_release(MAENARAWANAI, None, "2027-01-01")  # not found
        cached = store.mb_releases()
        assert cached[BRAT]["title"].startswith("Brat") and cached[MAENARAWANAI] is None
        assert store.mb_cache_counts() == (2, 2)
        blob = store.conn.execute("SELECT data FROM mb_releases WHERE mbid=?", (BRAT,)).fetchone()
        assert len(blob[0]) < len(json.dumps(fixture("brat"))) / 3  # stored compressed


def test_sync_order_limit_and_refresh(tmp_path) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    ids = [f"11111111-1111-4111-8111-{i:012d}" for i in range(4)]
    with store_with_files(tmp_path, *ids) as store:
        store.save_mb_release(ids[0], {"id": ids[0]}, "2026-10-01")  # due
        store.save_mb_release(ids[1], {"id": ids[1]}, "2027-01-01")  # fresh
        assert store.mb_releases_due(now.isoformat()) == [ids[2], ids[3], ids[0]]

        client, opener = client_with((200, {"id": ids[2]}), (404, None))
        result = sync_releases(store, client, limit=2, now=now)
        assert (result.due, result.fetched, result.not_found) == (2, 1, 1)
        assert len(opener.requests) == 2
        assert store.mb_releases_due(now.isoformat()) == [ids[0]]
        refresh = store.conn.execute(
            "SELECT refresh_after FROM mb_releases WHERE mbid=?", (ids[2],)
        ).fetchone()[0]
        assert (
            now + timedelta(days=45) <= datetime.fromisoformat(refresh) <= now + timedelta(days=75)
        )


def test_sync_stops_at_first_error_and_keeps_work(tmp_path) -> None:
    ids = [f"11111111-1111-4111-8111-{i:012d}" for i in range(3)]
    with store_with_files(tmp_path, *ids) as store:
        client, opener = client_with((200, {"id": ids[0]}), (500, None))
        result = sync_releases(store, client)
        assert result.fetched == 1 and "500" in result.error
        assert len(opener.requests) == 2  # nothing after the failure
        assert set(store.mb_releases()) == {ids[0]}


# --- Parsing ----------------------------------------------------------------------------


def test_parse_real_releases() -> None:
    brat = MbRelease.from_json(fixture("brat"))
    assert [m.position for m in brat.media] == [2, 3]
    first = brat.media[0].tracks[0]
    assert (first.disc, first.position, first.number, first.title) == (2, 1, "A1", "360")
    assert brat.medium(3).track_count == 8 and brat.track_at(3, 8).title == "Spring breakers"

    ag = MbRelease.from_json(fixture("maenarawanai"))
    track = ag.tracks[0]
    assert track.title == "sekigae Gattuso" and track.recording_title != track.title
    assert track.length_ms == 189000


# --- Checks -----------------------------------------------------------------------------


def brat_track(disc: int, position: int, **tags):
    """A file tagged by Picard from the Brat fixture."""
    release = MbRelease.from_json(fixture("brat"))
    t = release.track_at(disc, position)
    defaults = {
        "musicbrainz_albumid": BRAT,
        "musicbrainz_releasetrackid": t.id,
        "musicbrainz_trackid": t.recording_id,
        "tracknumber": str(t.position),
        "discnumber": str(t.disc),
        "totaltracks": str(release.medium(t.disc).track_count),
        "totaldiscs": "2",
        "~length": str(t.length_ms),
    }
    return track_ctx(f"Charli xcx/Brat/{disc}-{position:02d}.flac", **(defaults | tags))


def brat_album(*tracks, release: dict | None = None) -> AlbumContext:
    data = fixture("brat") if release is None else release
    releases = {BRAT: MbRelease.from_json(data)}
    return AlbumContext(PurePosixPath("Charli xcx/Brat"), tracks, (), (), Settings(), releases)


ALL_MB_CHECKS = (ReleaseUnknown(), TrackMismatch(), Numbering(), Duration())


def issues(album: AlbumContext) -> list:
    return [i for check in ALL_MB_CHECKS for i in check.run(album)]


def test_picard_tagged_album_has_no_issues() -> None:
    assert issues(brat_album(brat_track(2, 1), brat_track(2, 2), brat_track(3, 8))) == []


def test_unlooked_up_release_is_skipped() -> None:
    album = AlbumContext(PurePosixPath("X/Y"), (brat_track(2, 1),), (), (), Settings(), {})
    assert issues(album) == []


def test_release_not_found_or_merged() -> None:
    album = AlbumContext(
        PurePosixPath("Charli xcx/Brat"), (brat_track(2, 1),), (), (), Settings(), {BRAT: None}
    )
    (issue,) = ReleaseUnknown().run(album)
    assert "isn't in MusicBrainz" in issue.message and issue.severity == "error"

    merged = fixture("brat") | {"id": "99999999-9999-4999-8999-999999999999"}
    (issue,) = ReleaseUnknown().run(brat_album(brat_track(2, 1), release=merged))
    assert "was merged into 99999999-9999-4999-8999-999999999999" in issue.message


def test_track_mismatch() -> None:
    wrong_recording = brat_track(2, 1, musicbrainz_trackid="11111111-1111-4111-8111-111111111111")
    (issue,) = TrackMismatch().run(brat_album(wrong_recording))
    assert issue.message.startswith("Recording MBID differs: MusicBrainz has")
    assert "('360') for disc 2 track 1" in issue.message

    other_release = brat_track(
        2, 1, musicbrainz_releasetrackid="22222222-2222-4222-8222-222222222222"
    )
    (issue,) = TrackMismatch().run(brat_album(other_release))
    assert "Release track MBID isn't on" in issue.message
    assert "at disc 2 track 1 it has '360'" in issue.message


def test_numbering() -> None:
    album = brat_album(
        brat_track(2, 1, tracknumber="3", totaldiscs="3"),
        brat_track(2, 2, totaltracks="11", totaldiscs="3"),
        brat_track(2, 3, totaltracks="11", totaldiscs="3"),
    )
    track, totals = Numbering().run(album)
    assert track.subject == "Charli xcx/Brat/2-01.flac"
    assert track.message == "MusicBrainz has track number 1 (tagged 3)"
    # Totals once, on the album: one disc involved, so just "total tracks".
    assert totals.subject == "Charli xcx/Brat"
    assert totals.message == "MusicBrainz has total discs 2 (tagged 3), total tracks 10 (tagged 11)"
    # Missing tags aren't this check's business.
    assert not list(Numbering().run(brat_album(brat_track(2, 1, totaltracks=""))))


def test_duration_tolerance() -> None:
    length = MbRelease.from_json(fixture("brat")).track_at(2, 1).length_ms
    close = brat_track(2, 1, **{"~length": str(length + 2500)})
    assert not list(Duration().run(brat_album(close)))
    live = brat_track(2, 1, **{"~length": str(length + 41_000)})
    (issue,) = Duration().run(brat_album(live))
    assert issue.message == "Length 2:55 vs 2:14 in MusicBrainz (+41 s)"
    assert not list(Duration({"tolerance_seconds": 60}).run(brat_album(live)))
    assert not list(Duration().run(brat_album(brat_track(2, 1, **{"~length": "0"}))))


# --- Scan and web -----------------------------------------------------------------------


def test_scan_applies_cached_releases_only_when_enabled(tmp_path) -> None:
    lib = tmp_path / "music"
    tags = good_tags(musicbrainz_albumid=MAENARAWANAI, TITLE="sekigae Gattuso")
    build_flac(lib / "ATARASHII GAKKO!/Maenarawanai/a.flac", tags, length_ms=150_000)
    settings = settings_for(lib, tmp_path)
    scan(settings)
    with Store(settings.database_path) as store:
        store.save_mb_release(MAENARAWANAI, fixture("maenarawanai"), "2099-01-01")

    _, found = scan(settings)
    assert not [k for k in found if k[0].startswith("mb.")]  # lookups off: cache ignored

    _, found = scan(settings_for(lib, tmp_path, musicbrainz_enabled=True))
    subject = "ATARASHII GAKKO!/Maenarawanai/a.flac"
    assert ("mb.duration", subject) in found  # 2:30 vs 3:09
    assert ("mb.track_mismatch", subject) in found  # the fixture's recording IDs aren't ours


def test_setting_from_toml_and_env(tmp_path) -> None:
    assert Settings().musicbrainz_enabled is False
    cfg = tmp_path / "config.toml"
    cfg.write_text("[musicbrainz]\nenabled = true\n", encoding="utf-8")
    assert load_settings(cfg, env={}).musicbrainz_enabled is True
    assert load_settings(cfg, env={"TAGDRIFTER_MUSICBRAINZ": "off"}).musicbrainz_enabled is False


def test_sync_after_scan_then_rescan(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path, musicbrainz_enabled=True)
    release_id = good_tags()["musicbrainz_albumid"]
    client, opener = client_with((200, {"id": release_id, "title": "Doolittle", "media": []}))
    sync = MusicBrainzSync(settings, client)
    runner = ScanRunner(settings, mb_sync=sync)
    assert runner.start()
    runner.wait(30)
    sync.wait(30)
    runner.wait(30)  # the rescan the sync started
    assert len(opener.requests) == 1  # every fixture track shares one release
    assert sync.status().last.fetched == 1
    with Store(settings.database_path) as store:
        assert store.mb_cache_counts() == (1, 1)
        scans = store.recent_scans()
    assert len(scans) == 2
    assert not sync.after_scan()  # nothing due: no thread, no request
    assert len(opener.requests) == 1


def test_sync_off_by_default(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path)
    client, opener = client_with()
    sync = MusicBrainzSync(settings, client)
    assert not sync.after_scan()
    assert opener.requests == []


def test_dashboard_panel(library, tmp_path) -> None:
    off = settings_for(library, tmp_path)
    with TestClient(create_app(off)) as c:
        assert "Set <code>TAGDRIFTER_MUSICBRAINZ=true</code>" in c.get("/").text
    on = settings_for(library, tmp_path, musicbrainz_enabled=True)
    scan(on)
    runner = ScanRunner(on, mb_sync=MusicBrainzSync(on, client_with()[0]))
    with TestClient(create_app(on, runner)) as c:
        assert "0 of 1 release cached" in " ".join(c.get("/").text.split())
        assert "0 of 1 release cached" in " ".join(c.get("/musicbrainz/status").text.split())


def test_tests_cannot_reach_musicbrainz() -> None:
    with pytest.raises(AssertionError, match="test tried to call MusicBrainz"):
        MusicBrainzClient(limiter=RateLimiter(0)).release(MAENARAWANAI)


# --- Politeness: Retry-After, cooldown, contact -------------------------------------------


def test_retry_after_parsing() -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert retry_after_seconds({"Retry-After": "120"}) == 120
    assert retry_after_seconds({"Retry-After": "Sun, 01 Nov 2026 12:05:00 GMT"}, now) == 300
    assert retry_after_seconds({"Retry-After": "Sun, 01 Nov 2026 11:00:00 GMT"}, now) == 0
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    assert retry_after_seconds({}) is None


def test_503_waits_at_least_retry_after() -> None:
    clock = FakeClock()
    client, _ = client_with((503, None, {"Retry-After": "40"}), (200, {"id": "ok"}), clock=clock)
    assert client.release(MAENARAWANAI) == {"id": "ok"}
    assert clock.slept == [40.0]  # longer than the 15 s backoff, so MusicBrainz's wait wins


def test_long_retry_after_ends_the_run_at_once() -> None:
    clock = FakeClock()
    client, opener = client_with((503, None, {"Retry-After": "3600"}), clock=clock)
    with pytest.raises(MusicBrainzError, match="retry after 3600s") as exc:
        client.release(MAENARAWANAI)
    assert exc.value.retry_after == 3600 and len(opener.requests) == 1
    assert client.limiter._next >= clock.now + 3600  # nothing else goes out before then


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def failing_sync(tmp_path, library, *answers):
    settings = settings_for(library, tmp_path, musicbrainz_enabled=True)
    scan(settings)
    client, opener = client_with(*answers)
    clock = Clock()
    sync = MusicBrainzSync(settings, client, clock=clock)
    return sync, opener, clock


def test_failed_run_pauses_lookups_for_an_hour(library, tmp_path) -> None:
    sync, opener, clock = failing_sync(tmp_path, library, (500, None))
    assert sync.after_scan()
    sync.wait(10)
    assert "500" in sync.status().last.error
    assert sync.status().paused_until == clock.now + 3600
    assert not sync.after_scan()  # however many scans start meanwhile
    clock.now += 3599
    assert not sync.after_scan()
    assert len(opener.requests) == 1
    clock.now += 2
    assert sync.status().paused_until is None
    opener.answers.append((200, {"id": "x", "media": []}))
    assert sync.after_scan()
    sync.wait(10)
    assert len(opener.requests) == 2


def test_retry_after_longer_than_an_hour_is_respected(library, tmp_path) -> None:
    sync, _, clock = failing_sync(tmp_path, library, (503, None, {"Retry-After": "7200"}))
    sync.client.limiter = RateLimiter(0)  # don't actually wait in the test
    assert sync.after_scan()
    sync.wait(10)
    assert sync.status().paused_until == clock.now + 7200


def test_successful_run_does_not_pause(library, tmp_path) -> None:
    sync, _, _ = failing_sync(tmp_path, library, (200, {"id": "x", "media": []}))
    assert sync.after_scan()
    sync.wait(10)
    assert sync.status().paused_until is None and sync.status().last == SyncResult(1, 1)


def test_user_agent_contact() -> None:
    assert user_agent() == USER_AGENT
    assert user_agent("me@example.com").endswith(
        "( me@example.com ; https://github.com/emilystap/tagdrifter )"
    )
    client, opener = client_with((200, {"id": "x"}))
    client = MusicBrainzClient(limiter=client.limiter, opener=opener, contact=" me@example.com ")
    client.release(MAENARAWANAI)
    assert opener.requests[0].get_header("User-agent") == user_agent("me@example.com")
    for bad in ("me@example.com" + chr(13) + chr(10) + "X-Evil: 1", "(me)", "x" * 201):
        with pytest.raises(ValueError, match="musicbrainz_contact"):
            MusicBrainzClient(contact=bad)


def test_contact_setting(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[musicbrainz]\ncontact = "a@example.com"\n', encoding="utf-8")
    assert load_settings(cfg, env={}).musicbrainz_contact == "a@example.com"
    env = {"TAGDRIFTER_MUSICBRAINZ_CONTACT": "b@example.com"}
    settings = load_settings(cfg, env=env)
    assert MusicBrainzSync(settings).client.user_agent == user_agent("b@example.com")
    with pytest.raises(ValueError):
        create_app(Settings(musicbrainz_contact="bad\nvalue"))


# --- Background trickle (lookups spread through the day) ---------------------------------


def trickle_sync(tmp_path, n_releases: int, *answers, **kwargs):
    ids = [f"11111111-1111-4111-8111-{i:012d}" for i in range(n_releases)]
    settings = Settings(database_path=tmp_path / "td.db", musicbrainz_enabled=True)
    store_with_files(tmp_path, *ids).close()
    client, opener = client_with(*answers)
    return MusicBrainzSync(settings, client, **kwargs), opener


def test_scheduled_scan_leaves_lookups_to_the_trickle(tmp_path) -> None:
    sync, opener = trickle_sync(tmp_path, 3)
    assert not sync.after_scan(automatic=True)
    assert opener.requests == []


def test_trickle_looks_up_a_small_batch(tmp_path) -> None:
    answers = [(200, {"id": "x", "media": []})] * 8
    sync, opener = trickle_sync(tmp_path, 8, *answers, trickle_batch=5)
    rescans = []
    sync.rescan = lambda: rescans.append(1)
    assert sync.trickle_once()
    assert len(opener.requests) == 5 and sync.status().last.fetched == 5
    assert rescans == [1]  # apply what was fetched
    assert sync.trickle_once()
    assert len(opener.requests) == 8
    assert not sync.trickle_once()  # nothing due: no request
    assert len(opener.requests) == 8


def test_trickle_respects_the_error_pause(tmp_path) -> None:
    clock = Clock()
    sync, opener = trickle_sync(tmp_path, 3, (500, None), clock=clock)
    assert sync.trickle_once()
    assert not sync.trickle_once()
    assert len(opener.requests) == 1
    clock.now += 3601
    opener.answers.append((200, {"id": "x", "media": []}))
    assert sync.trickle_once()


def test_trickle_and_scan_lookups_never_overlap(tmp_path) -> None:
    sync, _ = trickle_sync(tmp_path, 3)
    assert sync._claim(background=False, total=3)  # a run after a scan is going
    assert not sync.trickle_once()
    assert not sync.after_scan()


def test_trickle_wakes_at_random_intervals(tmp_path, monkeypatch) -> None:
    import tagdrifter.web.mbsync as mbsync

    drawn = []

    def uniform(a, b):
        drawn.append((a, b))
        return 0.01

    monkeypatch.setattr(mbsync.random, "uniform", uniform)
    answers = [(200, {"id": "x", "media": []})] * 3
    sync, opener = trickle_sync(tmp_path, 3, *answers, trickle_batch=1)
    sync.start()
    try:
        deadline = time.monotonic() + 10
        while len(opener.requests) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        sync.shutdown()
    assert len(opener.requests) == 3
    assert drawn[0] == mbsync.TRICKLE_INTERVAL  # a fresh random wait before every wake-up
    assert len(drawn) >= 3
    assert sync.status().next_background is not None


def test_trickle_off_when_lookups_off(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "td.db")
    sync = MusicBrainzSync(settings, client_with()[0])
    sync.start()
    assert sync._trickle is None and not sync.trickle_once()


def test_rescan_after_background_lookups_doesnt_fetch_the_rest(library, tmp_path) -> None:
    ids = [f"11111111-1111-4111-8111-{i:012d}" for i in range(3)]
    for i, mbid in enumerate(ids):
        build_flac(library / f"Extra/A{i}/1.flac", good_tags(musicbrainz_albumid=mbid))
    settings = settings_for(library, tmp_path, musicbrainz_enabled=True)
    scan(settings)
    answers = [(200, {"id": "x", "media": []})] * 4
    client, opener = client_with(*answers)
    sync = MusicBrainzSync(settings, client, trickle_batch=1)
    runner = ScanRunner(settings, mb_sync=sync)
    assert sync.trickle_once()
    runner.wait(30)  # the rescan that applies it
    with Store(settings.database_path) as store:
        assert len(store.recent_scans()) == 2
    assert len(opener.requests) == 1  # the rescan didn't look up the other three


def test_release_group_browse_pages_and_stops() -> None:
    group = "34cc7616-0e7e-34aa-a42d-bc9c17755f18"

    def page(count: int, n: int, start: int = 0) -> tuple:
        return (
            200,
            {"release-count": count, "releases": [{"id": str(start + i)} for i in range(n)]},
        )

    client, opener = client_with(page(150, 100), page(150, 50, 100))
    data = client.release_group_releases(group)
    assert (data["release-count"], len(data["releases"])) == (150, 150)
    assert ["offset=0" in r.full_url for r in opener.requests] == [True, False]
    assert "offset=100" in opener.requests[1].full_url

    # A huge release group: at most MAX_BROWSE_PAGES requests.
    client, opener = client_with(*(page(5000, 100, 100 * i) for i in range(MAX_BROWSE_PAGES)))
    data = client.release_group_releases(group)
    assert len(opener.requests) == MAX_BROWSE_PAGES
    assert (data["release-count"], len(data["releases"])) == (5000, 100 * MAX_BROWSE_PAGES)

    client, _ = client_with((404, None))
    assert client.release_group_releases(group) == {"release-count": 0, "releases": []}
