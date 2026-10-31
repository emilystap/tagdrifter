"""FastAPI app: dashboard, issue list, scan history and a "Scan now" button.

Server-rendered Jinja templates; HTMX (vendored in static/) swaps partials for filtering,
paging and scan progress. Each request opens its own Store; scans run in a ScanRunner thread.
Every POST is CSRF-checked, and with a password configured every page needs a login (auth.py).
With `fix_enabled`, album cards offer a rename preview + apply, and /renames lists the
journal with undo (renamer.py); scans are held off while files move. Notifications
(notify.py) go out from the scan runner and for renames left half-done; the dashboard can
send a test one. With `musicbrainz_enabled`, MusicBrainz lookups trickle in at random times
in the background and run at once after a scan someone started (mbsync.py); the dashboard
shows the cache's progress.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..checks import REGISTRY, Severity
from ..config import Settings
from ..db import IssueFilter, Store
from ..notify import Notifications, NotifyError
from ..renamer import RenameError, apply_plan, library_writable, plan_album, undo_batch
from . import auth
from . import format as fmt
from .mbsync import MusicBrainzSync
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
    """Build the app. Raises ValueError for an invalid scan schedule, password hash or
    notification setting."""
    authenticator = auth.Authenticator(settings)
    notifications = Notifications(settings)
    runner = runner or ScanRunner(settings)
    if runner.notifications is None:
        runner.notifications = notifications
    if runner.mb_sync is None:
        runner.mb_sync = MusicBrainzSync(settings)
        runner.mb_sync.rescan = runner.rescan
    mb_sync = runner.mb_sync
    scheduler = ScanScheduler(runner, settings.scan_schedule)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler.start()
        mb_sync.start()
        try:
            yield
        finally:
            scheduler.shutdown()
            mb_sync.shutdown()

    app = FastAPI(
        title="Tag Drifter", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.auth = authenticator
    app.state.notifications = notifications
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if auth.is_cross_site(request.method, request.headers):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        if (
            authenticator.enabled
            and not auth.is_public(request.url.path)
            and not authenticator.valid_session(request.cookies.get(auth.SESSION_COOKIE))
        ):
            login = "/login?" + urlencode({"next": _current_path(request)})
            if request.headers.get("HX-Request") == "true":
                # An HTMX swap can't follow a redirect to a full page; ask it to navigate.
                return Response(status_code=401, headers={"HX-Redirect": login})
            if request.method == "GET":
                return RedirectResponse(login, status_code=303)
            return PlainTextResponse("Login required.", status_code=401)
        return await call_next(request)

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
        "auth_enabled": authenticator.enabled,
        "fix_enabled": settings.fix_enabled,
        "move_labels": fmt.move_labels,
        "notifications": notifications,
        "mb_sync": mb_sync,
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
            mb_cache=store.mb_cache_counts() if mb_sync.enabled else None,
            mb_status=mb_sync.status(),
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
        scan: str = "",
        view: str = "album",
        page: Annotated[int, Query(ge=1)] = 1,
    ) -> HTMLResponse:
        filters = IssueFilter(
            check_id=check or None,
            severity=severity or None,
            path_prefix=artist or None,
            mbid_tagged={"yes": True, "no": False}.get(mbid),
            text=q.strip() or None,
            first_seen_scan=int(scan) if scan.isdigit() else None,
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
            "scan": scan if scan.isdigit() else "",
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

    # --- Login ------------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/") -> Response:
        if not authenticator.enabled:
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", next=auth.safe_next(next), failed=False)

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        if not authenticator.enabled:
            return RedirectResponse("/", status_code=303)
        form = await _form(request)
        password = form.get("password", "")
        target = auth.safe_next(form.get("next", "/"))
        # scrypt is deliberately slow; keep it off the event loop.
        if await asyncio.to_thread(authenticator.check_password, password):
            response = RedirectResponse(target, status_code=303)
            response.set_cookie(
                auth.SESSION_COOKIE,
                authenticator.new_session(),
                max_age=auth.SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=_is_https(request),
            )
            return response
        await asyncio.sleep(1)  # slow down guessing
        response = render(request, "login.html", next=target, failed=True)
        response.status_code = 401
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login" if authenticator.enabled else "/", status_code=303)
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

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

    # --- Renames ----------------------------------------------------------------------

    def require_fix() -> None:
        if not settings.fix_enabled:
            raise HTTPException(404, "Renaming is off (set TAGDRIFTER_FIX=true to enable it).")

    def renamed(request: Request, name: str, **context) -> HTMLResponse:
        """Render, and start a rescan so the issues reflect the new paths."""
        response = render(request, name, **context)
        if app.state.runner.start():
            response.headers["HX-Trigger"] = "scan-started"
        return response

    @app.get("/rename/preview", response_class=HTMLResponse)
    def rename_preview(request: Request, store: StoreDep, album: str) -> HTMLResponse:
        require_fix()
        plan = plan_album(settings, store, album)
        return render(
            request, "_rename_preview.html", plan=plan, writable=library_writable(settings)
        )

    @app.post("/rename/apply", response_class=HTMLResponse)
    async def rename_apply(request: Request) -> HTMLResponse:
        require_fix()
        form = await _form(request)
        album, fingerprint = form.get("album", ""), form.get("fingerprint", "")

        def apply():
            with app.state.runner.paused(), Store(settings.database_path) as store:
                return apply_plan(settings, store, album, fingerprint)

        try:
            result = await asyncio.to_thread(apply)
        except (RenameError, RuntimeError) as exc:
            if isinstance(exc, RenameError) and exc.partial:
                notifications.rename_failed(album, str(exc))
            return render(
                request,
                "_rename_result.html",
                album=album,
                error=str(exc),
                partial=getattr(exc, "partial", False),
            )
        return renamed(
            request, "_rename_result.html", album=album, result=result, error=None, partial=False
        )

    @app.get("/renames", response_class=HTMLResponse)
    def renames(request: Request, store: StoreDep) -> HTMLResponse:
        require_fix()
        return render(
            request,
            "renames.html",
            page="renames",
            batches=store.rename_batches(100),
            writable=library_writable(settings),
            error=request.query_params.get("error"),
        )

    @app.get("/renames/{batch_id}", response_class=HTMLResponse)
    def rename_moves(request: Request, store: StoreDep, batch_id: int) -> HTMLResponse:
        require_fix()
        return render(request, "_rename_moves.html", moves=store.rename_moves(batch_id))

    @app.post("/renames/{batch_id}/undo")
    def rename_undo(request: Request, batch_id: int) -> Response:
        require_fix()
        with Store(settings.database_path) as store:
            batch = store.rename_batch(batch_id)
            if batch is None or batch["status"] not in ("applied", "partial"):
                raise HTTPException(404, "Nothing to undo.")
            try:
                with app.state.runner.paused():
                    undo_batch(settings, store, batch_id)
            except (RenameError, RuntimeError) as exc:
                if isinstance(exc, RenameError) and exc.partial:
                    notifications.rename_failed(batch["album"], str(exc))
                return RedirectResponse(
                    "/renames?" + urlencode({"error": str(exc)}), status_code=303
                )
        app.state.runner.start()
        return RedirectResponse("/renames", status_code=303)

    # --- MusicBrainz ------------------------------------------------------------------

    @app.get("/musicbrainz/status", response_class=HTMLResponse)
    def musicbrainz_status(request: Request, store: StoreDep) -> HTMLResponse:
        return render(
            request,
            "_mb_status.html",
            mb_cache=store.mb_cache_counts() if mb_sync.enabled else None,
            mb_status=mb_sync.status(),
        )

    # --- Notifications ----------------------------------------------------------------

    @app.post("/notify/test", response_class=HTMLResponse)
    async def notify_test(request: Request) -> HTMLResponse:
        try:
            reply = await asyncio.to_thread(notifications.send_test)
        except NotifyError as exc:
            return render(request, "_notify_test.html", ok=False, reply=str(exc))
        return render(request, "_notify_test.html", ok=True, reply=reply)

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


async def _form(request: Request) -> dict[str, str]:
    """A urlencoded form's fields (first value each). Parsed by hand to avoid the
    python-multipart dependency for a few small forms."""
    form = parse_qs((await request.body()).decode(errors="replace"))
    return {k: v[0] for k, v in form.items()}


def _current_path(request: Request) -> str:
    """Where to return after logging in. For an HTMX request, the page it was made from."""
    current = request.headers.get("HX-Current-URL")
    if current:
        parts = current.split("/", 3)
        return "/" + (parts[3] if len(parts) > 3 else "")
    query = f"?{request.url.query}" if request.url.query else ""
    return request.url.path + query


def _is_https(request: Request) -> bool:
    """Behind a TLS-terminating proxy the app sees http; the proxy says so in a header."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def _last_scan(store: Store):
    scans = store.recent_scans(1)
    return scans[0] if scans else None
