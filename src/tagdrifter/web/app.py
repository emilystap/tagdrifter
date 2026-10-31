"""FastAPI app: dashboard, issue list, scan history and a "Scan now" button.

Server-rendered Jinja templates; HTMX (vendored in static/) swaps partials for filtering,
paging and scan progress. Each request opens its own Store; scans run in a ScanRunner thread.
Every POST is CSRF-checked, and with a password configured every page needs a login (auth.py).
With `fix_enabled`, album cards offer a rename preview + apply, and /renames lists the
journal with undo (renamer.py); scans are held off while files move. Several albums can be
selected and renamed together in the background (bulk.py). Notifications
(notify.py) go out from the scan runner and for renames left half-done; the dashboard can
send a test one. With `musicbrainz_enabled`, MusicBrainz lookups trickle in at random times
in the background and run at once after a scan someone started (mbsync.py); the dashboard
shows the cache's progress.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
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

from .. import __version__, library
from ..checks import REGISTRY, Severity
from ..checks.builtin_musicbrainz import DEFAULT_TOLERANCE_SECONDS
from ..config import Settings
from ..db import FOLLOW_UP_REASONS, SCAN_REASONS, IssueFilter, Store
from ..musicbrainz import SyncResult
from ..notify import Notifications, NotifyError
from ..renamer import (
    RenameError,
    apply_plan,
    library_writable,
    plan_album,
    plan_albums,
    undo_batch,
)
from . import auth
from . import format as fmt
from .bulk import BulkRenamer
from .mbsync import LookupRefused, MusicBrainzSync
from .runner import ScanRunner
from .scheduler import ScanScheduler

HERE = Path(__file__).parent
PAGE_SIZE = 100  # issues per page in the flat list
GROUP_PAGE_SIZE = 50  # albums per page when grouped
SCAN_PAGE_SIZE = 50  # rows per page of scan history
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
    bulk = BulkRenamer(settings, runner, notifications)

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
    app.state.bulk = bulk
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
        "track_length": fmt.track_length,
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
        "bulk": bulk,
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
        filters = _issue_filter(check, severity, artist, mbid, q, scan)
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
            # Albums matching the filters that "select all" would preview.
            "candidates": (
                len(store.rename_candidates(filters))
                if settings.fix_enabled and view == "album"
                else 0
            ),
            # Each dropdown counts the issues the other filters leave, so it only offers
            # choices that find something (and keeps the current one).
            "check_counts": _with_selected(
                sorted(store.open_issue_checks(replace(filters, check_id=None)).items()), check
            ),
            "artists": _with_selected(
                store.open_issue_artists(replace(filters, path_prefix=None)), artist
            ),
        }
        if is_partial(request, "results"):
            # The dropdowns are outside #results; refresh them out of band.
            return render(request, "_issue_results.html", facets_oob=True, **context)
        return render(request, "issues.html", **context)

    @app.get("/library", response_class=HTMLResponse)
    def library_page(
        request: Request,
        store: StoreDep,
        q: str = "",
        page: Annotated[int, Query(ge=1)] = 1,
    ) -> HTMLResponse:
        albums = library.album_summaries(store)
        found = library.search_albums(albums, q) if q.strip() else albums
        pages = max(1, ceil(len(found) / GROUP_PAGE_SIZE))
        page = min(page, pages)
        offset = (page - 1) * GROUP_PAGE_SIZE

        def page_url(n: int) -> str:
            query = ({"q": q} if q else {}) | ({"page": n} if n > 1 else {})
            return "/library" + (f"?{urlencode(query)}" if query else "")

        context = {
            "page": "library",
            "q": q,
            "albums": found[offset : offset + GROUP_PAGE_SIZE],
            "total": len(albums),
            "entries": len(found),
            "first_index": offset + 1 if found else 0,
            "page_no": page,
            "pages": pages,
            "page_url": page_url,
        }
        if is_partial(request, "results"):
            return render(request, "_library_results.html", **context)
        return render(request, "library.html", **context)

    @app.get("/library/album", response_class=HTMLResponse)
    def library_album(request: Request, store: StoreDep, folder: str = "") -> HTMLResponse:
        album = library.album_detail(store, folder)
        if album is None:
            response = render(request, "album.html", page="library", album=None, folder=folder)
            response.status_code = 404
            return response
        issues = store.open_issues_in_groups([album.folder])[album.folder]
        renamable = settings.fix_enabled and album.folder in store.rename_candidates(
            IssueFilter(path_prefix=album.folder)
        )
        tolerance = duration_tolerance_ms()
        return render(
            request,
            "album.html",
            page="library",
            album=album,
            issues=issues,
            renamable=renamable,
            groups=[
                (g, library.compare_release_group(store, album, g, tolerance))
                for g in album.release_group_ids
            ],
        )

    @app.post("/library/refresh", response_class=HTMLResponse)
    async def refresh_album(request: Request, folder: str = "") -> HTMLResponse:
        """*Refresh from MusicBrainz*: look the album's releases up again now (not ones
        fetched minutes ago), then rescan so the checks use them."""
        with Store(settings.database_path) as store:
            album = library.album_detail(store, folder)
        if album is None or not album.releases:
            raise HTTPException(404, "No such album, or it has no release MBID.")
        try:
            result = await asyncio.to_thread(mb_sync.refresh, [r.id for r in album.releases])
        except LookupRefused as exc:
            return render(request, "_mb_refresh.html", album=album, message=str(exc), refused=True)
        rescanning = result.changed and app.state.runner.rescan()
        response = render(
            request,
            "_mb_refresh.html",
            album=album,
            message=_refresh_message(result, rescanning),
            refused=bool(result.error),
        )
        if rescanning:
            response.headers["HX-Trigger"] = "scan-started"
        return response

    @app.post("/library/release-group", response_class=HTMLResponse)
    async def release_group_lookup(request: Request, folder: str = "", id: str = "") -> Response:
        """*Compare with other releases*: look up the release group's releases (unless
        looked up minutes ago), then show how each fits the album's files."""
        with Store(settings.database_path) as store:
            album = library.album_detail(store, folder)
        if album is None or id.lower() not in album.release_group_ids:
            raise HTTPException(404, "No such album or release group.")
        message, refused = "", False
        try:
            if not await asyncio.to_thread(mb_sync.lookup_release_group, id.lower()):
                message = "Looked up a few minutes ago; showing that."
        except LookupRefused as exc:
            message, refused = str(exc), True
        with Store(settings.database_path) as store:
            comparison = library.compare_release_group(
                store, album, id.lower(), duration_tolerance_ms()
            )
        return render(
            request,
            "_release_group.html",
            album=album,
            group_id=id.lower(),
            comparison=comparison,
            message=message,
            refused=refused,
        )

    def duration_tolerance_ms() -> int:
        options = settings.options_for("mb.duration")
        return round(1000 * float(options.get("tolerance_seconds", DEFAULT_TOLERANCE_SECONDS)))

    @app.get("/scans", response_class=HTMLResponse)
    def scans(
        request: Request,
        store: StoreDep,
        all: bool = False,
        page: Annotated[int, Query(ge=1)] = 1,
    ) -> HTMLResponse:
        total, routine = store.count_scans()
        entries = total if all else total - routine
        pages = max(1, ceil(entries / SCAN_PAGE_SIZE))
        page = min(page, pages)
        offset = (page - 1) * SCAN_PAGE_SIZE

        def page_url(n: int, show_all: bool = all) -> str:
            query = ({"all": 1} if show_all else {}) | ({"page": n} if n > 1 else {})
            return "/scans" + (f"?{urlencode(query)}" if query else "")

        context = {
            "page": "scans",
            "scans": store.recent_scans(SCAN_PAGE_SIZE, offset, hide_routine=not all),
            "total": total,
            "routine": routine,
            "show_all": all,
            "entries": entries,
            "first_index": offset + 1 if entries else 0,
            "page_no": page,
            "pages": pages,
            "page_url": page_url,
            "toggle_url": page_url(1, not all),
            "reasons": SCAN_REASONS,
            "follow_up": FOLLOW_UP_REASONS,
        }
        if is_partial(request, "results"):
            return render(request, "_scan_results.html", **context)
        return render(request, "scans.html", **context)

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
        if app.state.runner.start(reason="rename"):
            response.headers["HX-Trigger"] = "scan-started"
        return response

    @app.get("/rename/preview", response_class=HTMLResponse)
    def rename_preview(
        request: Request, store: StoreDep, album: str, bare: bool = False
    ) -> HTMLResponse:
        """One album's preview; `bare`: just its moves (expanded in a bulk preview)."""
        require_fix()
        plan = plan_album(settings, store, album)
        return render(
            request,
            "_rename_preview.html",
            plan=plan,
            writable=library_writable(settings),
            bare=bare,
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

    @app.post("/rename/bulk/preview", response_class=HTMLResponse)
    async def bulk_preview(request: Request) -> HTMLResponse:
        """Preview the selected albums (`album` fields), or with `all`, every album matching
        the issue filters (`f_<name>` fields) that has proposed renames."""
        require_fix()
        form = await _form_lists(request)

        def plan():
            with Store(settings.database_path) as store:
                if _first(form, "all"):
                    filters = _issue_filter(*(_first(form, f"f_{k}") for k in FILTER_FIELDS))
                    albums = store.rename_candidates(filters)
                else:
                    albums = form.get("album", [])
                return plan_albums(settings, store, albums)

        plans = await asyncio.to_thread(plan)
        return render(
            request,
            "_bulk_preview.html",
            ok=[p for p in plans if p.ok],
            refused=[p for p in plans if not p.ok],
            writable=library_writable(settings),
        )

    @app.post("/rename/bulk/apply", response_class=HTMLResponse)
    async def bulk_apply(request: Request) -> HTMLResponse:
        """Start renaming the previewed plans (`plan` fields: "<fingerprint> <album>")."""
        require_fix()
        form = await _form_lists(request)
        plans = [tuple(v.split(" ", 1)[::-1]) for v in form.get("plan", []) if " " in v]
        error = None
        if not plans:
            error = "Nothing selected to rename."
        else:
            try:
                bulk.start(plans)
            except RuntimeError as exc:
                error = str(exc)
        return render(request, "_bulk_status.html", status=bulk.status(), error=error)

    @app.get("/rename/bulk/status", response_class=HTMLResponse)
    def bulk_status(request: Request, watching: bool = False) -> HTMLResponse:
        require_fix()
        status = bulk.status()
        response = render(request, "_bulk_status.html", status=status, error=None)
        if watching and not status.running:
            response.headers["HX-Trigger"] = "scan-started"  # its rescan; show progress
        return response

    @app.post("/rename/bulk/stop", response_class=HTMLResponse)
    def bulk_stop(request: Request) -> HTMLResponse:
        require_fix()
        bulk.stop()
        return render(request, "_bulk_status.html", status=bulk.status(), error=None)

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
        app.state.runner.start(reason="undo")
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


def _refresh_message(result: SyncResult, rescanning: bool) -> str:
    """What *Refresh from MusicBrainz* did, in a sentence or two."""
    parts = []
    if result.fetched:
        parts.append(f"Refreshed {result.fetched} release{'s' * (result.fetched != 1)}.")
    if result.not_found:
        parts.append(
            "MusicBrainz no longer has "
            + (
                "this release."
                if result.not_found == 1
                else f"{result.not_found} of these releases."
            )
        )
    if result.skipped:
        parts.append("Looked up minutes ago, so not asked again.")
    if result.error:
        parts.append(f"Stopped: {result.error}. Lookups are paused for a while.")
    if result.changed:
        parts.append("Rescanning…" if rescanning else "A scan is running; the next scan uses it.")
    return " ".join(parts)


FILTER_FIELDS = ("check", "severity", "artist", "mbid", "q", "scan")


def _issue_filter(
    check: str, severity: str, artist: str, mbid: str, q: str, scan: str
) -> IssueFilter:
    """The /issues filters from their query parameters (empty = any)."""
    return IssueFilter(
        check_id=check or None,
        severity=severity or None,
        path_prefix=artist or None,
        mbid_tagged={"yes": True, "no": False}.get(mbid),
        text=q.strip() or None,
        first_seen_scan=int(scan) if scan.isdigit() else None,
    )


def _with_selected(options: list[tuple[str, int]], selected: str) -> list[tuple[str, int]]:
    """(value, count) options plus the selected value with no matches, so it stays chosen."""
    if selected and selected not in {value for value, _ in options}:
        options = sorted([*options, (selected, 0)], key=lambda o: o[0].casefold())
    return options


async def _form_lists(request: Request) -> dict[str, list[str]]:
    """A urlencoded form's fields with every value, in order (e.g. repeated checkboxes)."""
    return parse_qs((await request.body()).decode(errors="replace"))


def _first(form: dict[str, list[str]], name: str) -> str:
    return form.get(name, [""])[0]


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
