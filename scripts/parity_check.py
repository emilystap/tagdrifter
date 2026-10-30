"""Compare Tag Drifter's core against the v1 `library_scanner` on a real library (read-only).

Usage (from the tagdrifter repo, with v1's dependencies installed: mutagen, pandas, tqdm):

    uv run --with pandas --with tqdm python scripts/parity_check.py Y:\\music ^
        --v1 C:\\Users\\easst\\code\\music_lib_scanner_v1

Optionally limit to some artist folders with --artist "Wolfmother" --artist "Kesha".
Any difference is printed; a clean run means the port reproduces v1's target paths and
album-placement verdicts exactly.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections import defaultdict
from pathlib import Path

from tagdrifter.core import (
    MetadataReadError,
    audit_album_placement,
    compute_target,
    read_metadata,
    resolve_release_collisions,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library", type=Path)
    parser.add_argument("--v1", type=Path, required=True, help="path to music_lib_scanner_v1")
    parser.add_argument("--artist", action="append", default=[], help="limit to artist folder(s)")
    parser.add_argument("--max-diffs", type=int, default=50)
    args = parser.parse_args()

    sys.path.insert(0, str(args.v1))
    import library_scanner.scanner as v1

    library: Path = args.library
    albums: dict[Path, list[Path]] = defaultdict(list)
    for root, _, files in os.walk(library):
        root_path = Path(root)
        if root_path == library:
            continue
        artist_dir = library / root_path.relative_to(library).parts[0]
        if args.artist and artist_dir.name not in args.artist:
            continue
        for f in files:
            if f.lower().endswith(".flac"):
                albums[root_path].append(root_path / f)

    by_artist: dict[Path, list[Path]] = defaultdict(list)
    for album, files in albums.items():
        by_artist[library / album.relative_to(library).parts[0]].extend(sorted(files))

    path_diffs: list[str] = []
    checked = 0
    for artist_dir, files in sorted(by_artist.items()):
        # v1
        v1_infos = [v1.build_track_target(f, artist_dir) for f in files]
        v1.resolve_release_collisions(v1_infos, artist_dir)
        # v2
        items, ok_files = [], []
        for f in files:
            try:
                meta = read_metadata(f)
            except MetadataReadError:
                continue
            items.append((meta, compute_target(meta, artist_dir.name)))
            ok_files.append(f)
        targets, _ = resolve_release_collisions(items)
        new_by_file = dict(zip(ok_files, targets, strict=True))

        for f, info in zip(files, v1_infos, strict=True):
            new = new_by_file.get(f)
            if info is None or new is None:
                if (info is None) != (new is None):
                    path_diffs.append(
                        f"READ  {f}: v1={'ok' if info else 'error'} new={'ok' if new else 'error'}"
                    )
                continue
            checked += 1
            old_rel = info["target_path"].relative_to(artist_dir).as_posix()
            new_rel = f"{new.album_folder}/{new.filename}"
            if old_rel != new_rel:
                path_diffs.append(f"PATH  {artist_dir.name}\n   v1:  {old_rel}\n   new: {new_rel}")

    placement_diffs: list[str] = []
    for album, files in sorted(albums.items()):
        if album.parent == library:
            continue
        old = v1._audit_single_album((album, [f.name for f in sorted(files)]))
        metas = []
        for f in sorted(files):
            with contextlib.suppress(MetadataReadError):
                metas.append(read_metadata(f))
        # v1 compared folder names with os.path.normcase -> case-insensitive on Windows.
        new = audit_album_placement(album.parent.name, metas, case_sensitive=os.name != "nt")
        old_type = old["Issue Category"] if old else None
        new_type = new.issue_type.value if new else None
        if old_type != new_type:
            placement_diffs.append(
                f"PLACE {album.relative_to(library)}: v1={old_type} new={new_type}"
            )

    print(f"Tracks compared: {checked}   Albums compared: {len(albums)}")
    print(f"Path differences: {len(path_diffs)}   Placement differences: {len(placement_diffs)}")
    for line in (path_diffs + placement_diffs)[: args.max_diffs]:
        print(line)
    return 1 if path_diffs or placement_diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
