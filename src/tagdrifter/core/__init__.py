"""Pure naming/tagging rules. Only `metadata.read_metadata` touches the filesystem."""

from .metadata import MetadataReadError, TrackMeta, parse_index, read_metadata
from .naming import CollisionNotice, TrackTarget, compute_target, resolve_release_collisions
from .paths import PathMatch, compare_paths, find_case_collisions, is_mismatch, path_key
from .placement import (
    PlacementIssue,
    PlacementIssueType,
    album_artist_matches_folder,
    audit_album_placement,
)
from .sanitize import (
    SLASH_REPLACEMENT,
    normalize_artist_name,
    normalize_collaborators,
    sanitize,
    strip_featured_artists,
)

__all__ = [
    "SLASH_REPLACEMENT",
    "CollisionNotice",
    "MetadataReadError",
    "PathMatch",
    "PlacementIssue",
    "PlacementIssueType",
    "TrackMeta",
    "TrackTarget",
    "album_artist_matches_folder",
    "audit_album_placement",
    "compare_paths",
    "compute_target",
    "find_case_collisions",
    "is_mismatch",
    "normalize_artist_name",
    "normalize_collaborators",
    "parse_index",
    "path_key",
    "read_metadata",
    "resolve_release_collisions",
    "sanitize",
    "strip_featured_artists",
]
