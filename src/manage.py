"""Saves page: every save and state in the sync folders and on RomM in one list, with bulk actions (GUI fork).

inventory() builds the list from the app's in-memory libraries plus RomM's save/state lists. It reads the
sync folders only for file sizes/fingerprints (cached), and only when someone opens the page.

Bulk actions run as one background job (one at a time) under the sync lock:
- delete:   remove from the sync folder AND RomM (all RomM versions). Copies go to the recycle bin first.
- prune:    delete older RomM versions of a file, keeping the newest (RomM 5.x keeps one per upload).
- download: copy RomM-only files into the sync folder.
- upload:   upload folder-only files to RomM.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from .sync_state import STATE, PendingAction, utcnow_iso

_lock = threading.Lock()
_rows: Dict[str, dict] = {}        # id -> row with private fields (_local, _rf, _items)
_job: dict = {"running": False}


def _rid(platform: str, ft: str, rom_id, name: str) -> str:
    return hashlib.sha1(f"{platform}|{ft}|{rom_id}|{name}".encode()).hexdigest()[:16]


def _dt(v) -> datetime:
    from .sync_engine import parse_dt
    return parse_dt(v) or datetime.min.replace(tzinfo=timezone.utc)


def inventory() -> List[dict]:
    """All saves/states: one row per file name per game, showing where it exists."""
    from .sync_engine import SyncEngine, file_key, md5_file  # noqa: F401
    from .library_classes import LibraryScanner
    orch = STATE.orchestrator
    if orch is None or orch.lcl is None:
        raise RuntimeError("The app is still starting; try again once the first sync has finished.")
    eng = SyncEngine()
    remote = eng.fetch_remote()
    items_by_id = {(it.get("_api"), it["id"]): it for ft in remote for lst in remote[ft].values() for it in lst}
    srv, lcl = orch.srv, orch.lcl
    rows_by_rom = {r["id"]: r for r in (srv.library if srv else [])}
    names_by_platform: Dict[str, set] = {}
    for g in lcl.games.values():
        names_by_platform.setdefault(g.platform.lower(), set()).add(g.name)

    out: Dict[str, dict] = {}
    seen_roms = set()
    taken_remote = set()  # (api, id) already listed under a local game

    def add_row(game_name, platform, rom_id, rom_row, ft, name, lf, rf, local_dest: Optional[Path]):
        row_id = _rid(platform, ft, rom_id, name)
        rom_row = rom_row or {}
        key = file_key(ft, rom_id, name) if rom_id else None
        status = "folder" if rf is None else ("romm" if lf is None else "both")
        if status == "both":
            b = STATE.get_baseline(key) or {}
            same = (b.get("local_hash") == lf.hash and b.get("romm_id") == rf.get("id")) or \
                   (rf.get("content_hash") and rf.get("content_hash") == lf.hash)
            status = "both" if same else "different"
        group = [tuple(x) for x in (rf or {}).get("_group", [])] if rf else []
        shot = (rf or {}).get("screenshot")
        out[row_id] = {
            "id": row_id, "game": game_name, "platform": platform,
            "system": rom_row.get("platform_display_name") or platform,
            "rom_id": rom_id, "matched": bool(rom_id), "type": ft, "name": name, "status": status,
            "local": None if lf is None else {"size": lf.size, "mtime": lf.mtime.isoformat(),
                                               "screenshot": str(lf.screenshot) if lf.screenshot else None},
            "romm": None if rf is None else {"id": rf.get("id"), "size": rf.get("file_size_bytes"),
                                             "updated_at": rf.get("updated_at"), "versions": max(len(group), 1),
                                             "screenshot": shot.get("download_path") if isinstance(shot, dict) else None},
            "can_download": rf is not None and lf is None and local_dest is not None and not local_dest.exists(),
            "can_upload": lf is not None and rf is None and bool(rom_id),
            "_lf": lf, "_rf": rf, "_group": group, "_dest": local_dest, "_key": key,
        }

    for g in lcl.games.values():
        local = eng.local_files(g)
        rbt = eng._remote_for_rom(remote, g.romm_id, g.romm_row) if g.is_matched and g.romm_id else {"save": {}, "state": {}}
        if g.is_matched:
            seen_roms.add(g.romm_id)
        others = names_by_platform.get(g.platform.lower(), set()) - {g.name}
        names = set(local) | {(ft, n) for ft in ("save", "state") for n in rbt[ft]}
        for ft, name in sorted(names):
            lf = local.get((ft, name))
            rf = rbt[ft].get(name)
            if rf is not None and lf is None and LibraryScanner.parse_save_filename(name)[0] in others:
                continue  # belongs to another local game (listed there)
            if rf is not None:
                if (rf.get("_api"), rf["id"]) in taken_remote:
                    rf = None  # second folder pointing at the same RomM platform
                    if lf is None:
                        continue
                else:
                    taken_remote.add((rf.get("_api"), rf["id"]))
            root = eng.cfg.sync_dir_for(ft)
            dest = (root / g.platform / name) if root is not None else None
            add_row(g.name, g.platform, g.romm_id if g.is_matched else None, g.romm_row, ft, name, lf, rf, dest)

    # RomM files for games that have nothing in the sync folders
    folders = {}
    for ft in ("save", "state"):
        root = eng.cfg.sync_dir_for(ft)
        try:
            folders[ft] = [p for p in root.iterdir() if p.is_dir()] if root is not None and root.exists() else []
        except OSError:
            folders[ft] = []
    for rom_id in sorted(set(remote["save"]) | set(remote["state"])):
        if rom_id in seen_roms or rom_id not in rows_by_rom:
            continue
        row = rows_by_rom[rom_id]
        files = eng._remote_for_rom(remote, rom_id, row)
        game_name = row.get("fs_name_no_ext") or Path(row.get("fs_name") or str(rom_id)).stem
        for ft in ("save", "state"):
            folder = next((f for f in folders[ft] if srv.local_folder_matches_slug(f.name, row["platform_slug"])), None)
            for name, rf in files[ft].items():
                if (rf.get("_api"), rf["id"]) in taken_remote:
                    continue
                taken_remote.add((rf.get("_api"), rf["id"]))
                add_row(game_name, folder.name if folder else row.get("platform_slug"), rom_id, row, ft, name,
                        None, rf, (folder / name) if folder else None)

    with _lock:
        _rows.clear()
        _rows.update(out)
        for r in out.values():
            r["_items"] = items_by_id
    return [public(r) for r in sorted(out.values(), key=lambda r: (r["system"].lower(), r["game"].lower(),
                                                                    r["type"], r["name"].lower()))]


def public(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def job_status() -> dict:
    return dict(_job)


def start(action: str, ids: List[str]) -> dict:
    if action not in ("delete", "prune", "download", "upload"):
        raise ValueError("Unknown action")
    with _lock:
        if _job.get("running"):
            raise RuntimeError("Another bulk action is still running.")
        rows = [_rows[i] for i in ids if i in _rows]
        if not rows:
            raise ValueError("Nothing selected (the list may be out of date; reload it).")
        _job.clear()
        _job.update({"running": True, "action": action, "total": len(rows), "done": 0, "ok": 0, "failed": 0,
                     "errors": [], "started_at": utcnow_iso()})
    threading.Thread(target=_run, args=(action, rows), name="saves-bulk", daemon=True).start()
    return job_status()


def _run(action: str, rows: List[dict]):
    from .sync_engine import SyncEngine
    eng = SyncEngine()
    label = {"delete": "Deleting", "prune": "Removing old RomM versions of", "download": "Downloading",
             "upload": "Uploading"}[action]
    with STATE.sync_lock:
        STATE.set_phase("syncing", f"{label} {len(rows)} file(s) (Saves page)", total=len(rows))
        for r in rows:
            STATE.tick(r["game"])
            try:
                fn = {"delete": _delete, "prune": _prune, "download": _download, "upload": _upload}[action]
                ok, msg = fn(eng, r)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)
                logger.error(f"✘ {action} {r['name']} failed: {e}")
            with _lock:
                _job["done"] += 1
                _job["ok" if ok else "failed"] += 1
                if not ok:
                    _job["errors"].append({"game": r["game"], "name": r["name"], "message": msg})
        STATE.set_phase("idle", "")
        STATE.save()
    with _lock:
        _job["running"] = False
        _job["finished_at"] = utcnow_iso()
        summary = f"{_job['ok']} done, {_job['failed']} failed"
    STATE.add_activity("INFO", f"Saves page: {label.lower()} {len(rows)} file(s): {summary}")


def _action(r: dict, kind: str) -> PendingAction:
    lf, rf = r["_lf"], r["_rf"]
    shot = (rf or {}).get("screenshot") or {}
    return PendingAction(
        kind=kind, file_type=r["type"], game=r["game"], platform=r["platform"], rom_id=r["rom_id"],
        file_name=r["name"],
        local_path=str(lf.path) if lf else None, local_hash=lf.hash if lf else None,
        local_size=lf.size if lf else None, local_mtime=lf.mtime.isoformat() if lf else None,
        local_screenshot=str(lf.screenshot) if lf and lf.screenshot else None,
        romm_id=rf.get("id") if rf else None, romm_file_name=rf.get("file_name") if rf else None,
        romm_size=rf.get("file_size_bytes") if rf else None, romm_updated_at=rf.get("updated_at") if rf else None,
        romm_hash=rf.get("content_hash") if rf else None,
        romm_screenshot=shot.get("download_path") if isinstance(shot, dict) else None,
        romm_api=rf.get("_api") if rf else None, romm_group=[list(x) for x in r["_group"]] or None,
        reason="Chosen on the Saves page")


def _shot_bytes(eng, item: dict) -> Optional[bytes]:
    s = item.get("screenshot")
    if isinstance(s, dict) and s.get("download_path"):
        try:
            resp = eng.romm.get_raw(s["download_path"])
            return resp.content if resp.ok else None
        except Exception:  # noqa: BLE001
            return None
    return None


def _forget(key: Optional[str]):
    if not key:
        return
    with STATE.lock:
        STATE.baselines.pop(key, None)
    STATE.drop_pending_for_key(key)
    STATE.clear_error(key)


def _delete(eng, r: dict):
    """Folder + RomM -> recycle bin. Nothing is removed until its copy is safely in the bin."""
    from . import recycle
    from .sync_engine import KIND_API, md5_file
    lf, rf, a = r["_lf"], r["_rf"], _action(r, "manage_delete")
    note = "Deleted from the Saves page"
    if eng.cfg.DRY_RUN:
        return True, f"[dry run] would delete {r['name']}"
    path = Path(lf.path) if lf else None
    if path is not None and not path.exists():
        path = None
    local_hash = md5_file(path) if path else None
    restore_to = str(path or r["_dest"] or Path(r["platform"]) / r["name"])
    if path:
        png = path.with_name(path.name + ".png")
        recycle.add(file_name=path.name, file_type=r["type"], game=r["game"], platform=r["platform"],
                    rom_id=r["rom_id"], deleted_from="local", reason=note, content=path.read_bytes(),
                    screenshot=png.read_bytes() if png.exists() else None, restore_path=restore_to,
                    file_mode=eng.cfg.FILE_MODE, note=note + (" (folder and RomM)" if rf else " (folder)"))
    if rf is not None:
        api = rf.get("_api") or KIND_API[r["type"]]
        content = eng.romm.download_asset(api, rf)
        if hashlib.md5(content).hexdigest() != local_hash:  # only keep RomM's copy too when it's different
            recycle.add(file_name=r["name"], file_type=r["type"], game=r["game"], platform=r["platform"],
                        rom_id=r["rom_id"], deleted_from="romm", reason=note, content=content,
                        screenshot=_shot_bytes(eng, rf), restore_path=restore_to, file_mode=eng.cfg.FILE_MODE,
                        note=note + (" (RomM's copy, which was different)" if path else " (RomM)"))
        by_api: Dict[str, list] = {}
        for g_api, g_id in (r["_group"] or [(api, rf["id"])]):
            by_api.setdefault(g_api or api, []).append(g_id)
        for g_api, ids in by_api.items():
            eng.romm.delete_assets(g_api, ids)
    if path:
        path.unlink()
        png = path.with_name(path.name + ".png")
        if png.exists():
            png.unlink()
    _forget(r["_key"])
    where = "your sync folder and RomM" if (path and rf) else ("RomM" if rf else "your sync folder")
    msg = f"Moved {r['type']} {r['name']} from {where} to the recycle bin"
    logger.info(f"✔ {msg}")
    STATE.finish_action(a, "done", msg)
    return True, msg


def _prune(eng, r: dict):
    """Keep the newest RomM version, move the older ones to the recycle bin."""
    from . import recycle
    rf = r["_rf"]
    if rf is None or len(r["_group"]) < 2:
        return True, "Only one version on RomM"
    keep = (rf.get("_api"), rf["id"])
    old = [tuple(x) for x in r["_group"] if tuple(x) != keep]
    if eng.cfg.DRY_RUN:
        return True, f"[dry run] would remove {len(old)} older version(s) of {r['name']}"
    items = r.get("_items") or {}
    by_api: Dict[str, list] = {}
    for api, i in old:
        item = items.get((api, i)) or {"id": i}
        content = eng.romm.download_asset(api, item)
        when = item.get("updated_at", "")[:16].replace("T", " ")
        recycle.add(file_name=r["name"], file_type=r["type"], game=r["game"], platform=r["platform"],
                    rom_id=r["rom_id"], deleted_from="romm", reason="Older RomM version", content=content,
                    screenshot=_shot_bytes(eng, item), file_mode=eng.cfg.FILE_MODE,
                    restore_path=str(r["_dest"] or Path(r["platform"]) / r["name"]),
                    note=f"Older RomM version{(' from ' + when) if when else ''} (Saves page)")
        by_api.setdefault(api, []).append(i)
    for api, ids in by_api.items():
        eng.romm.delete_assets(api, ids)
    a = _action(r, "manage_prune")
    msg = f"Removed {len(old)} older RomM version(s) of {r['name']} (kept the newest)"
    logger.info(f"✔ {msg}")
    STATE.finish_action(a, "done", msg)
    return True, msg


def _download(eng, r: dict):
    if not r["can_download"]:
        return False, "Not on RomM only, or there's no sync folder for this system."
    a = _action(r, "pull_new")
    a.platform = r["_dest"].parent.name
    return eng.execute(a)


def _upload(eng, r: dict):
    if not r["can_upload"]:
        return False, "Only files in your sync folder that are matched to a RomM game can be uploaded."
    return eng.execute(_action(r, "push_new"))
