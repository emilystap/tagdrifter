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
- **Read-only by default**: deployments mount the library `:ro`. Renames are opt-in
  (`TAGDRIFTER_FIX` plus a read-write mount), always previewed and approved in the UI,
  journaled for undo, and run as TrueNAS's `apps` user (568).
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
4. Deployment ✅ (CI builds + smoke-tests the image; running on the reference TrueNAS setup
   behind an HTTPS reverse proxy, so templates use root-relative URLs, never `url_for()`
   absolute ones): `web/scheduler.py` (`ScanScheduler`, APScheduler 3 `BackgroundScheduler`,
   started in the app lifespan; `scan_schedule` / `TAGDRIFTER_SCHEDULE`, default `0 4 * * *`).
   APScheduler 3 numbers weekdays Monday = 0, so `_cron_weekdays` translates standard cron
   weekdays; don't pass raw crontab strings to `CronTrigger.from_crontab`. `Dockerfile`
   (uv 0.13 build stage, python:3.13-slim, UID/GID 568, `/music` `/data` `/config`,
   HEALTHCHECK on `/healthz`), `compose.yaml`, `docs/deployment.md`, workflows `ci.yml` and
   `image.yml` (build, smoke test, push to `ghcr.io/emilystap/tagdrifter` from main and `v*`
   tags). Image changes are tested by CI (build + smoke test) unless Docker is available locally.
   Both workflows start with a `changes` job (`.github/scripts/code-changed.sh`): if only
   `docs/**`, `CLAUDE.md` or `LICENSE` changed, `test`/`build` are skipped via `if:`, which
   still satisfies the required checks. Keep that list docs-only; everything else counts as
   code. Never use `paths-ignore` on these workflows: the required checks would never report.
5. Fix actions, in PRs:
   - 5a ✅ Issues grouped by album: the scanner stores each issue's album folder (`album`
     column, migration 3; NULL for artist/library scope, which group by subject).
     `Store.open_issue_groups` / `open_issues_in_groups`; `/issues?view=album` (default) or
     `view=list`. `format.album_changes` derives the folder rename shown on each card.
   - 5b ✅ Login + CSRF (`web/auth.py`, stdlib only): `password_hash` /
     `TAGDRIFTER_PASSWORD_HASH` (scrypt, from `tagdrifter hash-password`; empty = no login),
     HMAC-signed 30-day session cookie keyed by `secret_key` or `<data dir>/secret_key`.
     The `guard` middleware refuses cross-site POSTs (`Sec-Fetch-Site`, else `Origin` vs
     `Host`) and, with a password, sends unauthenticated requests to `/login` (HTMX ones via
     `HX-Redirect`); `/login`, `/healthz` and `/static/` are public. Every new POST route is
     covered automatically. The login form is parsed by hand (no python-multipart).
   - 5c ✅ Rename preview + apply + undo per album (`renamer.py`, off unless `fix_enabled` /
     `[fix] enabled` / `TAGDRIFTER_FIX`; needs a read-write mount). `plan_album` builds moves
     from the album's open `path.track_location` issues and refuses (with reasons) unless every
     audio file is readable, MBID-tagged and unchanged since the scan (size + mtime vs the
     `files` cache), targets stay in the artist folder, nothing would be overwritten and no two
     targets collide case-insensitively. One new folder for all tracks that doesn't exist yet
     → the folder itself is renamed (artwork travels); otherwise tracks move one by one and an
     emptied folder is removed. Sidecars follow their track. Case-only renames go through a
     hidden temp name. `apply_plan` re-plans and requires the preview's `fingerprint`, journals
     each move (`rename_batches` / `rename_moves`, migration 4) and rolls back on failure;
     `undo_batch` reverses pending moves newest first. Routes: `GET /rename/preview`,
     `POST /rename/apply` (fires `scan-started`, which the scan widget listens for),
     `/renames` (journal + Undo). `ScanRunner.paused()` keeps scans out while files move.
   - 5d dropped: a Navidrome rescan trigger after renames. Navidrome's own file watcher
     already rescans (and purges missing files) on change, so it would duplicate that.
   - 5e ✅ Notifications (`notify.py`, stdlib only): Gotify (`gotify_url` + `gotify_token`,
     `[notify]` / `TAGDRIFTER_GOTIFY_*`; token in the `X-Gotify-Key` header). Events
     (`notify_on`): `new_issues` (scan found issues with `first_seen_scan` = that scan; Markdown
     summary by album, links to `/issues?scan=N`) and `failures` (scan failed; a rename or undo
     raised `RenameError(partial=True)`). `ScanRunner` and the rename routes call
     `Notifications`, which sends from a daemon thread and only logs errors. `public_url` adds
     click-through links. Dashboard panel + `POST /notify/test`. Another service = a class
     with `describe()` and `send(Notification)`.
   - **Next**: step 6b-2 below.
   - Later: ntfy/webhook notifiers.
   - Later: rename across artist folders for `album.placement` issues; a CLI for renames.
   - Later: non-FLAC formats via mutagen.
   - Later (owner's request): a conventional first-run login. On first launch with no
     password set, every page leads to a "Set a password" form; the scrypt hash is stored in
     SQLite (a small `app_settings` table) instead of env/config, with a *Change password* page
     and a CLI reset (`tagdrifter reset-password`). Single user: a username adds little for
     this tool. `TAGDRIFTER_AUTH=off` / `[web] auth = false` turns login off entirely (for
     setups behind proxy auth); `password_hash` from env/config still works and takes
     precedence (the UI can't change it then). Open points: guard the setup window, since
     whoever reaches the UI first could set the password (e.g. require a one-time setup code
     printed to the container log, or only allow setup from the LAN); and this changes the
     default for existing deployments from "no login" to "set a password" on upgrade, which
     the release notes must say.
6. MusicBrainz integration, in PRs. Note Picard's naming: `MUSICBRAINZ_TRACKID` holds the
   **recording** MBID (what ListenBrainz scrobbles match on, via Navidrome);
   `MUSICBRAINZ_RELEASETRACKID` is the track MBID. Messages say "recording MBID
   (`musicbrainz_trackid`)".
   - 6a ✅ Local tag checks (`checks/builtin_tags.py`, no network). `tags.required` is split
     so each concern has its own row, filter and severity; its default list is now empty
     (extra user-chosen tags only):
     - `tags.recording_mbid` (error): `musicbrainz_trackid` missing; scrobbles fall back to
       fuzzy artist + title matching.
     - `tags.mbids` (warning): release, release group, release track, artist or album artist
       MBID missing. Not `musicbrainz_workid` (track relationships are off in the owner's
       Picard, so it's never written). Standalone recordings (a recording MBID but no release
       MBID) are flagged too, deliberately: the owner wants them listed.
     - `tags.naming` (warning): tags the naming rules read (`NAMING_TAGS`: album artist,
       artist, album, title, track number, date; disc number on multi-disc releases), each
       with what the path falls back to.
     - `tags.artist_mbid_count` (track) / `album.artist_mbid_count` (album, reported once per
       folder): number of `ARTISTS` / `ALBUMARTISTS` values ≠ number of artist / album artist
       MBIDs. Without the names tag, one MBID is fine and several are flagged (about 3,100 of
       the owner's files predate `ALBUMARTISTS`). Never guess by splitting `ARTIST` on
       "feat." / "&".
     - `album.duplicate_tracks` (error): same release-track MBID, or same release + disc +
       track number.
     - `album.missing_tracks` (info): fewer tracks per disc than `TOTALTRACKS` / `TRACKTOTAL`
       (or "n/N"), or missing discs. Disc gaps only when the numbering fits `TOTALDISCS`
       (Picard can number discs 2-3 of 2 when it skips a medium).
     - `album.cover_art` (warning; added after 6b): tracks with no embedded picture, or
       none of type 3 (front cover); `min_size` (px, off by default) flags small front
       covers. `core/flac.py` reads only each PICTURE block's header (type, MIME, width,
       height) and seeks past the image; stored as the `~pictures` pseudo-tag
       (`"3 image/jpeg 1200x1200"` per picture, empty = none). Pseudo-tags the reader records
       are `DERIVED_KEYS`; a cached file missing one is re-read once. Files read before
       `~pictures` existed are skipped, not flagged. Different covers within an album
       aren't compared (that needs the image data).
     - First run on the owner's library: 25 `album.missing_tracks` (mostly singles kept
       without B-sides), 5 `album.artist_mbid_count` (cast recordings and collabs), nothing
       else new.
   - 6b ✅ MusicBrainz cache + structural checks. Off unless `musicbrainz_enabled` /
     `[musicbrainz] enabled` / `TAGDRIFTER_MUSICBRAINZ`. Keep the strain on the API minimal
     (MusicBrainz is a small non-profit; the owner asked for this explicitly):
     - `musicbrainz.py`: `MusicBrainzClient` makes one lookup per release,
       `/ws/2/release/<mbid>?inc=recordings+artist-credits+release-groups+aliases&fmt=json`,
       which already carries everything 6b-2 needs (release, recording and artist aliases),
       so adding checks never needs a refetch. Process-wide `RateLimiter` (≥1.1 s between
       requests), gzip; User-Agent `TagDrifter/<ver> ( <contact> ; <repo URL> )`, where the
       optional `musicbrainz_contact` / `TAGDRIFTER_MUSICBRAINZ_CONTACT` is the operator's
       address (validated: one line, no parentheses). A 503 backs off 15 s and 60 s, or the
       `Retry-After` if longer; a Retry-After over 5 min ends the run at once. Then the run
       ends (`MusicBrainzError.retry_after`). `sync_releases` fetches releases missing from the cache first, then
       refreshes due (`refresh_after`: random 45-75 days; not found: 30-60), stopping at the
       first error and keeping what it fetched.
     - Cache: `mb_releases` (migration 5), the raw response zlib-compressed (3-11 KB each),
       NULL data = not found. `Store.mb_releases_due` / `mb_releases` / `mb_cache_counts`.
     - `web/mbsync.py` `MusicBrainzSync`. MusicBrainz asks background jobs not to wake at a
       set time of day but to spread calls through it, so lookups never follow the scan
       schedule: a background trickle (started in the app lifespan) wakes at random
       intervals (`TRICKLE_INTERVAL` 30-150 min, drawn each time) and looks up
       `TRICKLE_BATCH` (5) due releases. `ScanRunner` calls `after_scan` when a scan
       finishes: after a scan someone started, everything due is looked up at once; after a
       *scheduled* scan, nothing (left to the trickle). One run at a time (`_claim`). A run
       that stopped on an error pauses
       lookups for an hour, or the Retry-After if longer (`paused_until`, in memory), so
       repeated scans during an outage don't keep poking the API. If anything changed it
       rescans so the issues show up. Dashboard panel (`_mb_status.html`, polls while running).
     - Checks only read the cache: the scanner parses it into `core/mbrelease.py`
       `MbRelease` objects (`AlbumContext.releases`, keyed by lower-case release MBID; a
       release not looked up yet is absent and skipped). `checks/builtin_musicbrainz.py`,
       all album scope, reporting per track where it's a track's problem:
       `mb.release_unknown` (404, or the response id differs = merged), `mb.track_mismatch`
       (release-track MBID not on the release, else recording MBID differs),
       `mb.numbering` (track/disc number per track; total tracks/discs once per album),
       `mb.duration` (file vs MusicBrainz track length, `tolerance_seconds` default 3).
       Picard's mapping: tracknumber = track `position` (not `number`, which can be "A1"),
       discnumber = medium `position` (Brat's media are 2 and 3), totaldiscs = number of media.
     - Audio length: `core/flac.py` reads STREAMINFO; `read_tags` stores it as the `~length`
       pseudo-tag (ms; "~" can't start a Vorbis comment name), `TrackMeta.length_ms`. Files
       cached without it are re-read once (about a minute for the owner's 7,179 files).
     - Tests never call the API: an autouse fixture in `tests/conftest.py` makes real
       requests fail, and `tests/fixtures/musicbrainz/` holds two recorded lookups. Verify
       against the live API sparingly (a handful of releases via `sync_releases(limit=...)`).
     - First sample (20 releases): Songs for the Deaf has 16 tracks in MusicBrainz but 17 in
       the tags; two CAKE tracks are 7 s and 26 s shorter than MusicBrainz.
   - **6b-2 next**: text checks against the cache (no new requests needed):
     - `mb.album`: album title, album artist credit, release group.
     - `mb.track_title`: title, including letter case.
     - `mb.artist_credit`: artist MBIDs (set and order) and names vs the track's artist credit,
       both directions; album artists vs the release credit.
     - Comparison rules follow the owner's Picard 3 settings (personal tool; don't generalize
       yet): no ASCII punctuation conversion (compare exactly, case-sensitive); artist names
       standardized for *variations only*, and artist, album and track names translated to
       the `en` locale when available. Picard (source, master) translates from **aliases**:
       artist aliases, the release's own aliases (album) and recording aliases (titles),
       taking the primary alias whose locale matches (`en_*` then `en`), with "Artist name"
       / "Recording name" types preferred; it doesn't follow pseudo-release relationships.
       So: artist names match the credited name, the artist's name or its primary `en`
       alias (新しい学校のリーダーズ → "ATARASHII GAKKO!"); titles match the MusicBrainz track
       / release title or the primary `en` alias. Maenarawanai's release and track titles
       are already Latin in MusicBrainz (e.g. track "sekigae Gattuso", recording
       "席替ガットゥーゾ"), so track titles compare with the *track* title first.
   - 6c: Picard-style retagging with preview + approve; lookups for releases missing MBIDs.

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
- Tests never call the MusicBrainz API (blocked in `tests/conftest.py`); use the recorded
  fixtures in `tests/fixtures/musicbrainz/`.
- Tests build real FLAC files on the fly (`tests/conftest.py::build_flac`, including
  cover-art blocks); never commit audio fixtures.
- `.gitattributes` forces LF line endings.
- When changing naming rules, update `docs/naming-conventions.md` and add a test for the
  example.
