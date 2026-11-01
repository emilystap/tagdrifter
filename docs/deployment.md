# Deploying with Docker

Tag Drifter runs as one container: the web UI plus scheduled scans. The image is built by GitHub
Actions and published to `ghcr.io/emilystap/tagdrifter` (`latest` from `main`, `X.Y.Z` from
version tags). The image is public, so it pulls without registry credentials.
[`compose.yaml`](../compose.yaml) works with Docker Compose, or with a NAS whose app installer
accepts a compose file.

## 1. Create a directory for the app's data

The container runs as UID/GID 568 by default; set `user:` in `compose.yaml` to another account
if you prefer (it needs read access to the library). Create a directory for the database and
give that user write access, e.g.:

```bash
mkdir -p /srv/tagdrifter/data
sudo chown -R 568:568 /srv/tagdrifter/data
```

The database holds the tag cache, scan history and issues. It's rebuilt by the next scan if
lost (only issue history is lost), but back it up with your other app data anyway.

## 2. Check library permissions

The library is mounted **read-only**. The container's user needs read and traverse access to
every folder of the library. If its permissions come from ACLs (e.g. a share set up for SMB),
add an entry for that user (or group) with read permission, applied recursively. A quick
check from the host:

```bash
sudo -u '#568' find /path/to/music -maxdepth 2 | head
```

## 3. Edit compose.yaml and start it

Edit [`compose.yaml`](../compose.yaml):

- the `/path/to/...` host paths (library and data);
- `user`, if you chose another account in step 1;
- `TZ`, so the schedule and the times in logs are local;
- `TAGDRIFTER_SCHEDULE` if you want something other than daily at 04:00. It's standard cron:
  `minute hour day month weekday`, e.g. `0 4 * * 1` for Mondays only, or `off`.
- the published port (`8080:8080`) if 8080 is taken.

Then `docker compose up -d` (or paste it into your NAS's compose installer). The UI is then at
`http://<host>:8080`. Don't expose it beyond your LAN.

**Behind a reverse proxy** (Nginx Proxy Manager, Caddy, Traefik): proxy a hostname to
`http://<host>:<port>`; plain HTTP to the container is fine, with HTTPS terminated at the
proxy. The UI uses root-relative links, so it works over HTTPS without extra headers. It must
be served at the root of its hostname (e.g. `https://tags.example.com/`), not under a path.
Keep the proxy host LAN-only (e.g. an access list). If the proxy sends `X-Forwarded-Proto:
https` (NPM does), the login cookie is marked HTTPS-only.

## 4. First scan

The first scan reads every file's tags. It only reads the tag blocks at the start of each file
(cover art is skipped), so it's quick: several thousand files a minute even over a network
share, and faster on local disks. Press *Scan now* in the top bar, or wait for
the schedule. Later scans re-read only files whose size or modification time changed.

## Optional: a login

Without a password the UI has no login. To require one:

1. Generate a hash, with the app running (type the password twice):

   ```bash
   sudo docker exec -it tagdrifter tagdrifter hash-password
   ```

   It prints a line starting `scrypt:`. The password itself is never stored.
2. Add it under `environment` in `compose.yaml`:
   `TAGDRIFTER_PASSWORD_HASH: "scrypt:16384:8:1:..."` (the whole line, in quotes). The hash
   contains no `$`, so compose won't try to expand it.
3. Recreate the container (`docker compose up -d`). Every page now asks for the password; *Log out* is in the top bar.

Sessions last 30 days and are signed with a key generated in `/data/secret_key`. Delete that
file (or set `TAGDRIFTER_SECRET_KEY`) to log every browser out. `/healthz` never needs a login.
To forget the password, remove the variable and restart.

## Optional: renaming files

Tag Drifter can rename an album's tracks (and their folder) to the expected paths it reports.
It's off by default and only acts on albums whose every track is readable and MBID-tagged.
To turn it on:

1. Give the container's user **write** access to the library (with ACLs, the same entry as
   in step 2, with modify instead of read permission).
2. In `compose.yaml`, change the library mount from `:ro` to `:rw` and add
   `TAGDRIFTER_FIX: "true"` under `environment`, then recreate the container.
3. On the *Issues* page, album cards with proposed renames get a *Preview renames* button. It
   lists every move (folder, tracks, `.lrc`/`.txt` sidecars) and why an album can't be renamed,
   if it can't. *Rename* applies it, and a scan runs afterwards to update the issues.
4. To rename many albums at once, tick their cards, or "all albums with renames matching these
   filters" (filter by artist first to do one artist), then *Preview together*. Albums that
   can't be renamed are listed with the reason and skipped; expand any album to see its moves.
   *Rename N albums* runs in the background with progress and a *Stop* button (it finishes the
   album in progress), then rescans once. Each album gets its own entry on the *Renames* page,
   so you can undo one album without the others. From a shell:
   `docker exec -it tagdrifter tagdrifter rename --artist "Pixies" --dry-run` lists the plan;
   without `--dry-run` it asks before renaming. Avoid running it while the web UI is scanning.

Safeguards: nothing is ever overwritten; each file must be unchanged since the last scan (size
and modification time) or the album is refused until you scan again; renames stay inside the
album's artist folder; scans wait while files move. Every rename is recorded on the *Renames*
page (top bar), where it can be undone. If a rename fails halfway, the moves already made are
reversed automatically.

Take a snapshot or backup of the library before your first batch of renames. Your music
server sees renamed files as moved and picks them up on its next scan; whether play counts and
favourites follow moved files depends on the server. Set `TAGDRIFTER_FIX` back to
`false` and the mount to `:ro` to go back to report-only.

## Optional: notifications

Tag Drifter can push to a [Gotify](https://gotify.net) server when something needs attention:

- **new issues** (`new_issues`): after a scan, scheduled or manual, that found issues that
  weren't open before: how many, and the albums they're in (up to 10). A scan with nothing new
  sends nothing. The first scan reports everything it finds.
- **failures** (`failures`): a scan failed, or a rename or undo stopped partway and left files
  half-moved (high priority).

1. In Gotify: *Apps* → *Create application* (e.g. "Tag Drifter") and copy its token.
2. Add to `environment` in `compose.yaml`:

   ```yaml
   TAGDRIFTER_GOTIFY_URL: "http://gotify.lan:8080"   # the address the container can reach
   TAGDRIFTER_GOTIFY_TOKEN: "AbCdEf..."
   TAGDRIFTER_PUBLIC_URL: "https://tagdrifter.example" # optional: tap a notification to open it
   # TAGDRIFTER_NOTIFY_ON: "failures"                 # optional; default new_issues,failures
   ```

3. Recreate the container. The dashboard's *Notifications* panel shows where they go and has
   *Send test notification*, which shows Gotify's reply (e.g. a wrong token).

The token is sent in the `X-Gotify-Key` header, so it doesn't appear in proxy logs. If Gotify
is unreachable, the scan or rename carries on and the container log says why the notification
wasn't sent. If Gotify runs in another container on the same host, use the host's LAN address and
Gotify's published port; `localhost` inside the container is the container itself.

## Optional: MusicBrainz lookups

With `TAGDRIFTER_MUSICBRAINZ: "true"` under `environment`, Tag Drifter looks up each
release in the library on [MusicBrainz](https://musicbrainz.org) (by `musicbrainz_albumid`)
and the `mb.*` checks compare your tags and audio lengths with it: a track tagged with
another version's recording, track or disc numbers that changed, a release that was merged
or deleted, a length that doesn't match (live vs studio, edits, the wrong release), and
titles or artist credits that differ from MusicBrainz (compared exactly, letter case
included; English translations of names and titles, as Picard makes them, are accepted).

MusicBrainz is a small non-profit, so Tag Drifter is careful with its API:

- **One request per release**, at most **one request a second**, identified with a
  User-Agent that links to this project (and to you, if you set a contact; see below).
- Each release is cached and only looked up again after a random 45-75 days, so
  refreshes trickle in rather than coming due together.
- **No set time of day.** MusicBrainz asks background jobs not to wake at, say, 03:00 (with
  many installs, that's a peak at the top of every hour somewhere) but to spread calls
  through the day. So background lookups (releases new to the library, and refreshes) don't
  follow the scan schedule: a worker wakes at random intervals of 30-150 minutes and looks up
  up to 5 releases each time. A scheduled scan never triggers lookups itself.
- After a scan **you** start (*Scan now*), everything due is looked up at once, since you're
  waiting for the result.
- If MusicBrainz answers "slow down" (503), Tag Drifter waits (at least as long as its
  `Retry-After` header asks), retries twice, then stops. After a run stops on any error,
  lookups pause for an hour (or longer, if MusicBrainz asked), however many scans start
  meanwhile; the dashboard shows until when.

MusicBrainz asks every client to identify a way to reach whoever runs it. The User-Agent
always links to this project; to add your own address (recommended, so MusicBrainz can reach
*you* rather than the project if your instance misbehaves), set
`TAGDRIFTER_MUSICBRAINZ_CONTACT: "you@example.com"`. It is sent only to MusicBrainz, in the
User-Agent header: `TagDrifter/0.1.0 ( you@example.com ; https://github.com/emilystap/tagdrifter )`.

### Your own MusicBrainz server (optional)

If you run a MusicBrainz mirror (e.g. [musicbrainz-docker](https://github.com/metabrainz/musicbrainz-docker)),
point Tag Drifter at it with `TAGDRIFTER_MUSICBRAINZ_API_ROOT: "http://mirror:5000"` (the
server, or its `/ws/2` web service root) and, since it's yours, drop the wait between
requests with `TAGDRIFTER_MUSICBRAINZ_INTERVAL: "0"` (seconds; default 1.5). Everything
else (caching, refresh timing, the pause after errors) stays the same, and links in the UI
still go to musicbrainz.org. The interval can't go below 1.5 s while the server is
musicbrainz.org: Tag Drifter refuses to start with such a setting.

For the first full lookup, press *Scan now* once lookups are on: ~600 releases take about 25
minutes (the dashboard's *MusicBrainz* panel shows progress), and Tag Drifter rescans when
it's done so the issues appear right away. Left to the background lookups alone, the first
pass would take several days. After that, the background lookups keep up with new releases
and refreshes (about 20 a day for ~600 releases) and rescan whenever they fetched something.

Upgrading to this version re-reads every file once (to record its audio length), so the
first scan after updating takes about as long as the very first one.

Tolerance for `mb.duration` (default 3 seconds) is set in `config.toml`:
`[checks."mb.duration"] tolerance_seconds = 5`.

## Optional: config.toml

Environment variables cover the main settings. For per-check options (required tags, disabled
checks), put a `config.toml` (see [`config.example.toml`](../config.example.toml)) in a
directory mounted at `/config` (`/config/config.toml`). Precedence is: defaults <
`config.toml` < `TAGDRIFTER_*` variables. Leave `[library] path` and `[database] path` out of
it, or set them to `/music` and `/data/tagdrifter.db`; the image sets both through env vars
anyway.

## Updating

With the `latest` tag: `docker compose pull && docker compose up -d`. To pin a version
instead, use `ghcr.io/emilystap/tagdrifter:X.Y.Z` in `compose.yaml`. Database
migrations run automatically on start.

## Health and logs

The container's health check calls `GET /healthz`, which also reports whether a scan is
running and when the next scheduled scan is. Each scan logs a line when it starts and a summary
when it finishes; view them with `docker logs tagdrifter`.
