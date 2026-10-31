"""MusicBrainz web service client and release cache sync (roadmap 6b).

MusicBrainz is a small non-profit; its API asks every client to make at most one request
per second, to identify itself with a contact address, and not to hammer it on a fixed
schedule (https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting). So:

- one process-wide `RateLimiter` spaces requests at least `MIN_INTERVAL` apart, whichever
  thread makes them;
- one request per release fetches everything the checks need now *and later* (recordings,
  artist credits, release group, aliases), and the raw response is cached compressed, so
  adding a check never needs a refetch;
- releases are refreshed after a randomized 45-75 days, so refreshes trickle in;
- a 503 (rate limited) backs off twice, then ends the run; it resumes at the next sync;
- responses are requested gzip-compressed.

`sync_releases` fetches what is missing or due; the checks only ever read the cache
(`Store.mb_releases`), so scans stay offline.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import __version__
from .core import is_mbid
from .db import Store

log = logging.getLogger(__name__)

API_ROOT = "https://musicbrainz.org/ws/2"
# One lookup covers every current and planned check: tracks and recordings (with their
# aliases, which Picard translates track titles from), artist credits (with artist aliases),
# the release group, and the release's own aliases (translated album titles).
RELEASE_INC = "recordings+artist-credits+release-groups+aliases"
USER_AGENT = f"TagDrifter/{__version__} ( https://github.com/emilystap/tagdrifter )"
MIN_INTERVAL = 1.1  # seconds between requests; the documented limit is 1/s
BACKOFF = (15.0, 60.0)  # waits after a 503 before retrying; then give up for this run
REFRESH_DAYS = (45, 75)  # a cached release is looked up again after this many days
NOT_FOUND_REFRESH_DAYS = (30, 60)


class MusicBrainzError(Exception):
    """A request failed in a way worth stopping for (rate limited, network, server error)."""


class ReleaseNotFound(Exception):
    """MusicBrainz has no release with this MBID (deleted, or never existed)."""


class RateLimiter:
    """Spaces calls at least `interval` seconds apart across threads."""

    def __init__(
        self,
        interval: float = MIN_INTERVAL,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval

    def pause(self, seconds: float) -> None:
        """Push the next allowed call at least `seconds` into the future (after a 503)."""
        with self._lock:
            self._next = max(self._next, self._clock() + seconds)


# Shared by every client in the process, so two syncs can never double the rate.
LIMITER = RateLimiter()

Opener = Callable[..., object]
# Looked up at request time, so tests can block real requests (tests/conftest.py).
_urlopen: Opener = urllib.request.urlopen


class MusicBrainzClient:
    def __init__(
        self,
        *,
        api_root: str = API_ROOT,
        limiter: RateLimiter = LIMITER,
        timeout: float = 30,
        opener: Opener | None = None,
        backoff: tuple[float, ...] = BACKOFF,
    ) -> None:
        self.api_root = api_root.rstrip("/")
        self.limiter = limiter
        self.timeout = timeout
        self._open = opener
        self.backoff = backoff
        self.requests = 0  # made by this client, for logs and tests

    def release(self, mbid: str) -> dict:
        """The release with recordings, artist credits, release group and aliases. Raises
        ReleaseNotFound, or MusicBrainzError when the run should stop."""
        if not is_mbid(mbid):
            raise ValueError(f"not an MBID: {mbid!r}")
        return self._get(f"/release/{mbid.lower()}?inc={RELEASE_INC}&fmt=json", mbid)

    def _get(self, path: str, what: str) -> dict:
        request = urllib.request.Request(
            self.api_root + path,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        for attempt in range(len(self.backoff) + 1):
            self.limiter.wait()
            self.requests += 1
            try:
                with (self._open or _urlopen)(request, timeout=self.timeout) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise ReleaseNotFound(what) from exc
                if exc.code == 503 and attempt < len(self.backoff):
                    wait = self.backoff[attempt]
                    log.warning("MusicBrainz is rate limiting us; waiting %.0fs", wait)
                    self.limiter.pause(wait)
                    continue
                raise MusicBrainzError(f"MusicBrainz answered {exc.code} for {what}") from exc
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise MusicBrainzError(f"Couldn't reach MusicBrainz: {reason}") from exc
            except ValueError as exc:
                raise MusicBrainzError(f"Unexpected reply from MusicBrainz: {exc}") from exc
        raise AssertionError("unreachable")


# --- Cache sync -------------------------------------------------------------------------


@dataclass
class SyncResult:
    due: int = 0  # releases missing from the cache or due for a refresh
    fetched: int = 0
    not_found: int = 0
    error: str | None = None  # why the run stopped early

    @property
    def changed(self) -> bool:
        return bool(self.fetched or self.not_found)


def _refresh_after(now: datetime, days: tuple[int, int]) -> str:
    return (now + timedelta(days=random.uniform(*days))).isoformat(timespec="seconds")


def sync_releases(
    store: Store,
    client: MusicBrainzClient,
    *,
    limit: int | None = None,
    stop: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    now: datetime | None = None,
) -> SyncResult:
    """Fetch the library's releases that aren't cached yet (first), then those due for a
    refresh, at most `limit` of them. Stops at the first MusicBrainzError, keeping what it
    fetched."""
    now = now or datetime.now(UTC)
    due = store.mb_releases_due(now.isoformat(timespec="seconds"))
    if limit is not None:
        due = due[:limit]
    result = SyncResult(due=len(due))
    for done, mbid in enumerate(due, 1):
        if stop is not None and stop.is_set():
            break
        try:
            data = client.release(mbid)
        except ReleaseNotFound:
            store.save_mb_release(mbid, None, _refresh_after(now, NOT_FOUND_REFRESH_DAYS))
            result.not_found += 1
        except MusicBrainzError as exc:
            result.error = str(exc)
            log.warning("MusicBrainz sync stopped after %d of %d: %s", done - 1, len(due), exc)
            break
        else:
            store.save_mb_release(mbid, data, _refresh_after(now, REFRESH_DAYS))
            result.fetched += 1
        if progress:
            progress(done, len(due))
    return result
