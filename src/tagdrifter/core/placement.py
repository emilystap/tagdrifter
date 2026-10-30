"""Is an album filed under the right artist folder? (v1 `audit_album_placement`)"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .metadata import TrackMeta
from .paths import PathMatch, compare_paths, path_key
from .sanitize import (
    VARIOUS_ARTISTS_FOLDER,
    extract_artist_set,
    is_various_artists_folder,
    is_various_artists_tag,
    normalize_artist_name,
    normalize_collaborators,
    sanitize_folder,
)


class PlacementIssueType(StrEnum):
    COMPILATION_IN_ARTIST_FOLDER = "Compilation inside Solo Artist Folder"
    ARTIST_ALBUM_IN_VA_FOLDER = "Solo/Collab Album inside Various Artists"
    COLLAB_UNDER_SINGLE_CONTRIBUTOR = "Collab Album placed under Single Contributor"
    SOLO_UNDER_COLLAB_FOLDER = "Solo Album placed under Collab Folder"
    ARTIST_NAME_MISMATCH = "Artist Name Mismatch"
    ARTIST_FOLDER_SANITIZATION = "Artist Folder Sanitization Mismatch"
    ARTIST_FOLDER_CASE = "Artist Folder Letter Case Mismatch"


@dataclass(frozen=True, slots=True)
class PlacementIssue:
    issue_type: PlacementIssueType
    current_artist_folder: str
    tagged_album_artist: str
    expected_artist_folder: str
    unique_track_artists: int


def album_artist_matches_folder(meta: TrackMeta, artist_folder: str) -> bool:
    """Track-level check from the v1 organizer: does this track's album artist belong here?"""
    album_artist = meta.effective_album_artist
    norm_folder = normalize_artist_name(artist_folder)
    return (
        normalize_collaborators(artist_folder)
        == extract_artist_set(meta.albumartists, album_artist)
        or norm_folder == normalize_artist_name(album_artist)
        or norm_folder == normalize_artist_name(meta.effective_album_artist_sort)
        or (is_various_artists_folder(artist_folder) and is_various_artists_tag(album_artist))
    )


def audit_album_placement(
    artist_folder: str,
    tracks: Sequence[TrackMeta],
    *,
    case_sensitive: bool = True,
) -> PlacementIssue | None:
    """Check one album (its tracks) against the artist folder it sits in.

    Uses the first track that has an album artist or artist tag, like v1. Returns None
    when the album is correctly placed or has no usable artist tags.
    """
    primary = next((t for t in tracks if t.albumartist or t.artist), None)
    if primary is None:
        return None

    tagged_aa = primary.albumartist or primary.artist
    tagged_sort = primary.albumartist_sort or tagged_aa

    norm_folder = normalize_artist_name(artist_folder)
    norm_tagged = normalize_artist_name(tagged_aa)
    norm_sort = normalize_artist_name(tagged_sort)

    sanitized_aa = sanitize_folder(tagged_aa)
    candidates = (
        sanitized_aa,
        sanitize_folder(tagged_sort),
        sanitize_folder(tagged_aa.replace("$", "s")),
    )
    acceptable_folders = {path_key(name, case_sensitive=case_sensitive) for name in candidates}
    is_sanitized_match = (
        path_key(artist_folder, case_sensitive=case_sensitive) in acceptable_folders
    )

    tagged_va = is_various_artists_tag(tagged_aa)
    folder_va = is_various_artists_folder(artist_folder)

    issue: PlacementIssueType | None = None
    expected_folder = VARIOUS_ARTISTS_FOLDER if tagged_va else sanitized_aa
    if tagged_va and not folder_va:
        issue = PlacementIssueType.COMPILATION_IN_ARTIST_FOLDER
    elif folder_va and not tagged_va:
        issue = PlacementIssueType.ARTIST_ALBUM_IN_VA_FOLDER
    elif not tagged_va and not folder_va:
        folder_collabs = normalize_collaborators(artist_folder)
        tagged_collabs = extract_artist_set(primary.albumartists, tagged_aa)
        is_artist_match = norm_folder in (norm_tagged, norm_sort) or (
            bool(folder_collabs) and folder_collabs == tagged_collabs
        )
        if not is_artist_match:
            if len(folder_collabs) == 1 and folder_collabs <= tagged_collabs:
                issue = PlacementIssueType.COLLAB_UNDER_SINGLE_CONTRIBUTOR
            elif len(tagged_collabs) == 1 and tagged_collabs <= folder_collabs:
                issue = PlacementIssueType.SOLO_UNDER_COLLAB_FOLDER
            else:
                issue = PlacementIssueType.ARTIST_NAME_MISMATCH
        elif not is_sanitized_match:
            # Only letter case differs from an accepted spelling: say so, and expect that one.
            case_match = next(
                (n for n in candidates if compare_paths(artist_folder, n) is PathMatch.CASE_ONLY),
                None,
            )
            if case_match:
                issue = PlacementIssueType.ARTIST_FOLDER_CASE
                expected_folder = case_match
            else:
                issue = PlacementIssueType.ARTIST_FOLDER_SANITIZATION

    if issue is None:
        return None

    return PlacementIssue(
        issue_type=issue,
        current_artist_folder=artist_folder,
        tagged_album_artist=tagged_aa,
        expected_artist_folder=expected_folder,
        unique_track_artists=len({t.artist for t in tracks if t.artist}),
    )
