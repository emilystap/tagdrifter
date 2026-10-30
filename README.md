# Tag Drifter

A self-hosted service that watches a music library and reports files that have drifted from its
naming and tagging conventions: misnamed tracks and folders, albums filed under the wrong artist,
and missing tags such as Album Artist or MusicBrainz IDs.

The conventions are described in [docs/naming-conventions.md](docs/naming-conventions.md).

## Status

| Step | What | State |
| --- | --- | --- |
| 0 | Repo, tooling, tests | ✅ |
| 1 | Pure core: sanitization, metadata, expected paths, album placement | ✅ |
| 2 | Scanner + SQLite + CLI, pluggable checks (path, required tags, MBID validity, case collisions) | ⏳ |
| 3 | Web UI (FastAPI + Jinja/HTMX) | — |
| 4 | Scheduler, Docker image, TrueNAS compose deployment | — |
| 5 | Fix actions, Navidrome rescan, notifications, MusicBrainz lookups | — |

## Layout

```
src/tagdrifter/
  core/              Pure rules. Only metadata.read_metadata touches files.
    sanitize.py      Filesystem-safe names, artist normalization
    metadata.py      TrackMeta (tags -> facts with v1 fallbacks), mutagen reader
    naming.py        Expected album folder / filename, release-collision suffixes
    placement.py     Is an album under the right artist folder?
    paths.py         Case- and Unicode-aware path comparison, case-collision detection
tests/               pytest suite (no audio files needed except two mutagen round-trip tests)
scripts/
  parity_check.py    Compare against music_lib_scanner_v1 on a real library (read-only)
docs/
```

## Development

Requires [uv](https://docs.astral.sh/uv/).

```powershell
uv sync                 # create .venv with runtime + dev dependencies
uv run pytest           # run tests
uv run ruff check .     # lint
uv run ruff format .    # format
```

### Check parity with v1 on the real library

Read-only. Prints any track whose expected path, or album whose placement verdict, differs from
the v1 scanner.

```powershell
uv run --with pandas --with tqdm python scripts/parity_check.py Y:\music `
    --v1 C:\Users\easst\code\music_lib_scanner_v1
```

Add `--artist "Wolfmother"` (repeatable) to limit the run.
