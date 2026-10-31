from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tagdrifter.config import load_settings
from tagdrifter.web import create_app
from tagdrifter.web.auth import (
    SESSION_COOKIE,
    Authenticator,
    hash_password,
    is_cross_site,
    safe_next,
    verify_password,
)

from .conftest import settings_for

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def settings(library, tmp_path, password_hash):
    return replace(settings_for(library, tmp_path), password_hash=password_hash)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def log_in(client, password: str = PASSWORD, next_: str = "/"):
    return client.post(
        "/login",
        data={"password": password, "next": next_},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


# --- Passwords and sessions -----------------------------------------------------------------


def test_password_hash_round_trip(password_hash) -> None:
    assert password_hash.startswith("scrypt:") and "$" not in password_hash
    assert verify_password(PASSWORD, password_hash)
    assert not verify_password("wrong", password_hash)
    assert not verify_password(PASSWORD, "garbage")
    assert hash_password(PASSWORD) != password_hash  # salted


def test_invalid_hash_fails_at_startup(library, tmp_path) -> None:
    with pytest.raises(ValueError, match="invalid password hash"):
        create_app(replace(settings_for(library, tmp_path), password_hash="hunter2"))


def test_sessions_are_signed_and_expire(settings) -> None:
    authenticator = Authenticator(settings)
    cookie = authenticator.new_session(now=1_000)
    assert authenticator.valid_session(cookie, now=1_001)
    assert not authenticator.valid_session(cookie, now=1_000 + 31 * 86400)  # expired
    expires, signature = cookie.split(".")
    assert not authenticator.valid_session(f"{int(expires) + 999}.{signature}", now=1_001)
    assert not authenticator.valid_session("garbage", now=1_001)


def test_secret_key_persists_in_data_dir(settings) -> None:
    cookie = Authenticator(settings).new_session()
    assert (settings.database_path.parent / "secret_key").is_file()
    assert Authenticator(settings).valid_session(cookie)  # survives a restart
    other = replace(settings, secret_key="something else")
    assert not Authenticator(other).valid_session(cookie)


def test_auth_settings_from_env(tmp_path) -> None:
    env = {"TAGDRIFTER_PASSWORD_HASH": "scrypt:1:2:3:a:b", "TAGDRIFTER_SECRET_KEY": "k"}
    s = load_settings(env=env)
    assert (s.password_hash, s.secret_key) == ("scrypt:1:2:3:a:b", "k")
    assert "scrypt" not in repr(s)  # kept out of logs and tracebacks


# --- Login flow -----------------------------------------------------------------------------


def test_pages_need_login(client) -> None:
    response = client.get("/issues?view=list", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fissues%3Fview%3Dlist"
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/login").status_code == 200


def test_htmx_requests_are_sent_to_login(client) -> None:
    response = client.get(
        "/scan/status",
        headers={"HX-Request": "true", "HX-Current-URL": "https://tags.example.com/scans"},
    )
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login?next=%2Fscans"


def test_scan_needs_login(client) -> None:
    assert client.post("/scan", headers=SAME_ORIGIN).status_code == 401
    assert not client.app.state.runner.status().running


def test_login_and_logout(client) -> None:
    response = log_in(client, next_="/scans")
    assert response.status_code == 303 and response.headers["location"] == "/scans"
    set_cookie = response.headers["set-cookie"]
    assert SESSION_COOKIE in set_cookie and "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower() and "Secure" not in set_cookie

    page = client.get("/")
    assert page.status_code == 200 and "Log out" in page.text

    out = client.post("/logout", headers=SAME_ORIGIN, follow_redirects=False)
    assert out.headers["location"] == "/login"
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).status_code == 303


def test_wrong_password(client, monkeypatch) -> None:
    import asyncio

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    response = log_in(client, password="nope")
    assert response.status_code == 401 and "Wrong password." in response.text
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_cookie_is_secure_behind_https_proxy(client) -> None:
    response = client.post(
        "/login",
        data={"password": PASSWORD, "next": "/"},
        headers=SAME_ORIGIN | {"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert "Secure" in response.headers["set-cookie"]


def test_login_redirect_stays_on_site(client) -> None:
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example"):
        assert log_in(client, next_=evil).headers["location"] == "/"


def test_no_password_means_no_login(library, tmp_path) -> None:
    with TestClient(create_app(settings_for(library, tmp_path))) as c:
        page = c.get("/")
        assert page.status_code == 200 and "Log out" not in page.text
        assert c.get("/login", follow_redirects=False).headers["location"] == "/"


# --- CSRF -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "cross_site"),
    [
        ({"sec-fetch-site": "same-origin"}, False),
        ({"sec-fetch-site": "none"}, False),  # typed into the address bar, bookmarks
        ({"sec-fetch-site": "cross-site"}, True),
        ({"sec-fetch-site": "same-site"}, True),  # another subdomain is another app
        ({"origin": "https://tags.example.com", "host": "tags.example.com"}, False),
        ({"origin": "https://evil.example", "host": "tags.example.com"}, True),
        ({"origin": "null", "host": "tags.example.com"}, True),
        ({}, False),  # curl, scripts: not a browser, so not CSRF
    ],
)
def test_is_cross_site(headers, cross_site) -> None:
    assert is_cross_site("POST", headers) is cross_site
    assert is_cross_site("GET", headers) is False


def test_cross_site_posts_are_refused(library, tmp_path) -> None:
    with TestClient(create_app(settings_for(library, tmp_path))) as c:
        response = c.post("/scan", headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403
        assert not c.app.state.runner.status().running
        assert c.post("/scan", headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.post("/scan", headers=SAME_ORIGIN).status_code == 200


def test_cross_site_login_is_refused(client) -> None:
    response = client.post(
        "/login", data={"password": PASSWORD}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403


@pytest.mark.parametrize("target", ["/", "/issues?view=list", "/scans"])
def test_safe_next_keeps_local_paths(target) -> None:
    assert safe_next(target) == target


def test_hash_password_cli_reads_stdin(monkeypatch, capsys) -> None:
    import io

    from tagdrifter.cli import main

    # Windows PowerShell 5.1 pipes text with a leading byte-order mark and CRLF.
    monkeypatch.setattr("sys.stdin", io.StringIO("﻿" + PASSWORD + "\r\n"))
    assert main(["hash-password", "--stdin"]) == 0
    assert verify_password(PASSWORD, capsys.readouterr().out.strip())

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["hash-password", "--stdin"]) == 1
