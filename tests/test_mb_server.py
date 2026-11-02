"""Another MusicBrainz server (a mirror of your own): `[musicbrainz] api_root` / `interval`,
and musicbrainz.org's own rate limit, which can't be loosened."""

import pytest
from fastapi.testclient import TestClient

from tagdrifter.config import Settings, load_settings
from tagdrifter.musicbrainz import (
    API_ROOT,
    LIMITER,
    MIN_INTERVAL,
    client_for,
    is_public_server,
    normalize_api_root,
)
from tagdrifter.web import create_app
from tagdrifter.web.mbsync import MusicBrainzSync

from .conftest import settings_for
from .test_musicbrainz import MAENARAWANAI, FakeOpener

MIRROR = "http://mirror.lan:5000"


def test_api_root_forms() -> None:
    assert normalize_api_root("") == API_ROOT == "https://musicbrainz.org/ws/2"
    assert normalize_api_root(MIRROR + "/") == MIRROR + "/ws/2"
    assert normalize_api_root(f" {MIRROR}/ws/2/ ") == MIRROR + "/ws/2"
    assert normalize_api_root("https://mb.example.com/musicbrainz") == (
        "https://mb.example.com/musicbrainz/ws/2"
    )
    for bad in ("mirror.lan:5000", "ftp://mirror.lan", "http:///ws/2", MIRROR + "?x=1"):
        with pytest.raises(ValueError, match="api_root"):
            normalize_api_root(bad)
    assert is_public_server(API_ROOT) and is_public_server("https://beta.musicbrainz.org/ws/2")
    assert not is_public_server(MIRROR + "/ws/2")
    assert not is_public_server("https://notmusicbrainz.org/ws/2")


def test_musicbrainz_org_by_default() -> None:
    client = client_for(Settings())
    assert client.api_root == API_ROOT and client.limiter is LIMITER


def test_musicbrainz_org_never_faster_than_its_limit() -> None:
    for interval in (0, 1.0, MIN_INTERVAL - 0.01):
        with pytest.raises(ValueError, match=r"musicbrainz\.org"):
            client_for(Settings(musicbrainz_interval=interval))
    with pytest.raises(ValueError, match=r"musicbrainz\.org"):
        client_for(
            Settings(musicbrainz_api_root="https://beta.musicbrainz.org", musicbrainz_interval=0)
        )
    assert client_for(Settings(musicbrainz_interval=MIN_INTERVAL)).limiter is LIMITER
    slower = client_for(Settings(musicbrainz_interval=3))
    assert slower.limiter is not LIMITER and slower.limiter.interval == 3


def test_a_mirror_of_your_own() -> None:
    settings = Settings(musicbrainz_api_root=MIRROR, musicbrainz_interval=0)
    client = client_for(settings)
    assert client.api_root == MIRROR + "/ws/2" and client.limiter.interval == 0
    # One limiter per server, shared like musicbrainz.org's.
    assert client_for(settings).limiter is client.limiter
    assert client_for(Settings(musicbrainz_api_root=MIRROR)).limiter.interval == MIN_INTERVAL
    with pytest.raises(ValueError, match="negative"):
        client_for(Settings(musicbrainz_api_root=MIRROR, musicbrainz_interval=-1))

    opener = FakeOpener((200, {"id": MAENARAWANAI}))
    client._open = opener
    client.release(MAENARAWANAI)
    (request,) = opener.requests
    assert request.full_url.startswith(f"{MIRROR}/ws/2/release/{MAENARAWANAI}?")


def test_server_settings(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[musicbrainz]\napi_root = "{MIRROR}"\ninterval = 0.2\n', encoding="utf-8")
    settings = load_settings(cfg, env={})
    assert (settings.musicbrainz_api_root, settings.musicbrainz_interval) == (MIRROR, 0.2)
    env = {
        "TAGDRIFTER_MUSICBRAINZ_API_ROOT": "http://other:5000",
        "TAGDRIFTER_MUSICBRAINZ_INTERVAL": "0",
    }
    settings = load_settings(cfg, env=env)
    assert MusicBrainzSync(settings).client.api_root == "http://other:5000/ws/2"
    assert load_settings(env={}).musicbrainz_interval is None
    with pytest.raises(ValueError):  # refused at startup, not at the first lookup
        create_app(Settings(musicbrainz_interval=0.5))


def test_dashboard_names_another_server(library, tmp_path) -> None:
    settings = settings_for(
        library,
        tmp_path,
        musicbrainz_enabled=True,
        musicbrainz_api_root=MIRROR,
        musicbrainz_interval=0,
    )
    with TestClient(create_app(settings)) as c:
        page = c.get("/").text
    assert f"MusicBrainz server at <code>{MIRROR}</code>, 0 s between requests" in page
    with TestClient(create_app(settings_for(library, tmp_path, musicbrainz_enabled=True))) as c:
        assert "MusicBrainz server at" not in c.get("/").text


def test_footer_credits_musicbrainz_while_lookups_are_on(library, tmp_path) -> None:
    credit = 'Release data from <a href="https://musicbrainz.org/">MusicBrainz</a>'
    with TestClient(create_app(settings_for(library, tmp_path, musicbrainz_enabled=True))) as c:
        assert credit in c.get("/library").text
    with TestClient(create_app(settings_for(library, tmp_path))) as c:
        assert credit not in c.get("/library").text
