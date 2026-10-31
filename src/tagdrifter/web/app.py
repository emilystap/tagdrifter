"""FastAPI app: dashboard, issue list, scan history and a "Scan now" button.

Server-rendered Jinja templates; HTMX (vendored in static/) swaps partials for filtering,
paging and scan progress. Each request opens its own Store; scans run in a ScanRunner thread.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..checks import REGISTRY, Severity
from ..config import Settings
from ..db import IssueFilter, Store
from . import format as fmt
from .runner import ScanRunner
from .scheduler import ScanScheduler

HERE = Path(__file__).parent
PAGE_SIZE = 100  # issues per page in the flat list
GROUP_PAGE_SIZE = 50  # albums per page when grouped
SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def get_store(request: Request) -> Iterator[Store]:
    """One Store (SQLite connection) per request."""
    with Store(request.app.state.settings.database_path) as store:
        yield store


StoreDep = Annotated[Store, Depends(get_store)]


def create_app(settings: Settings, runner: ScanRunner | None = None) -> FastAPI:
    """Build the app. Raises ValueError for an invalid scan schedule."""
    runner = runner or ScanRunner(settings)
    scheduler = ScanScheduler(runner, settings.scan_schedule)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()

    app = FastAPI(
        title="Tag Drifter", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters |= {
        "ago": fmt.ago,
        "duration": fmt.duration,
        "scan_duration": fmt.scan_duration,
        "timestamp": fmt.timestamp,
    }
    env.globals |= {
        "version": __version__,
        "settings": settings,
        "diff_marks": fmt.diff_marks,
        "expected_location": fmt.expected_location,
        "scheduler": scheduler,
    }

    def is_partial(request: Request, target: str) -> bool:
        return request.headers.get("HX-Request") == "true" and (
            request.headers.get("HX-Target") == target
        )

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    # --- Pages ------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, store: StoreDep) -> HTMLResponse:
        counts = store.open_issue_counts()
        by_check: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for (check_id, severity), n in counts.items():
            by_check[check_id] = by_check.get(check_id, 0) + n
            by_severity[severity] = by_severity.get(severity, 0) + n
        check_rows = sorted(
            (
                {
                    "id": check_id,
                    "description": cls.description,
                    "severity": cls.severity.value,
                    "scope": cls.scope.value,
                    "enabled": check_id not in settings.disabled_checks,
                    "count": by_check.get(check_id, 0),
                }
                for check_id, cls in REGISTRY.items()
            ),
            key=lambda r: (-r["count"], SEVERITY_ORDER[Severity(r["severity"])], r["id"]),
        )
        # Issues from checks that are no longer registered (e.g. scanner.check_error).
        for check_id in sorted(by_check.keys() - REGISTRY.keys()):
            check_rows.insert(
                0,
                {
                    "id": check_id,
                    "description": "",
                    "severity": next(s for (c, s) in counts if c == check_id),
                    "scope": "",
                    "enabled": True,
                    "count": by_check[check_id],
                },
            )
        total = sum(by_check.values())
        artists = sorted(store.open_issue_artists(), key=lambda a: (-a[1], a[0].casefold()))
        scans = store.recent_scans(1)
        return render(
            request,
            "dashboard.html",
            page="dashboard",
            total=total,
            by_severity=by_severity,
            mbid_tagged=store.count_open_issues(mbid_tagged=True),
            files=store.file_count(),
            last_scan=scans[0] if scans else None,
            checks=check_rows,
            top_artists=artists[:10],
        )

    @app.get("/issues", response_class=HTMLResponse)
    def issues(
        request: Request,
        store: StoreDep,
        check: str = "",
        severity: str = "",
        artist: str = "",
        mbid: str = "",
        q: str = "",
        view: str = "album",
        page: Annotated[int, Query(ge=1)] = 1,
    ) -> HTMLResponse:
        filters = IssueFilter(
            check_id=check or None,
            severity=severity or None,
            path_prefix=artist or None,
            mbid_tagged={"yes": True, "no": False}.get(mbid),
            text=q.strip() or None,
        )
        view = "list" if view == "list" else "album"
        total = store.count_open_issues(filters)
        if view == "album":
            # One entry per album folder; the page size counts albums, not issues.
            entries = store.count_open_issue_groups(filters)
            page_size = GROUP_PAGE_SIZE
        else:
            entries, page_size = total, PAGE_SIZE
        pages = max(1, ceil(entries / page_size))
        page = min(page, pages)
        offset = (page - 1) * page_size
        if view == "album":
            groups = store.open_issue_groups(filters, limit=page_size, offset=offset)
            by_key = store.open_issues_in_groups([g.key for g in groups], filters)
            rows = [
                {
                    "group": g,
                    "issues": by_key[g.key],
                    "changes": fmt.album_changes(g.key, by_key[g.key]),
                }
                for g in groups
            ]
        else:
            rows = store.open_issues(filters, limit=page_size, offset=offset)
        params = {
            "check": check,
            "severity": severity,
            "artist": artist,
            "mbid": mbid,
            "q": q,
            "view": "" if view == "album" else view,
        }

        def page_url(n: int) -> str:
            query = {k: v for k, v in params.items() if v} | ({"page": n} if n > 1 else {})
            return "/issues" + (f"?{urlencode(query)}" if query else "")

        context = {
            "page": "issues",
            "view": view,
            "rows": rows,
            "total": total,
            "entries": entries,
            "page_no": page,
            "pages": pages,
            "page_url": page_url,
            "first_index": offset + 1 if entries else 0,
            "params": params,
        }
        if is_partial(request, "results"):
            return render(request, "_issue_results.html", **context)
        return render(
            request,
            "issues.html",
            check_ids=sorted({c for c, _ in store.open_issue_counts()} | REGISTRY.keys()),
            artists=store.open_issue_artists(),
            **context,
        )

    @app.get("/scans", response_class=HTMLResponse)
    def scans(request: Request, store: StoreDep) -> HTMLResponse:
        return render(request, "scans.html", page="scans", scans=store.recent_scans(50))

    # --- Scan control -----------------------------------------------------------------

    @app.get("/scan/status", response_class=HTMLResponse)
    def scan_status(request: Request, store: StoreDep, watching: bool = False) -> HTMLResponse:
        status = app.state.runner.status()
        response = render(request, "_scan_status.html", status=status, last_scan=_last_scan(store))
        if watching and not status.running:
            # The page was showing a running scan that has now finished: let it refresh.
            response.headers["HX-Trigger"] = "scan-finished"
        return response

    @app.post("/scan", response_class=HTMLResponse)
    def scan_now(request: Request, store: StoreDep, full: bool = False) -> HTMLResponse:
        app.state.runner.start(full=full)
        status = app.state.runner.status()
        return render(request, "_scan_status.html", status=status, last_scan=_last_scan(store))

    @app.get("/healthz")
    def healthz(store: StoreDep) -> JSONResponse:
        store.conn.execute("SELECT 1")
        next_run = scheduler.next_run()
        return JSONResponse(
            {
                "status": "ok",
                "scanning": runner.status().running,
                "next_scheduled_scan": next_run.isoformat() if next_run else None,
            }
        )

    return app


def _last_scan(store: Store):
    scans = store.recent_scans(1)
    return scans[0] if scans else None
