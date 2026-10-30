# Tag Drifter: notes for Claude

Self-hosted service that monitors a FLAC music library for files that drift from a set of
naming and tagging conventions. Python backend, deployed as a single Docker container (TrueNAS
SCALE via Docker compose is the reference setup). Read `README.md` (usage, checks, layout) and
`docs/naming-conventions.md` (the rules) before changing behaviour. Personal setup details, if
present, are in the git-ignored `CLAUDE.local.md`.

## Target setup

- Libraries of thousands of `.flac` tracks with embedded cover art, tagged with Picard /
  MusicBrainz IDs, on a **case-sensitive** ZFS dataset shared over SMB and streamed by a
  server such as Navidrome.
- Development uses `uv`. The test suite builds FLAC files on the fly, so no library is needed.
- Work goes through feature branches and squash-merged PRs: `main` publishes
  `ghcr.io/emilystap/tagdrifter:latest`, which deployments pull.

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
- Lossless sanitization: `: ? < >` become lookalikes instead of `-` / removed, dots are kept
  except at the edges of a whole folder/file name (`protect_edges`), and only dots-only values
  get a word (`(period)`). A name should read back as its MusicBrainz tag.

## Status / roadmap

0. Repo, tooling ✅  1. Pure core ✅  2. Scanner + SQLite + checks + CLI ✅
3. Web UI ✅: `tagdrifter serve` → `web/app.py` (dashboard, filterable/paged issue list with
   highlighted expected paths, scan history, `/healthz`), `web/runner.py` (`ScanRunner`: one
   background scan at a time, progress polled by HTMX every 1s; a finished scan fires the
   `scan-finished` event and the page reloads `#page`). Server-rendered Jinja; partials are
   chosen by the `HX-Target` header. HTMX 2.0.4 is vendored in `web/static/` (no CDN).
   Per-track issues should be grouped by album (step 5): one album rename produces one issue per
   track, e.g. 42 rows for a 42-track album.
4. Deployment ✅ (written; verified by CI's image build + smoke test, not yet installed on
   TrueNAS): `web/scheduler.py` (`ScanScheduler`, APScheduler 3 `BackgroundScheduler`, started
   in the app lifespan; `scan_schedule` / `TAGDRIFTER_SCHEDULE`, default `0 4 * * *`).
   APScheduler 3 numbers weekdays Monday = 0, so `_cron_weekdays` translates standard cron
   weekdays; don't pass raw crontab strings to `CronTrigger.from_crontab`. `Dockerfile`
   (uv 0.13 build stage, python:3.13-slim, UID/GID 568, `/music` `/data` `/config`,
   HEALTHCHECK on `/healthz`), `compose.yaml`, `docs/deployment.md`, workflows `ci.yml` and
   `image.yml` (build, smoke test, push to `ghcr.io/emilystap/tagdrifter` from main and `v*`
   tags). Image changes are tested by CI (build + smoke test) unless Docker is available locally.
5. **Next**: fix actions (rename + move `.lrc`/`.txt` sidecars, prune empty dirs; v1's
   `organize_artist_directory` has the logic) with per-album grouping in the UI, Navidrome
   rescan trigger, notifications (ntfy/Apprise), non-FLAC formats via mutagen.
6. MusicBrainz integration: look up releases by `musicbrainz_albumid` (WS/2 JSON, 1 req/s,
   descriptive User-Agent), cache them in SQLite, and add checks comparing tags with MusicBrainz
   (album artist, album, track titles incl. case, numbering). Then Picard-style retagging with
   preview + approve, and lookups for releases missing MBIDs.

## Source of truth

MusicBrainz is the authority. Until step 6, tags on **MBID-tagged** tracks (valid
`musicbrainz_albumid` + `musicbrainz_trackid`, i.e. Picard-tagged) are trusted over paths,
including letter case, so path fixes rename to match those tags. Issues record this as
`mbid_tagged` (track: that track; album: all its tracks; artist/library: NULL). Files without
MBIDs are untrusted; don't auto-fix their paths from tags. See `docs/naming-conventions.md`.

## Commands

```powershell
uv sync
uv run pytest
uv run ruff check . ; uv run ruff format .
uv run tagdrifter scan            # uses config.toml (git-ignored; copy config.example.toml)
uv run tagdrifter issues --check path.track_location --limit 20
uv run tagdrifter serve           # web UI on http://127.0.0.1:8080
```

## Conventions

- Ruff (line length 100). The RUF001-003 ambiguous-unicode rules are disabled only for
  `core/sanitize.py` and `tests/`, where the filesystem-safe substitutes
  (∕ ꞉ ∗ ∣ ？ ‹ › ․ ″ “ ”) are intentional. Elsewhere, keep them out of literals (use `sanitize()` or constants such as
  `SLASH_REPLACEMENT`), so the rules still catch accidental lookalikes.
- Tests build real FLAC files on the fly (`tests/conftest.py::build_flac`, including
  cover-art blocks); never commit audio fixtures.
- `.gitattributes` forces LF line endings.
- When changing naming rules, update `docs/naming-conventions.md` and add a test for the
  example.
