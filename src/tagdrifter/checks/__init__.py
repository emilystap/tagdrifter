"""Checks. Importing this package registers every built-in check.

To add a check: write a `Check` subclass decorated with `@register` (see base.py) in a module
here, and import that module below.
"""

from . import builtin_musicbrainz, builtin_paths, builtin_tags  # noqa: F401  (registration)
from .base import (
    REGISTRY,
    AlbumContext,
    ArtistContext,
    Check,
    Issue,
    LibraryContext,
    Scope,
    Severity,
    TrackContext,
    UnreadableFile,
    build_checks,
    register,
)

__all__ = [
    "REGISTRY",
    "AlbumContext",
    "ArtistContext",
    "Check",
    "Issue",
    "LibraryContext",
    "Scope",
    "Severity",
    "TrackContext",
    "UnreadableFile",
    "build_checks",
    "register",
]
