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
    audio_extensions: tuple[str, ...] = (".flac",)
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


def _from_toml(data: Mapping[str, Any], base: Settings) -> Settings:
    library = data.get("library", {})
    database = data.get("database", {})
    scan = data.get("scan", {})
    web = data.get("web", {})
    fix = data.get("fix", {})
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
    }
    for var, (attr, convert) in env_map.items():
        if env.get(var):
            settings = replace(settings, **{attr: convert(env[var])})

    return replace(settings, **{k: v for k, v in overrides.items() if v is not None})
