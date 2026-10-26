# Two-way sync: how decisions are made

*Added in the GUI fork. The original one-way design by amn-96 is described in [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md).*

## The baseline

The original app compared modification times: if the local file was newer than RomM's copy, it uploaded it.
That works one way, but it can't tell "RomM got newer because *we* uploaded" from "RomM got newer because another device uploaded".
Two-way sync needs that difference.

So after every successful transfer, `src/sync_state.py` stores a **baseline** for that file in `/data/romm_sync_state.json`:

```json
"save:31:Crystal Caverns (USA).srm": {
  "local_hash": "md5 of the file in the sync folder",
  "romm_id": 113,
  "romm_updated_at": "2026-10-20T18:00:00+00:00",
  "romm_hash": "md5 RomM reports (5.x saves only)",
  "synced_at": "..."
}
```

Files are keyed by type, RomM ROM id and file name, the same exact-name match the original app uses.

On the next sync each side is compared to its baseline:
- **local changed** = the MD5 of the local file differs from `local_hash`
- **RomM changed** = a different RomM id, a different `updated_at`, or (when available) a different content hash

## Decision table (`src/sync_engine.py → decide()`)

| Local | RomM | Baseline | Result |
|---|---|---|---|
| ✓ | — | none | `push_new`: automatic (asks only with `PUSH_NEW_MODE=confirm`) |
| ✓ | — | exists | deleted on RomM: local unchanged → `delete_local` (local copy moved to the recycle bin; automatic with `DELETE_MODE=auto`, asks with `ask`). Local changed, or `DELETE_MODE=off` → `push_new`, always asks |
| — | ✓ | none | `pull_new`: automatic (asks with `PULL_MODE=confirm`; skipped if `off`) |
| — | ✓ | exists | deleted locally: RomM unchanged → `delete_romm` (RomM copy downloaded into the recycle bin, then deleted on RomM). RomM changed, or `DELETE_MODE=off` → `pull_new`, always asks |
| ✓ | ✓ | any | same content hash → in sync (baseline recorded) |
| ✓ | ✓ | none | local newer → upload. RomM newer with a different hash → `pull_update`, **always asks** (no history, so it's unclear which copy has your progress). Otherwise assume in sync. (This is the upgrade path from 1.x.) |
| ✓ | ✓ | exists | nothing changed → nothing to do |
| ✓ | ✓ | exists | only local changed → upload automatically |
| ✓ | ✓ | exists | only RomM changed → download automatically (`PULL_MODE`) |
| ✓ | ✓ | exists | both changed → **conflict**, always asks |

`FORCE_PUSH_SYNC=true` turns every differing pair into an upload, as before.

## Errors

When a transfer fails, `explain_error()` turns the exception into a sentence you can act on: token permissions, read-only or full disk, RomM unreachable or timing out, file too large, missing game, or RomM server errors. It's shown on the dashboard's **Errors** tab and stays there until that file syncs successfully, you dismiss it, or you click **Retry all**.

## Safety rails

- **Re-check before acting.** When you approve something, the engine re-reads the local file hash and RomM's `updated_at`. If either changed since the item was queued, the item is dropped as *stale* and re-evaluated on the next sync, instead of overwriting something you haven't seen.
- **Backups.** Every overwrite first copies the losing version to `/data/backups/YYYY-MM-DD/<system>/HHMMSS-{local|romm}-<file>`. If RomM's copy can't be downloaded for the backup, the upload is refused.
- **Atomic writes.** Downloads go to a hidden temp file and are renamed into place. Partial files never appear in the sync folder, so Syncthing never sees them.
- **No echo.** After a download the baseline holds the new hash. The file event that follows is recognised as "in sync" and not uploaded back.
- **Recycle bin.** A delete carried to the other side keeps a copy in `/data/recycle/<id>/` first; if RomM's copy can't be downloaded, it isn't deleted. Items are cleared after `RECYCLE_RETENTION_DAYS`.
- **Mass-delete guard.** More than 10 deletions in one sync are queued for approval instead of carried out.
- **Skips are remembered** (`dismissed` in the state file) until either copy changes.
- **Conflict copies** (`… (conflict 2026-10-23 120000).srm`) uploaded by **Keep both** are archive-only. They are never downloaded to devices.
- **Path safety.** Names coming from RomM are confined to `<sync folder>/<system folder>/`. The dashboard's image proxy only fetches RomM-relative paths and only serves images from inside the sync folders.

## When syncs run

- **watch mode:** local file events (debounced by `WATCHDOG_DELAY_SECONDS`) run an incremental sync for the affected games. Every `ROMM_POLL_INTERVAL_SECONDS` a poller fetches RomM's save/state list (`id` + `updated_at`) and compares it with the previous check, ignoring files this app uploaded, replaced or deleted itself. Only if something else changed does it sync, and then only the games those RomM files belong to (plus RomM-only files for those ROMs), so the rest of the sync folders (and the disks under them) are left alone. A file this app wrote counts as its own only while RomM still has the exact version it wrote; if another device changes it afterwards, that change is picked up. A full sync also runs every `FULL_SCAN_INTERVAL_HOURS` (default 24, `0` = never) as a safety net.
- **periodic mode:** a full sync every `SYNC_INTERVAL_SECONDS`.
- **Sync now** in the dashboard, and after each approval, a full sync runs right away.

All syncs and approvals share one lock, so they never run at the same time.

A full sync reads the RomM library (paginated), then **all** saves and **all** states in one request each (`GET /api/saves`, `GET /api/states`), and hashes local files. Hashes are cached by path, size and mtime.

## RomM API used

| Purpose | Endpoint |
|---|---|
| Library | `GET /api/roms?limit=&offset=` |
| List saves / states | `GET /api/saves`, `GET /api/states` |
| Upload new | `POST /api/{saves,states}?rom_id=` (multipart `saveFile`/`stateFile`, optional `screenshotFile`) |
| Replace | `PUT /api/{saves,states}/{id}` |
| Delete | `POST /api/{saves,states}/delete` (body `{"saves": [ids]}` / `{"states": [ids]}`) |
| Check before acting | `GET /api/{saves,states}/{id}` |
| Download | `GET /api/{saves,states}/{id}/content` (RomM 5.x), falling back to the item's `download_path` |
| Covers, screenshots, platform icons | `path_cover_small`, `merged_screenshots`, state `screenshot.download_path`, `/assets/platforms/<slug>.svg` |
