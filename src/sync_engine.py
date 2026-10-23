"""Two-way sync engine with approvals and conflict handling.

Added in the GUI fork of romm_syncthing_sync (original project by amn-96, MIT licensed).

How a decision is made for one file (a save or a state, matched by exact file name):

    baseline = what both sides looked like the last time this file was synced (persisted)

    local only            -> push_new     (confirm by default, PUSH_NEW_MODE=auto to skip)
    RomM only             -> pull_new     (PULL_MODE: off | confirm | auto)
    both, same content    -> in sync (baseline refreshed)
    both, no baseline     -> legacy rule: local newer -> push; otherwise adopt as in sync
                             (with RomM 5.x content hashes: differing + RomM newer -> pull_update)
    both, only local changed since baseline -> push_update (automatic; this is the original app's job)
    both, only RomM changed since baseline  -> pull_update (PULL_MODE)
    both changed since baseline             -> conflict (always asks; FORCE_PUSH_SYNC overrides)

Nothing that would overwrite a file is done without either an approval or a backup:
every overwrite (local or RomM side) first copies the version being replaced to DATA_DIR/backups.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

from .config import get_config
from .games_class import Game
from .sync_state import STATE, PendingAction, data_dir, utcnow_iso

CONFLICT_MARKER = "(conflict "
_VERSION_TAG = re.compile(r"\s*\[\d{4}-\d{2}-\d{2}[^\]]*\]")  # RomM 5.x slot version tag

KIND_API = {"save": "saves", "state": "states"}


# region helpers
def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_hash_cache: Dict[str, Tuple[float, int, str]] = {}


def cached_md5(path: Path) -> str:
    st = path.stat()
    c = _hash_cache.get(str(path))
    if c and c[0] == st.st_mtime and c[1] == st.st_size:
        return c[2]
    h = md5_file(path)
    _hash_cache[str(path)] = (st.st_mtime, st.st_size, h)
    return h


def parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def same_time(a, b) -> bool:
    da, db = parse_dt(a), parse_dt(b)
    if da is None or db is None:
        return da is db
    return abs((da - db).total_seconds()) < 1


def local_name_for_remote(item: dict) -> str:
    """Name a RomM save/state should have in the sync folder (strip RomM 5.x version tags)."""
    return _VERSION_TAG.sub("", item["file_name"])


def file_key(file_type: str, rom_id: int, name: str) -> str:
    return f"{file_type}:{rom_id}:{name}"


@dataclass
class LocalFile:
    file_type: str
    path: Path
    hash: str
    size: int
    mtime: datetime
    screenshot: Optional[Path] = None


@dataclass
class Decision:
    kind: Optional[str]      # None = nothing to do, "adopt" = record baseline only, else ACTION_KINDS
    auto: bool = False
    reason: str = ""
# endregion


def decide(local: Optional[LocalFile], remote: Optional[dict], baseline: Optional[dict],
           push_new_mode: str = "confirm", pull_mode: str = "confirm", force: bool = False) -> Decision:
    """Pure decision function — see module docstring. Kept free of I/O so it is easy to test."""
    pull_on = pull_mode != "off"
    pull_auto = pull_mode == "auto"

    if local and not remote:
        if baseline and baseline.get("romm_id"):
            return Decision("push_new", False, "Was synced before but is no longer on RomM (deleted there?)")
        return Decision("push_new", push_new_mode == "auto", "Not on RomM yet")

    if remote and not local:
        if not pull_on:
            return Decision(None)
        if baseline:
            return Decision("pull_new", False, "Was synced before but is missing from the sync folder (deleted locally?)")
        return Decision("pull_new", pull_auto, "On RomM but not in the sync folder")

    if not local and not remote:
        return Decision(None)

    r_hash = remote.get("content_hash")
    r_time = parse_dt(remote.get("updated_at"))

    if r_hash and r_hash == local.hash:
        return Decision("adopt", True, "Identical content")

    if force:
        return Decision("push_update", True, "FORCE_PUSH_SYNC")

    if baseline is None:
        # First time we see both copies (fresh install / upgrade from 1.x). No history to compare,
        # so fall back to the original timestamp rule and don't invent conflicts.
        if r_time is None or local.mtime > r_time:
            return Decision("push_update", True, "Local copy is newer")
        if r_hash and pull_on:  # RomM 5.x saves: we *know* contents differ and RomM is newer
            return Decision("pull_update", pull_auto, "RomM copy is newer")
        return Decision("adopt", True, "Assumed in sync (no previous sync record)")

    local_changed = local.hash != baseline.get("local_hash")
    remote_changed = (remote.get("id") != baseline.get("romm_id")
                      or not same_time(remote.get("updated_at"), baseline.get("romm_updated_at")))
    if remote_changed and r_hash and baseline.get("romm_hash") and r_hash == baseline.get("romm_hash"):
        remote_changed = False  # metadata touched, content identical

    if not local_changed and not remote_changed:
        return Decision(None)
    if local_changed and not remote_changed:
        return Decision("push_update", True, "Changed locally since last sync")
    if remote_changed and not local_changed:
        if not pull_on:
            return Decision(None)
        return Decision("pull_update", pull_auto, "Changed on RomM since last sync")
    return Decision("conflict", False, "Changed both locally and on RomM since last sync")


class SyncEngine:
    def __init__(self):
        self.cfg = get_config()
        self.romm = self.cfg.ROMM_CREDENTIALS

    # region effective modes
    @property
    def pull_mode(self) -> str:
        if self.cfg.PULL_MODE != "off" and not self.cfg.pull_writable():
            return "off"
        return self.cfg.PULL_MODE
    # endregion

    # region gathering
    def fetch_remote(self) -> Dict[str, Dict[int, List[dict]]]:
        out = {"save": {}, "state": {}}
        for ft in ("save", "state"):
            for item in self.romm.list_all(KIND_API[ft]):
                if CONFLICT_MARKER in item.get("file_name", ""):
                    continue  # conflict copies we created are archive-only
                out[ft].setdefault(item["rom_id"], []).append(item)
        return out

    @staticmethod
    def _latest_by_name(items: List[dict]) -> Dict[str, dict]:
        latest: Dict[str, dict] = {}
        for it in items:
            name = local_name_for_remote(it)
            cur = latest.get(name)
            if cur is None or (parse_dt(it.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)) > \
                    (parse_dt(cur.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc)):
                latest[name] = it
        return latest

    @staticmethod
    def local_files(game: Game) -> Dict[Tuple[str, str], LocalFile]:
        out = {}
        for sf in game.local_save_files:
            if sf.path.exists():
                st = sf.path.stat()
                out[("save", sf.path.name)] = LocalFile("save", sf.path, cached_md5(sf.path), st.st_size, sf.modified_at)
        for ss in game.local_state_files.values():
            if ss.path.exists():
                st = ss.path.stat()
                out[("state", ss.path.name)] = LocalFile("state", ss.path, cached_md5(ss.path), st.st_size,
                                                         ss.modified_at, ss.screenshot)
        return out
    # endregion

    def reconcile(self, games: List[Game], srv=None, discover_remote: bool = False, force: bool = False) -> dict:
        """Compare local vs RomM for the given games; run automatic actions, queue the rest."""
        summary = {"pushed": 0, "pulled": 0, "queued": 0, "conflicts": 0, "in_sync": 0, "errors": 0, "unmatched": 0}
        pull_mode = self.pull_mode
        if self.cfg.PULL_MODE != "off" and pull_mode == "off":
            logger.warning("PULL_MODE is enabled but the sync folders are mounted read-only. "
                           "Remove ':ro' from the volume mounts to allow RomM -> folder sync.")

        STATE.set_phase("fetching", "Fetching saves and states from RomM")
        remote = self.fetch_remote()

        matched = [g for g in games if g.is_matched]
        unmatched = [g for g in games if not g.is_matched]
        summary["unmatched"] = len(unmatched)
        for g in unmatched:
            STATE.update_game(self.snapshot(g, {}, {}, error=None))

        STATE.set_phase("syncing", f"Comparing {len(matched)} games", total=len(matched))
        seen_rom_ids = set()
        for game in matched:
            STATE.tick(game.name)
            seen_rom_ids.add(game.romm_id)
            try:
                self._reconcile_game(game, remote, pull_mode, force, summary)
            except Exception as e:
                summary["errors"] += 1
                logger.error(f"Sync failed for {game.name}: {e}")
                STATE.update_game(self.snapshot(game, {}, {}, error=str(e)))

        if discover_remote and pull_mode != "off" and srv is not None:
            self._discover_remote_only(remote, seen_rom_ids, srv, pull_mode, summary)

        STATE.save()
        return summary

    def _reconcile_game(self, game: Game, remote, pull_mode, force, summary):
        local = self.local_files(game)
        file_status: Dict[str, str] = {}
        remote_by_type = {ft: self._latest_by_name(remote[ft].get(game.romm_id, [])) for ft in ("save", "state")}

        names = set(local.keys())
        for ft in ("save", "state"):
            names |= {(ft, n) for n in remote_by_type[ft].keys()}

        for (ft, name) in sorted(names):
            lf = local.get((ft, name))
            rf = remote_by_type[ft].get(name)
            key = file_key(ft, game.romm_id, name)
            d = decide(lf, rf, STATE.get_baseline(key), self.cfg.PUSH_NEW_MODE, pull_mode, force)

            if d.kind is None:
                STATE.drop_pending_for_key(key)
                file_status[f"{ft}:{name}"] = "synced" if (lf and rf) else ("local-only" if lf else "romm-only")
                if lf and rf:
                    summary["in_sync"] += 1
                continue

            if d.kind == "adopt":
                STATE.set_baseline(key, lf.hash, rf.get("updated_at"), rf.get("id"), rf.get("content_hash"), persist=False)
                STATE.drop_pending_for_key(key)
                file_status[f"{ft}:{name}"] = "synced"
                summary["in_sync"] += 1
                continue

            action = self._build_action(d, game, ft, name, lf, rf)
            if d.auto:
                ok, msg = self.execute(action)
                file_status[f"{ft}:{name}"] = "synced" if ok else "error"
                if ok:
                    summary["pushed" if action.kind.startswith("push") else "pulled"] += 1
                else:
                    summary["errors"] += 1
            else:
                if STATE.is_dismissed(key, action.kind, action.local_hash, action.romm_updated_at):
                    file_status[f"{ft}:{name}"] = "skipped"
                    continue
                STATE.queue(action)
                file_status[f"{ft}:{name}"] = "conflict" if d.kind == "conflict" else "pending"
                summary["conflicts" if d.kind == "conflict" else "queued"] += 1

        STATE.update_game(self.snapshot(game, remote_by_type, file_status))

    def _build_action(self, d: Decision, game: Game, ft: str, name: str,
                      lf: Optional[LocalFile], rf: Optional[dict]) -> PendingAction:
        shot = (rf or {}).get("screenshot") or {}
        return PendingAction(
            kind=d.kind, file_type=ft, game=game.name, platform=game.platform, rom_id=game.romm_id,
            file_name=name,
            local_path=str(lf.path) if lf else None,
            local_hash=lf.hash if lf else None,
            local_size=lf.size if lf else None,
            local_mtime=lf.mtime.isoformat() if lf else None,
            local_screenshot=str(lf.screenshot) if lf and lf.screenshot else None,
            romm_id=rf.get("id") if rf else None,
            romm_file_name=rf.get("file_name") if rf else None,
            romm_size=rf.get("file_size_bytes") if rf else None,
            romm_updated_at=rf.get("updated_at") if rf else None,
            romm_hash=rf.get("content_hash") if rf else None,
            romm_screenshot=shot.get("download_path") if isinstance(shot, dict) else None,
            reason=d.reason,
        )

    def _discover_remote_only(self, remote, seen_rom_ids, srv, pull_mode, summary):
        """Saves/states on RomM for games that have nothing in the sync folder yet."""
        rows = {r["id"]: r for r in srv.library}
        for ft in ("save", "state"):
            sync_root = self.cfg.sync_dir_for(ft)
            if sync_root is None or not sync_root.exists():
                continue
            folders = [p for p in sync_root.iterdir() if p.is_dir()]
            for rom_id, items in remote[ft].items():
                if rom_id in seen_rom_ids or rom_id not in rows:
                    continue
                row = rows[rom_id]
                folder = next((f for f in folders if srv.local_folder_matches_slug(f.name, row["platform_slug"])), None)
                if folder is None:
                    logger.debug(f"No local folder for platform '{row['platform_slug']}' — can't place {row['name']} saves.")
                    continue
                game_name = row.get("fs_name_no_ext") or Path(row["fs_name"]).stem
                game = Game(name=game_name, path=folder / game_name, platform=folder.name)
                game.set_romm_data(row)
                for name, rf in self._latest_by_name(items).items():
                    key = file_key(ft, rom_id, name)
                    d = decide(None, rf, STATE.get_baseline(key), self.cfg.PUSH_NEW_MODE, pull_mode)
                    if d.kind is None:
                        continue
                    action = self._build_action(d, game, ft, name, None, rf)
                    if d.auto:
                        ok, _ = self.execute(action)
                        summary["pulled" if ok else "errors"] += 1
                    elif not STATE.is_dismissed(key, action.kind, None, action.romm_updated_at):
                        STATE.queue(action)
                        summary["queued"] += 1
                snap = self.snapshot(game, {ft: self._latest_by_name(items)}, {}, error=None)
                snap["status"] = "romm-only"
                existing = STATE.games.get(f"{game.platform}/{game.name}")
                if existing is None:
                    STATE.update_game(snap)

    # region execution
    def execute(self, action: PendingAction, choice: Optional[str] = None) -> Tuple[bool, str]:
        """Run an action. For conflicts, choice is 'local' | 'romm' | 'both'."""
        with STATE.sync_lock:
            action.status = "running"
            try:
                if action.kind == "conflict":
                    if choice == "local":
                        self._backup_remote(action)
                        msg = self._push(action)
                    elif choice == "romm":
                        msg = self._pull(action)  # backs up the local copy itself
                    elif choice == "both":
                        copy_name = self._upload_conflict_copy(action)
                        msg = self._pull(action) + f"; your local version was kept on RomM as '{copy_name}'"
                    else:
                        raise ValueError(f"Unknown conflict choice: {choice}")
                elif action.kind in ("push_new", "push_update"):
                    if action.kind == "push_update" and action.romm_id:
                        self._backup_remote(action)
                    msg = self._push(action)
                elif action.kind in ("pull_new", "pull_update"):
                    msg = self._pull(action)
                else:
                    raise ValueError(f"Unknown action {action.kind}")
                logger.info(f"✔ {msg}")
                STATE.finish_action(action, "done", msg)
                return True, msg
            except StaleAction as e:
                STATE.finish_action(action, "stale", str(e))
                logger.warning(f"Skipped {action.file_name}: {e}")
                return False, str(e)
            except Exception as e:
                STATE.finish_action(action, "failed", str(e))
                logger.error(f"✘ {action.kind} {action.file_name} failed: {e}")
                return False, str(e)

    def _check_local_unchanged(self, action: PendingAction) -> Path:
        p = Path(action.local_path)
        if not p.exists():
            raise StaleAction("Local file no longer exists")
        if action.local_hash and md5_file(p) != action.local_hash:
            raise StaleAction("Local file changed after this was queued — it will be re-checked on the next sync")
        return p

    def _check_remote_unchanged(self, action: PendingAction) -> dict:
        import requests
        url = f"{self.romm.url}/api/{KIND_API[action.file_type]}/{action.romm_id}"
        r = requests.get(url, timeout=30, **self.romm._auth_kwargs())
        if r.status_code == 404:
            raise StaleAction("RomM copy no longer exists")
        r.raise_for_status()
        item = r.json()
        if action.romm_updated_at and not same_time(item.get("updated_at"), action.romm_updated_at):
            raise StaleAction("RomM copy changed after this was queued — it will be re-checked on the next sync")
        return item

    def _push(self, action: PendingAction) -> str:
        path = self._check_local_unchanged(action)
        kind = KIND_API[action.file_type]
        screenshot = Path(action.local_screenshot) if action.local_screenshot else None
        if self.cfg.DRY_RUN:
            return f"[dry run] would upload {path.name}"
        resp = self.romm.upload(kind, path, action.rom_id, action.romm_id, screenshot)
        STATE.set_baseline(action.key, action.local_hash or md5_file(path), resp.get("updated_at"),
                           resp.get("id"), resp.get("content_hash"))
        verb = "Updated" if action.romm_id else "Uploaded"
        return f"{verb} {action.file_type} {path.name} on RomM"

    def _pull(self, action: PendingAction) -> str:
        item = self._check_remote_unchanged(action)
        if action.local_path:
            dest = Path(action.local_path)
            if action.kind == "pull_update" and action.local_hash and dest.exists() and md5_file(dest) != action.local_hash:
                raise StaleAction("Local file changed after this was queued — it will be re-checked on the next sync")
        else:
            root = self.cfg.sync_dir_for(action.file_type)
            dest = root / action.platform / action.file_name
            # never let a remote file name escape the platform folder
            if dest.resolve().parent != (root / action.platform).resolve():
                raise ValueError(f"Refusing unsafe file name {action.file_name!r}")
        if self.cfg.DRY_RUN:
            return f"[dry run] would download {dest.name}"
        content = self.romm.download_asset(KIND_API[action.file_type], item)
        if dest.exists():
            self._backup_local(dest, action.platform)
        self._write_local(dest, content, parse_dt(item.get("updated_at")))

        # RetroArch keeps state thumbnails next to the state as <file>.png
        shot = item.get("screenshot")
        if action.file_type == "state" and isinstance(shot, dict) and shot.get("download_path"):
            try:
                r = self.romm.get_raw(shot["download_path"])
                if r.ok:
                    png = dest.with_name(dest.name + ".png")
                    if png.exists():
                        self._backup_local(png, action.platform)
                    self._write_local(png, r.content, parse_dt(item.get("updated_at")))
            except Exception as e:
                logger.debug(f"Could not fetch screenshot for {dest.name}: {e}")

        new_hash = hashlib.md5(content).hexdigest()
        STATE.set_baseline(action.key, new_hash, item.get("updated_at"), item.get("id"), item.get("content_hash"))
        return f"Downloaded {action.file_type} {dest.name} from RomM"

    def _upload_conflict_copy(self, action: PendingAction) -> str:
        path = self._check_local_unchanged(action)
        stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
        stem, dot, ext = path.name.partition(".")
        copy_name = f"{stem} {CONFLICT_MARKER}{stamp}).{ext}" if dot else f"{stem} {CONFLICT_MARKER}{stamp})"
        if not self.cfg.DRY_RUN:
            self.romm.upload(KIND_API[action.file_type], path, action.rom_id, None,
                             Path(action.local_screenshot) if action.local_screenshot else None, upload_name=copy_name)
        return copy_name

    # backups
    def _backup_dir(self, platform: str) -> Path:
        d = data_dir() / "backups" / datetime.now().strftime("%Y-%m-%d") / platform
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _backup_local(self, path: Path, platform: str):
        dst = self._backup_dir(platform) / f"{datetime.now().strftime('%H%M%S')}-local-{path.name}"
        shutil.copy2(path, dst)
        logger.info(f"Backed up local {path.name} → {dst}")

    def _backup_remote(self, action: PendingAction):
        if not action.romm_id or self.cfg.DRY_RUN:
            return
        try:
            content = self.romm.download_asset(KIND_API[action.file_type], {"id": action.romm_id})
            dst = self._backup_dir(action.platform) / f"{datetime.now().strftime('%H%M%S')}-romm-{action.file_name}"
            dst.write_bytes(content)
            logger.info(f"Backed up RomM copy of {action.file_name} → {dst}")
        except Exception as e:
            # Don't overwrite something we couldn't back up.
            raise RuntimeError(f"Could not back up the RomM copy before overwriting it: {e}")

    def _write_local(self, dest: Path, content: bytes, mtime: Optional[datetime]):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.romm_sync.tmp")
        tmp.write_bytes(content)
        if mtime:
            ts = mtime.timestamp()
            os.utime(tmp, (ts, ts))
        if self.cfg.PUID is not None and self.cfg.PGID is not None:
            try:
                os.chown(tmp, self.cfg.PUID, self.cfg.PGID)
            except PermissionError:
                pass
        os.replace(tmp, dest)
    # endregion

    # region GUI snapshot
    def snapshot(self, game: Game, remote_by_type: dict, file_status: dict, error: Optional[str] = None) -> dict:
        row = game.romm_row or {}
        romm_states = list((remote_by_type.get("state") or {}).values())
        romm_saves = list((remote_by_type.get("save") or {}).values())

        # Screenshot priority: newest RomM state screenshot > local RetroArch .png > RomM game screenshot
        screenshot = None
        with_shot = [s for s in romm_states if isinstance(s.get("screenshot"), dict) and s["screenshot"].get("download_path")]
        if with_shot:
            newest = max(with_shot, key=lambda s: parse_dt(s.get("updated_at")) or datetime.min.replace(tzinfo=timezone.utc))
            screenshot = {"src": "romm", "path": newest["screenshot"]["download_path"]}
        else:
            local_shots = [s for s in game.local_state_files.values() if s.screenshot]
            if local_shots:
                newest = max(local_shots, key=lambda s: s.modified_at)
                screenshot = {"src": "local", "path": str(newest.screenshot)}
            elif row.get("merged_screenshots"):
                screenshot = {"src": "romm", "path": row["merged_screenshots"][0]}

        cover = None
        if row.get("path_cover_small"):
            cover = {"src": "romm", "path": row["path_cover_small"]}
        elif row.get("url_cover"):
            cover = {"src": "url", "path": row["url_cover"]}

        files = []
        for k, st in sorted(file_status.items()):
            ft, name = k.split(":", 1)
            files.append({"type": ft, "name": name, "status": st})

        statuses = set(file_status.values())
        if error:
            status = "error"
        elif "error" in statuses:
            status = "error"
        elif not game.is_matched:
            status = "unmatched"
        else:
            status = "synced"

        last_times = [parse_dt(s.get("updated_at")) for s in romm_saves + romm_states]
        last_times = [t for t in last_times if t]
        return {
            "name": game.name,
            "platform": game.platform,
            "platform_display_name": row.get("platform_display_name"),
            "platform_slug": row.get("platform_slug") or game.romm_platform,
            "matched": game.is_matched,
            "rom_id": game.romm_id,
            "romm_name": game.romm_name,
            "cover": cover,
            "screenshot": screenshot,
            "local_saves": len(game.local_save_files),
            "local_states": len(game.local_state_files),
            "romm_saves": len(romm_saves),
            "romm_states": len(romm_states),
            "romm_last_updated": max(last_times).isoformat() if last_times else None,
            "files": files,
            "status": status,
            "error": error,
            "checked_at": utcnow_iso(),
        }
    # endregion


class StaleAction(Exception):
    """The situation changed between queueing and approval."""


def cleanup_backups(days: int = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))):
    root = data_dir() / "backups"
    if days <= 0 or not root.exists():
        return
    cutoff = datetime.now() - timedelta(days=days)
    for d in root.iterdir():
        try:
            if d.is_dir() and datetime.strptime(d.name, "%Y-%m-%d") < cutoff:
                shutil.rmtree(d)
                logger.info(f"Removed old backups {d}")
        except ValueError:
            continue
