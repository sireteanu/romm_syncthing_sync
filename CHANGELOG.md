# Changelog

## 2.9.0 — Saves page

### Added
- **Saves** page: every save and state in your sync folders and on RomM in one list, grouped by game, showing where each file exists, its size and age on each side, how many versions RomM keeps, and whether the copies match.
  - Filters: search, system, saves/states, *In both* / *Folder only* / *RomM only* / *Different* / *Old versions*.
  - Select files, whole games or everything shown, then **Delete** (folder and RomM, via the recycle bin), **Remove old RomM versions** (keeps the newest), **Download to folder** or **Upload to RomM**.
  - Runs in the background with progress, and lists any failures with the reason. Every action shows up under Logs → Recent actions.
- Recycle bin entries say where they came from (e.g. "Older RomM version from …").

## 2.8.0 — lets your disks sleep

### Changed
- **The RomM check no longer reads your sync folders.** In watch mode, every `ROMM_POLL_INTERVAL_SECONDS` the app now only asks RomM for its list of saves and states. Your folders are read only if RomM actually has something new (another device, the web player…). Before, every check re-read every file's details in the saves and states folders, which could spin up Unraid array disks every 10 minutes.
- Uploads, updates and deletions the app made itself don't count as "RomM changed", so syncing a save you just played doesn't trigger a folder scan.
- Syncing one changed save no longer checks every other file in the folders.

### Added
- `FULL_SCAN_INTERVAL_HOURS` (default 24, `0` = never): a scheduled full re-read of the sync folders as a safety net. Also on the Settings page.
- **Last folder scan** on the dashboard.

## 2.7.0 — recycle bin

### Added
- **Deletions sync both ways, through a recycle bin.** Delete a save or state in your sync folder and it's removed from RomM too; delete it in RomM and the local file is removed. The other side's copy is moved to the **Recycle bin** first (`/data/recycle`), never destroyed outright.
- **Recycle bin page:** Restore (back into the sync folder; the next sync uploads it to RomM again), Download, Delete forever, Empty recycle bin. A badge in the menu shows how many items it holds.
- **Clear the recycle bin after X days:** `RECYCLE_RETENTION_DAYS` (default 30, `0` = never), also on the Settings page.
- **When a synced file is deleted on one side:** `DELETE_MODE` = Recycle bin (`auto`, default) / Ask first (`ask`) / Never (`off`), on the Settings page.
- **Deletions** tab under Needs attention, with *Delete all* / *Bring all back*.

### Safety
- A file is only deleted when the other copy is unchanged since the last sync; otherwise you're asked.
- More than 10 deletions in one sync (e.g. an unmounted share) are held for approval instead of carried out.
- If RomM's copy can't be downloaded into the bin, it isn't deleted on RomM.

### Fixed
- Menu items no longer wrap on narrow screens.

## 2.6.1 — fixes for downloads that kept coming back

### Fixed
- **Saves were overwritten from the wrong ROM:** a stray copy of a save attached to another ROM (e.g. "(Disc 1)" while your save syncs with the multi-disc ROM) could be downloaded over your real save. Files that already exist locally are now never overwritten by the "games only on RomM" check. A "new file" download also refuses to replace a file it didn't expect.
- **States in RomM's saves list never paired:** RomM files are now classified by name, so a `.state1` stored in RomM's *saves* list pairs with your local state. Updates go back to the list the file came from.
- **Prefix matches stole ROMs:** each ROM is now claimed by one local game and exact names win, so "Fallen" no longer takes "Fallen Angel" (and "It's Magic" no longer takes "It's Magic II").
- **Other games' files were pulled in:** RomM files belonging to another local game in the same folder are ignored instead of downloaded.

### Added
- Files and folders written to the sync folders (and backups) get permissions `0777` by default, so Syncthing, SMB shares and devices can read and write them. Configurable with `FILE_MODE` or on the Settings page (0777 / 0666 / 0775 / 0664).

## 2.6.0 — logs page, queue fixes

### Added
- **Logs** page:
  - the full log with level filter (All / Info / Warnings / Errors), search with highlighting, live updates and **Download log**
  - **Recent actions**: what was uploaded, downloaded or skipped, and why anything failed. Kept across restarts.
- The log is written to `/data/logs/romm_sync.log` (rotated at 5 MB, 5 files kept).

### Fixed
- **Download all / Upload all / Skip all did nothing.** The bulk request was being handled as a single item called "bulk". This bug had been there since 2.0.
- **Files from RomM 5.x's own player and other devices kept coming back to the queue.** Names like `autosave [date].srm` or `Game [date].autosave.state0` were downloaded under those names, which RetroArch can't use and the app couldn't match back to the game. They're now renamed to the ROM's file name (`Game (USA).srm`, `Game (USA).state`).
- `Game.state` and `Game.state0` both counted as "slot 0", so one hid the other. Local states are now tracked by file name.
- A sync running while an approved item was still being processed could queue that item again.
- Queued items that no longer apply (game re-matched, file renamed, queued by an older version) are removed on the next full sync.
- Multi-disc games pick "(Disc 1)" consistently when the save name matches several discs.
- Comparing large libraries is much faster: the state file is saved once per sync instead of once per queued item.

## 2.5.0 — settings, notifications, speed, matching, RomM look

### Added
- **Settings page**: sync behaviour, RomM check interval, file-change delay, periodic interval, backup retention and dashboard login, all editable in the browser and applied immediately. Stored in `/data/settings.json`, overriding container values, each with a reset link.
- **Notifications** via Discord, ntfy, Gotify, Pushbullet or a generic webhook:
  - per-event toggles (questions, errors, sync problems/recovery, sync activity)
  - one message per sync, never repeated for the same item
  - optional dashboard link
  - a **Send test** button

### Changed
- **The dashboard follows RomM 5.x's design tokens.** That covers colours (dark `#07070f` / violet `#8b74e8`, light `#f5f5fa` / `#553e98`), RomM's font stack and sizes, glass cards, chip and card radii, the pill navigation, uppercase section labels and the scrollbar. The logo stays this app's own, and the footer states it isn't affiliated with RomM.
- **Faster:**
  - File fingerprints are cached across restarts.
  - The RomM library is reused between syncs. Each sync makes one request to compare per-platform ROM counts and fetches only ROMs changed since the last read (RomM 5.x `updated_after`). A full re-read happens when counts change, when your folders/platforms change, or every 6 hours.
  - Watch mode no longer re-reads the library on every file change.
- "Could not match" warnings appear once per game instead of on every sync.

### Fixed
- The same game in two system folders (e.g. `gb/` and `gbc/`) was merged into one game and synced to only one RomM ROM. Games are now kept apart per system folder.
- When two folders point at the same RomM platform and hold the same game, one copy is synced and the other is reported under Errors, instead of both writing to one RomM file.
- Saves are matched to ROMs by exact name first. A partial match must be at the start of the ROM name and end at a word boundary, so "Mario" no longer matches "Super Mario World".
- Watch mode read "Dr. Mario (USA).srm" as a game called "Dr". Names with dots now work.
- Image, text and other non-save files in system folders are no longer treated as saves.

## 2.4.0 — automatic by default, asks only when unclear

### Changed
- Uploads and downloads are automatic by default (`PUSH_NEW_MODE=auto`, `PULL_MODE=auto`). You're only asked when the right answer is unclear:
  - a conflict (the file changed on both sides)
  - a previously synced file that is missing on one side (possibly a deliberate delete)
  - two different copies with no sync history where RomM's is newer
- Each question explains in plain language why it's being asked.

### Added
- An **Errors** tab lists files that failed to sync, with a human-readable cause (token permissions, read-only folder, file too large, RomM unreachable, RomM server errors…), technical details, **Retry all** and **Dismiss**. Errors clear themselves once the file syncs.
- The status headline says "Synced, with errors" or "Needs your input" instead of "Up to date" when appropriate.

## 2.3.1 — version in the interface

### Added
- The app version is shown in the dashboard header (links to its release notes) and the footer, along with your RomM version, and is logged at startup.
- An **Update available** badge appears when a newer release exists (checked against GitHub at most every 6 hours).
- The release workflow stamps the exact release number into the image (`APP_VERSION`).

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
