"""Optional Syncthing integration (GUI fork).

With SYNCTHING_URL + SYNCTHING_API_KEY set (Settings page), the app
- waits for Syncthing to finish syncing the save folders before it syncs a changed file, so it never uploads a
  half-received save, and
- shows which Syncthing devices are connected and how up to date they are on the dashboard.

Only Syncthing's REST API is used (read-only): /rest/system/status, /rest/system/connections,
/rest/config/folders, /rest/config/devices, /rest/db/status, /rest/db/completion.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests
from loguru import logger

from .config import get_config

_cache: Dict[str, tuple] = {}


def configured() -> bool:
    cfg = get_config()
    return bool(getattr(cfg, "SYNCTHING_URL", "") and getattr(cfg, "SYNCTHING_API_KEY", ""))


def folder_ids() -> List[str]:
    return [f.strip() for f in (getattr(get_config(), "SYNCTHING_FOLDERS", "") or "").split(",") if f.strip()]


def _get(path: str, params: Optional[dict] = None, ttl: float = 0):
    cfg = get_config()
    key = path + repr(sorted((params or {}).items()))
    hit = _cache.get(key)
    if ttl and hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    r = requests.get(cfg.SYNCTHING_URL.rstrip("/") + path, params=params, timeout=5,
                     headers={"X-API-Key": cfg.SYNCTHING_API_KEY})
    if r.status_code in (401, 403):
        raise RuntimeError("Syncthing rejected the API key.")
    r.raise_for_status()
    data = r.json()
    _cache[key] = (time.monotonic(), data)
    return data


def folders() -> List[dict]:
    """All Syncthing folders (for the Settings picker)."""
    return [{"id": f["id"], "label": f.get("label") or f["id"], "path": f.get("path", "")}
            for f in _get("/rest/config/folders")]


def busy() -> Optional[str]:
    """A short reason if one of the save folders is still receiving files, else None. Never raises."""
    if not configured() or not folder_ids():
        return None
    try:
        for fid in folder_ids():
            st = _get("/rest/db/status", {"folder": fid})
            state = st.get("state", "idle")
            if state in ("syncing", "sync-preparing", "scanning", "sync-waiting", "scan-waiting") or st.get("needFiles"):
                return f"Syncthing is still {state.replace('-', ' ')} folder '{fid}'"
    except Exception as e:  # noqa: BLE001 - Syncthing being down must never stop syncing
        logger.debug(f"Syncthing status unavailable: {e}")
    return None


def overview() -> dict:
    """Devices sharing the save folders, whether they're connected, and how complete they are."""
    if not configured():
        return {"configured": False}
    try:
        me = _get("/rest/system/status", ttl=300).get("myID")
        conns = _get("/rest/system/connections", ttl=10).get("connections", {})
        dev_names = {d["deviceID"]: d.get("name") or d["deviceID"][:7] for d in _get("/rest/config/devices", ttl=60)}
        cfg_folders = {f["id"]: f for f in _get("/rest/config/folders", ttl=60)}
        ids = folder_ids() or list(cfg_folders)
        flds, devices = [], {}
        for fid in ids:
            f = cfg_folders.get(fid)
            if f is None:
                flds.append({"id": fid, "label": fid, "state": "missing"})
                continue
            st = _get("/rest/db/status", {"folder": fid}, ttl=10)
            flds.append({"id": fid, "label": f.get("label") or fid, "state": st.get("state"),
                         "need_files": st.get("needFiles", 0)})
            for d in f.get("devices", []):
                did = d["deviceID"]
                if did == me:
                    continue
                c = _get("/rest/db/completion", {"folder": fid, "device": did}, ttl=30)
                dev = devices.setdefault(did, {"id": did, "name": dev_names.get(did, did[:7]),
                                                "connected": bool(conns.get(did, {}).get("connected")),
                                                "completion": [], "last_seen": conns.get(did, {}).get("startedAt")})
                dev["completion"].append(c.get("completion", 0))
        devs = []
        for d in devices.values():
            d["completion"] = round(min(d["completion"]), 1) if d["completion"] else None
            devs.append(d)
        devs.sort(key=lambda d: (not d["connected"], d["name"].lower()))
        return {"configured": True, "folders": flds, "devices": devs, "picked": bool(folder_ids())}
    except Exception as e:  # noqa: BLE001
        return {"configured": True, "error": str(e)}
