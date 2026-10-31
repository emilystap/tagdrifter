import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from tagdrifter.config import Settings, load_settings
from tagdrifter.notify import (
    GotifyNotifier,
    Notification,
    Notifications,
    NotifyError,
    md_escape,
    new_issues_message,
    notifier_for,
)
from tagdrifter.web import ScanRunner, create_app

from .conftest import build_flac, good_tags, scan, settings_for

TOKEN = "AbCdEf.123"
UNAUTHORIZED = "you need to provide a valid access token or user credentials to access this api"


class FakeGotify:
    """A local stand-in for Gotify's POST /message: records requests, answers like Gotify."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.received.append({"path": self.path, "headers": dict(self.headers), **body})
                if self.headers.get("X-Gotify-Key") != TOKEN:
                    code, reply = 401, {"error": "Unauthorized", "errorDescription": UNAUTHORIZED}
                else:
                    code, reply = fake.status, {"id": len(fake.received)}
                data = json.dumps(reply).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/gotify/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gotify():
    fake = FakeGotify()
    yield fake
    fake.close()


@pytest.fixture
def settings(library, tmp_path, gotify):
    return settings_for(
        library,
        tmp_path,
        gotify_url=gotify.url,
        gotify_token=TOKEN,
        public_url="https://td.example/",
    )


def test_settings_from_toml_and_env(tmp_path) -> None:
    assert Settings().notify_on == {"new_issues", "failures"}
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[web]\npublic_url = "https://td.lan"\n'
        '[notify]\ngotify_url = "http://gotify.lan"\ngotify_token = "t"\non = ["failures"]\n',
        encoding="utf-8",
    )
    s = load_settings(cfg, env={})
    assert (s.public_url, s.gotify_url, s.gotify_token) == (
        "https://td.lan",
        "http://gotify.lan",
        "t",
    )
    assert s.notify_on == {"failures"}
    assert "gotify_token" not in repr(s)
    env = {"TAGDRIFTER_NOTIFY_ON": "new_issues, Failures", "TAGDRIFTER_GOTIFY_TOKEN": "u"}
    s = load_settings(cfg, env=env)
    assert s.notify_on == {"new_issues", "failures"} and s.gotify_token == "u"
    assert load_settings(cfg, env={"TAGDRIFTER_NOTIFY_ON": "none"}).notify_on == frozenset()


def test_invalid_settings_are_refused(tmp_path) -> None:
    assert notifier_for(Settings()) is None
    with pytest.raises(ValueError, match="gotify_token is empty"):
        notifier_for(Settings(gotify_url="http://gotify.lan"))
    with pytest.raises(ValueError, match="gotify_url is empty"):
        notifier_for(Settings(gotify_token="t"))
    with pytest.raises(ValueError, match="http"):
        notifier_for(Settings(gotify_url="gotify.lan", gotify_token="t"))
    with pytest.raises(ValueError, match="unknown notify_on events: scans"):
        Notifications(Settings(notify_on=frozenset({"scans"})))


def test_gotify_message_format(gotify) -> None:
    notifier = GotifyNotifier(gotify.url, TOKEN, "https://td.example/")
    reply = notifier.send(Notification("Title", "**body**", 7, "/issues?scan=3"))
    assert reply == "Gotify accepted message #1."
    [sent] = gotify.received
    assert sent["path"] == "/gotify/message"  # base path kept, token not in the URL
    assert sent["headers"]["X-Gotify-Key"] == TOKEN
    assert (sent["title"], sent["message"], sent["priority"]) == ("Title", "**body**", 7)
    assert sent["extras"]["client::display"] == {"contentType": "text/markdown"}
    assert sent["extras"]["client::notification"]["click"]["url"] == (
        "https://td.example/issues?scan=3"
    )

    GotifyNotifier(gotify.url, TOKEN).send(Notification("No link", "x"))
    assert "client::notification" not in gotify.received[-1]["extras"]


def test_gotify_errors_are_explained(gotify) -> None:
    with pytest.raises(NotifyError, match="Gotify answered 401: you need to provide a valid"):
        GotifyNotifier(gotify.url, "wrong").send(Notification("t", "m"))
    gotify.status = 500
    with pytest.raises(NotifyError, match="Gotify answered 500"):
        GotifyNotifier(gotify.url, TOKEN).send(Notification("t", "m"))
    port = gotify.server.server_port
    gotify.close()
    with pytest.raises(NotifyError, match="Couldn't reach Gotify"):
        GotifyNotifier(f"http://127.0.0.1:{port}", TOKEN, timeout=2).send(Notification("t", "m"))


def test_md_escape() -> None:
    assert md_escape("*NSYNC/[Live]_1") == r"\*NSYNC/\[Live\]\_1"


def test_new_issues_message_lists_albums(settings) -> None:
    from tagdrifter.db import Store

    result, _ = scan(settings)
    with Store(settings.database_path) as store:
        n = new_issues_message(store, result.scan_id, resolved=2)
        assert new_issues_message(store, result.scan_id + 1, resolved=0) is None
    assert n is not None
    assert n.title == "Tag Drifter: 7 new issues"
    assert n.path == f"/issues?scan={result.scan_id}"
    assert "7 new issues in " in n.message and "; 2 resolved. 7 open in all." in n.message
    assert "- **Pixies/Hits**: `album.placement`" in n.message
    assert "`path.track_location`, `tags.invalid_mbid`, `tags.mbids`\n" in n.message


def test_scan_sends_new_issues_once(settings, library, gotify) -> None:
    runner = ScanRunner(settings)
    with TestClient(create_app(settings, runner)):
        assert runner.start()
        runner.wait(30)
        runner.notifications.wait(10)
        [sent] = gotify.received
        assert sent["title"] == "Tag Drifter: 7 new issues" and sent["priority"] == 5

        assert runner.start()  # nothing new: nothing sent
        runner.wait(30)
        runner.notifications.wait(10)
        assert len(gotify.received) == 1

        build_flac(library / "Pixies" / "Hits" / "09 New.flac", good_tags(TITLE="Other"))
        assert runner.start()
        runner.wait(30)
        runner.notifications.wait(10)
        assert len(gotify.received) == 2
        new = runner.status().result.issues_new
        assert gotify.received[-1]["title"] == f"Tag Drifter: {new} new issue{'s' * (new > 1)}"


def test_scan_failure_and_event_filter(tmp_path, gotify) -> None:
    settings = settings_for(
        tmp_path / "missing", tmp_path, gotify_url=gotify.url, gotify_token=TOKEN
    )
    runner = ScanRunner(settings, Notifications(settings))
    runner.start()
    runner.wait(10)
    runner.notifications.wait(10)
    [sent] = gotify.received
    assert sent["title"] == "Tag Drifter: scan failed" and sent["priority"] == 8
    assert "FileNotFoundError" in sent["message"]

    quiet = Settings(**{**settings.__dict__, "notify_on": frozenset({"new_issues"})})
    runner = ScanRunner(quiet, Notifications(quiet))
    runner.start()
    runner.wait(10)
    runner.notifications.wait(10)
    assert len(gotify.received) == 1


def test_rename_left_half_done_is_notified(settings, gotify) -> None:
    notifications = Notifications(settings)
    notifications.rename_failed("Pixies/Hits_(1)", "Undo stopped at Pixies/x: gone")
    notifications.wait(10)
    [sent] = gotify.received
    assert sent["priority"] == 9 and sent["title"] == "Tag Drifter: rename needs attention"
    assert sent["message"].startswith(r"**Pixies/Hits\_\(1\)**")
    assert sent["extras"]["client::notification"]["click"]["url"] == "https://td.example/renames"


def test_unreachable_service_never_breaks_a_scan(library, tmp_path) -> None:
    settings = settings_for(library, tmp_path, gotify_url="http://127.0.0.1:9", gotify_token=TOKEN)
    runner = ScanRunner(settings, Notifications(settings))
    runner.start()
    runner.wait(30)
    runner.notifications.wait(30)
    assert runner.status().error is None and runner.status().result.issues_new == 7


def test_dashboard_test_button(settings, gotify) -> None:
    with TestClient(create_app(settings)) as c:
        page = c.get("/").text
        assert "Sending to Gotify at 127.0.0.1:" in page and "Send test notification" in page
        response = c.post("/notify/test")
        assert response.status_code == 200 and "Gotify accepted message #1." in response.text
        assert gotify.received[0]["title"] == "Tag Drifter: test notification"


def test_dashboard_test_button_shows_errors(settings) -> None:
    bad = Settings(**{**settings.__dict__, "gotify_token": "wrong"})
    with TestClient(create_app(bad)) as c:
        assert "Gotify answered 401" in c.post("/notify/test").text


def test_notifications_off_by_default(library, tmp_path) -> None:
    with TestClient(create_app(settings_for(library, tmp_path))) as c:
        assert "Off. Set <code>TAGDRIFTER_GOTIFY_URL</code>" in c.get("/").text
        assert "No notification service is configured." in c.post("/notify/test").text


def test_issues_new_in_scan_filter(settings, library) -> None:
    first, _ = scan(settings)
    build_flac(library / "Pixies" / "Hits" / "09 New.flac", good_tags(TITLE="Other"))
    second, _ = scan(settings)
    with TestClient(create_app(settings)) as c:
        page = c.get("/issues", params={"scan": second.scan_id, "view": "list"}).text
        n = second.issues_new
        assert f"1–{n} of {n} open issue" in " ".join(page.split())
        assert f"New in #{second.scan_id}" in page
        assert f'href="/issues?scan={second.scan_id}"' in c.get("/scans").text
        assert f"/issues?scan={first.scan_id}" in c.get("/scans").text
