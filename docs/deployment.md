# Deploying on TrueNAS SCALE

Tag Drifter runs as one container: the web UI plus scheduled scans. The image is built by GitHub
Actions and published to `ghcr.io/emilystap/tagdrifter` (`latest` from `main`, `X.Y.Z` from
version tags). These steps assume TrueNAS SCALE 24.10 (Electric Eel) or later, where apps are
Docker Compose projects.

## 1. Make the image pullable

GHCR packages start out **private**. Either:

- **Public (simplest):** on GitHub, open your profile → *Packages* → `tagdrifter` →
  *Package settings* → *Change visibility* → Public. The image contains only this repo's code.
- **Private:** in TrueNAS, *Apps* → *Configuration* → *Manage Container Image Registries* → add
  `ghcr.io` with your GitHub username and a personal access token with only `read:packages`.

## 2. Create a dataset for the app's data

Create a dataset for the database, e.g. `POOL/apps/tagdrifter` with a `data` directory in it,
and give the `apps` user (UID/GID 568) write access:

- *Datasets* → select it → *Permissions* → *Edit* → owner user `apps`, group `apps`, apply
  recursively; or from a shell: `chown -R 568:568 /mnt/POOL/apps/tagdrifter`.

The database holds the tag cache, scan history and issues. It's rebuilt by the next scan if
lost (only issue history is lost), but snapshot the dataset with your other app data anyway.

## 3. Check library permissions

The container runs as `568:568` and mounts the library **read-only**. The `apps` user needs
read and traverse access to every folder of the music dataset. If the dataset uses an SMB ACL
preset, add an ACL entry for the `apps` user (or group) with *Read* permission, applied
recursively. A quick check from the TrueNAS shell:

```bash
sudo -u apps find /mnt/POOL/media/music -maxdepth 2 | head
```

## 4. Install via YAML

*Apps* → *Discover Apps* → ⋮ → *Install via YAML*. Name it `tagdrifter` and paste
[`compose.yaml`](../compose.yaml) after editing:

- the two `/mnt/POOL/...` host paths (library and data);
- `TZ`, so the schedule and the times in logs are local;
- `TAGDRIFTER_SCHEDULE` if you want something other than daily at 04:00. It's standard cron:
  `minute hour day month weekday`, e.g. `0 4 * * 1` for Mondays only, or `off`.
- the published port (`8080:8080`) if 8080 is taken; Navidrome's default is 4533.

The UI is then at `http://<truenas>:8080`. It has no login, so don't expose it beyond your LAN.

## 5. First scan

The first scan reads every file's tags (about a minute for ~7,000 files over SMB from a laptop;
local disk access on the NAS should be faster). Press *Scan now* in the top bar, or wait for
the schedule. Later scans re-read only files whose size or modification time changed.

## Optional: config.toml

Environment variables cover the main settings. For per-check options (required tags, disabled
checks), put a `config.toml` (see [`config.example.toml`](../config.example.toml)) in a
directory mounted at `/config` (`/config/config.toml`). Precedence is: defaults <
`config.toml` < `TAGDRIFTER_*` variables. Leave `[library] path` and `[database] path` out of
it, or set them to `/music` and `/data/tagdrifter.db`; the image sets both through env vars
anyway.

## Updating

With the `latest` tag: *Apps* → `tagdrifter` → *Update* (or ⋮ → *Pull image*, then restart).
To pin a version instead, use `ghcr.io/emilystap/tagdrifter:X.Y.Z` in the YAML. Database
migrations run automatically on start.

## Health and logs

The container's health check calls `GET /healthz`, which also reports whether a scan is
running and when the next scheduled scan is. Each scan logs a line when it starts and a summary
when it finishes; view them under *Apps* → `tagdrifter` → *Logs*.
