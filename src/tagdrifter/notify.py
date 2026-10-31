"""Push notifications for things that need attention: new issues after a scan, and failures.

Gotify is the only service so far (`gotify_url` + `gotify_token`); another one is a class
with a `send(Notification)` method. Messages are Markdown. `Notifications` sends in a
background thread and only logs failures, so a notification service that is down never slows
or breaks a scan or a rename. Nothing is sent unless a service is configured.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from . import __version__
from .config import Settings
from .db import IssueFilter, Store

log = logging.getLogger(__name__)

EVENTS = frozenset({"new_issues", "failures"})
MAX_ALBUMS = 10  # albums listed in a new-issues message
USER_AGENT = f"tagdrifter/{__version__} (+https://github.com/emilystap/tagdrifter)"


class NotifyError(Exception):
    """The notification service refused or couldn't be reached; the message says why."""


@dataclass(frozen=True)
class Notification:
    title: str
    message: str  # Markdown
    priority: int = 5  # Gotify scale: 0-10; 8+ is shown as urgent by most clients
    path: str = ""  # page in the web UI to open on a click, e.g. "/issues?scan=3"


class Notifier(Protocol):
    def describe(self) -> str: ...

    def send(self, notification: Notification) -> str:
        """Deliver it, or raise NotifyError. Returns a short confirmation for the UI."""
        ...


class GotifyNotifier:
    """Gotify's `POST /message` with an application token
    (https://gotify.net/api-docs#/message/createMessage)."""

    def __init__(self, url: str, token: str, public_url: str = "", timeout: float = 10) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"gotify_url must be an http(s) URL, not {url!r}")
        if not token:
            raise ValueError("gotify_url is set but gotify_token is empty")
        self.url = url.rstrip("/")
        self.token = token
        self.public_url = public_url.rstrip("/")
        self.timeout = timeout

    def describe(self) -> str:
        return f"Gotify at {urlsplit(self.url).netloc}"

    def send(self, notification: Notification) -> str:
        extras: dict = {"client::display": {"contentType": "text/markdown"}}
        if self.public_url and notification.path:
            extras["client::notification"] = {"click": {"url": self.public_url + notification.path}}
        body = {
            "title": notification.title,
            "message": notification.message,
            "priority": notification.priority,
            "extras": extras,
        }
        request = urllib.request.Request(
            self.url + "/message",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                # A header rather than ?token=, so the token stays out of proxy logs.
                "X-Gotify-Key": self.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise NotifyError(f"Gotify answered {exc.code}: {_gotify_error(exc)}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NotifyError(f"Couldn't reach Gotify at {self.url}: {reason}") from exc
        except ValueError as exc:
            raise NotifyError(f"Gotify sent an unexpected reply: {exc}") from exc
        return f"Gotify accepted message #{reply.get('id', '?')}."


def _gotify_error(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read())
        return str(data.get("errorDescription") or data.get("error") or exc.reason)
    except (ValueError, OSError):
        return str(exc.reason)


def notifier_for(settings: Settings) -> Notifier | None:
    """The configured service, or None. Raises ValueError for an incomplete configuration."""
    if settings.gotify_url:
        return GotifyNotifier(settings.gotify_url, settings.gotify_token, settings.public_url)
    if settings.gotify_token:
        raise ValueError("gotify_token is set but gotify_url is empty")
    return None


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+!|~])")


def md_escape(text: str) -> str:
    """Show `text` literally in Markdown (folder names can contain `*`, `_`, `[`, ...)."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def new_issues_message(store: Store, scan_id: int, resolved: int) -> Notification | None:
    """What scan `scan_id` found that wasn't open before, by album; None if nothing."""
    new = IssueFilter(first_seen_scan=scan_id)
    total = store.count_open_issues(new)
    if not total:
        return None
    album_count = store.count_open_issue_groups(new)
    groups = store.open_issue_groups(new, limit=MAX_ALBUMS)
    summary = _plural(total, "new issue") + (
        f" in {_plural(album_count, 'album')}" if album_count > 1 else ""
    )
    if resolved:
        summary += f"; {resolved:,} resolved"
    open_total = store.count_open_issues()
    lines = [f"{summary}. {open_total:,} open in all.", ""]
    for g in groups:
        checks = ", ".join(f"`{c}`" + (f" ({n})" if n > 1 else "") for c, n in g.checks.items())
        lines.append(f"- **{md_escape(g.key or '(library root)')}**: {checks}")
    if album_count > len(groups):
        lines.append(f"- …and {_plural(album_count - len(groups), 'more album')}")
    return Notification(
        title=f"Tag Drifter: {_plural(total, 'new issue')}",
        message="\n".join(lines),
        priority=5,
        path="/issues?" + urlencode({"scan": scan_id}),
    )


class Notifications:
    """Decides what to send for each event and sends it in the background."""

    def __init__(self, settings: Settings, notifier: Notifier | None = None) -> None:
        unknown = settings.notify_on - EVENTS
        if unknown:
            raise ValueError(
                f"unknown notify_on events: {', '.join(sorted(unknown))}"
                f" (choose from {', '.join(sorted(EVENTS))})"
            )
        self.settings = settings
        self.notifier = notifier if notifier is not None else notifier_for(settings)
        self._threads: list[threading.Thread] = []

    @property
    def enabled(self) -> bool:
        return self.notifier is not None

    def describe(self) -> str:
        return self.notifier.describe() if self.notifier is not None else ""

    def wants(self, event: str) -> bool:
        return self.enabled and event in self.settings.notify_on

    # --- Events -------------------------------------------------------------------------

    def scan_finished(self, scan_id: int, new: int, resolved: int) -> None:
        if not new or not self.wants("new_issues"):
            return
        try:
            with Store(self.settings.database_path) as store:
                notification = new_issues_message(store, scan_id, resolved)
        except Exception:
            log.exception("Couldn't build the new-issues notification for scan #%d", scan_id)
            return
        if notification is not None:
            self._send_later(notification)

    def scan_failed(self, error: str) -> None:
        if self.wants("failures"):
            self._send_later(
                Notification("Tag Drifter: scan failed", md_escape(error), 8, "/scans")
            )

    def rename_failed(self, album: str, error: str) -> None:
        """A rename or undo stopped partway: files may be left half-moved."""
        if self.wants("failures"):
            self._send_later(
                Notification(
                    "Tag Drifter: rename needs attention",
                    f"**{md_escape(album)}**\n\n{md_escape(error)}\n\n"
                    "Some files may still be at their old paths. Check the folder, then retry"
                    " the undo on the Renames page.",
                    9,
                    "/renames",
                )
            )

    def send_test(self) -> str:
        """Send a test message now. Returns the service's confirmation; raises NotifyError."""
        if self.notifier is None:
            raise NotifyError("No notification service is configured.")
        return self.notifier.send(
            Notification(
                "Tag Drifter: test notification",
                f"Notifications work. Sending: {', '.join(sorted(self.settings.notify_on))}.",
                5,
                "/",
            )
        )

    # --- Delivery -----------------------------------------------------------------------

    def _send_later(self, notification: Notification) -> None:
        thread = threading.Thread(
            target=self._deliver, args=(notification,), name="tagdrifter-notify", daemon=True
        )
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()

    def _deliver(self, notification: Notification) -> None:
        assert self.notifier is not None
        try:
            self.notifier.send(notification)
            log.info("Sent notification: %s", notification.title)
        except NotifyError as exc:
            log.warning("Notification %r not sent: %s", notification.title, exc)
        except Exception:
            log.exception("Notification %r not sent", notification.title)

    def wait(self, timeout: float | None = None) -> None:
        """Wait for notifications being sent (for tests and shutdown)."""
        for thread in list(self._threads):
            thread.join(timeout)
