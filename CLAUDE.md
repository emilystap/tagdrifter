# Tag Drifter: notes for Claude

Self-hosted service that monitors a FLAC music library for files that drift from the owner's
naming and tagging conventions. Backend is Python, by the owner's preference. It will be
deployed as a single Docker container on TrueNAS SCALE via Docker compose. Read `README.md`
(usage, checks, layout) and `docs/naming-conventions.md` (the rules) before changing behaviour.

## Owner's setup

- Library: ~7,000 tracks, almost all `.flac`, all with **embedded cover art**. Stored on a
  TrueNAS dataset that is **case-sensitive**, shared over SMB, streamed with Navidrome. Sources
  are mainly Bandcamp and Qobuz, tagged with MusicBrainz IDs.
- Dev machine: Windows laptop with the library mounted at `Y:\music` over SMB. Use PowerShell
  syntax in instructions. `uv` manages the environment.
- The predecessor, `../music_lib_scanner_v1` (notebook + `library_scanner/scanner.py`), is
  reference only. Its logic has been ported into `src/tagdrifter/core/`. Don't import it.

## Architecture decisions (agreed)

- **One container, one process**: FastAPI + Jinja2/HTMX UI (no JS build step), SQLite on a
  mounted volume, APScheduler for periodic scans plus a "scan now" button.
- **Core stays pure**: `core/` never touches the filesystem except `metadata.read_tags` /
  `core/flac.py`. Everything else operates on `TrackMeta` and is unit-testable without audio.
- **Checks are plugins**: subclass `Check`, set `id`/`scope`, decorate with `@register`, import
  in `checks/__init__.py`. Issue identity is `(check_id, subject)`; at most one issue per
  subject per check. The scanner, DB and CLI pick checks up automatically, and the UI should too.
- **Case sensitivity is a setting** (`case_sensitive`, default true to match the dataset).
  Release-collision grouping always compares case-insensitively so it never proposes folder
  names that would collide over SMB.
- **Incremental scans**: re-read tags only when size or mtime changed; checks always run over
  the whole library (they're cheap and some depend on siblings).
- **Read-only first**: the first deployment mounts the library `:ro`. Fix/rename actions come
  later, with preview + approve in the UI, a read-write mount, and PUID/PGID matching TrueNAS's
  `apps` user (568).
- **Config precedence**: defaults < `config.toml` < `TAGDRIFTER_*` env vars < CLI flags.
  The config file is TOML (stdlib `tomllib`).

## Deliberate differences from v1 (keep them)

- Empty tag values are skipped rather than treated as authoritative.
- Unicode NFC normalization before any comparison.
- Tracks missing `musicbrainz_albumid` inherit their folder's single album ID, so they don't
  split an album into `(Year, N tracks)` releases.
- Case-only name collisions are detected (`path.case_collision_*`).

## Status / roadmap

0. Repo, tooling ✅  1. Pure core ✅  2. Scanner + SQLite + checks + CLI ✅
3. **Next: web UI**: FastAPI app with a dashboard (issue counts per check, last scan), a
   filterable issue list (check / artist / severity / path), scan history, and a "Scan now"
   button that runs `run_scan` in a background thread and reports progress via `ScanProgress`.
   Reuse `Store` queries and add more as needed.
4. APScheduler, Dockerfile (python:3.13-slim, non-root), `compose.yaml` (library `:ro`, data
   volume, env vars), healthcheck, TrueNAS "Install via YAML" notes, GitHub Actions → GHCR
   once a remote exists.
5. Later: fix actions (rename + move `.lrc`/`.txt` sidecars, prune empty dirs; v1's
   `organize_artist_directory` has the logic), Navidrome rescan trigger, notifications
   (ntfy/Apprise), MusicBrainz lookups for missing MBIDs, non-FLAC formats via mutagen.

## Commands

```powershell
uv sync
uv run pytest
uv run ruff check . ; uv run ruff format .
uv run tagdrifter scan            # uses config.toml (git-ignored; copy config.example.toml)
uv run tagdrifter issues --check path.track_location --limit 20
```

## Conventions

- Ruff (line length 100). The RUF001-003 ambiguous-unicode rules are disabled because the
  filesystem-safe substitutes (∕ ∗ ∣ ″ “ ”) are intentional.
- Tests build real FLAC files on the fly (`tests/conftest.py::build_flac`, including
  cover-art blocks); never commit audio fixtures.
- `.gitattributes` forces LF line endings.
- When changing naming rules, update `docs/naming-conventions.md` and add a test for the
  example.
