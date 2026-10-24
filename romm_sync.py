# romm_sync — sync emulator saves/states between a Syncthing folder and RomM.
# Original project: https://github.com/amn-96/romm_syncthing_sync (c) amn-96, MIT License.
# This fork adds a web GUI and two-way sync with approvals/conflict handling.

# external library imports
from watchdog.observers import Observer
from typing import Callable
import signal
import sys
import threading
import time
from dotenv import load_dotenv
# logging special import
from loguru import logger
# internal imports
from src.config import get_config
from src.sync_state import STATE, install_log_sink
from src.sync_orchestrator import (SyncOrchestrator, WatchdogSyncManager, PeriodicSyncManager,
                                   FileChangeHandler, RommPoller)

# Load environment variables early
load_dotenv()


def initialize_romm_sync():
    # Get the app's configuration from docker env variables. See .env.example
    app_cfg = get_config()
    install_log_sink()
    from src import settings as dashboard_settings
    dashboard_settings.apply_to_config(app_cfg)  # values saved on the Settings page override the container
    from src import notify
    notify.seed_from_state()
    from src.version import get_version
    logger.info(f"romm_sync v{get_version()} — https://github.com/sireteanu/romm_syncthing_sync")
    STATE.sync_mode = app_cfg.SYNC_MODE

    if app_cfg.WEB_UI:
        from src.web import start_web_server
        start_web_server(app_cfg)

    # RomM's platform list + folder aliases (from GitHub, cached in /data). In the background so a
    # server without internet access never delays startup.
    from src import platforms as platform_catalog
    catalog_thread = threading.Thread(target=lambda: platform_catalog.ensure_catalog(app_cfg.ROMM_CREDENTIALS.version()),
                                      name="platform-catalog", daemon=True)
    catalog_thread.start()
    if not platform_catalog.get_catalog().get("platforms"):
        # First run: give the download a moment so the first sync can already use RomM's folder aliases.
        catalog_thread.join(timeout=30)

    # Say what the sync folders look like from inside the container (most setup problems show up here).
    for f in platform_catalog.sync_folder_report([app_cfg.SAVE_SYNC_DIR, app_cfg.STATE_SYNC_DIR, app_cfg.ALL_SYNC_DIR]):
        if not f["exists"]:
            logger.error(f"Sync folder {f['path']} doesn't exist inside the container.")
        elif f.get("error"):
            logger.error(f"Sync folder {f['path']}: {f['error']}")
        elif not f["folders"]:
            logger.warning(f"Sync folder {f['path']} has no system subfolders"
                           + (f" ({f['files']} loose files)." if f["files"] else " — it's empty. Check the host path mapped to it."))
        else:
            shown = ", ".join(f["folders"][:12]) + (", …" if len(f["folders"]) > 12 else "")
            logger.info(f"Sync folder {f['path']}: {len(f['folders'])} system folders ({shown})")
    platform_catalog.effective_mapping_path(app_cfg.PLATFORM_MAP_PATH)  # warns about folder-instead-of-file mounts
    platform_catalog.ensure_mapping_seeded(app_cfg.PLATFORM_MAP_PATH)

    romm = app_cfg.ROMM_CREDENTIALS
    logger.info(f"Connecting to RomM at {romm.url} (auth: {romm.auth_type})...")
    attempt = 0
    while True:
        try:
            version = romm.version()
            if version:
                STATE.romm_version = version
                logger.info(f"Connected to RomM {version}.")
            # First sync. Compares all save states
            logger.info("Building local and romm libraries. This may take a while for large libraries...")
            STATE.set_phase("building", "Reading RomM library and sync folders")
            srv, lcl = SyncOrchestrator.build_libraries(app_cfg=app_cfg)
            break
        except Exception as e:
            wait = min(300, 15 * 2 ** attempt)
            attempt += 1
            logger.error(f"Startup failed: {e} Retrying in {wait}s.")
            STATE.sync_finished(error=f"{e} — retrying in {wait}s")
            time.sleep(wait)

    orchestrator = SyncOrchestrator(local_library=lcl, romm_server=srv)
    if app_cfg.FULL_INITIAL_SYNC == "false":
        logger.info("Initial sync skipped ('FULL_INITIAL_SYNC' = false).")
        STATE.set_phase("idle", "Waiting for changes")
    else:
        logger.info("Performing initial sync.")
        force_sync: bool = app_cfg.FULL_INITIAL_SYNC == "force"
        orchestrator.full_sync(force_sync=force_sync, reuse_libraries=True)

    return app_cfg, orchestrator.srv, orchestrator.lcl


class AppExit():
    def __init__(self, exit_handler):
        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, exit_handler)
        signal.signal(signal.SIGINT, exit_handler)

    @classmethod
    def periodic(cls, stop_callback: Callable[[], None]):
        """Factory for periodic sync mode exit handler"""
        def handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down gracefully...")
            stop_callback()
        return cls(handler)

    @classmethod
    def watchdog(cls, sync_manager: WatchdogSyncManager, observer_stop_callback: Callable[[], None]):
        """Factory for watchdog sync mode exit handler"""
        def handler(signum, frame):
            logger.info(f"Received signal {signum}, shutting down gracefully...")
            sync_manager.cancel_pending()
            observer_stop_callback()
        return cls(handler)


def run_periodic_sync(srv, lcl):
    """Run ROMM sync in periodic mode, performing full syncs at regular intervals."""
    sync_manager = PeriodicSyncManager(srv=srv, lcl=lcl)
    STATE.request_sync_callback = sync_manager.trigger

    AppExit.periodic(sync_manager.stop)  # register the exit signal handler

    sync_manager.run()


def run_watch_sync(srv, lcl, app_cfg):
    """Run ROMM sync in watch mode, syncing on file system changes.
    A background poller also re-checks RomM every ROMM_POLL_INTERVAL_SECONDS for changes made elsewhere."""

    # Create sync manager to control the watchdog sync
    sync_manager = WatchdogSyncManager(srv=srv, lcl=lcl)

    poller = RommPoller(sync_manager.orchestrator, app_cfg.ROMM_POLL_INTERVAL_SECONDS)
    STATE.request_sync_callback = poller.trigger
    poller.start()

    # Create event handler with sync manager
    event_handler = FileChangeHandler(sync_manager)
    observer = Observer()

    def stop_all():
        poller.stop()
        observer.stop()

    AppExit.watchdog(sync_manager=sync_manager, observer_stop_callback=stop_all)  # register the exit signal handler

    # Schedule observer for each directory
    for watch_dir in lcl.sync_folders:
        observer.schedule(event_handler, str(watch_dir), recursive=True)
        logger.info(f"Watching directory: {watch_dir}")

    # Start observer
    observer.start()
    logger.info("Observer started.")

    try:
        # Keep the observer running
        observer.join()
    finally:
        observer.stop()
        observer.join()
        logger.info("Exiting ('Watch' sync mode)...")


def main():
    """Main orchestration function."""
    app_cfg, srv, lcl = initialize_romm_sync()

    logger.info("Romm_sync starting!")

    if app_cfg.SYNC_MODE == "periodic":
        run_periodic_sync(srv, lcl)
    elif app_cfg.SYNC_MODE == "watch":
        run_watch_sync(srv, lcl, app_cfg)
    else:
        logger.critical(f"Invalid sync mode {app_cfg.SYNC_MODE}. Allowed: 'periodic', 'watch'.\n\nExiting...")
        sys.exit(1)


if __name__ == "__main__":
    main()
