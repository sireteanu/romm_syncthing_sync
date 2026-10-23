# Changelog

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
