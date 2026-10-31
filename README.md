# Tag Drifter

A self-hosted service that watches a music library and reports files that have drifted from its
naming and tagging conventions: misnamed tracks and folders, albums filed under the wrong artist,
missing or malformed tags (Album Artist, MusicBrainz IDs, …), and names that differ only by
letter case.

The conventions are described in [docs/naming-conventions.md](docs/naming-conventions.md).

> **A personal project, shared as-is.** Tag Drifter is built around one library's conventions:
> FLAC files tagged with [Picard](https://picard.musicbrainz.org/) / MusicBrainz, on a
> case-sensitive ZFS dataset served over SMB. The naming rules are opinionated and not
> configurable yet. By default it only reports; renaming files is opt-in. The web UI's
> login is optional and basic, so keep it on your LAN. AI relied on heavily during
> development - use at your own peril.

## Status

| Step | What | State |
| --- | --- | --- |
| 0 | Repo, tooling, tests | ✅ |
| 1 | Pure core: sanitization, metadata, expected paths, album placement | ✅ |
| 2 | Incremental scanner, SQLite, pluggable checks, CLI | ✅ |
| 3 | Web UI (FastAPI + Jinja/HTMX) | ✅ |
| 4 | Scheduler, Docker image, TrueNAS compose deployment | ✅ |
| 5 | Album grouping ✅, login + CSRF protection ✅, rename preview + undo ✅, Gotify notifications ✅, bulk renames ✅, later a first-run password setup | ⏳ |
| 6 | MusicBrainz integration: local tag checks (MBIDs, artist counts, duplicates) ✅, a cached MusicBrainz lookup per release with checks for track versions, numbering and durations ✅, titles and artist credits ✅, other releases ranked by track-length fit ✅, refresh on demand ✅, then Picard-style retagging | ⏳ |
| 7 | Library browser: search any album, release page with tags and the MusicBrainz tracklist ✅, then embedded cover art | ⏳ |

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
uv run tagdrifter rename --dry-run       # proposed renames for every album (bulk; see below)
uv run tagdrifter refresh "CAKE/Prolonging the Magic"  # look its release up on MusicBrainz again
uv run tagdrifter serve                  # web UI on http://127.0.0.1:8080
```

`--library` and `--db` override the config file for a single run, e.g.
`uv run tagdrifter --library M:\music scan`. `scan --full` ignores the cache and re-reads every
file. Ctrl+C during a scan keeps the tags read so far, so the next scan picks up where it stopped.

## Web UI

`tagdrifter serve [--host 0.0.0.0] [--port 8080]` runs the UI:

- **Dashboard**: open issues by severity and by check, how many are on MBID-tagged (trusted)
  files, library size, the last scan, and the artists with the most issues.
- **Issues**: grouped by album folder by default, one card per album with its checks, the
  proposed folder rename and how many filenames change; expand a card for its individual
  issues, or switch *Show* to *Every issue* for a flat list. Filter by check, severity, artist,
  MBID status, or a search over paths and messages. The check and artist lists follow the
  other filters: with a check chosen, only artists with that issue are listed, with their
  counts (and the other way round). Proposed renames show the current and
  expected path with the differing characters highlighted. Filtered views have their own URL,
  so they can be bookmarked.
- **Library**: every album folder, searchable by artist, album, folder or release MBID, from
  the scan's cache (browsing never reads the disk or calls MusicBrainz). An album's page shows
  its tracks, the album-wide tags once (highlighting any that differ between tracks), each
  track's other tags, embedded pictures, open issues (with *Preview renames*), links to the
  release and release group on musicbrainz.org, and, once the release has been looked up, the
  MusicBrainz tracklist beside the files with title and length differences marked. Issue cards
  link to their album's page. *Compare with other releases* (MusicBrainz lookups on) fetches
  every release of the album's release group once, when you click it, and ranks them by how
  well their track lengths fit your files, with links to musicbrainz.org: handy for
  `mb.duration` issues, which usually mean the files are a different edition.
- **Scans**: scan history with files read and issues opened/resolved; the new count links
  to the issues first seen in that scan (`/issues?scan=N`).
- **Scan now** (top bar, every page): runs a scan in the background with live progress; the page
  refreshes its counts when it finishes. Only one scan runs at a time.
- **Scheduled scans**: while the UI runs, it scans on `[scan] schedule` / `TAGDRIFTER_SCHEDULE`,
  a standard cron expression (default `0 4 * * *`, daily at 04:00 local time) or `off`. A
  scheduled scan that comes due while another is running is skipped. The footer shows the
  next one.

- **Renames (opt-in)**: with `[fix] enabled = true` / `TAGDRIFTER_FIX=true` and the library
  writable, album cards get *Preview renames*: every folder, track and `.lrc`/`.txt` move, or
  why the album can't be renamed. Only albums whose tracks are all MBID-tagged and unchanged
  since the last scan qualify, and nothing is ever overwritten. Applied renames are listed on
  the *Renames* page with *Undo*. To rename many albums at once, tick their cards (or "all
  albums with renames matching these filters", e.g. one artist) and *Preview together*; each
  album is still checked and journaled on its own, so refused ones are skipped and Undo stays
  per album. `tagdrifter rename [--artist X] [--dry-run]` does the same from the command line.
  See [docs/deployment.md](docs/deployment.md#optional-renaming-files).
- **Notifications (optional)**: with a [Gotify](https://gotify.net) server
  (`TAGDRIFTER_GOTIFY_URL` + `TAGDRIFTER_GOTIFY_TOKEN`), a scan that finds new issues sends a
  summary by album, and a failed scan or a rename left half-done sends an alert. Quiet scans
  send nothing. Tapping one opens the matching page if `TAGDRIFTER_PUBLIC_URL` is set; the
  dashboard has *Send test notification*. See [docs/deployment.md](docs/deployment.md#optional-notifications).
- **MusicBrainz lookups (optional)**: with `TAGDRIFTER_MUSICBRAINZ=true`, each release in the
  library is looked up on MusicBrainz (at most one request a second; in the background a few
  at a time at random times of day, as MusicBrainz asks, or all at once after *Scan now*) and
  cached for 45-75 days; the `mb.*` checks compare tags, titles, artist credits and audio
  lengths with it. After editing a release on MusicBrainz, *Refresh from MusicBrainz* on the
  album's page (or `tagdrifter refresh <album folder or release MBID>`) looks it up again
  now and rescans; a release looked up in the last 5 minutes isn't asked for again.
  Releases no longer tagged on any file are dropped from the cache. The
  dashboard shows how many releases are cached. See
  [docs/deployment.md](docs/deployment.md#optional-musicbrainz-lookups).
- **Login (optional)**: set a password hash and every page needs a login; sessions last 30
  days. Generate the hash with `tagdrifter hash-password` and set it as
  `TAGDRIFTER_PASSWORD_HASH` or `[web] password_hash`. Without one, there's no login (rely on
  your network or your proxy's authentication).
- **CSRF protection** (always on): a POST is refused when the browser says it came from
  another site, so a web page elsewhere can't act on the UI through your browser.

`GET /healthz` returns `{"status": "ok", ...}` for container health checks and never needs a
login. Keep the UI on your LAN either way.

## Docker / TrueNAS SCALE

The image is published to `ghcr.io/emilystap/tagdrifter` by GitHub Actions. It runs as UID/GID
568 (TrueNAS's `apps` user), serves on port 8080, reads the library at `/music` (mount it
`:ro` unless you enable renaming) and keeps its database in `/data`. [`compose.yaml`](compose.yaml) is ready for TrueNAS's
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
| `tags.recording_mbid` | track | No recording MBID (`musicbrainz_trackid`), which ListenBrainz uses to match scrobbles (error) |
| `tags.mbids` | track | Missing release, release group, release track, artist or album artist MBIDs (standalone recordings included) |
| `tags.naming` | track | Missing tags the naming rules use (album artist, artist, album, title, track/disc number, date), with what the path falls back to |
| `tags.required` | track | Extra tags you require (configurable list with `a\|b` alternatives; empty by default) |
| `tags.invalid_mbid` | track | MusicBrainz ID tags that aren't valid UUIDs |
| `tags.artist_mbid_count` | track | `ARTISTS` names and artist MBIDs differ in number (several MBIDs need an `ARTISTS` tag) |
| `album.artist_mbid_count` | album | Same for `ALBUMARTISTS` and album artist MBIDs |
| `album.duplicate_tracks` | album | Two files are the same track: same release track MBID, or same release, disc and track number (error) |
| `album.cover_art` | album | Tracks without an embedded cover, or with pictures but none marked as the front cover; optionally covers smaller than `min_size` px. Read from the picture headers only, never the image |
| `album.missing_tracks` | album | Fewer tracks or discs than `TOTALTRACKS` / `TOTALDISCS` say (info; e.g. a single kept without its B-side) |
| `mb.release_unknown` | album | Release MBID MusicBrainz doesn't have (deleted) or has merged into another (error) |
| `mb.track_mismatch` | album | A track's release-track or recording MBID doesn't match the tagged release (a different version, or a stale tag) |
| `mb.numbering` | album | Track/disc numbers (per track) or track/disc totals (per album) differ from MusicBrainz |
| `mb.album` | album | Album title or release group MBID differs from MusicBrainz (Picard's English translations accepted) |
| `mb.track_title` | album | A track's title differs from MusicBrainz, letter case included (translations accepted) |
| `mb.artist_credit` | album | Artist names (`ARTIST`, `ARTISTS`) or MBIDs differ from the track's MusicBrainz credit, per track; album artists vs the release credit, once per album |
| `mb.duration` | album | A track's audio length is off from MusicBrainz by more than 3 s (configurable): live vs studio, edits, the wrong release |
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
    scope = Scope.ALBUM  # TRACK, ALBUM, ARTIST or LIBRARY
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
  core/          Pure naming rules (sanitize, metadata, naming, placement, paths), mbrelease.py
                 (parsed MusicBrainz releases) + flac.py reader
  checks/        Check framework (base.py) and built-in checks
  config.py      Settings: defaults < config.toml < TAGDRIFTER_* env vars < CLI flags
  renamer.py     Rename plans (preview), apply with a journal, undo
  library.py     Library browser: album list, search and release pages from the cache
  notify.py      Notifications (Gotify): new issues after a scan, failures
  musicbrainz.py MusicBrainz client (rate-limited) and release cache sync
  db.py          SQLite: file/tag cache, scan history, issue lifecycle
  scanner.py     Discover → read changed → check → save
  cli.py         `tagdrifter` command
  web/           FastAPI app (app.py), background ScanRunner, cron ScanScheduler,
                 MusicBrainzSync (mbsync.py), BulkRenamer (bulk.py), Jinja
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
publishes `ghcr.io/emilystap/tagdrifter:latest`. Changes that only touch `docs/`, `CLAUDE.md`
or `LICENSE` skip the tests and the image build (the checks report as skipped), so merging
them publishes no new image.

## License

[GPL-3.0-or-later](LICENSE). Tag Drifter uses [mutagen](https://github.com/quodlibet/mutagen)
(GPL-2.0-or-later); the vendored [htmx](https://htmx.org/) is 0BSD, and the other dependencies
are MIT, BSD or Apache-2.0 licensed.
