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
from time import perf_counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

from .config import get_config
from .games_class import Game
from .sync_state import STATE, PendingAction, ProgressReporter, data_dir, utcnow_iso

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
_hash_cache_loaded = False
_hash_cache_dirty = False


def _hash_cache_file() -> Path:
    return data_dir() / "hash_cache.json"


def _load_hash_cache():
    global _hash_cache_loaded
    if _hash_cache_loaded:
        return
    _hash_cache_loaded = True
    try:
        import json
        raw = json.loads(_hash_cache_file().read_text())
        _hash_cache.update({k: tuple(v) for k, v in raw.items()})
        logger.debug(f"Loaded {len(_hash_cache)} cached file fingerprints")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"Ignoring unreadable hash cache: {e}")


def save_hash_cache(prune: bool = False):
    """Persist fingerprints so a restart doesn't re-read every save. prune (full syncs only, which read the
    folders anyway) drops files that no longer exist; small syncs skip it so they don't touch every file."""
    global _hash_cache_dirty
    if not _hash_cache_dirty:
        return
    import json
    if prune:
        for k in [k for k in _hash_cache if not os.path.exists(k)]:
            del _hash_cache[k]
    tmp = _hash_cache_file().with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(_hash_cache))
        os.replace(tmp, _hash_cache_file())
        _hash_cache_dirty = False
    except OSError as e:
        logger.debug(f"Couldn't save hash cache: {e}")


def cached_md5(path: Path) -> str:
    global _hash_cache_dirty
    _load_hash_cache()
    st = path.stat()
    c = _hash_cache.get(str(path))
    if c and c[0] == st.st_mtime and c[1] == st.st_size:
        return c[2]
    h = md5_file(path)
    _hash_cache[str(path)] = (st.st_mtime, st.st_size, h)
    _hash_cache_dirty = True
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


def local_name_for_remote(item: dict, rom_row: Optional[dict] = None) -> str:
    """Name a RomM save/state should have in the sync folder.

    RetroArch only loads saves/states named after the ROM file, but RomM 5.x's own players and other
    devices store them under other names: a version tag ("Game [2026-08-17_19-02-17].srm"), a slot name
    ("Game.autosave.state0"), or no game name at all ("autosave.srm"). Strip the tag, and if what's left
    doesn't name this ROM, rename it to the ROM's file name + the save/state extension:
        "autosave [2026-08-17_19-02-17].srm"                 -> "Mario's Picross (USA, Europe).srm"
        "Mario's Picross (USA, Europe).autosave.state0"      -> "Mario's Picross (USA, Europe).state0"
        "Panzer Dragoon Saga (USA).bcr" (ROM "... (Disc 1)")  -> unchanged (multi-disc .m3u name)
    """
    name = _VERSION_TAG.sub("", item["file_name"])
    if not rom_row:
        return name
    from .library_classes import LibraryScanner
    game_name, ftype = LibraryScanner.parse_save_filename(name)
    if not game_name or not ftype or LibraryScanner.find_rom(game_name, [rom_row]) is not None:
        return name
    rom_stem = rom_row.get("fs_name_no_ext") or os.path.splitext(rom_row.get("fs_name") or "")[0]
    if not rom_stem:
        return name
    suffix = name[len(game_name):]  # ".srm", ".state0", ".state.auto", ".autosave.state0" ...
    if ftype in ("state", "state_screen"):
        m = re.search(r"\.state(?:\d+|\.auto)?(?:\.png)?$", suffix)
        suffix = m.group(0) if m else suffix
        suffix = re.sub(r"^\.state0(?=$|\.png$)", ".state", suffix)  # RetroArch's slot 0 is ".state"
    else:
        suffix = os.path.splitext(name)[1]
    return rom_stem + suffix


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
           push_new_mode: str = "auto", pull_mode: str = "auto", force: bool = False,
           delete_mode: str = "auto", remote_ids: Optional[set] = None) -> Decision:
    """Pure decision function — see module docstring. Kept free of I/O so it is easy to test.

    remote_ids: ids of every RomM save/state for this ROM. A file only counts as deleted on RomM when the
    RomM entry it was synced with is really gone (not just renamed or re-mapped)."""
    pull_on = pull_mode != "off"
    pull_auto = pull_mode == "auto"

    if local and not remote:
        if baseline and baseline.get("romm_id"):
            gone = remote_ids is None or baseline["romm_id"] not in remote_ids
            if gone and delete_mode != "off" and local.hash == baseline.get("local_hash"):
                return Decision("delete_local", delete_mode == "auto",
                                "Deleted on RomM since the last sync, so the copy in your sync folder is moved "
                                "to the recycle bin.")
            return Decision("push_new", False, "This file was synced before but is now missing on RomM. "
                                               "Upload it again, or skip if it was deleted on purpose.")
        return Decision("push_new", push_new_mode == "auto", "New in your sync folder; not on RomM yet.")

    if remote and not local:
        if baseline and delete_mode != "off" and remote.get("id") == baseline.get("romm_id") \
                and same_time(remote.get("updated_at"), baseline.get("romm_updated_at")):
            return Decision("delete_romm", delete_mode == "auto",
                            "Deleted from your sync folder since the last sync, so RomM's copy is moved to the "
                            "recycle bin.")
        if not pull_on:
            return Decision(None)
        if baseline:
            return Decision("pull_new", False, "This file was synced before but is now missing from your sync folder. "
                                               "Download it again, or skip if it was deleted on purpose.")
        return Decision("pull_new", pull_auto, "On RomM but not in your sync folder yet.")

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
            # Ambiguous: no sync history, so we can't tell which copy holds the real progress. Always ask.
            return Decision("pull_update", False, "Both copies exist but are different, and there's no sync history "
                                                  "for this file yet, so it isn't clear which one has your latest "
                                                  "progress. RomM's copy is newer.")
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
        return Decision("pull_update", pull_auto, "Changed on RomM since the last sync.")
    return Decision("conflict", False, "Changed both in your sync folder and on RomM since the last sync, "
                                       "so either version could be the one you want.")


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
                item["_api"] = KIND_API[ft]  # which RomM list it lives in (used for updates/downloads)
                out[ft].setdefault(item["rom_id"], []).append(item)
        return out

    @classmethod
    def _remote_for_rom(cls, remote, rom_id: int, rom_row: Optional[dict]) -> Dict[str, Dict[str, dict]]:
        """RomM files for one ROM as {"save": {name: item}, "state": {name: item}}, classified by the name they
        get in the sync folder, not by which RomM list they're in: other clients sometimes upload states
        ("Game.state1") to RomM's saves list. The newest item wins when several map to the same name."""
        from .library_classes import LibraryScanner
        items = list(remote["save"].get(rom_id, [])) + list(remote["state"].get(rom_id, []))
        out: Dict[str, Dict[str, dict]] = {"save": {}, "state": {}}
        groups: Dict[str, list] = {}
        for it in items:  # every RomM version of a file (5.x keeps several), so a delete removes them all
            groups.setdefault(local_name_for_remote(it, rom_row), []).append((it.get("_api"), it["id"]))
        for name, it in cls._latest_by_name(items, rom_row).items():
            it["_group"] = groups.get(name, [(it.get("_api"), it["id"])])
            _, ftype = LibraryScanner.parse_save_filename(name)
            out["state" if ftype in ("state", "state_screen") else "save"][name] = it
        return out

    @staticmethod
    def _latest_by_name(items: List[dict], rom_row: Optional[dict] = None) -> Dict[str, dict]:
        """RomM files by the name they get in the sync folder; if several map to one name, the newest wins."""
        latest: Dict[str, dict] = {}
        for it in items:
            name = local_name_for_remote(it, rom_row)
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

    def reconcile(self, games: List[Game], srv=None, discover_remote: bool = False, force: bool = False,
                  all_games: Optional[List[Game]] = None, scope: Optional[set] = None) -> dict:
        """Compare local vs RomM for the given games; run automatic actions, queue the rest.
        scope: RomM ROM ids. Limits the search for RomM-only files to those ROMs (a sync triggered by changes on
        RomM); the whole-library clean-ups only run on a full sync (discover_remote and no scope)."""
        full = discover_remote and scope is None
        summary = {"pushed": 0, "pulled": 0, "deleted": 0, "queued": 0, "conflicts": 0, "in_sync": 0, "errors": 0,
                   "unmatched": 0}
        pull_mode = self.pull_mode
        if self.cfg.PULL_MODE != "off" and pull_mode == "off":
            logger.warning("PULL_MODE is enabled but the sync folders are mounted read-only. "
                           "Remove ':ro' from the volume mounts to allow RomM -> folder sync.")

        STATE.set_phase("fetching", "Fetching saves and states from RomM")
        t0 = perf_counter()
        remote = self.fetch_remote()
        n_saves = sum(len(v) for v in remote["save"].values())
        n_states = sum(len(v) for v in remote["state"].values())
        logger.info(f"RomM has {n_saves:,} saves and {n_states:,} states for this user ({perf_counter() - t0:.1f}s).")

        matched = [g for g in games if g.is_matched]
        unmatched = [g for g in games if not g.is_matched]
        summary["unmatched"] = len(unmatched)
        for g in unmatched:
            STATE.update_game(self.snapshot(g, {}, {}, error=None))

        self._claimed: Dict[str, str] = {}  # file key -> "platform folder" that owns it in this run
        self._evaluated: set = set()         # file keys looked at in this run
        self._auto_deletes: List[PendingAction] = []
        self._local_names: Dict[str, set] = {}  # platform folder -> local game names (incl. unmatched)
        for g in (all_games if all_games is not None else games):
            self._local_names.setdefault(g.platform.lower(), set()).add(g.name)

        if full:  # full sync: duplicate warnings are re-detected from scratch
            with STATE.lock:
                for k in [k for k in STATE.errors if k.startswith("dup:")]:
                    del STATE.errors[k]
        STATE.set_phase("syncing", f"Comparing {len(matched)} games", total=len(matched))
        report = ProgressReporter("syncing", "Comparing local and RomM files")
        seen_rom_ids = set()
        for i, game in enumerate(matched, 1):
            STATE.tick(game.name)
            report("games", i, len(matched), final=(i == len(matched)))
            STATE.current_game = game.name
            seen_rom_ids.add(game.romm_id)
            try:
                self._reconcile_game(game, remote, pull_mode, force, summary)
            except Exception as e:
                summary["errors"] += 1
                logger.error(f"Sync failed for {game.name}: {e}")
                STATE.update_game(self.snapshot(game, {}, {}, error=str(e)))

        if discover_remote and srv is not None:
            self._discover_remote_only(remote, seen_rom_ids, srv, pull_mode, summary, scope)
        self._run_auto_deletes(summary)

        if full:
            # Full sync: anything still queued that this sync didn't come across any more (file renamed,
            # game re-matched, queued by an older version...) is stale.
            with STATE.lock:
                stale = [a for a in STATE.pending.values() if a.status == "pending" and a.key not in self._evaluated]
                for a in stale:
                    del STATE.pending[a.id]
            if stale:
                logger.info(f"Removed {len(stale)} queued item(s) that no longer apply.")

        STATE.save()
        save_hash_cache(prune=full)
        return summary

    def _reconcile_game(self, game: Game, remote, pull_mode, force, summary):
        local = self.local_files(game)
        file_status: Dict[str, str] = {}
        remote_by_type = self._remote_for_rom(remote, game.romm_id, game.romm_row)
        remote_ids = {it["id"] for it in remote["save"].get(game.romm_id, []) + remote["state"].get(game.romm_id, [])}
        from .library_classes import LibraryScanner
        other_local = self._local_names.get(game.platform.lower(), set()) - {game.name}

        names = set(local.keys())
        for ft in ("save", "state"):
            names |= {(ft, n) for n in remote_by_type[ft].keys()}

        for (ft, name) in sorted(names):
            lf = local.get((ft, name))
            rf = remote_by_type[ft].get(name)
            if rf is not None and lf is None:
                owner_name, _ = LibraryScanner.parse_save_filename(name)
                if owner_name in other_local:
                    # e.g. "Fallen.state.auto" attached to the "Fallen Angel" ROM by an older version: that file
                    # belongs to the local game "Fallen", so never download it as part of this game.
                    logger.debug(f"Ignoring RomM file {name} on {game.name}: it belongs to local game {owner_name}")
                    continue
            key = file_key(ft, game.romm_id, name)
            self._evaluated.add(key)

            # Two system folders that point at the same RomM platform (e.g. "md" and "megadrive") and contain
            # the same game would fight over one RomM file. Sync the first, report the second.
            owner = self._claimed.get(key)
            if owner is not None and owner.lower() != game.platform.lower():
                if lf:
                    STATE.record_issue(f"dup:{game.platform}:{key}", game.name, game.platform, name, ft, "duplicate",
                                       f"The same {ft} is also in the '{owner}' folder, and both folders point at the "
                                       f"same RomM platform, so only the '{owner}' copy is synced. Keep the game in "
                                       f"one folder, or point one of the folders at a different platform on the "
                                       f"Platforms page.")
                    file_status[f"{ft}:{name}"] = "error"
                continue
            self._claimed[key] = game.platform
            STATE.clear_error(f"dup:{game.platform}:{key}")
            d = decide(lf, rf, STATE.get_baseline(key), self.cfg.PUSH_NEW_MODE, pull_mode, force,
                       self.cfg.DELETE_MODE, remote_ids)

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
            if d.auto and d.kind.startswith("delete"):
                self._auto_deletes.append(action)   # run after the loop (mass-delete guard)
                file_status[f"{ft}:{name}"] = "pending"
                continue
            if d.auto:
                STATE.drop_pending_for_key(key)  # e.g. queued by an older "confirm" setting
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
                STATE.queue(action, persist=False)  # saved once at the end of the sync
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
            romm_api=rf.get("_api") if rf else None,
            romm_group=[list(x) for x in rf.get("_group", [])] if rf else None,
            reason=d.reason,
        )

    def _discover_remote_only(self, remote, seen_rom_ids, srv, pull_mode, summary, scope: Optional[set] = None):
        """Saves/states on RomM for games that have nothing in the sync folder yet."""
        rows = {r["id"]: r for r in srv.library}
        folders_by_type = {}
        for ft in ("save", "state"):
            root = self.cfg.sync_dir_for(ft)
            folders_by_type[ft] = [p for p in root.iterdir() if p.is_dir()] if root is not None and root.exists() else []
        from .settings import sync_enabled
        for ft in folders_by_type:
            folders_by_type[ft] = [f for f in folders_by_type[ft] if sync_enabled(f.name)]
        for rom_id in sorted(set(remote["save"]) | set(remote["state"])):
            if rom_id in seen_rom_ids or rom_id not in rows or (scope is not None and rom_id not in scope):
                continue
            row = rows[rom_id]
            files = self._remote_for_rom(remote, rom_id, row)
            game_name = row.get("fs_name_no_ext") or Path(row["fs_name"]).stem
            shown = None
            for ft in ("save", "state"):
                folder = next((f for f in folders_by_type[ft] if srv.local_folder_matches_slug(f.name, row["platform_slug"])), None)
                if folder is None:
                    if files[ft]:
                        logger.debug(f"No local folder for platform '{row['platform_slug']}' — can't place {row['name']} {ft}s.")
                    continue
                game = Game(name=game_name, path=folder / game_name, platform=folder.name)
                game.set_romm_data(row)
                shown = shown or game
                for name, rf in files[ft].items():
                    if (folder / name).exists():
                        # Already in the sync folder under another ROM (e.g. a copy an older version attached to
                        # "(Disc 1)" while your save syncs with the multi-disc ROM). Never overwrite it from here.
                        logger.debug(f"Skipping {name} from RomM ROM #{rom_id}: that file already exists locally")
                        continue
                    key = file_key(ft, rom_id, name)
                    self._evaluated.add(key)
                    d = decide(None, rf, STATE.get_baseline(key), self.cfg.PUSH_NEW_MODE, pull_mode,
                               delete_mode=self.cfg.DELETE_MODE)
                    if d.kind is None:
                        continue
                    action = self._build_action(d, game, ft, name, None, rf)
                    if d.auto and d.kind.startswith("delete"):
                        self._auto_deletes.append(action)
                    elif d.auto:
                        ok, _ = self.execute(action)
                        summary["pulled" if ok else "errors"] += 1
                    elif not STATE.is_dismissed(key, action.kind, None, action.romm_updated_at):
                        STATE.queue(action, persist=False)
                        summary["queued"] += 1
            if shown is not None and STATE.games.get(f"{shown.platform}/{shown.name}") is None:
                snap = self.snapshot(shown, files, {}, error=None)
                snap["status"] = "romm-only"
                STATE.update_game(snap)

    DELETE_GUARD = 10  # more automatic deletions than this in one sync -> ask instead

    def _run_auto_deletes(self, summary):
        actions = self._auto_deletes
        self._auto_deletes = []
        if not actions:
            return
        if len(actions) > self.DELETE_GUARD:
            logger.warning(f"{len(actions)} synced files disappeared at once. Not deleting anything automatically; "
                           "they're waiting for your decision.")
            for a in actions:
                a.reason = (f"{len(actions)} files disappeared in one sync, which is unusual, so nothing was deleted "
                            f"automatically. {a.reason}")
                if not STATE.is_dismissed(a.key, a.kind, a.local_hash, a.romm_updated_at):
                    STATE.queue(a, persist=False)
                    summary["queued"] += 1
            return
        for a in actions:
            STATE.drop_pending_for_key(a.key)
            ok, _ = self.execute(a)
            summary["deleted" if ok else "errors"] += 1

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
                elif action.kind in ("delete_romm", "delete_local") and choice == "restore":
                    # "Bring it back" instead of deleting: copy the surviving side over again
                    if action.kind == "delete_romm":
                        action.kind = "pull_new"
                        msg = self._pull(action)
                    else:
                        action.kind = "push_new"
                        action.romm_id = None
                        msg = self._push(action)
                elif action.kind == "delete_romm":
                    msg = self._delete_on_romm(action)
                elif action.kind == "delete_local":
                    msg = self._delete_local(action)
                else:
                    raise ValueError(f"Unknown action {action.kind}")
                logger.info(f"✔ {msg}")
                STATE.clear_error(action.key)
                STATE.finish_action(action, "done", msg)
                return True, msg
            except StaleAction as e:
                STATE.clear_error(action.key)
                STATE.finish_action(action, "stale", str(e))
                logger.warning(f"Skipped {action.file_name}: {e}")
                return False, str(e)
            except Exception as e:
                msg = explain_error(e, action)
                STATE.finish_action(action, "failed", msg)
                STATE.record_error(action, msg, detail=str(e))
                logger.error(f"✘ {action.kind} {action.file_name} failed: {msg}")
                return False, msg

    def _check_local_unchanged(self, action: PendingAction) -> Path:
        p = Path(action.local_path)
        if not p.exists():
            raise StaleAction("Local file no longer exists")
        if action.local_hash and md5_file(p) != action.local_hash:
            raise StaleAction("Local file changed after this was queued — it will be re-checked on the next sync")
        return p

    def _check_remote_unchanged(self, action: PendingAction) -> dict:
        import requests
        url = f"{self.romm.url}/api/{action.romm_api or KIND_API[action.file_type]}/{action.romm_id}"
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
        # updates go back to the RomM list the file is in; new files go to the list for their type
        kind = (action.romm_api or KIND_API[action.file_type]) if action.romm_id else KIND_API[action.file_type]
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
        if action.kind == "pull_new" and dest.exists():
            # We expected no local file. Something else owns this name; never overwrite it.
            raise StaleAction(f"{dest.name} already exists in your sync folder, so it wasn't overwritten. "
                              "It will be re-checked on the next sync.")
        content = self.romm.download_asset(action.romm_api or KIND_API[action.file_type], item)
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

    def _expected_local_path(self, action: PendingAction) -> Path:
        root = self.cfg.sync_dir_for(action.file_type)
        dest = root / action.platform / action.file_name
        if dest.resolve().parent != (root / action.platform).resolve():
            raise ValueError(f"Refusing unsafe file name {action.file_name!r}")
        return dest

    def _delete_on_romm(self, action: PendingAction) -> str:
        """The file was deleted from the sync folder: keep RomM's copy in the recycle bin, then delete it on RomM."""
        from . import recycle
        item = self._check_remote_unchanged(action)
        local_path = Path(action.local_path) if action.local_path else self._expected_local_path(action)
        if local_path.exists():
            raise StaleAction(f"{local_path.name} is back in your sync folder, so nothing was deleted.")
        if self.cfg.DRY_RUN:
            return f"[dry run] would move {action.file_name} from RomM to the recycle bin"
        api = action.romm_api or KIND_API[action.file_type]
        content = self.romm.download_asset(api, item)
        shot = None
        if isinstance(item.get("screenshot"), dict) and item["screenshot"].get("download_path"):
            try:
                r = self.romm.get_raw(item["screenshot"]["download_path"])
                shot = r.content if r.ok else None
            except Exception:
                shot = None
        recycle.add(file_name=action.file_name, file_type=action.file_type, game=action.game, platform=action.platform,
                    rom_id=action.rom_id, deleted_from="romm", reason="Deleted from your sync folder",
                    content=content, screenshot=shot, restore_path=str(local_path), file_mode=self.cfg.FILE_MODE)
        group = action.romm_group or [[api, action.romm_id]]
        by_api: Dict[str, list] = {}
        for g_api, g_id in group:
            by_api.setdefault(g_api or api, []).append(g_id)
        for g_api, ids in by_api.items():
            self.romm.delete_assets(g_api, ids)
        with STATE.lock:
            STATE.baselines.pop(action.key, None)
        STATE.save()
        extra = f" ({len(group)} versions)" if len(group) > 1 else ""
        return f"Moved {action.file_type} {action.file_name} from RomM to the recycle bin{extra}"

    def _delete_local(self, action: PendingAction) -> str:
        """The file was deleted on RomM: move the local copy (and its thumbnail) into the recycle bin."""
        from . import recycle
        path = self._check_local_unchanged(action)
        if action.romm_id:
            import requests
            r = requests.get(f"{self.romm.url}/api/{action.romm_api or KIND_API[action.file_type]}/{action.romm_id}",
                             timeout=30, **self.romm._auth_kwargs())
            if r.status_code == 200:
                raise StaleAction("The file is still on RomM, so nothing was deleted.")
        if self.cfg.DRY_RUN:
            return f"[dry run] would move {path.name} to the recycle bin"
        png = path.with_name(path.name + ".png")
        recycle.add(file_name=path.name, file_type=action.file_type, game=action.game, platform=action.platform,
                    rom_id=action.rom_id, deleted_from="local", reason="Deleted on RomM",
                    content=path.read_bytes(), screenshot=png.read_bytes() if png.exists() else None,
                    restore_path=str(path), file_mode=self.cfg.FILE_MODE)
        path.unlink()
        if png.exists():
            png.unlink()
        with STATE.lock:
            STATE.baselines.pop(action.key, None)
        STATE.save()
        return f"Moved {action.file_type} {path.name} from your sync folder to the recycle bin"

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
        self._make_dirs(d, stop_at=data_dir())
        return d

    def _backup_local(self, path: Path, platform: str):
        dst = self._backup_dir(platform) / f"{datetime.now().strftime('%H%M%S')}-local-{path.name}"
        shutil.copy2(path, dst)
        self._fix_perms(dst)
        logger.info(f"Backed up local {path.name} → {dst}")

    def _backup_remote(self, action: PendingAction):
        if not action.romm_id or self.cfg.DRY_RUN:
            return
        try:
            content = self.romm.download_asset(action.romm_api or KIND_API[action.file_type], {"id": action.romm_id})
            dst = self._backup_dir(action.platform) / f"{datetime.now().strftime('%H%M%S')}-romm-{action.file_name}"
            dst.write_bytes(content)
            self._fix_perms(dst)
            logger.info(f"Backed up RomM copy of {action.file_name} → {dst}")
        except Exception as e:
            # Don't overwrite something we couldn't back up.
            raise RuntimeError(f"Could not back up the RomM copy before overwriting it: {e}")

    def _fix_perms(self, path: Path, is_dir: bool = False):
        """Unraid-friendly ownership and permissions (FILE_MODE, default 0777) so Syncthing, SMB shares and
        your devices can all read and write what this app creates."""
        try:
            os.chmod(path, self.cfg.FILE_MODE)
        except OSError as e:
            logger.debug(f"Couldn't chmod {path}: {e}")
        if self.cfg.PUID is not None and self.cfg.PGID is not None:
            try:
                os.chown(path, self.cfg.PUID, self.cfg.PGID)
            except OSError:
                pass

    def _make_dirs(self, d: Path, stop_at: Optional[Path] = None):
        """mkdir -p, applying permissions to every folder this app creates (not to existing ones)."""
        missing = []
        p = d
        while not p.exists() and p != p.parent and p != stop_at:
            missing.append(p)
            p = p.parent
        d.mkdir(parents=True, exist_ok=True)
        for m in reversed(missing):
            self._fix_perms(m, is_dir=True)

    def _write_local(self, dest: Path, content: bytes, mtime: Optional[datetime]):
        self._make_dirs(dest.parent)
        tmp = dest.with_name(f".{dest.name}.romm_sync.tmp")
        tmp.write_bytes(content)
        if mtime:
            ts = mtime.timestamp()
            os.utime(tmp, (ts, ts))
        self._fix_perms(tmp)
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


def explain_error(e: Exception, action: PendingAction) -> str:
    """Turn an exception into a sentence a person can act on."""
    import requests
    text = str(e)
    where = "RomM" if action.kind.startswith("push") or action.kind == "conflict" else "your sync folder"
    if isinstance(e, PermissionError) or "Permission denied" in text or "Read-only file system" in text:
        return ("Couldn't write to your sync folder (permission denied / read-only). Make sure the saves and "
                "states folders are mounted read-write (no ':ro') and that PUID/PGID can write there.")
    if isinstance(e, OSError) and "No space left" in text:
        return "The disk is full, so the file couldn't be written."
    if isinstance(e, requests.exceptions.ConnectionError) or "Can't reach RomM" in text:
        return f"Couldn't reach RomM at {get_config().ROMM_CREDENTIALS.url}. It will be retried on the next sync."
    if isinstance(e, requests.exceptions.Timeout):
        return "RomM took too long to respond. It will be retried on the next sync."
    m = re.search(r"RomM returned (\d{3})", text) or re.search(r"(\d{3}) (?:Client|Server) Error", text)
    code = int(m.group(1)) if m else None
    if code in (401, 403):
        return ("RomM refused this request. The API token probably lacks permission to "
                + ("write saves/states (assets.write)." if where == "RomM" else "read saves/states (assets.read)."))
    if code == 404:
        return "RomM couldn't find this game or file any more (it may have been removed from RomM)."
    if code == 413:
        return "RomM rejected the file as too large. Check the upload size limit of RomM or your reverse proxy."
    if code == 409:
        return "RomM reported a conflict with an existing file. It will be re-checked on the next sync."
    missing = re.search(r"Table '[^.']+\.(\w+)' doesn't exist|relation \"(\w+)\" does not exist", text)
    if missing:
        table = missing.group(1) or missing.group(2)
        return (f"RomM's database is missing the '{table}' table, so RomM can't save or delete files. Its database "
                "update (migration) didn't finish when RomM was last updated. Look for \"Failed to run database "
                "migrations\" in RomM's container log, fix the cause, and restart RomM; it retries the update on "
                "every start. Nothing was changed on either side.")
    if code and code >= 500:
        return f"RomM had an internal error ({code}). Check RomM's logs; it will be retried on the next sync."
    if "back up" in text:
        return "Couldn't make a backup of the RomM copy first, so nothing was overwritten. " + text
    return text


class StaleAction(Exception):
    """The situation changed between queueing and approval."""


def cleanup_backups(days: Optional[int] = None):
    if days is None:
        days = int(getattr(get_config(), "BACKUP_RETENTION_DAYS", 30))
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
