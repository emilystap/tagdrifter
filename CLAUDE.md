# Tag Drifter: notes for Claude

Self-hosted service that monitors a FLAC (and MP3) music library for files that drift from a set of
naming and tagging conventions. Python backend, deployed as a single Docker container (Docker
compose). Read `README.md` (usage, checks, layout) and
`docs/naming-conventions.md` (the rules) before changing behaviour. Personal setup details, if
present, are in the git-ignored `CLAUDE.local.md`.

## Target setup

- Libraries of thousands of `.flac` tracks with embedded cover art, tagged with Picard /
  MusicBrainz IDs, on a **case-sensitive** filesystem that may also be shared over SMB
  (where names differing only by case collide) and streamed by a music server.
- Development uses `uv`. The test suite builds FLAC files on the fly, so no library is needed.
- Work goes through feature branches and squash-merged PRs: `main` publishes
  `ghcr.io/emilystap/tagdrifter:latest`, which deployments pull.

## Architecture decisions (agreed)

- **One container, one process**: FastAPI + Jinja2/HTMX UI (no JS build step), SQLite on a
  mounted volume, APScheduler for periodic scans plus a "scan now" button.
- **Core stays pure**: `core/` never touches the filesystem except `metadata.read_tags` /
  `core/flac.py` (`core/id3.py` only renames ID3 frames mutagen has read). Everything else operates on `TrackMeta` and is unit-testable without audio.
- **Checks are plugins**: subclass `Check`, set `id`/`scope`, decorate with `@register`, import
  in `checks/__init__.py`. Issue identity is `(check_id, subject)`; at most one issue per
  subject per check. The scanner, DB and CLI pick checks up automatically, and the UI should too.
- **Case sensitivity is a setting** (`case_sensitive`, default true, for case-sensitive
  filesystems). Release-collision grouping always compares case-insensitively so it never
  proposes folder names that would collide over SMB.
- **Incremental scans**: re-read tags only when size or mtime changed; checks always run over
  the whole library (they're cheap and some depend on siblings).
- **Read-only by default**: deployments mount the library `:ro`. Renames are opt-in
  (`TAGDRIFTER_FIX` plus a read-write mount), always previewed and approved in the UI,
  journaled for undo, and run as the container's user (UID/GID 568 by default).
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
   highlighted expected paths, scan history, `/healthz`; the Check and Artist dropdowns
   count what the other filters leave and are swapped out of band with each results update,
   `_issue_facets.html`), `web/runner.py` (`ScanRunner`: one
   background scan at a time, progress polled by HTMX every 1s; a finished scan fires the
   `scan-finished` event and the page reloads `#page`). Each scan records why it ran
   (`scans.reason`, migration 7, `db.SCAN_REASONS`: manual, schedule, cli, rename, undo,
   musicbrainz, triage; pass `reason=` to `ScanRunner.start` / `run_scan` for a new trigger). The
   Scans page (`_scan_results.html`, 50 per page) hides follow-up rescans that completed with
   no new issues unless `?all=1`; older scans that started within a minute of a rename
   finishing were backfilled as `rename`. Old scans aren't deleted (rows are tiny, and
   issues refer to scan ids). Server-rendered Jinja; partials are
   chosen by the `HX-Target` header. HTMX 2.0.4 is vendored in `web/static/` (no CDN).
4. Deployment ✅ (CI builds + smoke-tests the image; deployments may sit behind an HTTPS
   reverse proxy, so templates use root-relative URLs, never `url_for()` absolute ones): `web/scheduler.py` (`ScanScheduler`, APScheduler 3 `BackgroundScheduler`,
   started in the app lifespan; `scan_schedule` / `TAGDRIFTER_SCHEDULE`, default `0 4 * * *`).
   APScheduler 3 numbers weekdays Monday = 0, so `_cron_weekdays` translates standard cron
   weekdays; don't pass raw crontab strings to `CronTrigger.from_crontab`. `Dockerfile`
   (uv 0.13 build stage, python:3.13-slim, both pinned by digest, UID/GID 568, `/music`
   `/data` `/config`,
   HEALTHCHECK on `/healthz`), `compose.yaml`, `docs/deployment.md`, workflows `ci.yml` and
   `image.yml` (build, smoke test, then a `publish` job, the only one with `packages: write`,
   pushes to `ghcr.io/emilystap/tagdrifter` from main and `v*`
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
   - 5d dropped: a music server rescan trigger after renames. Servers such as Navidrome
     watch their files and rescan (and purge missing files) on change, so it would duplicate
     that.
   - 5e ✅ Notifications (`notify.py`, stdlib only): Gotify (`gotify_url` + `gotify_token`,
     `[notify]` / `TAGDRIFTER_GOTIFY_*`; token in the `X-Gotify-Key` header). Events
     (`notify_on`): `new_issues` (scan found issues with `first_seen_scan` = that scan; Markdown
     summary by album, links to `/issues?scan=N`) and `failures` (scan failed; a rename or undo
     raised `RenameError(partial=True)`). `ScanRunner` and the rename routes call
     `Notifications`, which sends from a daemon thread and only logs errors. `public_url` adds
     click-through links. Dashboard panel + `POST /notify/test`. Another service = a class
     with `describe()` and `send(Notification)`.
   - **Next**: 6c (retagging; "retag to the release that fits" could start from 6d's
     results) or 7b (embedded cover art, mostly cosmetic); later AcoustID (6d).
   - Later: ntfy/webhook notifiers.
   - 5f ✅ Bulk renames (for libraries with hundreds of `path.track_location` issues).
     `/issues` (album view, fix on) has a checkbox per renamable card (`form="bulk-form"`) and
     "all N albums with renames matching these filters" (`Store.rename_candidates`; an artist
     filter = every album of an artist). `POST /rename/bulk/preview` → `renamer.plan_albums`:
     each album planned on its own by `plan_album`, plus a refusal for albums whose moves land
     in a folder another selected album's do (their plans would change under each other).
     Refused albums are listed and skipped. Albums expand to their moves lazily
     (`/rename/preview?bare=1`). The apply form carries one `plan` = "<fingerprint> <album>"
     per album, so each is re-planned and checked against its own preview.
     `web/bulk.py` `BulkRenamer` runs `renamer.apply_albums` in a thread holding
     `ScanRunner.paused()` for the whole run: one journal batch per album (Undo per album),
     a failed album is rolled back and skipped, a half-moved one stops the run (and notifies),
     *Stop* finishes the current album. One rescan at the end; the outcome stays on Issues and
     Renames for 30 min (`BulkStatus.shown`). CLI: `tagdrifter rename [--artist X] [--check C]
     [--album F ...] [--dry-run] [--yes] [--no-scan]` (needs fix enabled unless `--dry-run`;
     Ctrl+C stops after the current album). The CLI can't hold the server's scan pause.
   - Later: rename across artist folders for `album.placement` issues.
   - `album.placement` follows the artist's **current** MusicBrainz name once the release is
     cached (`artist_folder = "musicbrainz"`, the default; `"album_artist"` = the tags):
     `CurrentArtist` from the release credit (`MbCredit.current_name`: translation, else the
     artist's name; sort names from the artist). Picard's "variations only"
     (`StandardizeArtistNames.VARIATIONS`) standardizes every credit except one equal to an
     ended "Artist name" alias's name or sort name, i.e. a former name (CRASH: "Charli XCX",
     now Charli xcx), so tags may keep a former name; folders shouldn't, or an artist splits
     (and case-only splits collide over SMB).
   - MP3 ✅ (`audio_extensions` default `.flac`, `.mp3`): mutagen reads it, `core/id3.py`
     renames the frames as Picard maps them (`TPE1` = artist, `TXXX:MusicBrainz Album Id`,
     the `UFID` owned by http://musicbrainz.org = recording MBID, `TRCK` "n/N" =
     tracknumber + totaltracks; other TXXX keep their description, other text frames their
     frame ID), so every check applies unchanged. `~pictures` from APIC frames, with the size
     read from the PNG / JPEG / GIF header. ID3v2.3 can't hold several values in a frame
     (Picard joins them with "/"): MBID lists are split again, names aren't ("AC/DC").
     `tests/conftest.py::build_mp3` writes silent MPEG frames plus `picard_id3()` frames.
   - Later: other formats (M4A: an MP4 atom mapping like `id3.py`).
   - Later: a conventional first-run login. On first launch with no
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
   **recording** MBID (what ListenBrainz scrobbles match on);
   `MUSICBRAINZ_RELEASETRACKID` is the track MBID. Messages say "recording MBID
   (`musicbrainz_trackid`)".
   - 6a ✅ Local tag checks (`checks/builtin_tags.py`, no network). `tags.required` is split
     so each concern has its own row, filter and severity; its default list is now empty
     (extra user-chosen tags only):
     - `tags.recording_mbid` (error): `musicbrainz_trackid` missing; scrobbles fall back to
       fuzzy artist + title matching.
     - `tags.mbids` (warning): release, release group, release track, artist or album artist
       MBID missing. Not `musicbrainz_workid` (track relationships are off in the supported
       Picard settings, so it's never written). Standalone recordings (a recording MBID but no
       release MBID) are flagged too, deliberately, so they're listed.
     - `tags.naming` (warning): tags the naming rules read (`NAMING_TAGS`: album artist,
       artist, album, title, track number, date; disc number on multi-disc releases), each
       with what the path falls back to.
     - `tags.artist_mbid_count` (track) / `album.artist_mbid_count` (album, reported once per
       folder): number of `ARTISTS` / `ALBUMARTISTS` values ≠ number of artist / album artist
       MBIDs. Without the names tag, one MBID is fine and several are flagged (older files
       often predate `ALBUMARTISTS`). Never guess by splitting `ARTIST` on
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
     - `album.replaygain` (info; added after 6d): any `replaygain_*` / `r128_*_gain` tag on
       any track (`expect = "none"`, the default), naming invalid values (`NaN dB`,
       `inf dB`; some taggers write them). `expect = "consistent"` is for keeping gain tags:
       only tracks without them, invalid values, or an album gain that differs within a disc
       of a release. Read-only: removing them is left to a tagger. Descriptive tags such as
       `volume level (replaygain)` don't count, nor do ID3 RVA2 frames.
   - 6b ✅ MusicBrainz cache + structural checks. Off unless `musicbrainz_enabled` /
     `[musicbrainz] enabled` / `TAGDRIFTER_MUSICBRAINZ`. Keep the strain on the API minimal
     (MusicBrainz is a small non-profit; this is a firm project rule):
     - `musicbrainz.py`: `MusicBrainzClient` makes one lookup per release,
       `/ws/2/release/<mbid>?inc=recordings+artist-credits+release-groups+aliases&fmt=json`,
       which already carries everything 6b-2 needs (release, recording and artist aliases),
       so adding checks never needs a refetch. Process-wide `RateLimiter`: ≥1.5 s from each
       response to the next request (not start to start, so jitter can't bunch them), gzip; User-Agent `TagDrifter/<ver> ( <contact> ; <repo URL> )`, where the
       optional `musicbrainz_contact` / `TAGDRIFTER_MUSICBRAINZ_CONTACT` is the operator's
       address (validated: one line, no parentheses). Clients come from
       `musicbrainz.client_for(settings)`: `musicbrainz_api_root` /
       `TAGDRIFTER_MUSICBRAINZ_API_ROOT` points at another server (a mirror; "/ws/2" added if
       missing) with its own shared limiter and `musicbrainz_interval` /
       `TAGDRIFTER_MUSICBRAINZ_INTERVAL` (default 1.5 s); an interval below `MIN_INTERVAL`
       for musicbrainz.org (or a subdomain) is refused at startup. The setting is there for
       bulk or search-heavy work (6c). UI links stay on musicbrainz.org. A 503 backs off 15 s and 60 s, or the
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
       `mb.duration` (file vs MusicBrainz track length, `tolerance_seconds` default 3). A
       release without track lengths (MusicBrainz shows ?:??; often vinyl) falls back to the
       recording's length (`MbTrack.recording_length_ms`, usually from other releases): only
       beyond `recording_tolerance_seconds` (default 30), as info, since a few seconds there is
       another master, not another version. `MbTrack.length_ms` is the release's own length
       only, so 6d and the tracklist don't count borrowed lengths (the tracklist shows them
       as "~m:ss"). A medium's hidden track before track 1 comes as its `pregap` (outside
       `tracks` and `track-count`): parsed as track 0 and counted in `track_count`, as
       Picard tags it.
       Picard's mapping: tracknumber = track `position` (not `number`, which can be "A1"),
       discnumber = medium `position` (not renumbered from 1), totaldiscs = number of media.
     - Audio length: `core/flac.py` reads STREAMINFO; `read_tags` stores it as the `~length`
       pseudo-tag (ms; "~" can't start a Vorbis comment name), `TrackMeta.length_ms`. Files
       cached without it are re-read once (about a minute for ~7,000 files).
     - Tests never call the API: an autouse fixture in `tests/conftest.py` makes real
       requests fail, and `tests/fixtures/musicbrainz/` holds two recorded lookups. Verify
       against the live API sparingly (a handful of releases via `sync_releases(limit=...)`).
   - 6b-2 ✅ Text checks against the cache (no new requests), in
     `checks/builtin_musicbrainz.py`; `MbRelease` / `MbTrack` now carry `MbCredit` artist
     credits, translated titles (`title_alias`) and the release group:
     - `mb.album` (once per album): album title, release group MBID.
     - `mb.track_title` (per track): title, including letter case. Skipped when the release
       track MBID isn't on the release (`mb.track_mismatch`'s job).
     - `mb.artist_credit`: per track, ARTIST (`credit_matches`: each artist by any of its
       names, joinphrases exact), ARTISTS (count and names) and artist MBIDs (missing, extra,
       order) vs the track's credit; once per album, the same for ALBUMARTIST /
       ALBUMARTISTS / album artist MBIDs vs the release credit. Missing tags are left to
       `tags.naming` / `tags.mbids`.
     - Comparison rules assume these Picard 3 settings (not configurable yet; generalize
       only when needed): no ASCII punctuation conversion (compare exactly, case-sensitive); artist names
       standardized for *variations only*, and artist, album and track names translated to
       the `en` locale when available. Picard (source, master) translates from **aliases**:
       artist aliases, the release's own aliases (album) and recording aliases (titles),
       taking the primary alias whose locale matches (exactly `en`, then any `en_*`), with "Artist name"
       / "Recording name" types preferred; it doesn't follow pseudo-release relationships.
       So: artist names match the credited name, the artist's name or its primary `en`
       alias (新しい学校のリーダーズ → "ATARASHII GAKKO!"); titles match the MusicBrainz track
       / release title or the primary `en` alias. Maenarawanai's release and track titles
       are already Latin in MusicBrainz (e.g. track "sekigae Gattuso", recording
       "席替ガットゥーゾ"), so track titles compare with the *track* title first.
   - 6b-3 ✅ Refresh on demand: *Refresh from MusicBrainz* in the release
     page header (`_mb_refresh.html`, `POST /library/refresh?folder=`) and `tagdrifter
     refresh <folder or release MBID>... [--no-scan]` look the album's releases up again now,
     then rescan (`ScanRunner.rescan`, `scan-started` so the page reloads when it's done).
     `musicbrainz.refresh_releases` shares `_fetch` with `sync_releases` (new
     `refresh_after` too), skips releases fetched less than `REFRESH_MIN_AGE` (5 min) ago and
     stops at the first error. `MusicBrainzSync.refresh` claims the run like a sync (refused
     while one is running), refuses while lookups are off or paused, and a failure pauses
     lookups (`_pause`, shared with failed runs and 6d). The tracklist shows when each release
     was looked up (`ReleaseView.fetched_at`). Retagging to another release needs no button:
     it's a new release for the next lookups. `Store.prune_mb_cache` (after each sync run
     without errors) drops releases no longer tagged on any file, unless more than 10% of the
     cache (at least 20) would go, which looks like a missing library and would mean
     refetching it all. Release group browses (6d) aren't pruned. The CLI uses its own rate
     limiter and doesn't see the server's pause (like `tagdrifter rename`).
   - 6c: Picard-style retagging with preview + approve; lookups for releases missing MBIDs.
   - 6d ✅ *Compare with other releases* on the release page (for `mb.duration`). The
     2026-10-31 API probe (CAKE, Prolonging the Magic, now the test fixture) found
     that one browse, `/release?release-group=<id>&inc=recordings+media&limit=100`, returns
     every release of the album with full tracklists, so a whole album is ranked from one
     request (recording / ISRC lookups and search are 1+ per track; search was fuzzy and
     disagreed with lookups). `MusicBrainzClient.release_group_releases` (at most
     `MAX_BROWSE_PAGES` = 3 pages), cached in `mb_release_groups` (migration 6),
     `MusicBrainzSync.lookup_release_group`: only on a click (`POST /library/release-group`),
     through the shared `RateLimiter`, refused while lookups are off or paused, one at a time,
     a no-op within `GROUP_REFETCH_AFTER` (10 min); a failure pauses lookups like a failed
     run. Never refreshed automatically. `library.fit_release` matches files to tracks (release
     track MBID, else recording MBID if unique on the release, else disc + track number) and
     counts fits within the `mb.duration` tolerance; `compare_release_group` sorts by files
     that fit, then off, unmatched, tracks without a file, mean difference
     (`_release_group.html`; the album page shows a cached comparison without a request).
     - ✅ Track by track, for folders
       assembled from several releases. When no release fits every file, `GroupComparison
       .tracks` (`library.track_sources`, `_track_sources.html`) puts each file down to a
       release of the group, greedily using as few releases as possible (lettered A, B, ...),
       from the cached browse alone (`ReleaseFit.fitting`: paths that fit). Files that fit
       nothing get *Look up these tracks* (`POST /library/tracks`,
       `MusicBrainzSync.lookup_tracks`, the shared one-at-a-time lock, limiter and pause): a
       recording lookup (`/recording/<id>?inc=releases+media`: the releases it's on, at most
       25, with its track's length on each; releases of the group are left out) and an ISRC
       lookup (`/isrc/<isrc>`: other recordings with that ISRC and their lengths; not
       followed further). `musicbrainz.lookup_tracks`: keys never looked up first, at most
       `MAX_TRACK_LOOKUPS` (10) requests a click, then *Look up again* refetches only after
       `GROUP_REFETCH_AFTER`. Cached in `mb_lookups` (migration 8, kind + key, NULL = not
       found), never refreshed automatically or pruned. The test fixture (CAKE, Prolonging the
       Magic) is a 1998 CD plus the 2023 Deluxe Edition's two live bonus tracks; Let Me Go's
       ISRC is also on a second recording (3:30), while the tagged one is 3:56. A browse by recording (`/release?recording=`) has no
       track lengths with `inc=media`, so it's no use here.
     - Later: AcoustID fingerprints (Chromaprint `fpcalc` on the audio, then
       the AcoustID API) to identify the recording from the audio itself, when the tags or
       lengths can't. Open points: needs `fpcalc` in the image and an AcoustID API key
       (free, per application), decodes audio (a full decode per file, slower still over a
       network share, so only on request and cached), and AcoustID's own rate limit (3 requests/s) and terms.
     - Set aside: ignoring leading/trailing silence (too loose; needs a FLAC decoder), a
       percent tolerance, accepting a known difference per album.
7. Library browser: a *Library* tab to look up any release, not just ones
   with issues.
   - 7a ✅ `library.py` + `/library` (search, paged 50 per page) and
     `/library/album?folder=` (`album.html`). Everything comes from the `files` cache
     (`Store.library_file_rows`: `json_extract` of five tags, about 0.1 s for 7,000 files, so
     no in-process cache yet) and the MusicBrainz cache (`Store.mb_releases(ids)`, now a
     `WHERE IN`): browsing never touches the disk or the network. An album = a folder with
     audio files below the library root (loose root files aren't albums). Search: every word
     must appear in the folder, album artist, album titles or release MBIDs (NFC, casefolded).
     Release page: header (title, artist, date, length, musicbrainz.org release and release
     group links), open issues (with *Preview renames* when `rename_candidates` has it),
     tracks, the MusicBrainz tracklist per tagged release (`_tracklist`: by release track
     MBID, else disc + track number; title and ±s length differences marked; "not looked up
     yet" / "lookups are off" / "MusicBrainz has no release"), album tags (`ALBUM_TAGS`, plus
     any tag identical on every track; differing values highlighted with track counts),
     embedded picture descriptions, then each track's remaining tags. Issue cards link their
     folder to the album page. The file column hides below 760 px.
   - 7b: embedded cover art.
     Cover art from the **embedded** PICTURE block (Picard-tagged libraries usually have
     one), not the Cover Art Archive: no extra requests to anyone, and it shows what music
     servers show. `core/flac.py` skips PICTURE blocks today (they can be several MB); add a reader
     that returns the front cover (type 3, else the first) on demand, served by a route that
     scales it down to a thumbnail cached under the data dir (keyed by path + size + mtime)
     and sent with a long `Cache-Control`. Scaling needs an image library (Pillow) or the
     browser does it from the full image; decide when building. Only read on page view,
     never during scans.
8. Secondary library: a second folder with the library's structure, made of hard links to its
   files minus exclusions, for a reader that can't exclude anything itself. Off unless
   `secondary_path` / `[secondary] path` / `TAGDRIFTER_SECONDARY`.
   - 8a ✅ Read-only plan. `core/secondary.py` (pure): `plan_sync(main, secondary, excluded)`
     needs no state of its own, since a link shares its library file's inode: a secondary file
     whose inode belongs to a library file at another path moves (renamed in the library), a
     library file with no link anywhere gets one, links to excluded files (or second links)
     are unlinked, a different file where a link belongs is *replaced*, files that aren't
     links to library files are *strays*, never removed (`only_copy` = link count 1), folders
     left empty go (not ones holding ignored names). Exclusions (`secondary_exclusions`,
     migration 9): `artist` (album artist MBIDs joined with ";"), `release`, `track` (release
     track MBID), `path` (folder or file) or `extension` (".lrc", any file anywhere;
     `normalize_extension`); `excluded_files` applies them to audio, then
     other files match path / extension rules, sidecars follow their track and the rest the
     nearest folder above with audio (left out when all its audio is). Audio changed since the last scan has no keys (path rules only).
     `propose_exclusions` describes what an existing secondary leaves out: artist folders,
     then album folders, then tracks, by shared MBID only when no linked file has it, else by
     path; `propose_extensions` adds non-audio extensions with no file in the secondary.
     Over SMB from Windows, inode numbers match the server's but link counts read 1. `secondary.py`: `validate_settings` (startup: not the library, inside it, around it,
     or a filesystem root), `check` (a `.tagdrifter-secondary` marker file in its root unless `secondary_marker` /
     `[secondary] marker` / `TAGDRIFTER_SECONDARY_MARKER` is off; same
     `st_dev`; warnings: separate mounts per `/proc/self/mountinfo`, since link() across mounts
     fails with EXDEV even on one filesystem, and read-only), `walk` (lstat, no symlinks),
     `analyze`. `/secondary` (+ `/secondary/analysis`, loaded after the page; proposals saved
     with a fingerprint; *Exclude a file type*) and `tagdrifter secondary [--propose [--save]]
     [--exclude-type EXT]`.
   - 8b ✅ Sync (`secondary_write.py`, off unless `secondary_sync` / `[secondary] sync` /
     `TAGDRIFTER_SECONDARY_SYNC`). `SecondaryWriter` is the only write path: link (library ->
     secondary), move (link to the new name, then unlink: never overwrites), unlink (only if
     the inode is the planned one and the link count is above 1, unless approved), mkdir /
     rmdir (empty only). Never opens a file for writing, never touches the library. Paths are
     split by hand (no "", ".", ".."); on Linux every component is opened with
     O_DIRECTORY | O_NOFOLLOW from a dir fd and the *at calls use it (`USE_FDS`), elsewhere
     each component is lstat'ed (symlinks, junctions refused). `apply_sync` takes the
     previewed `plan_fingerprint`, re-plans, refuses on problems or warnings (separate mounts,
     read-only), re-checks the marker, then: unlinks, replaces (only copies skipped unless
     `replace_only_copies`), moves (waiting for a destination freed by another move; a cycle
     parks one link as `<name>.tagdrifter-move`), links, rmdirs. Stops at the first error
     (`_explain` names EXDEV / EROFS / EPERM = protected_hardlinks); retrying is safe since the
     plan is recomputed. Journal: `secondary_syncs` / `secondary_ops` (migration 10), no undo
     (everything but replacing an only copy is redone by syncing again). One sync at a time per
     process (`_LOCK`). `POST /secondary/sync`, *Recent syncs*, `tagdrifter secondary --apply
     [--replace-only-copies] [--yes]`. Needs one read-write mount holding both, and
     `fs.protected_hardlinks` means the container's user must own the library's files or be
     able to read and write them. Scans aren't paused: a sync never touches the library.
   - 8c ✅ Exclusion toggles on the Library pages, from the scan's cache only (no walk, so
     they show what the rules say, not what the secondary holds): `secondary.album_exclusions`
     (saved rules leaving out any of the album's audio, per-track matches, and a `Toggle` each
     for its album artist, the album and every track) and `excluded_by_folder` (list badges),
     via `Store.file_tags` (just `KEY_TAGS`). `core/secondary.exclusion_for` keys a new rule on
     the MBID the files share (album artist / release / release track), else a path (artist
     folder, album folder, file). The album page's *Secondary library* panel (*Exclude this
     album*, *Exclude every album by X*, *Include again* per matching rule, confirming when a
     rule covers files outside the album) and a *Secondary* column in Tracks.
     `POST /library/exclude` (`folder`, `kind` = artist | release | track, `path`) re-derives
     the rule server side; `/secondary/exclusions/{id}/delete` takes a `next` to return to.
     Changes apply at the next sync.
   - 8c-2 ✅ Albums with open issues left out (`secondary_exclude_issues` /
     `[secondary] exclude_issues` / `TAGDRIFTER_SECONDARY_EXCLUDE_ISSUES`: error, warning,
     info or off, the default; true = warning). `secondary.issue_albums` reads
     `Store.open_issue_albums` (issues with an `album`, so not artist/library scope) and
     `issue_exclusions` turns them into unsaved rules of kind `ISSUES_KIND` (matches audio
     directly in the folder), added to the saved ones in `analyze`, `excluded_by_folder` and
     `album_exclusions` (`AlbumExclusions.issues` / `left_out`; saved-rule buttons stay, so a
     lasting exclusion can still be saved). Listed on *Secondary* (`Analysis.issue_albums`)
     and by `tagdrifter secondary`. An album is linked again at the first sync after its
     issues resolve.
   - 8d: automatic sync after scans and renames for changes that never free a last copy;
     the rest waits for approval; failures notify.
9. Triage folder ✅ (`triage.py`, `web/triage.py`; off unless `triage_path` / `[triage] path` /
   `TAGDRIFTER_TRIAGE`). Newly tagged files dropped there move into the library at their
   expected paths. `plan_triage` walks it (no symlinks), groups audio by release MBID (else by
   folder) and gives each album a status: *waiting* (a folder's files changed within
   `triage_settle`, default 120 s, so copies and tagging finish; or, with MusicBrainz on, the
   release isn't cached yet, then `TriageWatcher` looks it up via `MusicBrainzSync.refresh`),
   *blocked* (unreadable, `NEEDED_TAGS` or MBIDs missing, a target that exists (compared
   case-insensitively, never overwritten), two files with one target, the release already in
   the library in another folder (no splitting), an artist folder colliding by case),
   *confirm* (checks of warning or worse, run over the album as it would be in the library:
   its library tracks + the new ones, minus `SKIPPED_CHECKS` = `path.*`, `album.placement`;
   or the folder name clashes with another release, which then gets a suffix and a rename
   proposal) or *ready* (info issues don't hold up). The artist folder is
   `expected_artist_folder` of the album artist (`current_album_artist`, shared with
   `album.placement`), else an existing folder `audit_album_placement` accepts. Targets come
   from `resolve_release_collisions` together with the artist folder's library tracks (triage
   metas live under the fake `STAGING` folder meanwhile). Sidecars follow their track; when
   every audio file at or below a triage folder belongs to the album (`_owned`, walking up to
   the first parent with other audio), its other files and audio-less folders move into the
   album folder unless the name is taken. `move_album` journals one rename batch per album
   with `origin = 'triage'` (migration 11; `src` triage-relative), creates library folders,
   removes emptied triage folders, rolls back on failure; `undo_batch` sends a triage batch
   back to the triage folder (`renamer.move_path` takes two roots). Moves are renames, so
   `check` warns (and nothing moves) on another filesystem, separate mounts or read-only.
   `TriageWatcher`: a timer every `triage_interval` (default 300 s; 0 = on request only), one
   run or confirmed move at a time, moves ready albums under `ScanRunner.paused()`, one
   rescan (`reason="triage"`, automatic), and a `triage` notification (new default event)
   listing moves and albums that newly need you (once per album fingerprint, in memory).
   `/triage` (plan loaded by `/triage/plan`), *Process now* (`POST /triage/run`), *Move anyway*
   / *Move now* (`POST /triage/move`, `confirm_album` re-plans and checks the `fingerprint`,
   which covers the moves and the findings). The Renames page (journal + Undo) opens when fix
   or triage is on. CLI: `tagdrifter triage [--apply] [--confirm KEY ...] [--no-scan]`.
   - Later: an "ignore this issue" choice per album (e.g. a known `mb.duration` difference),
     and moving across artist folders for releases already in the library elsewhere.

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
- Tests build real FLAC and MP3 files on the fly (`tests/conftest.py::build_flac`, including
  cover-art blocks, and `build_mp3`); never commit audio fixtures.
- `.gitattributes` forces LF line endings.
- When changing naming rules, update `docs/naming-conventions.md` and add a test for the
  example.
