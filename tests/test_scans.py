"""Why each scan ran, and the Scans page hiding routine rescans (with paging)."""

import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tagdrifter.db import MIGRATIONS, Store
from tagdrifter.web import ScanRunner, create_app

from .conftest import scan, settings_for

HX = {"HX-Request": "true", "HX-Target": "results"}


@pytest.fixture
def settings(library, tmp_path):
    return settings_for(library, tmp_path)


def reasons(settings) -> list[str | None]:
    with Store(settings.database_path) as store:
        return [r["reason"] for r in reversed(store.recent_scans(100))]


def text(response) -> str:
    assert response.status_code == 200, response.text
    return " ".join(response.text.split())


def test_each_way_of_starting_a_scan_records_why(settings) -> None:
    scan(settings, reason="cli")
    runner = ScanRunner(settings)
    with TestClient(create_app(replace(settings, scan_schedule="0 4 * * *"), runner)) as c:
        c.post("/scan")
        runner.wait(30)
        c.app.state.scheduler._fire()
        runner.wait(30)
        runner.rescan()  # after MusicBrainz lookups
        runner.wait(30)
        runner.start(reason="rename")
        runner.wait(30)
    assert reasons(settings) == ["cli", "manual", "schedule", "musicbrainz", "rename"]
    with Store(settings.database_path) as store, pytest.raises(ValueError):
        store.start_scan("/music", "whim")


def add_scan(store: Store, reason: str | None, *, new: int = 0, status: str = "completed") -> int:
    scan_id = store.start_scan("/music", reason)
    store.finish_scan(scan_id, status, issues_new=new)
    return scan_id


def test_scans_page_hides_routine_rescans(settings) -> None:
    with Store(settings.database_path) as store:
        add_scan(store, "manual")
        routine = add_scan(store, "rename")
        found = add_scan(store, "musicbrainz", new=2)  # found something: shown
        failed = add_scan(store, "undo", status="failed")  # shown
        legacy = add_scan(store, None)  # reason unknown (before migration 7): shown
        assert store.count_scans() == (5, 1)

    with TestClient(create_app(settings)) as c:
        page = text(c.get("/scans"))
        assert "1–4 of 4 scans" in page
        assert "1 routine rescan hidden" in page and 'href="/scans?all=1"' in page
        assert f"<td>{routine}</td>" not in page
        for shown in (found, failed, legacy):
            assert f"<td>{shown}</td>" in page
        assert "Scan now" in page and "After MusicBrainz lookups" in page

        everything = text(c.get("/scans", params={"all": 1}, headers=HX))
        assert "<html" not in everything
        assert "1–5 of 5 scans" in everything and f"<td>{routine}</td>" in everything
        assert "After renames" in everything and "Hide them" in everything


def test_scans_page_pages(settings) -> None:
    with Store(settings.database_path) as store:
        ids = [add_scan(store, "manual") for _ in range(55)]
        add_scan(store, "rename")
    with TestClient(create_app(settings)) as c:
        first = text(c.get("/scans"))
        assert "1–50 of 55 scans" in first and "Page 1 of 2" in first
        assert 'href="/scans?page=2"' in first
        second = text(c.get("/scans", params={"page": 2}))
        assert "51–55 of 55 scans" in second and f"<td>{ids[0]}</td>" in second
        assert 'href="/scans?all=1&amp;page=2"' in text(c.get("/scans", params={"all": 1}))


def test_scans_page_before_any_scan(settings) -> None:
    with TestClient(create_app(settings)) as c:
        assert "No scans yet" in c.get("/scans").text


def test_migration_marks_earlier_rename_rescans(tmp_path) -> None:
    path = tmp_path / "td.db"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:6]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 6")
    conn.execute(
        "INSERT INTO rename_batches (album, created_at, finished_at, status)"
        " VALUES ('A/B', '2026-10-31T10:00:00+00:00', '2026-10-31T10:00:05+00:00', 'applied')"
    )
    for started in ("2026-10-31T10:00:06+00:00", "2026-10-31T10:30:00+00:00"):
        conn.execute(
            "INSERT INTO scans (started_at, status, library_path) VALUES (?, 'completed', '/m')",
            (started,),
        )
    conn.commit()
    conn.close()
    assert reasons_from(path) == ["rename", None]


def reasons_from(path) -> list[str | None]:
    with Store(path) as store:
        return [r["reason"] for r in reversed(store.recent_scans())]
