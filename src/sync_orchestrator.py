"""Orchestration for coordinating synchronization between local library and ROMM server.

Includes orchestration logic, sync state management, and file system event handling.
"""
from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path
from loguru import logger
import threading
from collections import namedtuple

from time import perf_counter as tpc

from .games_class import Game
from .library_classes import LocalLibrary, RetroGameServer, LibraryScanner
from .config import get_config, METADATA_FILE_FILTERS
from .sync_engine import SyncEngine, cleanup_backups
from .sync_state import STATE, utcnow_iso
from watchdog.events import FileSystemEventHandler


ValidEvent = namedtuple("ValidEvent", ['path', 'type'])


@dataclass
class SyncResult:
    """Result of a sync operation."""
    games_synced: List[Game] = field(default_factory=list)
    games_failed: List[tuple[Game, Exception]] = field(default_factory=list)
    unmatched_games: List[Game] = field(default_factory=list)
    success: bool = False
    error_message: Optional[str] = None
    stats: dict = field(default_factory=dict)  # SyncEngine counters

    def summary(self) -> str:
        """Return a human-readable summary of sync results."""
        lines = []
        lines.append(f"Status: {'SUCCESS' if self.success else 'FAILED'}")
        lines.append(f"Games synced: {len(self.games_synced)}")
        lines.append(f"Games failed: {len(self.games_failed)}")
        lines.append(f"Unmatched games: {len(self.unmatched_games)}")
        if self.error_message:
            lines.append(f"Error: {self.error_message}")
        return "\n".join(lines)


class SyncOrchestrator:
    """Orchestrates synchronization operations between local library and ROMM server.
    """

    def __init__(self, local_library: LocalLibrary, romm_server: RetroGameServer):
        """Orchestrator needs to keep and update a single instance of LocalLibrary and RetroGameServer.
        """
        self.lcl = local_library
        self.srv = romm_server
        self.app_cfg = get_config()
    
    @staticmethod
    def build_libraries(app_cfg):
        """Build fresh local and romm libraries in one step. For iniitialization."""
        if app_cfg.SAVE_SYNC_DIR and app_cfg.STATE_SYNC_DIR:
            sync_dirs = [app_cfg.SAVE_SYNC_DIR, app_cfg.STATE_SYNC_DIR]
        else:
            sync_dirs = [app_cfg.ALL_SYNC_DIR]

        srv = RetroGameServer.build_romm_library(app_cfg.ROMM_CREDENTIALS, sync_dirs=sync_dirs)

        STATE.set_phase("building", "Scanning sync folders")
        t0 = tpc()
        lcl = LocalLibrary(sync_dirs)
        lcl.build_local_library()
        games = list(lcl.games.values())
        n_saves = sum(len(g.local_save_files) for g in games)
        n_states = sum(len(g.local_state_files) for g in games)
        n_systems = len({g.platform for g in games})
        logger.info(f"Sync folders scanned: {len(games):,} games, {n_saves:,} saves, {n_states:,} states "
                    f"in {n_systems} system folder(s) ({tpc() - t0:.1f}s).")

        LibraryScanner.match_to_romm(games, srv)
        matched = sum(1 for g in games if g.is_matched)
        logger.info(f"Matched {matched:,} of {len(games):,} games to RomM"
                    + (f" ({len(games) - matched} not found — see 'Unmatched' in the dashboard)." if matched < len(games) else "."))

        return srv, lcl

    @staticmethod
    def _fetch_romm_data(games: List[Game]) -> None:
        """Fetch current saves and states from ROMM for given games.

        Args:
            games: List of games to fetch ROMM state for
        """
        for game in games:
            if game.is_matched:
                logger.debug(f"Fetching ROMM data for {game.name}")
                try:
                    game.fetch_romm_saves()
                    game.fetch_romm_states()
                except Exception as e:
                    logger.error(f"Failed to fetch ROMM data for {game.name}: {e}")

    @staticmethod
    def _push_local_to_romm(games: List[Game], force: bool) -> tuple[List[Game], List[tuple[Game, Exception]]]:
        """Push local saves and states to ROMM.

        Args:
            games: List of games to sync to ROMM

        Returns:
            Tuple of (successful_games, failed_games_with_errors)
        """
        successful = []
        failed = []

        for game in games:
            if game.is_matched:
                logger.debug(f"Pushing local save data for {game.name} to ROMM.")
                try:
                    game.sync_local_saves_to_remote(force)
                    game.sync_local_states_to_remote(force)
                    successful.append(game)
                except Exception as e:
                    logger.error(f"Failed to sync {game.name} to ROMM: {e}")
                    failed.append((game, e))
        return successful, failed

    @staticmethod
    def _validate_games(games: List[Game]) -> tuple[List[Game], List[Game]]:
        """Separate matched and unmatched games.

        Args:
            games: List of games to validate

        Returns:
            Tuple of (matched_games, unmatched_games)
        """
        matched = [g for g in games if g.is_matched]
        unmatched = [g for g in games if not g.is_matched]
        return matched, unmatched

    def full_sync(self, force_sync: bool = False, reuse_libraries: bool = False) -> SyncResult:
        """Perform a complete two-way reconcile between the local library and RomM.
        1. Rebuilds both libraries
        2. Lets the SyncEngine compare every file (push / pull / queue for approval / conflict)
        """
        with STATE.sync_lock:
            logger.info("---------- Starting full sync ----------")
            STATE.sync_started()
            result = SyncResult()
            try:
                STATE.set_phase("building", "Reading RomM library and sync folders")
                # Weekly refresh of RomM's platform list/aliases from GitHub (no-op when fresh).
                from . import platforms as platform_catalog
                platform_catalog.ensure_catalog(self.app_cfg.ROMM_CREDENTIALS.version())
                # Initialize a fresh state for the romm library and local library
                # (skipped right after startup, which just built them)
                if not reuse_libraries:
                    self.srv, self.lcl = self.build_libraries(self.app_cfg)

                games = list(self.lcl.games.values())
                matched_games, unmatched_games = self._validate_games(games)
                result.unmatched_games = unmatched_games
                # games that disappeared from the folder disappear from the GUI too
                STATE.replace_games([])

                summary = SyncEngine().reconcile(games, srv=self.srv, discover_remote=True, force=force_sync)
                result.games_synced = matched_games
                result.success = summary["errors"] == 0
                result.stats = summary

                if result.unmatched_games:
                    logger.info(f"[FULLSYNC] {len(result.unmatched_games)} games could not be matched to ROMM: "
                                f"{', '.join([ug.name for ug in result.unmatched_games])}")
                logger.info(f"[FULLSYNC] {self._fmt(summary)}")
                logger.info("---------- Full sync completed ----------")
                STATE.sync_finished(summary)
                STATE.last_folder_scan = utcnow_iso()
                cleanup_backups()
                from . import recycle
                recycle.purge(int(self.app_cfg.RECYCLE_RETENTION_DAYS))
                return result

            except Exception as e:
                result.success = False
                result.error_message = str(e)
                logger.error(f"[FULLSYNC] Failed! {e}")
                STATE.sync_finished(error=f"Full sync failed: {e}")
                return result

    @staticmethod
    def _fmt(summary: dict) -> str:
        return (f"uploaded {summary['pushed']}, downloaded {summary['pulled']}, "
                f"to recycle bin {summary.get('deleted', 0)}, "
                f"awaiting approval {summary['queued']}, conflicts {summary['conflicts']}, "
                f"in sync {summary['in_sync']}, errors {summary['errors']}")

    def watchdog_sync(self, event_paths: List[Path], event_types: List[str]) -> SyncResult:
        """Incremental sync of the games touched by file system events."""
        with STATE.sync_lock:
            logger.info("---------- Starting watchdog sync ----------")
            STATE.sync_started()
            result = SyncResult()
            try:
                if not event_paths:
                    logger.info("[WATCHDOGSYNC] No events to process")
                    result.success = True
                    STATE.sync_finished({})
                    return result

                STATE.set_phase("building", "Refreshing RomM library")
                # Refresh the romm library so that newly added games are not missed.
                self.srv = RetroGameServer.build_romm_library(self.app_cfg.ROMM_CREDENTIALS,
                                                              sync_dirs=self.lcl.sync_folders)

                for p, t in zip(event_paths, event_types):
                    logger.debug(f"[WATCHDOGSYNC] --- File: {p}, Type: {t}")

                logger.info(f"[WATCHDOGSYNC] Processing {len(event_paths)} file change events...")
                games_to_sync = self.lcl.update_from_watchdog_events(event_paths, self.srv)

                if not games_to_sync:
                    logger.info("[WATCHDOGSYNC] No games in ROMM library found matching local library. Will not sync.")
                    result.error_message = "No matched games found for syncing"
                    STATE.sync_finished({})
                    return result

                summary = SyncEngine().reconcile(games_to_sync, srv=self.srv, discover_remote=False,
                                                 force=self.app_cfg.FORCE_PUSH_SYNC,
                                                 all_games=list(self.lcl.games.values()))
                result.games_synced = games_to_sync
                result.success = summary["errors"] == 0
                result.stats = summary
                logger.info(f"[WATCHDOGSYNC] {self._fmt(summary)}")
                logger.info("---------- Watchdog sync completed ----------")
                STATE.sync_finished(summary)
                return result

            except Exception as e:
                result.success = False
                result.error_message = str(e)
                logger.error(f"[WATCHDOGSYNC] Failed! {e}")
                STATE.sync_finished(error=f"Watch sync failed: {e}")
                return result


class WatchdogSyncManager:
    """Manages watchdog sync state and coordinates filesystem events with ROMM sync operations."""

    def __init__(self, srv: RetroGameServer, lcl: LocalLibrary):
        """Initialize. Called in main to kick off the watchdog sync.
        """
        self.orchestrator = SyncOrchestrator(lcl, srv)
        self.watchdog_delay_seconds = get_config().WATCHDOG_DELAY_SECONDS
        self.pending_events: List[ValidEvent] = []
        self.event_lock = threading.Lock()
        self.timer_thread = None
        self.timer_lock = threading.Lock()
        self.is_syncing = False  # Track if sync is currently in progress

    def schedule_sync(self):
        """Schedule sync to run after watchdog_delay_seconds of file system inactivity.

        If a sync is already in progress, events are queued for the next sync cycle.
        If a timer is already scheduled, it's cancelled and restarted if an event happens within WATCHDOG_DELAY_SECONDS debounce period.
        """
        with self.timer_lock:
            # Don't schedule if sync is already running; events will be processed next cycle
            if self.is_syncing:
                logger.debug("Sync already in progress; events queued for next cycle")
                return

            # Cancel any existing timer to restart the delay
            if self.timer_thread is not None and self.timer_thread.is_alive():
                self.timer_thread.cancel()

            # read live so a change on the Settings page applies to the next file event
            self.timer_thread = threading.Timer(get_config().WATCHDOG_DELAY_SECONDS, self.execute_sync)
            self.timer_thread.daemon = False
            self.timer_thread.start()

    def add_event(self, event_path: str, event_type: str):
        """Add a filesystem event to the pending queue."""
        with self.event_lock:
            self.pending_events.append(ValidEvent(path=event_path, type=event_type))

    def execute_sync(self):
        """Execute the sync operation for pending events."""
        try:
            t0 = tpc()
            with self.timer_lock:
                self.is_syncing = True

            with self.event_lock:
                if not self.pending_events:
                    logger.debug("No pending events to process")
                    return

                event_paths = [Path(p_e.path) for p_e in self.pending_events]
                event_types = [p_e.type for p_e in self.pending_events]
                self.pending_events.clear()

            # Use orchestrator to handle watchdog sync
            result = self.orchestrator.watchdog_sync(event_paths, event_types)

            if result.success:
                logger.info(f"Watchdog sync succeeded! {len(result.games_synced)} games synced in {round(tpc() - t0, 2)} s.")
            else:
                logger.error(f"Watchdog sync had errors: {result.error_message}")
                if result.games_failed:
                    logger.warning(f"{len(result.games_failed)} games failed to sync")

        except Exception as e:
            logger.error(f"Failed to execute sync: {e}", exc_info=True)
        finally:

            logger.info("---------- Continuing to watch for file changes ----------")
            with self.timer_lock:
                self.is_syncing = False
            # Events that arrived while we were syncing were queued but not scheduled.
            with self.event_lock:
                leftover = bool(self.pending_events)
            if leftover:
                self.schedule_sync()

    def cancel_pending(self):
        """Cancel any pending timer."""
        with self.timer_lock:
            if self.timer_thread is not None and self.timer_thread.is_alive():
                self.timer_thread.cancel()


class PeriodicSyncManager:
    """Manages periodic sync, performing full syncs at regular intervals."""

    def __init__(self, srv: RetroGameServer, lcl: LocalLibrary):
        self.orchestrator = SyncOrchestrator(lcl, srv)
        self.sync_interval = get_config().SYNC_INTERVAL_SECONDS
        self.force_sync = get_config().FORCE_PUSH_SYNC
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()

    def trigger(self):
        """Run a sync now (GUI button)."""
        self.wake_event.set()

    def run(self):
        """Run the periodic sync loop. Blocks until stop() is called."""
        try:
            while not self.stop_event.is_set():
                logger.info("Running periodic sync...")
                try:
                    t0 = tpc()
                    self.orchestrator.full_sync(force_sync=get_config().FORCE_PUSH_SYNC)
                    logger.info(f"Periodic sync completed in {round(tpc() - t0, 2)} s.")
                except Exception as e:
                    logger.error(f"Sync failed: {e}")

                interval = int(get_config().SYNC_INTERVAL_SECONDS)  # live (Settings page)
                STATE.next_sync_at = _in_seconds(interval)
                self.wake_event.wait(timeout=interval)
                self.wake_event.clear()
        finally:
            logger.info("Exiting ('Periodic' sync mode)...")

    def stop(self):
        """Signal the sync loop to stop."""
        self.stop_event.set()
        self.wake_event.set()


def _in_seconds(sec: int) -> str:
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=sec)).isoformat()


class RommPoller:
    """Watch mode only reacts to local file events. This thread runs a full reconcile every
    ROMM_POLL_INTERVAL_SECONDS so changes made on RomM (other devices, the web player, Grout...)
    are noticed, and handles 'Sync now' requests from the GUI."""

    def __init__(self, orchestrator: 'SyncOrchestrator', interval: int):
        self.orchestrator = orchestrator
        self.interval = max(int(interval), 0)
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="romm-poller", daemon=True)

    def start(self):
        if STATE.last_folder_scan:  # the startup sync just read everything: remember what RomM looked like
            try:
                api = get_config().ROMM_CREDENTIALS
                self._last_fp = api.fingerprint()
                with api._own_lock:
                    api.own_changes.clear()
            except Exception as e:
                logger.debug(f"Couldn't read RomM's file list yet ({e}); the first check will do a full sync.")
        self.thread.start()

    def trigger(self):
        self.wake_event.set()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()

    def _run(self):
        while not self.stop_event.is_set():
            interval = max(int(get_config().ROMM_POLL_INTERVAL_SECONDS), 0)  # live (Settings page)
            wait = interval if interval > 0 else None
            STATE.next_sync_at = _in_seconds(interval) if interval > 0 else None
            manual = self.wake_event.wait(timeout=wait)  # True = "Sync now" pressed
            self.wake_event.clear()
            if self.stop_event.is_set():
                break
            try:
                if manual:
                    self._full()
                else:
                    self.check()
            except Exception as e:
                logger.error(f"RomM poll failed: {e}")

    # Quiet polling: the sync folders are only read when RomM actually has something new, so disks that hold
    # them (e.g. an Unraid array) aren't woken every few minutes. Local changes arrive through file events.
    _last_fp: Optional[dict] = None

    def _full(self):
        api = get_config().ROMM_CREDENTIALS
        before = set(api.own_changes)
        try:
            fp = api.fingerprint()
        except Exception:
            fp = None
        self.orchestrator.full_sync(force_sync=False)
        STATE.last_romm_check = utcnow_iso()
        if fp is not None:
            self._last_fp = fp
            with api._own_lock:
                api.own_changes -= before

    def romm_changes(self, fp: dict, own: set) -> list:
        """RomM files added, changed or removed since the last check, ignoring the ones this app changed."""
        last = self._last_fp or {}
        changed = [k for k, v in fp.items() if last.get(k) != v] + [k for k in last if k not in fp]
        return [k for k in changed if k not in own]

    def check(self):
        cfg = get_config()
        hours = int(getattr(cfg, "FULL_SCAN_INTERVAL_HOURS", 24) or 0)
        if self._last_fp is None:
            return self._full()
        if hours > 0 and STATE.last_folder_scan:
            from datetime import datetime, timezone
            age = datetime.now(timezone.utc) - datetime.fromisoformat(STATE.last_folder_scan)
            if age.total_seconds() >= hours * 3600:
                logger.info(f"Scheduled folder re-scan (every {hours} h).")
                return self._full()
        api = cfg.ROMM_CREDENTIALS
        before = set(api.own_changes)
        fp = api.fingerprint()
        STATE.last_romm_check = utcnow_iso()
        changes = self.romm_changes(fp, before)
        if not changes:
            logger.debug("RomM check: nothing new on RomM; sync folders not touched.")
            self._last_fp = fp
            with api._own_lock:
                api.own_changes -= before
            return
        logger.info(f"RomM check: {len(changes)} save/state change(s) on RomM; syncing.")
        self._full()


class FileChangeHandler(FileSystemEventHandler):
    """Handle filesystem events and trigger sync operations. Modified from watchdog library"""

    def __init__(self, sync_manager: WatchdogSyncManager):
        self.sync_manager = sync_manager

    def _handle_event(self, event):
        """Common handler for all filesystem events."""
        if not event.is_directory:
            # For renames use the destination: Syncthing (and our own downloads) write a temp file and
            # rename it into place, and the temp name is filtered out as metadata.
            event_path = Path(getattr(event, "dest_path", "") or event.src_path)
            # Filter out any metadata files
            if any(filter_str in event_path.name for filter_str in METADATA_FILE_FILTERS):
                logger.trace(f"Ignored file system event (METADATA): {event.event_type} - {event.src_path}")
                return

            logger.trace(f"Filesystem Event: {event.event_type} - {event_path}")
            self.sync_manager.add_event(str(event_path), event.event_type)
            self.sync_manager.schedule_sync()

    def on_modified(self, event):
        self._handle_event(event)

    def on_created(self, event):
        self._handle_event(event)

    def on_deleted(self, event):
        self._handle_event(event)

    def on_moved(self, event):
        self._handle_event(event)