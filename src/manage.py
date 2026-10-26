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

    def add_row(game_name, platform, rom_id, rom_row, ft, name, lf, rf, local_dest: Optional[Path],
                match_how: Optional[str] = None, local_game: bool = False):
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
            "match_how": match_how, "local_game": local_game,
            "romm_name": rom_row.get("name"), "romm_file": rom_row.get("fs_name"),
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
            add_row(g.name, g.platform, g.romm_id if g.is_matched else None, g.romm_row, ft, name, lf, rf, dest,
                    getattr(g, "match_how", None) if g.is_matched else None, True)

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
                from .sync_engine import explain_error
                ok, msg = False, explain_error(e, _action(r, "push_new" if action in ("upload", "delete", "prune")
                                                          else "pull_new"))
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
    _bin_versions(eng, [(api, items.get((api, i)) or {"id": i}) for api, i in old], r["name"], r["type"], r["game"],
                  r["platform"], r["rom_id"], str(r["_dest"] or _fallback_dest(eng, r["type"], r["platform"], r["name"])),
                  "Saves page")
    a = _action(r, "manage_prune")
    msg = f"Removed {len(old)} older RomM version(s) of {r['name']} (kept the newest)"
    logger.info(f"✔ {msg}")
    STATE.finish_action(a, "done", msg)
    return True, msg


def _fallback_dest(eng, ft: str, platform: str, name: str) -> Path:
    root = eng.cfg.sync_dir_for(ft)
    return (root / platform / name) if root is not None else Path(platform) / name


def _bin_versions(eng, olds, name, ft, game, platform, rom_id, restore_path: str, source: str):
    """Copy each (api, item) into the recycle bin, then delete them on RomM."""
    from . import recycle
    by_api: Dict[str, list] = {}
    for api, item in olds:
        content = eng.romm.download_asset(api, item)
        when = (item.get("updated_at") or "")[:16].replace("T", " ")
        recycle.add(file_name=name, file_type=ft, game=game, platform=platform, rom_id=rom_id, deleted_from="romm",
                    reason="Older RomM version", content=content, screenshot=_shot_bytes(eng, item),
                    file_mode=eng.cfg.FILE_MODE, restore_path=restore_path,
                    note=f"Older RomM version{(' from ' + when) if when else ''} ({source})")
        by_api.setdefault(api, []).append(item["id"])
    for api, ids in by_api.items():
        eng.romm.delete_assets(api, ids)


def auto_prune(srv, lcl, keep: int, rom_ids: Optional[set] = None) -> int:
    """Keep the newest `keep` RomM versions of every file (plus the one the sync is paired with); move older ones
    to the recycle bin. Uses RomM's lists only; doesn't read the sync folders."""
    from .sync_engine import SyncEngine, file_key, local_name_for_remote
    if not keep or keep < 1 or srv is None:
        return 0
    eng = SyncEngine()
    if eng.cfg.DRY_RUN:
        return 0
    remote = eng.fetch_remote()
    rows = {r["id"]: r for r in srv.library}
    local_platform = {g.romm_id: g.platform for g in (lcl.games.values() if lcl else []) if g.is_matched}
    removed = 0
    for ft_list in ("save", "state"):
        for rom_id, items in remote[ft_list].items():
            if rom_id not in rows or (rom_ids is not None and rom_id not in rom_ids):
                continue
            row = rows[rom_id]
            groups: Dict[str, list] = {}
            for it in items:
                groups.setdefault(local_name_for_remote(it, row), []).append(it)
            for name, its in groups.items():
                if len(its) <= keep:
                    continue
                from .library_classes import LibraryScanner
                ft = "state" if LibraryScanner.parse_save_filename(name)[1] in ("state", "state_screen") else "save"
                its.sort(key=lambda it: _dt(it.get("updated_at")), reverse=True)
                paired = (STATE.get_baseline(file_key(ft, rom_id, name)) or {}).get("romm_id")
                olds = [it for it in its[keep:] if it["id"] != paired]
                if not olds:
                    continue
                platform = local_platform.get(rom_id) or row.get("platform_slug")
                game = row.get("fs_name_no_ext") or row.get("name") or str(rom_id)
                try:
                    _bin_versions(eng, [(it["_api"], it) for it in olds], name, ft, game, platform, rom_id,
                                  str(_fallback_dest(eng, ft, platform, name)), f"kept the newest {keep}")
                    removed += len(olds)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Couldn't remove old RomM versions of {name}: {e}")
    if removed:
        logger.info(f"Moved {removed} old RomM version(s) to the recycle bin (keeping the newest {keep} per file).")
        STATE.add_activity("INFO", f"{removed} old RomM version(s) moved to the recycle bin")
    return removed


# region manual links
def search_roms(folder: str, q: str, all_platforms: bool = False, limit: int = 40) -> List[dict]:
    orch = STATE.orchestrator
    if orch is None or orch.srv is None:
        raise RuntimeError("The app is still starting.")
    srv = orch.srv
    slug = srv.get_romm_platform(folder)
    words = q.casefold().split()
    used = {g.romm_id: f"{g.name} ({g.platform})" for g in orch.lcl.games.values() if g.is_matched}
    out = []
    for r in srv.library:
        if not all_platforms and slug and r.get("platform_slug") != slug:
            continue
        hay = f"{r.get('name', '')} {r.get('fs_name', '')}".casefold()
        if all(w in hay for w in words):
            out.append({"id": r["id"], "name": r.get("name"), "fs_name": r.get("fs_name"),
                        "platform": r.get("platform_display_name") or r.get("platform_slug"),
                        "used_by": used.get(r["id"])})
            if len(out) >= limit:
                break
    return out


def link_game(folder: str, game_name: str, rom_id: Optional[int]) -> str:
    """Link a local game to a RomM ROM by hand (rom_id None = remove the link and match automatically)."""
    from . import links
    from .library_classes import LibraryScanner
    orch = STATE.orchestrator
    if orch is None or orch.lcl is None or orch.srv is None:
        raise RuntimeError("The app is still starting.")
    game = next((g for g in orch.lcl.games.values()
                 if g.platform.lower() == folder.lower() and g.name == game_name), None)
    if game is None:
        raise ValueError("That game isn't in your sync folders any more.")
    rows = {r["id"]: r for r in orch.srv.library}
    if rom_id is not None and rom_id not in rows:
        raise ValueError("That ROM isn't in your RomM library.")
    other = next((g for g in orch.lcl.games.values() if g is not game and g.is_matched and g.romm_id == rom_id), None)
    if rom_id is not None and other is not None:
        raise ValueError(f"That ROM already syncs with '{other.name}' in the '{other.platform}' folder. Two local "
                         "games can't share one RomM game, because their saves would overwrite each other.")
    links.set_link(folder, game_name, rom_id)
    with STATE.sync_lock:
        if game.romm_row:
            game.romm_row.pop("_local_stem", None)
        game.is_matched, game.romm_id, game.romm_row, game.match_how = False, None, None, None
        if rom_id is not None:
            LibraryScanner._set_match(game, rows[rom_id], "link")
        else:
            taken = {g.romm_id for g in orch.lcl.games.values() if g.is_matched and g is not game}
            LibraryScanner.match_to_romm([game], orch.srv, taken=taken)
    if rom_id is not None:
        msg = f"Linked '{game_name}' to {rows[rom_id].get('name')} ({rows[rom_id].get('fs_name')})"
    else:
        msg = f"Removed the link for '{game_name}'" + (f"; it now matches {game.romm_row.get('fs_name')}"
                                                         if game.is_matched else "; it isn't matched to a RomM game")
    STATE.add_activity("INFO", msg)
    logger.info(msg)
    return msg
# endregion


# region version history
def _row(row_id: str) -> dict:
    with _lock:
        r = _rows.get(row_id)
    if r is None:
        raise ValueError("This file isn't in the list any more; reload the Saves page.")
    return r


def versions(row_id: str) -> List[dict]:
    """Every copy of a file the app knows about: the folder copy, each RomM version, backups and recycle bin."""
    from . import recycle
    from .sync_state import data_dir
    r = _row(row_id)
    out = []
    lf = r["_lf"]
    if lf is not None and Path(lf.path).exists():
        st = Path(lf.path).stat()
        out.append({"source": "local", "ref": "", "when": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                    "size": st.st_size, "label": "In your sync folder", "current": True})
    rf = r["_rf"]
    items = r.get("_items") or {}
    paired = (STATE.get_baseline(r["_key"]) or {}).get("romm_id") if r["_key"] else None
    for api, i in (r["_group"] or ([(rf.get("_api"), rf["id"])] if rf else [])):
        it = items.get((api, i)) or {}
        latest = rf is not None and i == rf["id"]
        out.append({"source": "romm", "ref": f"{api}:{i}", "when": it.get("updated_at"), "size": it.get("file_size_bytes"),
                    "label": "On RomM" + (" (newest)" if latest else ""),
                    "current": latest and lf is None, "paired": i == paired})
    bdir = data_dir() / "backups"
    if bdir.exists():
        for day in bdir.iterdir():
            folder = day / r["platform"]
            if not folder.is_dir():
                continue
            for f in folder.iterdir():
                parts = f.name.split("-", 2)
                if len(parts) == 3 and parts[2] == r["name"] and parts[1] in ("local", "romm"):
                    try:
                        when = datetime.strptime(day.name + parts[0], "%Y-%m-%d%H%M%S").astimezone(timezone.utc).isoformat()
                    except ValueError:
                        when = None
                    out.append({"source": "backup", "ref": str(f.relative_to(bdir)), "when": when,
                                "size": f.stat().st_size,
                                "label": f"Backup of the {'folder' if parts[1] == 'local' else 'RomM'} copy before it was overwritten"})
    for e in recycle.entries():
        if e["file_name"] == r["name"] and (e["platform"] or "").lower() == r["platform"].lower():
            out.append({"source": "bin", "ref": e["id"], "when": e["deleted_at"], "size": e["size"],
                        "label": "Recycle bin: " + (e.get("note") or e.get("reason") or "deleted")})
    out.sort(key=lambda v: _dt(v.get("when")), reverse=True)
    return out


def version_content(row_id: str, source: str, ref: str) -> bytes:
    from . import recycle
    from .sync_engine import SyncEngine
    from .sync_state import data_dir
    r = _row(row_id)
    if source == "local":
        if r["_lf"] is None:
            raise ValueError("There's no copy in your sync folder.")
        return Path(r["_lf"].path).read_bytes()
    if source == "romm":
        api, _, i = ref.partition(":")
        if (api, int(i)) not in {tuple(x) for x in r["_group"]} | ({(r["_rf"].get("_api"), r["_rf"]["id"])} if r["_rf"] else set()):
            raise ValueError("That RomM version doesn't belong to this file.")
        item = (r.get("_items") or {}).get((api, int(i))) or {"id": int(i)}
        return SyncEngine().romm.download_asset(api, item)
    if source == "backup":
        base = (data_dir() / "backups").resolve()
        p = (base / ref).resolve()
        if base not in p.parents or not p.is_file():
            raise ValueError("Backup not found.")
        return p.read_bytes()
    if source == "bin":
        e = recycle.get(ref)
        if e is None:
            raise ValueError("That item is no longer in the recycle bin.")
        return recycle.path_of(e).read_bytes()
    raise ValueError("Unknown version source.")


def restore_version(row_id: str, source: str, ref: str) -> str:
    """Put an older copy into the sync folder. The current folder copy is backed up first; the next sync uploads
    the restored copy to RomM (it's the newest change)."""
    from .sync_engine import SyncEngine
    r = _row(row_id)
    if source == "local":
        raise ValueError("That's already the copy in your folder.")
    eng = SyncEngine()
    lf = r["_lf"]
    dest = Path(lf.path) if lf is not None else r["_dest"]
    if dest is None:
        raise ValueError("There's no sync folder for this system, so it can't be put back.")
    content = version_content(row_id, source, ref)
    if eng.cfg.DRY_RUN:
        return f"[dry run] would put this version of {r['name']} back into your sync folder"
    with STATE.sync_lock:
        if dest.exists():
            eng._backup_local(dest, r["platform"])
        eng._write_local(dest, content, datetime.now(timezone.utc))
    msg = f"Put an older version of {r['name']} back into your sync folder; it will be uploaded to RomM on the next sync"
    a = _action(r, "manage_restore")
    STATE.finish_action(a, "done", msg)
    logger.info(f"✔ {msg}")
    return msg
# endregion


# region zip export
def build_zip(ids: List[str]):
    """A zip of the selected files laid out like the sync folders (<system>/<file>). Folder copy if there is
    one, otherwise RomM's newest version. Returns (temp file, number of files, problems)."""
    import tempfile
    import zipfile
    from .sync_engine import SyncEngine, KIND_API
    eng = SyncEngine()
    with _lock:
        rows = [_rows[i] for i in ids if i in _rows]
    if not rows:
        raise ValueError("Nothing selected (the list may be out of date; reload it).")
    tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    n, problems, used = 0, [], set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            sub_dir = "saves" if r["type"] == "save" else "states"
            arc = f"{sub_dir}/{r['platform']}/{r['name']}"
            k = 2
            while arc in used:
                arc = f"{sub_dir}/{r['platform']}/{Path(r['name']).stem} ({k}){Path(r['name']).suffix}"
                k += 1
            used.add(arc)
            try:
                lf = r["_lf"]
                if lf is not None and Path(lf.path).exists():
                    z.write(lf.path, arc)
                    png = Path(lf.path).with_name(Path(lf.path).name + ".png")
                    if png.exists():
                        z.write(png, arc + ".png")
                elif r["_rf"] is not None:
                    z.writestr(arc, eng.romm.download_asset(r["_rf"].get("_api") or KIND_API[r["type"]], r["_rf"]))
                else:
                    continue
                n += 1
            except Exception as e:  # noqa: BLE001
                problems.append(f"{r['name']}: {e}")
        if problems:
            z.writestr("PROBLEMS.txt", "These files couldn't be added:\n" + "\n".join(problems))
    tmp.seek(0)
    return tmp, n, problems
# endregion


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
