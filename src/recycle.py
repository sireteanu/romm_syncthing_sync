"""Recycle bin for deletions carried between RomM and the sync folders (GUI fork).

When a synced file is deleted on one side, the copy on the other side isn't destroyed: it's moved here
(DATA_DIR/recycle/<id>/<file>), together with a small record of where it came from, and kept for
RECYCLE_RETENTION_DAYS (0 = forever). From the dashboard you can restore it (put back into the sync
folder; the normal sync then uploads it to RomM again), download it, or delete it forever.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from loguru import logger

from .sync_state import data_dir, utcnow_iso

_lock = threading.RLock()


def root() -> Path:
    d = data_dir() / "recycle"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_file() -> Path:
    return root() / "index.json"


def _load() -> List[dict]:
    try:
        return json.loads(_index_file().read_text())
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"Recycle bin index unreadable ({e}); starting a new one.")
        return []


def _save(entries: List[dict]):
    tmp = _index_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1))
    os.replace(tmp, _index_file())


def add(*, file_name: str, file_type: str, game: str, platform: str, rom_id: Optional[int],
        deleted_from: str, reason: str, content: bytes, restore_path: str,
        screenshot: Optional[bytes] = None, file_mode: int = 0o777, note: str = "") -> dict:
    """Store a copy. deleted_from = which side we removed it from ("romm" or "local")."""
    with _lock:
        eid = uuid.uuid4().hex[:12]
        d = root() / eid
        d.mkdir(parents=True, exist_ok=True)
        (d / file_name).write_bytes(content)
        if screenshot:
            (d / (file_name + ".png")).write_bytes(screenshot)
        for p in d.iterdir():
            try:
                os.chmod(p, file_mode)
            except OSError:
                pass
        entry = {"id": eid, "file_name": file_name, "file_type": file_type, "game": game, "platform": platform,
                 "rom_id": rom_id, "deleted_from": deleted_from, "reason": reason, "deleted_at": utcnow_iso(),
                 "size": len(content), "has_screenshot": bool(screenshot), "restore_path": restore_path,
                 "note": note}
        entries = _load()
        entries.append(entry)
        _save(entries)
        return entry


def entries() -> List[dict]:
    with _lock:
        return sorted(_load(), key=lambda e: e["deleted_at"], reverse=True)


def get(eid: str) -> Optional[dict]:
    return next((e for e in entries() if e["id"] == eid), None)


def path_of(entry: dict) -> Path:
    return root() / entry["id"] / entry["file_name"]


def remove(eid: str) -> bool:
    """Delete forever."""
    with _lock:
        items = _load()
        keep = [e for e in items if e["id"] != eid]
        if len(keep) == len(items):
            return False
        shutil.rmtree(root() / eid, ignore_errors=True)
        _save(keep)
        return True


def empty() -> int:
    with _lock:
        items = _load()
        for e in items:
            shutil.rmtree(root() / e["id"], ignore_errors=True)
        _save([])
        return len(items)


def purge(days: int) -> int:
    """Delete entries older than `days` (0 = keep forever)."""
    if not days or days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with _lock:
        items = _load()
        keep, gone = [], 0
        for e in items:
            try:
                old = datetime.fromisoformat(e["deleted_at"]) < cutoff
            except ValueError:
                old = False
            if old:
                shutil.rmtree(root() / e["id"], ignore_errors=True)
                gone += 1
            else:
                keep.append(e)
        if gone:
            _save(keep)
            logger.info(f"Recycle bin: permanently removed {gone} item(s) older than {days} days.")
        return gone


def restore(eid: str, file_mode: int = 0o777, chown=None) -> str:
    """Put the file back into the sync folder. The next sync uploads it to RomM as a new file."""
    with _lock:
        e = get(eid)
        if e is None:
            raise ValueError("That item is no longer in the recycle bin.")
        dest = Path(e["restore_path"])
        if dest.exists():
            raise ValueError(f"{dest.name} already exists in your sync folder, so it wasn't restored over it.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = path_of(e)
        shutil.copy2(src, dest)
        shot = src.with_name(src.name + ".png")
        if shot.exists() and not dest.with_name(dest.name + ".png").exists():
            shutil.copy2(shot, dest.with_name(dest.name + ".png"))
        for p in (dest, dest.with_name(dest.name + ".png")):
            if p.exists():
                try:
                    os.chmod(p, file_mode)
                    if chown:
                        chown(p)
                except OSError:
                    pass
        remove(eid)
        return str(dest)
