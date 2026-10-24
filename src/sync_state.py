"""Shared, thread-safe application state.

This module is the meeting point between the sync threads (watchdog / periodic / poller)
and the web GUI. The sync code *writes* progress, game snapshots, activity and pending
actions here; the web server *reads* it and posts user decisions back.

It also owns the persistent "baseline" store: for every file that has been successfully
synced, we remember the local content hash and the RomM `updated_at` at that moment.
The baseline is what lets us tell "only one side changed" (safe to sync automatically)
apart from "both sides changed" (a conflict that needs a human decision).
"""
from __future__ import annotations

import json
import re
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def data_dir() -> Path:
    """Persistent app data folder (state db, backups). Mapped to Unraid appdata in docker."""
    p = Path(os.getenv("DATA_DIR", "/data"))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fallback for local runs without /data
        p = Path.cwd() / ".romm_sync_data"
        p.mkdir(parents=True, exist_ok=True)
    return p


# region Pending actions
ACTION_KINDS = {
    "push_new": "New local file → upload to RomM",
    "push_update": "Local is newer → overwrite RomM copy",
    "pull_new": "New on RomM → download to folder",
    "pull_update": "RomM is newer → overwrite local copy",
    "conflict": "Both sides changed since last sync",
}


@dataclass
class PendingAction:
    """A sync operation waiting for a user decision in the GUI."""
    kind: str                      # one of ACTION_KINDS
    file_type: str                 # "save" | "state"
    game: str                      # local game name
    platform: str                  # local platform folder name
    rom_id: int
    file_name: str                 # local-style file name (Game.srm / Game.state1)
    local_path: Optional[str] = None
    local_hash: Optional[str] = None
    local_size: Optional[int] = None
    local_mtime: Optional[str] = None
    local_screenshot: Optional[str] = None
    romm_id: Optional[int] = None
    romm_file_name: Optional[str] = None
    romm_size: Optional[int] = None
    romm_updated_at: Optional[str] = None
    romm_hash: Optional[str] = None
    romm_screenshot: Optional[str] = None   # RomM download_path of the state screenshot
    romm_api: Optional[str] = None          # RomM list the file is in ("saves"/"states"); may differ from file_type
    reason: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=utcnow_iso)
    status: str = "pending"        # pending | running | done | failed | stale | dismissed
    result: str = ""

    @property
    def key(self) -> str:
        return f"{self.file_type}:{self.rom_id}:{self.file_name}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["kind_label"] = ACTION_KINDS.get(self.kind, self.kind)
        return d
# endregion


class AppState:
    """Process-wide state. Use `STATE` below rather than instantiating."""

    MAX_ACTIVITY = 300
    MAX_HISTORY = 100

    def __init__(self):
        self.lock = threading.RLock()
        # One sync/apply at a time across all threads (sync loop, poller, GUI approvals).
        self.sync_lock = threading.RLock()

        # Progress
        self.phase: str = "starting"        # starting | idle | building | fetching | syncing | applying | error
        self.phase_detail: str = ""
        self.progress_current: int = 0
        self.progress_total: int = 0
        self.current_game: Optional[str] = None
        self.last_sync_started: Optional[str] = None
        self.last_sync_finished: Optional[str] = None
        self.last_sync_result: Optional[dict] = None
        self.next_sync_at: Optional[str] = None
        self.sync_mode: str = ""
        self.last_error: Optional[str] = None
        self.romm_version: Optional[str] = None

        # Snapshots for the GUI
        self.games: Dict[str, dict] = {}    # key "platform/game" -> snapshot dict
        self.activity: deque = deque(maxlen=self.MAX_ACTIVITY)

        # Decisions
        self.pending: Dict[str, PendingAction] = {}   # action id -> action
        self.history: deque = deque(maxlen=self.MAX_HISTORY)
        self.errors: Dict[str, dict] = {}   # file key -> latest failure (cleared when that file syncs)

        # Baselines (persisted)
        self.baselines: Dict[str, dict] = {}
        self.dismissed: Dict[str, dict] = {}   # file key -> {"kind", "local_hash", "romm_updated_at"}
        self._state_file = data_dir() / "romm_sync_state.json"
        self._load()

        # Hook set by the app: called by the GUI "Sync now" button.
        self.request_sync_callback = None

    # region persistence
    def _load(self):
        try:
            if self._state_file.exists():
                raw = json.loads(self._state_file.read_text())
                self.baselines = raw.get("baselines", {})
                self.dismissed = raw.get("dismissed", {})
                self.history.extend(raw.get("history", [])[:self.MAX_HISTORY])
                for a in raw.get("pending", []):
                    a.pop("key", None)
                    a.pop("kind_label", None)
                    pa = PendingAction(**a)
                    if pa.status in ("pending", "running"):
                        pa.status = "pending"
                        self.pending[pa.id] = pa
                logger.info(f"Loaded {len(self.baselines)} sync baselines and {len(self.pending)} pending action(s).")
        except Exception as e:
            logger.warning(f"Could not read state file {self._state_file}: {e}. Starting fresh.")

    def save(self):
        with self.lock:
            payload = {
                "version": 1,
                "baselines": self.baselines,
                "dismissed": self.dismissed,
                "history": list(self.history),
                "pending": [a.to_dict() for a in self.pending.values() if a.status == "pending"],
            }
        tmp = self._state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1, default=str))
            os.replace(tmp, self._state_file)
        except Exception as e:
            logger.error(f"Could not persist state file {self._state_file}: {e}")
    # endregion

    # region baselines
    def get_baseline(self, key: str) -> Optional[dict]:
        with self.lock:
            return self.baselines.get(key)

    def set_baseline(self, key: str, local_hash: Optional[str], romm_updated_at: Optional[str],
                     romm_id: Optional[int], romm_hash: Optional[str] = None, persist: bool = True):
        with self.lock:
            self.baselines[key] = {
                "local_hash": local_hash,
                "romm_updated_at": romm_updated_at,
                "romm_id": romm_id,
                "romm_hash": romm_hash,
                "synced_at": utcnow_iso(),
            }
            self.dismissed.pop(key, None)
        if persist:
            self.save()
    # endregion

    # region progress
    def set_phase(self, phase: str, detail: str = "", total: Optional[int] = None):
        with self.lock:
            self.phase = phase
            self.phase_detail = detail
            if total is not None:
                self.progress_total = total
                self.progress_current = 0
            if phase == "idle":
                self.current_game = None

    def tick(self, game_name: Optional[str] = None):
        with self.lock:
            self.progress_current = min(self.progress_current + 1, max(self.progress_total, 1))
            if game_name is not None:
                self.current_game = game_name

    def set_current(self, game_name: Optional[str]):
        with self.lock:
            self.current_game = game_name

    def sync_started(self):
        with self.lock:
            self.last_sync_started = utcnow_iso()
            self.last_error = None

    def sync_finished(self, result: Optional[dict] = None, error: Optional[str] = None):
        with self.lock:
            self.last_sync_finished = utcnow_iso()
            self.last_sync_result = result
            if error:
                self.last_error = error
            self.phase = "error" if error else "idle"
            self.phase_detail = error or ""
            self.current_game = None
        try:
            from . import notify  # late import: notify imports this module
            notify.after_sync(result, error)
        except Exception as e:
            logger.debug(f"Notification step failed: {e}")

    def add_activity(self, level: str, message: str):
        with self.lock:
            self.activity.append({"t": utcnow_iso(), "level": level, "msg": message})
    # endregion

    # region games
    def update_game(self, snapshot: dict):
        with self.lock:
            self.games[f"{snapshot['platform']}/{snapshot['name']}"] = snapshot

    def replace_games(self, snapshots: List[dict]):
        with self.lock:
            self.games = {f"{s['platform']}/{s['name']}": s for s in snapshots}
    # endregion

    # region pending
    def queue(self, action: PendingAction, persist: bool = True) -> PendingAction:
        """Add (or refresh) a pending action. One pending action per file key."""
        with self.lock:
            for existing in list(self.pending.values()):
                if existing.key != action.key:
                    continue
                if existing.status == "running":
                    return existing  # being handled right now (e.g. approved a moment ago): don't re-queue
                if existing.status == "pending":
                    # Keep the id stable so an open dialog in the GUI still works, refresh data.
                    action.id = existing.id
                    action.created_at = existing.created_at
                    break
            is_new = action.id not in self.pending
            self.pending[action.id] = action
        if is_new:
            self.add_activity("WARNING" if action.kind == "conflict" else "INFO",
                              f"Waiting for approval: {ACTION_KINDS[action.kind]} — {action.platform}/{action.file_name}")
        if persist:
            self.save()
        return action

    def dismiss(self, action: "PendingAction"):
        """Remember that the user rejected this exact situation so it is not asked again
        until either side changes."""
        with self.lock:
            self.dismissed[action.key] = {"kind": action.kind, "local_hash": action.local_hash,
                                          "romm_updated_at": action.romm_updated_at}

    def is_dismissed(self, key: str, kind: str, local_hash, romm_updated_at) -> bool:
        with self.lock:
            d = self.dismissed.get(key)
        return bool(d) and d["kind"] == kind and d["local_hash"] == local_hash and d["romm_updated_at"] == romm_updated_at

    def drop_pending_for_key(self, key: str):
        with self.lock:
            for aid, a in list(self.pending.items()):
                if a.key == key and a.status == "pending":
                    del self.pending[aid]

    def finish_action(self, action: PendingAction, status: str, result: str = ""):
        with self.lock:
            action.status = status
            action.result = result
            self.pending.pop(action.id, None)
            self.history.appendleft(action.to_dict() | {"finished_at": utcnow_iso()})
        self.save()

    def record_error(self, action: "PendingAction", message: str, detail: str = ""):
        self.record_issue(action.key, action.game, action.platform, action.file_name, action.file_type,
                          action.kind, message, detail)

    def record_issue(self, key: str, game: str, platform: str, file_name: str, file_type: str,
                     kind: str, message: str, detail: str = ""):
        with self.lock:
            prev = self.errors.get(key, {})
            self.errors[key] = {
                "key": key, "game": game, "platform": platform,
                "file_name": file_name, "file_type": file_type, "kind": kind,
                "message": message, "detail": detail if detail != message else "",
                "at": prev.get("at") if prev.get("message") == message else utcnow_iso(),
                "count": prev.get("count", 0) + 1,
            }

    def clear_error(self, key: str):
        with self.lock:
            self.errors.pop(key, None)

    def pending_list(self) -> List[dict]:
        with self.lock:
            return [a.to_dict() for a in sorted(self.pending.values(), key=lambda a: a.created_at)]
    # endregion

    def snapshot(self) -> dict:
        """Everything the dashboard needs in one poll."""
        with self.lock:
            games = list(self.games.values())
            pending = self.pending_list()
            pending_keys = {}
            for p in pending:
                pending_keys.setdefault(f"{p['platform']}/{p['game']}", []).append(p["kind"])

            systems: Dict[str, dict] = {}
            for g in games:
                key = f"{g['platform']}/{g['name']}"
                kinds = pending_keys.get(key, [])
                if "conflict" in kinds:
                    g_status = "conflict"
                elif kinds:
                    g_status = "pending"
                elif not g.get("matched"):
                    g_status = "unmatched"
                elif g.get("error"):
                    g_status = "error"
                else:
                    g_status = g.get("status", "synced")
                g["ui_status"] = g_status
                s = systems.setdefault(g["platform"], {
                    "platform": g["platform"],
                    "display_name": g.get("platform_display_name") or g["platform"],
                    "games": 0, "saves": 0, "states": 0,
                    "synced": 0, "pending": 0, "conflict": 0, "unmatched": 0, "error": 0,
                })
                if g.get("platform_display_name"):
                    s["display_name"] = g["platform_display_name"]
                s["games"] += 1
                s["saves"] += g.get("local_saves", 0)
                s["states"] += g.get("local_states", 0)
                s[g_status if g_status in s else "synced"] += 1

            return {
                "phase": self.phase,
                "phase_detail": self.phase_detail,
                "progress": {"current": self.progress_current, "total": self.progress_total},
                "current_game": self.current_game,
                "last_sync_started": self.last_sync_started,
                "last_sync_finished": self.last_sync_finished,
                "last_sync_result": self.last_sync_result,
                "next_sync_at": self.next_sync_at,
                "sync_mode": self.sync_mode,
                "last_error": self.last_error,
                "systems": sorted(systems.values(), key=lambda s: s["display_name"].lower()),
                "games": sorted(games, key=lambda g: (g["platform"].lower(), g["name"].lower())),
                "pending": pending,
                "errors": sorted(self.errors.values(), key=lambda e: e["at"], reverse=True),
                "history": list(self.history)[:30],
                "activity": list(self.activity)[-120:],
            }


STATE = AppState()


class ProgressReporter:
    """Logs '<what>: 1,500 / 12,345 (12%)' at most every `every` seconds (plus the final line),
    and mirrors it to the dashboard's progress bar."""

    def __init__(self, phase: str, what: str, every: float = 5.0):
        import time
        self._time = time.monotonic
        self.phase, self.what, self.every = phase, what, every
        self._last = 0.0

    def __call__(self, label: str, done: int, total: Optional[int], final: bool = False):
        pct = f" ({done * 100 // total}%)" if total else ""
        tot = f" / {total:,}" if total else ""
        detail = f"{self.what} — {label}: {done:,}{tot}{pct}"
        if STATE is not None:
            with STATE.lock:
                STATE.phase = self.phase
                STATE.phase_detail = detail
                STATE.progress_total = total or 0
                STATE.progress_current = done if total else 0
        now = self._time()
        if final or now - self._last >= self.every or (total and done >= total):
            self._last = now
            logger.info(detail)


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file() -> Path:
    return log_dir() / "romm_sync.log"


def install_log_sink():
    """Mirror INFO+ log lines into the GUI activity feed, and keep a rotating log file in DATA_DIR/logs."""
    def sink(message):
        rec = message.record
        if STATE is not None:
            STATE.add_activity(rec["level"].name, rec["message"])
    logger.add(sink, level="INFO", format="{message}")
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    try:
        logger.add(str(log_file()), level="DEBUG" if level in ("DEBUG", "TRACE") else "INFO",
                   format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
                   rotation="5 MB", retention=5, encoding="utf-8", enqueue=True)
    except Exception as e:
        logger.warning(f"Couldn't open the log file in {log_dir()}: {e}")


_LEVEL_RANK = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "SUCCESS": 25, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (\w+)\s*\| (.*)$")


def read_log(min_level: str = "INFO", query: str = "", limit: int = 1000, max_bytes: int = 4_000_000) -> List[dict]:
    """Newest `limit` matching entries from the log file (multi-line entries kept together), oldest first."""
    path = log_file()
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        text = f.read().decode("utf-8", "replace")
    if size > max_bytes:
        text = text.split("\n", 1)[-1]  # drop the partial first line
    entries: List[dict] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            entries.append({"t": m.group(1), "level": m.group(2), "msg": m.group(3)})
        elif entries and line.strip():
            entries[-1]["msg"] += "\n" + line
    rank = _LEVEL_RANK.get(min_level.upper(), 20)
    q = query.casefold().strip()
    out = [e for e in entries if _LEVEL_RANK.get(e["level"], 20) >= rank and (not q or q in e["msg"].casefold())]
    return out[-limit:]
