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
| ✓ | — | none | `push_new`. Asks unless `PUSH_NEW_MODE=auto` |
| ✓ | — | exists | `push_new`, always asks (probably deleted on RomM) |
| — | ✓ | none | `pull_new`. Asks unless `PULL_MODE=auto`; skipped if `off` |
| — | ✓ | exists | `pull_new`, always asks (probably deleted locally) |
| ✓ | ✓ | any | same content hash → in sync (baseline recorded) |
| ✓ | ✓ | none | local newer → upload. RomM newer with a different hash → `pull_update`. Otherwise assume in sync. (This is the upgrade path from 1.x.) |
| ✓ | ✓ | exists | nothing changed → nothing to do |
| ✓ | ✓ | exists | only local changed → upload automatically |
| ✓ | ✓ | exists | only RomM changed → `pull_update` (`PULL_MODE`) |
| ✓ | ✓ | exists | both changed → **conflict**, always asks |

`FORCE_PUSH_SYNC=true` turns every differing pair into an upload, as before.

## Safety rails

- **Re-check before acting.** When you approve something, the engine re-reads the local file hash and RomM's `updated_at`. If either changed since the item was queued, the item is dropped as *stale* and re-evaluated on the next sync, instead of overwriting something you haven't seen.
- **Backups.** Every overwrite first copies the losing version to `/data/backups/YYYY-MM-DD/<system>/HHMMSS-{local|romm}-<file>`. If RomM's copy can't be downloaded for the backup, the upload is refused.
- **Atomic writes.** Downloads go to a hidden temp file and are renamed into place. Partial files never appear in the sync folder, so Syncthing never sees them.
- **No echo.** After a download the baseline holds the new hash. The file event that follows is recognised as "in sync" and not uploaded back.
- **Skips are remembered** (`dismissed` in the state file) until either copy changes.
- **Conflict copies** (`… (conflict 2026-10-23 120000).srm`) uploaded by **Keep both** are archive-only. They are never downloaded to devices.
- **Path safety.** Names coming from RomM are confined to `<sync folder>/<system folder>/`. The dashboard's image proxy only fetches RomM-relative paths and only serves images from inside the sync folders.

## When syncs run

- **watch mode:** local file events (debounced by `WATCHDOG_DELAY_SECONDS`) run an incremental sync for the affected games. A poller thread runs a full sync every `ROMM_POLL_INTERVAL_SECONDS` to catch RomM-side changes.
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
| Check before acting | `GET /api/{saves,states}/{id}` |
| Download | `GET /api/{saves,states}/{id}/content` (RomM 5.x), falling back to the item's `download_path` |
| Covers, screenshots, platform icons | `path_cover_small`, `merged_screenshots`, state `screenshot.download_path`, `/assets/platforms/<slug>.svg` |
