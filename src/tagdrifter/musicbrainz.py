"""MusicBrainz web service client and release cache sync (roadmap 6b).

MusicBrainz is a small non-profit; its API asks every client to make at most one request
per second, to identify itself with a contact address, and not to hammer it on a fixed
schedule (https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting). So:

- one process-wide `RateLimiter` waits at least `MIN_INTERVAL` after each response before
  the next request goes out, whichever thread makes it (counting from the response, not the
  request, so network jitter can't make two requests arrive less than a second apart);
- one request per release fetches everything the checks need now *and later* (recordings,
  artist credits, release group, aliases), and the raw response is cached compressed, so
  adding a check never needs a refetch;
- releases are refreshed after a randomized 45-75 days, so refreshes trickle in;
- a 503 (rate limited) backs off twice, then ends the run, waiting at least as long as a
  Retry-After header asks; a run that ends on an error isn't retried for an hour
  (web/mbsync.py);
- responses are requested gzip-compressed;
- the User-Agent names the project and, if configured, the operator's contact address.

`sync_releases` fetches what is missing or due, and `refresh_releases` the releases someone
asked for (*Refresh from MusicBrainz*, roadmap 6b-3); the checks only ever read the cache
(`Store.mb_releases`), so scans stay offline. `release_group_releases` is only ever called
when someone asks (the release page's *Compare with other releases*, roadmap 6d), and so are
`recording` and `isrc` (`lookup_tracks`: *Look up these tracks*, for files that fit no
release in their release group), at most `MAX_TRACK_LOOKUPS` requests per click.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from . import __version__
from .core import is_mbid
from .db import Store

log = logging.getLogger(__name__)

API_ROOT = "https://musicbrainz.org/ws/2"
# One lookup covers every current and planned check: tracks and recordings (with their
# aliases, which Picard translates track titles from), artist credits (with artist aliases),
# the release group, and the release's own aliases (translated album titles).
RELEASE_INC = "recordings+artist-credits+release-groups+aliases"
PROJECT_URL = "https://github.com/emilystap/tagdrifter"
USER_AGENT = f"TagDrifter/{__version__} ( {PROJECT_URL} )"
MIN_INTERVAL = 1.5  # seconds from one response to the next request; the documented limit is 1/s
BACKOFF = (15.0, 60.0)  # waits after a 503 before retrying; then give up for this run
MAX_RETRY_WAIT = 300.0  # a longer Retry-After ends the run instead of blocking the thread
REFRESH_DAYS = (45, 75)  # a cached release is looked up again after this many days
NOT_FOUND_REFRESH_DAYS = (30, 60)
REFRESH_MIN_AGE = 300.0  # seconds: a release fetched more recently isn't refreshed on request
# Browsing a release group's releases with their tracklists: 100 releases per request (the
# API's maximum), and no more than this many requests, so a huge release group stays cheap.
BROWSE_LIMIT = 100
MAX_BROWSE_PAGES = 3
# A recording with every release it's on (up to 25, the lookup's limit) and the length of its
# track on each; enough to tell which releases a file's audio could have come from.
RECORDING_INC = "releases+media"
MAX_TRACK_LOOKUPS = 10  # requests per click on *Look up these tracks* (about 15 s)
_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")


def normalize_isrc(value: str) -> str:
    """An ISRC in its canonical form (upper case, no hyphens), or "" if it isn't one."""
    value = value.strip().replace("-", "").upper()
    return value if _ISRC.fullmatch(value) else ""


class MusicBrainzError(Exception):
    """A request failed in a way worth stopping for (rate limited, network, server error).
    `retry_after`: seconds MusicBrainz asked us to wait (Retry-After), if it said."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def user_agent(contact: str = "") -> str:
    """The User-Agent MusicBrainz asks for: application, version and a way to reach whoever
    runs it. The project URL always; the operator's own address (e.g. an email) if set."""
    contact = contact.strip()
    return f"TagDrifter/{__version__} ( {contact} ; {PROJECT_URL} )" if contact else USER_AGENT


def validate_contact(contact: str) -> str:
    """Refuse values that would break the header (control characters, parentheses)."""
    contact = contact.strip()
    if len(contact) > 200 or any(c in contact for c in "()") or not contact.isprintable():
        raise ValueError(
            "musicbrainz_contact must be a short email address or URL on one line, "
            f"without parentheses: {contact!r}"
        )
    return contact


def retry_after_seconds(
    headers: Mapping[str, str] | None, now: datetime | None = None
) -> float | None:
    """A Retry-After header's wait in seconds: delta-seconds or an HTTP date."""
    value = (headers or {}).get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class ReleaseNotFound(Exception):
    """MusicBrainz has no release with this MBID (deleted, or never existed)."""


class RateLimiter:
    """Spaces calls across threads: `wait()` before each call, `done()` after it; the next
    call goes out at least `interval` seconds after the previous one finished."""

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

    def done(self) -> None:
        """The call finished (answered or failed): count the interval from now."""
        with self._lock:
            self._next = max(self._next, self._clock() + self.interval)

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
        contact: str = "",
    ) -> None:
        self.api_root = api_root.rstrip("/")
        self.user_agent = user_agent(validate_contact(contact) if contact else "")
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

    def release_group_releases(self, mbid: str) -> dict:
        """Every release in a release group with its tracklists (track lengths, recordings):
        `{"release-count": N, "releases": [...]}`, at most `MAX_BROWSE_PAGES` requests of
        `BROWSE_LIMIT` releases, so `releases` can be shorter than the count. An unknown
        release group has no releases. Raises MusicBrainzError when MusicBrainz fails."""
        if not is_mbid(mbid):
            raise ValueError(f"not an MBID: {mbid!r}")
        releases: list[dict] = []
        count = 0
        for _ in range(MAX_BROWSE_PAGES):
            try:
                data = self._get(
                    f"/release?release-group={mbid.lower()}&inc=recordings+media"
                    f"&limit={BROWSE_LIMIT}&offset={len(releases)}&fmt=json",
                    mbid,
                )
            except ReleaseNotFound:
                break
            page = data.get("releases") or []
            releases += page
            count = int(data.get("release-count") or len(releases))
            if not page or len(releases) >= count:
                break
        return {"release-count": max(count, len(releases)), "releases": releases}

    def recording(self, mbid: str) -> dict:
        """A recording with the releases it's on (at most 25) and its track on each
        (`media[].tracks`, with the length). Raises ReleaseNotFound for an unknown recording,
        or MusicBrainzError."""
        if not is_mbid(mbid):
            raise ValueError(f"not an MBID: {mbid!r}")
        return self._get(f"/recording/{mbid.lower()}?inc={RECORDING_INC}&fmt=json", mbid)

    def isrc(self, code: str) -> dict:
        """The recordings with this ISRC (`recordings`, with their lengths). Raises
        ReleaseNotFound when MusicBrainz has none, or MusicBrainzError."""
        isrc = normalize_isrc(code)
        if not isrc:
            raise ValueError(f"not an ISRC: {code!r}")
        return self._get(f"/isrc/{isrc}?fmt=json", isrc)

    def _get(self, path: str, what: str) -> dict:
        request = urllib.request.Request(
            self.api_root + path,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        for attempt in range(len(self.backoff) + 1):
            self.limiter.wait()
            self.requests += 1
            try:
                try:
                    with (self._open or _urlopen)(request, timeout=self.timeout) as response:
                        body = response.read()
                        gzipped = response.headers.get("Content-Encoding") == "gzip"
                finally:
                    self.limiter.done()  # whatever the outcome, space from the response
                return json.loads(gzip.decompress(body) if gzipped else body)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise ReleaseNotFound(what) from exc
                retry_after = retry_after_seconds(exc.headers) if exc.code == 503 else None
                if (
                    exc.code == 503
                    and attempt < len(self.backoff)
                    and (retry_after or 0) <= MAX_RETRY_WAIT
                ):
                    wait = max(self.backoff[attempt], retry_after or 0)
                    log.warning("MusicBrainz is rate limiting us; waiting %.0fs", wait)
                    self.limiter.pause(wait)
                    continue
                if retry_after is not None:
                    self.limiter.pause(retry_after)  # in case another run starts sooner
                raise MusicBrainzError(
                    f"MusicBrainz answered {exc.code} for {what}"
                    + (f" (retry after {retry_after:.0f}s)" if retry_after else ""),
                    retry_after=retry_after,
                ) from exc
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
    skipped: int = 0  # asked for, but fetched less than REFRESH_MIN_AGE ago
    error: str | None = None  # why the run stopped early
    retry_after: float | None = None  # how long MusicBrainz asked us to wait, if it did

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
        if not _fetch(store, client, mbid, now, result):
            log.warning(
                "MusicBrainz sync stopped after %d of %d: %s", done - 1, len(due), result.error
            )
            break
        if progress:
            progress(done, len(due))
    if not result.error:
        pruned = store.prune_mb_cache()
        if pruned:
            log.info("MusicBrainz cache: removed %d releases no longer in the library", pruned)
    return result


def refresh_releases(
    store: Store,
    client: MusicBrainzClient,
    mbids: Iterable[str],
    *,
    now: datetime | None = None,
    min_age: float = REFRESH_MIN_AGE,
) -> SyncResult:
    """Fetch these releases now, whatever their `refresh_after` (someone asked: they edited
    the release on MusicBrainz, or want to confirm a fix). Releases fetched less than
    `min_age` seconds ago are skipped, so repeated clicks can't hammer the API. Stops at the
    first MusicBrainzError."""
    now = now or datetime.now(UTC)
    ids = sorted({m.lower() for m in mbids if is_mbid(m)})
    fetched_at = store.mb_release_fetched_at(ids)
    result = SyncResult(due=len(ids))
    for mbid in ids:
        last = fetched_at.get(mbid)
        if last and (now - datetime.fromisoformat(last)).total_seconds() < min_age:
            result.skipped += 1
            continue
        if not _fetch(store, client, mbid, now, result):
            break
    return result


def lookup_tracks(
    store: Store,
    client: MusicBrainzClient,
    keys: Iterable[tuple[str, str]],
    *,
    now: datetime | None = None,
    min_age: float = REFRESH_MIN_AGE,
    limit: int = MAX_TRACK_LOOKUPS,
) -> SyncResult:
    """Per-track lookups someone asked for: `keys` are ("recording", MBID) or ("isrc", ISRC)
    pairs, in the order they matter. Ones never looked up go first; only when every one is
    cached are they looked up again (those older than `min_age` seconds). At most `limit`
    requests; stops at the first MusicBrainzError. `due` counts what was left to look up."""
    now = now or datetime.now(UTC)
    wanted = list(dict.fromkeys(_lookup_key(kind, key) for kind, key in keys))
    cached = store.mb_lookups(wanted)
    todo = [k for k in wanted if k not in cached]
    if not todo:
        todo = [
            k
            for k in wanted
            if (now - datetime.fromisoformat(cached[k][1])).total_seconds() >= min_age
        ]
    result = SyncResult(due=len(todo), skipped=len(wanted) - len(todo))
    for kind, key in todo[:limit]:
        try:
            data = client.recording(key) if kind == "recording" else client.isrc(key)
        except ReleaseNotFound:
            store.save_mb_lookup(kind, key, None)
            result.not_found += 1
        except MusicBrainzError as exc:
            result.error = str(exc)
            result.retry_after = exc.retry_after
            break
        else:
            store.save_mb_lookup(kind, key, data)
            result.fetched += 1
    return result


def _lookup_key(kind: str, key: str) -> tuple[str, str]:
    if kind == "recording" and is_mbid(key):
        return kind, key.lower()
    if kind == "isrc" and normalize_isrc(key):
        return kind, normalize_isrc(key)
    raise ValueError(f"not a lookup: {kind} {key!r}")


def _fetch(
    store: Store, client: MusicBrainzClient, mbid: str, now: datetime, result: SyncResult
) -> bool:
    """Look up one release into the cache, counting it in `result`. Returns False (with
    `result.error` set) when MusicBrainz failed and the run should stop."""
    try:
        data = client.release(mbid)
    except ReleaseNotFound:
        store.save_mb_release(mbid, None, _refresh_after(now, NOT_FOUND_REFRESH_DAYS))
        result.not_found += 1
    except MusicBrainzError as exc:
        result.error = str(exc)
        result.retry_after = exc.retry_after
        return False
    else:
        store.save_mb_release(mbid, data, _refresh_after(now, REFRESH_DAYS))
        result.fetched += 1
    return True
