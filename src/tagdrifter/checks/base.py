"""Check plugin framework.

A check is a class with an `id`, a `scope` and a `run(ctx)` method that yields Issues.
Decorate it with `@register` and import its module from `tagdrifter/checks/__init__.py`;
the scanner, database and (later) web UI pick it up automatically.

    @register
    class NoLiveAlbums(Check):
        id = "custom.no_live_albums"
        scope = Scope.ALBUM
        description = "Flags albums whose title contains '(Live)'."

        def run(self, ctx: AlbumContext) -> Iterable[Issue]:
            if "(Live)" in ctx.rel_path.name:
                yield self.issue(ctx.rel_path, "Live album")

Per-check options come from config.toml, e.g. [checks."tags.required"], via `self.options`.
Each check should report at most one issue per subject (path); (check id, subject) is the
issue's identity across scans.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ..config import Settings
from ..core import TrackMeta, TrackTarget
from ..core.mbrelease import MbRelease


class Scope(StrEnum):
    TRACK = "track"
    ALBUM = "album"
    ARTIST = "artist"
    LIBRARY = "library"


class Severity(StrEnum):
    ERROR = "error"  # unreadable files, data that will break playback or organization
    WARNING = "warning"  # convention violations
    INFO = "info"  # worth knowing, not necessarily wrong


@dataclass(frozen=True)
class Issue:
    check_id: str
    severity: Severity
    subject: str  # library-relative POSIX path the issue is about
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    # Set by the scanner: whether the subject's track (or every track of its album) has valid
    # release and recording MBIDs. None when that doesn't apply (artist/library scope).
    mbid_tagged: bool | None = None
    # Set by the scanner: the album folder the issue belongs to (track and album scope), used
    # to group issues per album. None for artist/library scope.
    album: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.check_id, self.subject)


# --- Contexts handed to checks ----------------------------------------------------------


@dataclass(frozen=True)
class TrackContext:
    rel_path: PurePosixPath  # e.g. Pixies/Doolittle/Pixies_Doolittle_01_Debaser.flac
    meta: TrackMeta
    target: TrackTarget  # expected location, after release-collision resolution
    settings: Settings


@dataclass(frozen=True)
class UnreadableFile:
    rel_path: PurePosixPath
    error: str


@dataclass(frozen=True)
class AlbumContext:
    rel_path: PurePosixPath  # folder containing the tracks, e.g. Pixies/Doolittle
    tracks: tuple[TrackContext, ...]
    unreadable: tuple[UnreadableFile, ...]
    other_files: tuple[str, ...]  # names of non-audio files in the folder
    settings: Settings
    # Cached MusicBrainz lookups by release MBID (lower-case), shared by every album: the
    # release, or None when MusicBrainz has no such release. Releases not looked up yet (or
    # lookups off) are absent, and the mb.* checks skip them.
    releases: Mapping[str, MbRelease | None] = field(default_factory=dict)

    def release_of(self, meta: TrackMeta) -> MbRelease | None:
        return self.releases.get(meta.release_mbid.lower())

    @property
    def artist_folder(self) -> str:
        return self.rel_path.parts[0]

    @property
    def is_directly_under_artist(self) -> bool:
        return len(self.rel_path.parts) == 2


@dataclass(frozen=True)
class ArtistContext:
    rel_path: PurePosixPath  # e.g. Pixies
    albums: tuple[AlbumContext, ...]
    subfolders: tuple[str, ...]  # names of immediate child folders
    settings: Settings


@dataclass(frozen=True)
class LibraryContext:
    artist_folders: tuple[str, ...]
    loose_files: tuple[str, ...]  # audio files sitting directly in the library root
    settings: Settings


Context = TrackContext | AlbumContext | ArtistContext | LibraryContext


# --- Base class and registry ------------------------------------------------------------


class Check:
    id: ClassVar[str]
    scope: ClassVar[Scope]
    severity: ClassVar[Severity] = Severity.WARNING
    description: ClassVar[str] = ""

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options: Mapping[str, Any] = options or {}

    def run(self, ctx: Any) -> Iterable[Issue]:
        raise NotImplementedError

    def issue(
        self,
        subject: PurePosixPath | str,
        message: str,
        *,
        severity: Severity | None = None,
        **details: Any,
    ) -> Issue:
        return Issue(
            check_id=self.id,
            severity=severity or self.severity,
            subject=str(subject),
            message=message,
            details=details,
        )


REGISTRY: dict[str, type[Check]] = {}


def register(cls: type[Check]) -> type[Check]:
    if not getattr(cls, "id", None) or not isinstance(getattr(cls, "scope", None), Scope):
        raise TypeError(f"{cls.__name__} must define `id` and `scope`")
    if cls.id in REGISTRY and REGISTRY[cls.id] is not cls:
        raise ValueError(f"duplicate check id {cls.id!r}")
    REGISTRY[cls.id] = cls
    return cls


def build_checks(settings: Settings) -> list[Check]:
    """Instantiate every registered, enabled check with its configured options."""
    unknown = settings.disabled_checks - REGISTRY.keys()
    if unknown:
        raise ValueError(f"unknown check id(s) in [checks] disabled: {sorted(unknown)}")
    return [
        cls(settings.options_for(check_id))
        for check_id, cls in sorted(REGISTRY.items())
        if check_id not in settings.disabled_checks
    ]
