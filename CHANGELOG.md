# Changelog

## 2.3.0 — mapping lives in the UI

### Changed
- The platform mapping is managed only on the Platforms page. On first start it's pre-filled with the original project's folder-name defaults (or a mapping you saved or mounted before), and no file needs to be mounted. The Unraid template and compose files no longer mount `platform_mapping.yaml`.
- The interface no longer refers to `platform_mapping.yaml`. "Your mapping" is now labelled **Mapped**.

### Fixed
- The saved mapping no longer collides with a `platform_mapping.yaml` *folder* that Docker creates in the appdata folder when the mapped file is missing (it's now stored as `platform_mapping.ui.yaml`).
- The "unsaved changes" bar no longer shows when there are no changes.
- On first install, the first sync waits briefly for RomM's folder aliases so those folders match right away.

### Added
- The Platforms page and the startup log show what each sync folder contains, with a clear explanation when one is empty or missing system subfolders.

## 2.2.0 — faster library reads, progress logging

### Changed
- Reading the RomM library is much faster:
  - only platforms that have a folder in your sync folders are read
  - RomM is asked not to build its gallery extras (letter index, filter values, id index) on every page
  - the default page size is now 500
  - pages are trimmed to the fields the sync uses as they arrive
- The library is read once at startup instead of twice.

### Added
- Progress in the log and on the dashboard's progress bar:
  - "Reading RomM library — <platform>: 1,500 / 12,345 (12%)"
  - the sync folder scan summary
  - "Matched X of Y games"
  - saves/states fetched from RomM
  - "Comparing … games: x / y"
- Platform folders with no RomM platform are listed at startup.
- A clear warning when the mounted `platform_mapping.yaml` is a folder (Docker creates one when the host file is missing).

## 2.1.1 — startup fixes

### Fixed
- Startup no longer hangs silently when RomM can't be reached: requests to RomM now time out, the log and dashboard say *why* (unreachable URL, rejected token), and startup retries with backoff.
- "Web UI ready" is only logged once the port is actually bound. A port clash is reported as an error instead.
- Downloading the platform list from GitHub happens in the background and never delays startup on servers without internet access.

## 2.1.0 — Platforms page

### Added
- **Platforms** page in the dashboard: every sync-folder system folder, the RomM platform it resolves to and how, with a searchable picker to map or override. Saves to `/data/platform_mapping.yaml` and re-syncs.
- RomM's platform list (slugs + display names) and built-in folder aliases are downloaded from GitHub (`rommapp/romm` at your server's version tag, `rommapp/docs`), cached in `/data/romm_platforms.json`, and refreshed weekly or on demand.
- Automatic platform matching now also uses your RomM library's folder names and RomM's folder aliases (Batocera / RetroBat / ES-DE names), so many folders need no mapping.

### Changed
- A mounted `platform_mapping.yaml` is optional. Once you save on the Platforms page, the UI-managed copy in `/data` is used instead (the mounted file is never modified).

## 2.0.0 — GUI & two-way sync fork

Fork of [romm_syncthing_sync](https://github.com/amn-96/romm_syncthing_sync) 1.4.1 by amn-96.

### Added
- Web dashboard: sync progress, systems, games with RomM covers and save-state screenshots, activity log, light/dark theme, phone layout.
- RomM → sync folder downloads (`PULL_MODE=confirm|auto|off`).
- Approval queue with confirmation dialogs for new uploads (`PUSH_NEW_MODE`) and downloads.
- Conflict detection using a persisted per-file sync baseline. Side-by-side resolution: keep local, keep RomM, or keep both.
- Backups of every overwritten file (`/data/backups`, `BACKUP_RETENTION_DAYS`).
- RomM poller for watch mode (`ROMM_POLL_INTERVAL_SECONDS`) and a **Sync now** button.
- Optional dashboard basic auth (`WEB_USERNAME` / `WEB_PASSWORD`). File ownership for downloads (`PUID` / `PGID`).
- Unraid template and icon (`unraid/`).
- Automatic release on every push to the default branch (tests → tag → GitHub release → multi-arch image on GHCR).
- Unit tests for the decision rules, a mock RomM server and demo data (`tests/`).

### Changed
- Full syncs read all saves/states in 2 API calls instead of 2 per game.
- Sync folders are mounted read-write in `docker-compose.yml` (needed for downloads).

### Fixed
- `ROMM_AUTH_TYPE=api` no longer requires `ROMM_USERNAME` / `ROMM_PASSWORD` to pass validation.
- Inline comments after `ROMM_AUTH_TYPE` in `.env` files are tolerated.
- File renames (how Syncthing delivers files) are now detected in watch mode.
- File events that arrive during a running sync are no longer left waiting for the next unrelated change.
