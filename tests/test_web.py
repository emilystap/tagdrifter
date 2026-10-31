import pytest
from fastapi.testclient import TestClient

from tagdrifter.db import IssueFilter, Store
from tagdrifter.web import ScanRunner, create_app
from tagdrifter.web.format import diff_marks, expected_location

from .conftest import build_flac, good_tags, scan, settings_for

HX = {"HX-Request": "true"}


def results(client, **params) -> str:
    """The issue list partial (what the filter form swaps in), whitespace collapsed.
    Defaults to the flat list (view=list); pass view="album" for the grouped view."""
    params.setdefault("view", "list")
    response = client.get("/issues", params=params, headers=HX | {"HX-Target": "results"})
    assert response.status_code == 200
    return " ".join(response.text.split())


@pytest.fixture
def settings(library, tmp_path):
    return settings_for(library, tmp_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_pages_render_before_any_scan(client) -> None:
    for url in ("/", "/issues", "/scans"):
        response = client.get(url)
        assert response.status_code == 200, url
    assert "Never" in client.get("/").text
    assert "No open issues match." in client.get("/issues").text
    assert "Scan now" in client.get("/scan/status").text


def test_dashboard_counts_issues_per_check(client, settings) -> None:
    scan(settings)
    page = client.get("/").text
    assert "/issues?check=path.track_location" in page
    assert "/issues?artist=Pixies" in page
    assert "/static/htmx.min.js" in page


def test_issue_filters(client, settings) -> None:
    scan(settings)
    page = client.get("/issues").text
    assert 'id="filters"' in page and "<html" in page
    assert "7 open issues" in results(client)

    only_errors = results(client, severity="error")
    assert "tags.invalid_mbid" in only_errors and "path.loose_file" not in only_errors

    by_artist = results(client, artist="Pixies")
    assert "loose.flac" not in by_artist and "02 Tame.flac" in by_artist

    assert "3 open issues" in results(client, q="tame")

    untagged = results(client, mbid="no")
    assert "no MBIDs" in untagged and "Pixies/Hits" not in untagged


def test_issue_filter_partial_for_htmx(client, settings) -> None:
    scan(settings)
    partial = results(client, check="path.loose_file")
    assert "<html" not in partial and 'id="filters"' not in partial
    assert "1–1 of 1 open issue" in partial


def test_track_location_shows_highlighted_expected_path(client, settings) -> None:
    scan(settings)
    page = results(client, check="path.track_location")
    assert "Expected" in page and "<mark>" in page


def test_paging(client, settings, monkeypatch) -> None:
    from tagdrifter.web import app as web_app

    monkeypatch.setattr(web_app, "PAGE_SIZE", 3)
    scan(settings)
    with TestClient(web_app.create_app(settings)) as c:
        first = results(c)
        assert "Page 1 of 3" in first and "/issues?view=list&amp;page=2" in first
        last = results(c, page=99)
        assert "Page 3 of 3" in last and "7–7 of 7" in last


def test_scan_now_runs_in_background_and_reports(settings) -> None:
    runner = ScanRunner(settings)
    with TestClient(create_app(settings, runner)) as c:
        response = c.post("/scan")
        assert response.status_code == 200
        runner.wait(30)
        assert not runner.status().running and runner.status().result is not None

        done = c.get("/scan/status", params={"watching": 1}, headers=HX)
        assert done.headers.get("HX-Trigger") == "scan-finished"
        assert "Scan now" in done.text
        assert "HX-Trigger" not in c.get("/scan/status").headers

        assert "7 open issues" in results(c)
        assert "completed" in c.get("/scans").text


def test_runner_refuses_concurrent_scans(settings, monkeypatch) -> None:
    import threading

    from tagdrifter.web import runner as runner_mod

    release = threading.Event()

    def slow_scan(*args, **kwargs):
        release.wait(10)
        raise RuntimeError("stopped")

    monkeypatch.setattr(runner_mod, "run_scan", slow_scan)
    runner = ScanRunner(settings)
    assert runner.start()
    assert runner.status().running
    assert not runner.start()
    release.set()
    runner.wait(10)
    assert runner.status().error == "RuntimeError: stopped"


def test_failed_scan_is_reported(tmp_path) -> None:
    settings = settings_for(tmp_path / "missing", tmp_path)
    runner = ScanRunner(settings)
    runner.start()
    runner.wait(10)
    assert "FileNotFoundError" in runner.status().error
    with TestClient(create_app(settings, runner)) as c:
        assert "Last scan failed" in c.get("/scan/status").text


def test_healthz(client) -> None:
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["scanning"] is False
    assert health["next_scheduled_scan"]  # default schedule: daily at 04:00


def test_schedule_runs_scans_and_shows_next_run(settings) -> None:
    from dataclasses import replace

    with TestClient(create_app(replace(settings, scan_schedule="30 2 * * 1"))) as c:
        scheduler = c.app.state.scheduler
        next_run = scheduler.next_run()
        assert (next_run.hour, next_run.minute, next_run.weekday()) == (2, 30, 0)
        assert "next automatic scan" in c.get("/").text

        scheduler._fire()  # what APScheduler calls when the schedule comes due
        c.app.state.runner.wait(30)
        assert c.app.state.runner.status().result is not None
    assert scheduler.next_run() is None  # stopped with the app


def test_schedule_off(settings) -> None:
    from dataclasses import replace

    with TestClient(create_app(replace(settings, scan_schedule="off"))) as c:
        assert c.get("/healthz").json()["next_scheduled_scan"] is None
        assert "automatic scans off" in c.get("/").text


def test_invalid_schedule_is_rejected(settings) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="invalid scan schedule"):
        create_app(replace(settings, scan_schedule="every day"))


def test_scheduled_scan_skipped_while_one_runs(settings, monkeypatch) -> None:
    from tagdrifter.web.scheduler import ScanScheduler

    runner = ScanRunner(settings)
    monkeypatch.setattr(runner, "start", lambda **kw: False)  # as if a scan were running
    ScanScheduler(runner, "0 4 * * *")._fire()  # must not raise


def test_store_artist_counts_and_text_filter(settings) -> None:
    scan(settings)
    with Store(settings.database_path) as store:
        assert dict(store.open_issue_artists()) == {"Pixies": 6, "loose.flac": 1}
        assert store.count_open_issues(text="100%_literal") == 0  # LIKE wildcards are escaped
        assert store.count_open_issues(text="Tame", check_id="path.track_location") == 1


def test_diff_marks_highlights_only_differences() -> None:
    current, expected = diff_marks("A/Born: X.flac", "A/Born꞉ X.flac")
    assert str(current) == "A/Born<mark>:</mark> X.flac"
    assert str(expected) == "A/Born<mark>꞉</mark> X.flac"
    escaped, _ = diff_marks("<b>", "<i>")
    assert "<b>" not in str(escaped)


def test_expected_location_for_placement() -> None:
    assert expected_location(
        "album.placement", "Charli xcx/CRASH", {"expected_artist_folder": "Charli XCX"}
    ) == ("Charli xcx/CRASH", "Charli XCX/CRASH")
    assert expected_location("tags.required", "a/b.flac", {"missing": ["x"]}) is None


@pytest.mark.parametrize(
    ("weekday", "days"),
    [
        ("1", {"Mon"}),
        ("0", {"Sun"}),
        ("7", {"Sun"}),
        ("1-5", {"Mon", "Tue", "Wed", "Thu", "Fri"}),
        ("0-6", {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"}),
        ("6,0", {"Sat", "Sun"}),
        ("*", {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"}),
        ("*/2", {"Sun", "Tue", "Thu", "Sat"}),
        ("1-5/2", {"Mon", "Wed", "Fri"}),
        ("5/1", {"Fri", "Sat", "Sun"}),
        ("mon-fri", {"Mon", "Tue", "Wed", "Thu", "Fri"}),
    ],
)
def test_cron_weekdays_use_standard_numbering(weekday, days) -> None:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from tagdrifter.web.scheduler import parse_schedule

    trigger = parse_schedule(f"0 4 * * {weekday}")
    now, fired = datetime(2026, 11, 1, tzinfo=ZoneInfo("UTC")), set()
    for _ in range(14):
        nxt = trigger.get_next_fire_time(None, now)
        fired.add(nxt.strftime("%a"))
        now = nxt + timedelta(seconds=1)
    assert fired == days


def test_invalid_weekday() -> None:
    from tagdrifter.web.scheduler import parse_schedule

    with pytest.raises(ValueError, match="invalid scan schedule"):
        parse_schedule("0 4 * * 3-9")


def test_asset_urls_work_behind_a_tls_proxy(client) -> None:
    # A reverse proxy terminates HTTPS and forwards plain HTTP with the public Host header.
    page = client.get("/", headers={"Host": "tags.example.com", "X-Forwarded-Proto": "https"}).text
    assert 'href="/static/style.css"' in page and 'src="/static/htmx.min.js"' in page
    assert "http://" not in page


def test_issues_grouped_by_album(client, settings) -> None:
    scan(settings)
    grouped = results(client, view="album")
    # Doolittle (track + album issues), Hits (placement) and the loose file (library scope).
    assert "1–3 of 3 albums · 7 issues" in grouped
    assert grouped.index("Pixies/Doolittle") < grouped.index("Pixies/Hits")
    assert "path.track_location" in grouped and "file.unreadable" in grouped
    assert "1 filename changes" in grouped  # 02 Tame.flac -> Pixies_Doolittle_02_Tame.flac
    assert "no MBIDs" in grouped  # 02 Tame has a malformed recording MBID
    # The default view is grouped; filters still apply.
    assert "3 albums" in " ".join(client.get("/issues").text.split())
    assert "1–1 of 1 album · 1 issue" in results(client, view="album", check="album.placement")


def test_grouped_view_shows_album_folder_rename(tmp_path) -> None:
    lib = tmp_path / "music"
    for n in (1, 2):
        tags = good_tags(TITLE=f"T{n}", TRACKNUMBER=str(n), ALBUM="Surfer Rosa")
        build_flac(lib / f"Pixies/Surfer Rosa Old/Pixies_Surfer Rosa_0{n}_T{n}.flac", tags)
    settings = settings_for(lib, tmp_path)
    scan(settings)
    with TestClient(create_app(settings)) as c:
        grouped = results(c, view="album")
    assert "1–1 of 1 album · 2 issues" in grouped
    assert "<dt>Folder</dt>" in grouped and "Surfer Rosa<mark> Old</mark>" in grouped
    assert "filename change" not in grouped  # only the folder differs


def test_group_store_queries(settings) -> None:
    scan(settings)
    with Store(settings.database_path) as store:
        groups = store.open_issue_groups()
        assert [g.key for g in groups] == ["loose.flac", "Pixies/Doolittle", "Pixies/Hits"]
        doolittle = groups[1]
        assert doolittle.count == 5 and doolittle.worst_severity == "error"
        assert doolittle.checks["path.track_location"] == 1 and doolittle.untagged == 5
        assert store.count_open_issue_groups() == 3
        assert store.count_open_issue_groups(IssueFilter(check_id="tags.required")) == 1
        issues = store.open_issues_in_groups(["Pixies/Hits"])
        assert [i.check_id for i in issues["Pixies/Hits"]] == ["album.placement"]
        assert all(
            i.album == "Pixies/Doolittle" for i in store.open_issues(path_prefix="Pixies/Doolittle")
        )
