"""Settings: defaults < TOML config file < environment variables < explicit overrides.

Environment variables (handy in Docker compose):
    TAGDRIFTER_CONFIG          path to config.toml
    TAGDRIFTER_LIBRARY         library root
    TAGDRIFTER_DB              SQLite database path
    TAGDRIFTER_CASE_SENSITIVE  true/false
    TAGDRIFTER_WORKERS         parallel file reads
    TAGDRIFTER_SCHEDULE        cron expression for automatic scans, or "off"
    TAGDRIFTER_PASSWORD_HASH   web UI password, as printed by `tagdrifter hash-password`
    TAGDRIFTER_SECRET_KEY      key for signing login sessions (default: generated in the data dir)
    TAGDRIFTER_FIX             true/false: allow renaming files from the web UI
    TAGDRIFTER_PUBLIC_URL      the web UI's address, for links in notifications
    TAGDRIFTER_GOTIFY_URL      Gotify server to send notifications to
    TAGDRIFTER_GOTIFY_TOKEN    Gotify application token
    TAGDRIFTER_NOTIFY_ON       comma-separated events: new_issues, failures (or "none")
    TAGDRIFTER_MUSICBRAINZ     true/false: look up releases on MusicBrainz for the mb.* checks
    TAGDRIFTER_MUSICBRAINZ_CONTACT  your email (or URL), added to the MusicBrainz User-Agent
    TAGDRIFTER_SECONDARY       a secondary library kept in step by hard links (secondary.py)
    TAGDRIFTER_SECONDARY_SYNC  true/false: allow writing the secondary (secondary_write.py)
    TAGDRIFTER_SECONDARY_MARKER  true/false: require a .tagdrifter-secondary file in it (default)
    TAGDRIFTER_SECONDARY_EXCLUDE_ISSUES  leave out albums with open issues of this severity or
                               worse: error, warning, info, or off (default)
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = (Path("config.toml"), Path("/config/config.toml"))


@dataclass(frozen=True)
class Settings:
    library_path: Path = Path("/music")
    database_path: Path = Path("data/tagdrifter.db")
    case_sensitive: bool = True
    audio_extensions: tuple[str, ...] = (".flac", ".mp3")
    sidecar_extensions: tuple[str, ...] = (".lrc", ".txt")
    # fnmatch patterns for file/folder names to skip (ZFS snapshots, NAS/OS metadata, ...)
    ignore_patterns: tuple[str, ...] = (".*", "@eaDir", "#recycle", "$RECYCLE.BIN", "Thumbs.db")
    workers: int = 8
    # When the web app scans automatically: a 5-field cron expression in the container's time
    # zone (TZ), or "off". Default: daily at 04:00.
    scan_schedule: str = "0 4 * * *"
    # Web UI login: an scrypt hash from `tagdrifter hash-password`; empty = no login.
    password_hash: str = field(default="", repr=False)
    # Signs session cookies. Empty = generate one and keep it next to the database.
    secret_key: str = field(default="", repr=False)
    # Allow renaming MBID-tagged albums to their expected paths from the web UI (needs the
    # library mounted read-write).
    fix_enabled: bool = False
    # The web UI's address as users reach it (e.g. behind a reverse proxy), for links in
    # notifications. Empty = notifications carry no link.
    public_url: str = ""
    # Notifications (notify.py): a Gotify server and application token; empty = off.
    gotify_url: str = ""
    gotify_token: str = field(default="", repr=False)
    notify_on: frozenset[str] = frozenset({"new_issues", "failures"})
    # Look up the library's releases on MusicBrainz (1 request/s, cached for weeks) so the
    # mb.* checks can compare tags with it. Off by default: it's the only network access
    # besides notifications.
    musicbrainz_enabled: bool = False
    # How MusicBrainz can reach whoever runs this instance (an email address or URL), added to
    # the User-Agent next to the project URL. Optional; never sent anywhere else.
    musicbrainz_contact: str = ""
    # Another MusicBrainz server (your own mirror, e.g. musicbrainz-docker): its web service
    # root, "http://host:5000" or ".../ws/2". Empty = musicbrainz.org.
    musicbrainz_api_root: str = ""
    # Seconds from each response to the next request; None = 1.5. Never below 1.5 for
    # musicbrainz.org (musicbrainz.client_for refuses it); a mirror of your own can use 0.
    musicbrainz_interval: float | None = None
    # A secondary library: the library's structure made of hard links to its files, minus
    # exclusions (secondary.py). On the same filesystem; None = none.
    secondary_path: Path | None = None
    # Allow syncing it: creating, moving and removing links there (needs a read-write mount).
    secondary_sync: bool = False
    # Require a `.tagdrifter-secondary` file in its root, against a mistyped path. Turn off when
    # whatever reads the secondary would show that file.
    secondary_marker: bool = True
    # Leave albums out of the secondary while they have an open issue of this severity or
    # worse ("error", "warning" or "info"); "off" = never. They're linked again at the first
    # sync after the issues are resolved.
    secondary_exclude_issues: str = "off"
    disabled_checks: frozenset[str] = frozenset()
    # Per-check options, keyed by check id: [checks."tags.required"] in TOML.
    check_options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def options_for(self, check_id: str) -> Mapping[str, Any]:
        return self.check_options.get(check_id, {})


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value!r}")


SEVERITY_THRESHOLDS = ("off", "error", "warning", "info")


def _parse_threshold(value: str | bool) -> str:
    """A severity threshold: error, warning, info or off (true = warning, false = off)."""
    if not isinstance(value, bool):
        lowered = str(value).strip().lower()
        if lowered in SEVERITY_THRESHOLDS:
            return lowered
        try:
            value = _parse_bool(lowered)
        except ValueError:
            raise ValueError(f"not a severity (error, warning, info or off): {value!r}") from None
    return "warning" if value else "off"


def _parse_events(value: str) -> frozenset[str]:
    """A comma-separated list; "none" (or "off") for no events."""
    events = {v.strip().lower() for v in value.split(",") if v.strip()}
    return frozenset() if events <= {"none", "off"} else frozenset(events)


def _from_toml(data: Mapping[str, Any], base: Settings) -> Settings:
    library = data.get("library", {})
    database = data.get("database", {})
    scan = data.get("scan", {})
    web = data.get("web", {})
    fix = data.get("fix", {})
    notify = data.get("notify", {})
    musicbrainz = data.get("musicbrainz", {})
    secondary = data.get("secondary", {})
    checks = dict(data.get("checks", {}))
    disabled = checks.pop("disabled", [])

    def ext_tuple(values: list[str]) -> tuple[str, ...]:
        return tuple(v.lower() if v.startswith(".") else f".{v.lower()}" for v in values)

    return replace(
        base,
        library_path=Path(library.get("path", base.library_path)),
        case_sensitive=bool(library.get("case_sensitive", base.case_sensitive)),
        audio_extensions=ext_tuple(library.get("audio_extensions", list(base.audio_extensions))),
        sidecar_extensions=ext_tuple(
            library.get("sidecar_extensions", list(base.sidecar_extensions))
        ),
        ignore_patterns=tuple(library.get("ignore", base.ignore_patterns)),
        database_path=Path(database.get("path", base.database_path)),
        workers=int(scan.get("workers", base.workers)),
        scan_schedule=str(scan.get("schedule", base.scan_schedule)),
        password_hash=str(web.get("password_hash", base.password_hash)),
        secret_key=str(web.get("secret_key", base.secret_key)),
        fix_enabled=bool(fix.get("enabled", base.fix_enabled)),
        public_url=str(web.get("public_url", base.public_url)),
        gotify_url=str(notify.get("gotify_url", base.gotify_url)),
        gotify_token=str(notify.get("gotify_token", base.gotify_token)),
        notify_on=frozenset(notify.get("on", base.notify_on)),
        musicbrainz_enabled=bool(musicbrainz.get("enabled", base.musicbrainz_enabled)),
        musicbrainz_contact=str(musicbrainz.get("contact", base.musicbrainz_contact)),
        musicbrainz_api_root=str(musicbrainz.get("api_root", base.musicbrainz_api_root)),
        musicbrainz_interval=(
            float(musicbrainz["interval"])
            if "interval" in musicbrainz
            else base.musicbrainz_interval
        ),
        secondary_path=Path(secondary["path"]) if secondary.get("path") else base.secondary_path,
        secondary_sync=bool(secondary.get("sync", base.secondary_sync)),
        secondary_marker=bool(secondary.get("marker", base.secondary_marker)),
        secondary_exclude_issues=_parse_threshold(
            secondary.get("exclude_issues", base.secondary_exclude_issues)
        ),
        disabled_checks=frozenset(disabled),
        check_options={k: v for k, v in checks.items() if isinstance(v, Mapping)},
    )


def load_settings(
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Settings:
    """Build Settings from (in increasing priority) defaults, a TOML file, env vars, overrides.

    `config_path` (or TAGDRIFTER_CONFIG) must exist if given; otherwise ./config.toml and
    /config/config.toml are tried. Overrides with value None are ignored.
    """
    env = os.environ if env is None else env
    settings = Settings()

    explicit = config_path or (
        Path(env["TAGDRIFTER_CONFIG"]) if "TAGDRIFTER_CONFIG" in env else None
    )
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"config file not found: {explicit}")
        candidates: tuple[Path, ...] = (explicit,)
    else:
        candidates = DEFAULT_CONFIG_PATHS
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as f:
                settings = _from_toml(tomllib.load(f), settings)
            break

    env_map = {
        "TAGDRIFTER_LIBRARY": ("library_path", Path),
        "TAGDRIFTER_DB": ("database_path", Path),
        "TAGDRIFTER_CASE_SENSITIVE": ("case_sensitive", _parse_bool),
        "TAGDRIFTER_WORKERS": ("workers", int),
        "TAGDRIFTER_SCHEDULE": ("scan_schedule", str),
        "TAGDRIFTER_PASSWORD_HASH": ("password_hash", str),
        "TAGDRIFTER_SECRET_KEY": ("secret_key", str),
        "TAGDRIFTER_FIX": ("fix_enabled", _parse_bool),
        "TAGDRIFTER_PUBLIC_URL": ("public_url", str),
        "TAGDRIFTER_GOTIFY_URL": ("gotify_url", str),
        "TAGDRIFTER_GOTIFY_TOKEN": ("gotify_token", str),
        "TAGDRIFTER_NOTIFY_ON": ("notify_on", _parse_events),
        "TAGDRIFTER_MUSICBRAINZ": ("musicbrainz_enabled", _parse_bool),
        "TAGDRIFTER_MUSICBRAINZ_CONTACT": ("musicbrainz_contact", str),
        "TAGDRIFTER_MUSICBRAINZ_API_ROOT": ("musicbrainz_api_root", str),
        "TAGDRIFTER_MUSICBRAINZ_INTERVAL": ("musicbrainz_interval", float),
        "TAGDRIFTER_SECONDARY": ("secondary_path", Path),
        "TAGDRIFTER_SECONDARY_SYNC": ("secondary_sync", _parse_bool),
        "TAGDRIFTER_SECONDARY_MARKER": ("secondary_marker", _parse_bool),
        "TAGDRIFTER_SECONDARY_EXCLUDE_ISSUES": ("secondary_exclude_issues", _parse_threshold),
    }
    for var, (attr, convert) in env_map.items():
        if env.get(var):
            settings = replace(settings, **{attr: convert(env[var])})

    return replace(settings, **{k: v for k, v in overrides.items() if v is not None})
