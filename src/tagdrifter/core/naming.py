"""Expected library paths, computed purely from tags (no filesystem access).

Layout: <Artist Folder>/<Album Folder>/<Track Filename>
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from .metadata import TrackMeta
from .paths import path_key
from .sanitize import (
    is_various_artists_folder,
    normalize_artist_name,
    protect_edges,
    sanitize,
    strip_featured_artists,
    unprotect_edges,
)


@dataclass(frozen=True, slots=True)
class TrackTarget:
    """Where a track should live, relative to the library root."""

    artist_folder: str
    album_folder: str
    filename: str
    track_artist: str  # sanitized artist used in the filename

    @property
    def relative_path(self) -> PurePosixPath:
        return PurePosixPath(self.artist_folder, self.album_folder, self.filename)

    @property
    def album_relative_path(self) -> PurePosixPath:
        return PurePosixPath(self.artist_folder, self.album_folder)


@dataclass(frozen=True, slots=True)
class CollisionNotice:
    artist_folder: str
    original_album_folder: str
    new_album_folder: str


def primary_track_artist(meta: TrackMeta, artist_folder: str) -> str:
    """Artist name used in the track filename (README section 2)."""
    primary = (
        meta.artists[0] if meta.artists else strip_featured_artists(meta.effective_track_artist)
    )
    primary_sort = strip_featured_artists(meta.effective_track_artist_sort)

    norm_folder = normalize_artist_name(artist_folder)
    norm_primary = normalize_artist_name(primary)
    norm_sort = normalize_artist_name(primary_sort)

    # The track belongs to the folder's artist: use the folder's canonical spelling (with any
    # edge-dot substitutes undone, as the artist sits inside the filename).
    if not is_various_artists_folder(artist_folder) and norm_folder in (norm_primary, norm_sort):
        return unprotect_edges(artist_folder)
    # Stylization resolved by a non-inverted sort tag (e.g. Ke$ha -> Kesha) on splits/comps.
    if "," not in primary_sort and norm_primary == norm_sort and primary != primary_sort:
        return primary_sort
    return primary


def album_folder_name(meta: TrackMeta) -> str:
    album = sanitize(meta.effective_album)
    if meta.disambiguation:
        return protect_edges(f"{album} ({sanitize(meta.disambiguation)})")
    return protect_edges(album)


def track_filename(meta: TrackMeta, track_artist: str, extension: str = ".flac") -> str:
    album = sanitize(meta.effective_album)
    title = sanitize(meta.effective_title)
    if meta.is_multi_disc:
        index = f"{meta.disc_number:02d}-{meta.track_number:02d}"
    else:
        index = f"{meta.track_number:02d}"
    return protect_edges(f"{track_artist}_{album}_{index}_{title}{extension}")


def compute_target(meta: TrackMeta, artist_folder: str) -> TrackTarget:
    """Expected location of a track that sits under `artist_folder`.

    The artist folder itself is validated separately (see placement.py); here it is taken
    as given, exactly like the v1 organizer.
    """
    track_artist = sanitize(primary_track_artist(meta, artist_folder))
    extension = meta.path.suffix.lower() or ".flac"
    return TrackTarget(
        artist_folder=artist_folder,
        album_folder=album_folder_name(meta),
        filename=track_filename(meta, track_artist, extension),
        track_artist=track_artist,
    )


def resolve_release_collisions(
    items: Sequence[tuple[TrackMeta, TrackTarget]],
) -> tuple[list[TrackTarget], list[CollisionNotice]]:
    """Disambiguate distinct releases that map to the same album folder under one artist.

    A release is identified by its MusicBrainz album ID, else by its current folder. A track
    without an album ID inherits it from its folder when every tagged sibling agrees on one
    ID, so a few untagged tracks don't split an album in two (v1 did split them).
    Folder names are compared case-insensitively regardless of the library setting, so
    the service never proposes two folders that would collide over SMB.

    Suffix strategy, first that makes every release unique wins:
      1. (Year)   2. (Year, N tracks)   3. (Year, Catalog# or Country) with [mbid8] fallback

    Returns targets in the same order as `items`, plus one notice per renamed release.
    """
    folder_mbids: dict[str, set[str]] = {}
    for meta, _ in items:
        source = path_key(meta.path.parent, case_sensitive=False)
        mbids = folder_mbids.setdefault(source, set())
        if meta.release_mbid:
            mbids.add(meta.release_mbid)

    # folder key -> release key -> indices into items
    groups: dict[str, dict[str, list[int]]] = {}
    for idx, (meta, target) in enumerate(items):
        folder_key = path_key(target.album_relative_path, case_sensitive=False)
        source = path_key(meta.path.parent, case_sensitive=False)
        inherited = folder_mbids[source]
        release_key = meta.release_mbid or (
            next(iter(inherited)) if len(inherited) == 1 else source
        )
        groups.setdefault(folder_key, {}).setdefault(release_key, []).append(idx)

    targets = [t for _, t in items]
    notices: list[CollisionNotice] = []

    for releases in groups.values():
        if len(releases) <= 1:
            continue
        suffixes = _choose_suffixes(
            {key: [items[i][0] for i in idxs] for key, idxs in releases.items()}
        )
        for release_key, idxs in releases.items():
            original = targets[idxs[0]]
            base = unprotect_edges(original.album_folder)
            new_folder = protect_edges(f"{base} ({sanitize(suffixes[release_key])})")
            notices.append(
                CollisionNotice(original.artist_folder, original.album_folder, new_folder)
            )
            for i in idxs:
                targets[i] = replace(targets[i], album_folder=new_folder)

    return targets, notices


def _choose_suffixes(releases: dict[str, list[TrackMeta]]) -> dict[str, str]:
    meta_by_release = {}
    for key, metas in releases.items():
        meta_by_release[key] = {
            "year": next((m.release_year for m in metas if m.release_year), ""),
            "cat": next((m.catalog_number for m in metas if m.catalog_number), ""),
            "country": next((m.release_country for m in metas if m.release_country), ""),
            "count": len(metas),
            "mbid_short": (metas[0].release_mbid or "")[:8],
        }
    n = len(releases)

    # 1. Year alone
    years = [m["year"] for m in meta_by_release.values()]
    if all(years) and len(set(years)) == n:
        return {k: m["year"] for k, m in meta_by_release.items()}

    # 2. Year + track count
    def year_count(m: dict) -> str:
        return f"{m['year']}, {m['count']} tracks" if m["year"] else f"{m['count']} tracks"

    year_counts = {k: year_count(m) for k, m in meta_by_release.items()}
    if len(set(year_counts.values())) == n:
        return year_counts

    # 3. Year + catalog number / country, with short-MBID fallback
    chosen: dict[str, str] = {}
    used: set[str] = set()
    for k, m in meta_by_release.items():
        parts = [p for p in (m["year"], m["cat"] or m["country"]) if p]
        candidate = ", ".join(parts) if parts else m["mbid_short"]
        if not candidate or candidate in used:
            candidate = (
                f"{candidate} [{m['mbid_short']}]".strip()
                if m["mbid_short"]
                else f"{candidate} ({m['count']} tracks)".strip()
            )
        used.add(candidate)
        chosen[k] = candidate
    return chosen
