# Tag Drifter

A self-hosted service that watches a music library and reports files that have drifted from its
naming and tagging conventions: misnamed tracks and folders, albums filed under the wrong artist,
missing or malformed tags (Album Artist, MusicBrainz IDs, …), and names that differ only by
letter case.

The conventions are described in [docs/naming-conventions.md](docs/naming-conventions.md).

> **A personal project, shared as-is.** Tag Drifter is built around one library's conventions:
> FLAC files tagged with [Picard](https://picard.musicbrainz.org/) / MusicBrainz, on a
> case-sensitive ZFS dataset served over SMB. The naming rules are opinionated and not
> configurable yet. It only reports for now; it never changes your files. The web UI has no
> login, so keep it on your LAN. AI relied on heavily during development - use at your
> own peril.

## Status

| Step | What | State |
| --- | --- | --- |
| 0 | Repo, tooling, tests | ✅ |
| 1 | Pure core: sanitization, metadata, expected paths, album placement | ✅ |
| 2 | Incremental scanner, SQLite, pluggable checks, CLI | ✅ |
| 3 | Web UI (FastAPI + Jinja/HTMX) | ✅ |
| 4 | Scheduler, Docker image, TrueNAS compose deployment | ✅ |
| 5 | Fix actions, Navidrome rescan, notifications | — |
| 6 | MusicBrainz integration: checks against MusicBrainz, Picard-style retagging | — |

## Quick start (local)

Requires [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
copy config.example.toml config.toml     # then set [library] path, e.g. "M:/music"
uv run tagdrifter scan                   # first scan reads every file; later scans only changes
uv run tagdrifter issues                 # open issues, grouped by check
uv run tagdrifter issues --check tags.required --path "Wolfmother"
uv run tagdrifter issues --mbid no       # only issues on files without release/recording MBIDs
uv run tagdrifter checks                 # available checks and whether they're enabled
uv run tagdrifter scans                  # scan history
uv run tagdrifter serve                  # web UI on http://127.0.0.1:8080
```

`--library` and `--db` override the config file for a single run, e.g.
`uv run tagdrifter --library M:\music scan`. `scan --full` ignores the cache and re-reads every
file. Ctrl+C during a scan keeps the tags read so far, so the next scan picks up where it stopped.

## Web UI

`tagdrifter serve [--host 0.0.0.0] [--port 8080]` runs the UI:

- **Dashboard**: open issues by severity and by check, how many are on MBID-tagged (trusted)
  files, library size, the last scan, and the artists with the most issues.
- **Issues**: filter by check, severity, artist, MBID status, or a search over paths and
  messages. Proposed renames show the current and expected path with the differing characters
  highlighted. Filtered views have their own URL, so they can be bookmarked.
- **Scans**: scan history with files read and issues opened/resolved.
- **Scan now** (top bar, every page): runs a scan in the background with live progress; the page
  refreshes its counts when it finishes. Only one scan runs at a time.
- **Scheduled scans**: while the UI runs, it scans on `[scan] schedule` / `TAGDRIFTER_SCHEDULE`,
  a standard cron expression (default `0 4 * * *`, daily at 04:00 local time) or `off`. A
  scheduled scan that comes due while another is running is skipped. The footer shows the
  next one.

`GET /healthz` returns `{"status": "ok", ...}` for container health checks. The UI has no
login; keep it on your LAN.

## Docker / TrueNAS SCALE

The image is published to `ghcr.io/emilystap/tagdrifter` by GitHub Actions. It runs as UID/GID
568 (TrueNAS's `apps` user), serves on port 8080, reads the library at `/music` (mount it
`:ro`) and keeps its database in `/data`. [`compose.yaml`](compose.yaml) is ready for TrueNAS's
*Install via YAML* once you fill in your dataset paths; [docs/deployment.md](docs/deployment.md)
walks through permissions, the first scan and updates.

```bash
docker build -t tagdrifter .
docker run -p 8080:8080 -v /path/to/music:/music:ro -v tagdrifter-data:/data tagdrifter
```

## How a scan works

1. **Discover**: walk the library (skipping `ignore` patterns such as `.zfs` and `@eaDir`),
   recording each audio file's size and modification time.
2. **Read**: re-read tags only for new or changed files. FLAC tags are read by a built-in parser
   that skips embedded cover art. Results are cached in SQLite.
3. **Check**: run every enabled check over the whole library (cheap; no disk access).
4. **Save**: issues are identified by (check, path). New ones are recorded with the scan they
   first appeared in, and ones no longer reported are marked resolved.

## Checks

| Check | Scope | What it reports |
| --- | --- | --- |
| `path.track_location` | track | Wrong album folder and/or filename (including case-only differences when case-sensitive) |
| `tags.required` | track | Missing required tags (configurable list, with `a\|b` alternatives) |
| `tags.invalid_mbid` | track | MusicBrainz ID tags that aren't valid UUIDs |
| `album.placement` | album | Album under the wrong artist folder (v1's placement audit) |
| `album.inconsistent_tags` | album | Tracks in one folder disagree on album artist, album, release MBID, … |
| `file.unreadable` | album | Audio files whose tags can't be read |
| `path.orphan_sidecar` | album | `.lrc`/`.txt` with no matching audio file |
| `path.case_collision_*` | album/artist/library | Sibling names differing only by case (collide over SMB) |
| `path.loose_file` | library | Audio files directly in the library root |

### Adding a check

Create a module in `src/tagdrifter/checks/`, subclass `Check`, decorate it with `@register`,
and import the module in `checks/__init__.py`:

```python
@register
class NoLiveAlbums(Check):
    id = "custom.no_live_albums"
    scope = Scope.ALBUM            # TRACK, ALBUM, ARTIST or LIBRARY
    description = "Flags album folders with '(Live)' in the name."

    def run(self, ctx: AlbumContext):
        if "(Live)" in ctx.rel_path.name:
            yield self.issue(ctx.rel_path, "Live album")
```

Options from `[checks."custom.no_live_albums"]` in `config.toml` are available as
`self.options`. The scanner, database and CLI pick the check up automatically.

## Layout

```
src/tagdrifter/
  core/          Pure naming rules (sanitize, metadata, naming, placement, paths) + flac.py reader
  checks/        Check framework (base.py) and built-in checks
  config.py      Settings: defaults < config.toml < TAGDRIFTER_* env vars < CLI flags
  db.py          SQLite: file/tag cache, scan history, issue lifecycle
  scanner.py     Discover → read changed → check → save
  cli.py         `tagdrifter` command
  web/           FastAPI app (app.py), background ScanRunner, cron ScanScheduler, Jinja
                 templates, static/ (CSS, vendored htmx.min.js 2.0.4)
tests/           pytest suite; builds real FLAC files on the fly (no fixtures to commit)
docs/            Naming conventions, TrueNAS deployment
Dockerfile, compose.yaml, .github/workflows/ (CI: lint + tests; Image: build, smoke test, GHCR)
```

## Development

```powershell
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # format
```

Changes go through pull requests into `main`; the CI and Image workflows must pass, and `main`
publishes `ghcr.io/emilystap/tagdrifter:latest`.

## License

[GPL-3.0-or-later](LICENSE). Tag Drifter uses [mutagen](https://github.com/quodlibet/mutagen)
(GPL-2.0-or-later); the vendored [htmx](https://htmx.org/) is 0BSD, and the other dependencies
are MIT, BSD or Apache-2.0 licensed.
