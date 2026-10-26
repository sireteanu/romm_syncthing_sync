"""Classes for ROMM library and Local Library (saves/states).
In general, these are one step abstracted over Game, so they operate on groups of Game at once."""
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict
from loguru import logger
import os
import re
import yaml

# program imports
from .games_class import Game
from .romm_api_func import RommUser
from .config import METADATA_FILE_FILTERS, get_config
from . import platforms as platform_catalog


class RetroGameServer:
    def __init__(self, library, platform_map):
        self.library: List[dict] = library
        self.PLATFORM_MAP: dict = platform_map
        # Used for automatic platform resolution (see src/platforms.py)
        self.library_slugs = {g['platform_slug'] for g in library}
        self.library_fs_slugs = {str(g['platform_fs_slug']).lower(): g['platform_slug']
                                 for g in library if g.get('platform_fs_slug')}

    @staticmethod
    def _load_platform_mapping() -> dict:
        """Load platform mapping from YAML file.

        Expected format:
            romm_platform_slug:
              - local_folder_name_1
              - local_folder_name_2

        Falls back to empty dictionary on errors, which means exact platform slug matching only.
        """
        platform_map = {}  # the `finally` below returns this
        try:
            # The web UI's mapping (DATA_DIR/platform_mapping.yaml) wins once it exists.
            platform_map_path = platform_catalog.effective_mapping_path(get_config().PLATFORM_MAP_PATH)
            logger.debug(f"Attempting to load {str(platform_map_path)}")
            
            data = None

            if platform_map_path is None:
                logger.debug("No saved platform mapping; using automatic platform matching only.")
                return {}

            if platform_map_path is not None:
                with open(platform_map_path, 'r') as f:
                    data = yaml.safe_load(f)

            if data is None:
                # safe fallback if the file is empty
                raise ValueError("Platform mapping file is empty.")

            if not isinstance(data, dict):
                # safe fallback if invalid yaml
                raise ValueError(f"Platform mapping must be a dictionary, got {type(data).__name__}.")

            # Validate structure: each value should be a list of strings (safe fallbacks again)
            for romm_slug, local_names in data.items():
                if not isinstance(local_names, list):
                    raise ValueError(f"Platform '{romm_slug}' must map to a list of strings, got {type(local_names).__name__}.")

                if not all(isinstance(name, str) for name in local_names):
                    raise ValueError(f"Platform '{romm_slug}' contains non-string values.")

            platform_map = data

        except yaml.YAMLError as e:
            logger.error(f"Failed to parse platform mapping YAML: {e}. Falling back to exact matching.")
            platform_map = {}
        except IOError as e:
            logger.error(f"Failed to read platform mapping file: {e}. Falling back to exact matching.")
            platform_map = {}
        except ValueError as e:
            logger.error(f"{e} Falling back to exact matching.")
            platform_map = {}
        except Exception as e:
            logger.error(f"Unexpected error while trying to read platform mapping file: {e}. Falling back to exact matching.")
            platform_map = {}
        
        finally:
            return platform_map

    def local_folder_matches_slug(self, folder_name: str, romm_slug: str) -> bool:
        """True if a local platform folder maps to this RomM platform slug (via platform map or exact name)."""
        mapped = self.get_romm_platform(folder_name)
        return (mapped or folder_name) == romm_slug

    def get_romm_platform(self, local_platform_name: str) -> str | None:
        """Get the ROMM platform slug for a local platform folder name.

        Order: platform_mapping.yaml -> exact RomM slug -> folder name used in your RomM library
        -> RomM's built-in folder aliases (fetched from rommapp/romm on GitHub). Case-insensitive.
        Returns None if nothing matches.
        """
        slug, how = self.resolve_platform(local_platform_name)
        return slug

    def resolve_platform(self, local_platform_name: str):
        """-> (slug or None, how) where how is mapping | exact | romm-folder | romm-alias | none."""
        aliases = platform_catalog.get_catalog().get("aliases", {})
        return platform_catalog.resolve(local_platform_name, self.PLATFORM_MAP, self.library_slugs,
                                        self.library_fs_slugs, aliases)

    # Cached RomM library, reused between syncs (see build_romm_library)
    _cache: Optional[dict] = None
    LIBRARY_MAX_AGE = 6 * 3600  # seconds before a full re-read regardless
    _last_unresolved: Optional[tuple] = None

    LIBRARY_COLUMNS = {
        'id', 'name', 'platform_slug', 'platform_id', 'fs_name', 'fs_size_bytes', 'platform_display_name',
        # used by the web GUI / pull sync
        'fs_name_no_ext', 'platform_fs_slug', 'path_cover_small', 'path_cover_large', 'url_cover',
        'merged_screenshots',
    }

    @classmethod
    def _trim(cls, page):
        # Keep only the fields we use, page by page (full ROM records are large).
        return [{k: v for k, v in item.items() if k in cls.LIBRARY_COLUMNS} for item in page]

    @classmethod
    def _platforms_to_fetch(cls, romm_user: RommUser, sync_dirs, platform_map: dict):
        """RomM platforms that have a matching folder in the sync folders.
        -> ([(platform_id, label)], {platform_id: rom_count}), or (None, None) = read everything."""
        if not sync_dirs:
            return None, None
        try:
            platforms = romm_user.list_platforms()
        except Exception as e:
            logger.info(f"Couldn't list RomM platforms ({e}); reading the whole library.")
            return None, None
        slugs = {p["slug"] for p in platforms}
        fs = {str(p.get("fs_slug", "")).lower(): p["slug"] for p in platforms if p.get("fs_slug")}
        aliases = platform_catalog.get_catalog().get("aliases", {})
        wanted, unresolved = set(), []
        for folder in platform_catalog.local_platform_folders(sync_dirs):
            slug, _ = platform_catalog.resolve(folder, platform_map, slugs, fs, aliases)
            if slug in slugs:
                wanted.add(slug)
            else:
                unresolved.append(folder)
        if unresolved and tuple(unresolved) != cls._last_unresolved:
            logger.info(f"Sync folders without a RomM platform (see the Platforms page): {', '.join(unresolved)}")
        cls._last_unresolved = tuple(unresolved)
        chosen = [(p["id"], p.get("custom_name") or p.get("display_name") or p.get("name") or p["slug"])
                  for p in platforms if p["slug"] in wanted]
        counts = {p["id"]: p.get("rom_count", 0) for p in platforms if p["slug"] in wanted}
        cls._all_total = sum(p.get("rom_count", 0) for p in platforms)
        return chosen, counts

    @classmethod
    def build_romm_library(cls,
                           romm_user: RommUser,
                           sync_dirs=None,
                           force: bool = False):
        """Factory method to instantiate a RetroGameServerClass with romm library info.

        With sync_dirs, only platforms that have a folder in the sync folders are read, and the result is
        cached: later calls re-use it when RomM's per-platform ROM counts are unchanged, fetching only ROMs
        changed since the last read (RomM 5.x `updated_after`). A full re-read happens when counts or the set
        of platforms change, after LIBRARY_MAX_AGE, or with force=True.
        """
        import time
        from datetime import datetime, timezone, timedelta
        from .sync_state import ProgressReporter, STATE

        platform_map = cls._load_platform_mapping()
        t0 = time.perf_counter()
        chosen, counts = cls._platforms_to_fetch(romm_user, sync_dirs, platform_map)
        progress = ProgressReporter("building", "Reading RomM library")
        cache = cls._cache
        key = tuple(sorted(counts)) if counts is not None else None

        reusable = (not force and cache is not None and key is not None and cache["key"] == key
                    and cache["counts"] == counts and time.time() - cache["at"] < cls.LIBRARY_MAX_AGE)
        if reusable:
            rows = {r["id"]: r for r in cache["library"]}
            romm_major = int((STATE.romm_version or "0").split(".")[0] or 0) if STATE.romm_version else 0
            changed = []
            if romm_major >= 5 and chosen:
                since = (cache["fetched_at"] - timedelta(minutes=1)).isoformat()
                try:
                    changed = romm_user._fetch_roms({"platform_ids": [pid for pid, _ in chosen], "updated_after": since},
                                                    "changed ROMs", cls._trim, None)
                    # Safety: if RomM didn't apply the filter we'd get the whole library back; treat as a full read.
                    if len(changed) > max(50, len(rows) // 2):
                        reusable = False
                except Exception as e:
                    logger.debug(f"Incremental library refresh failed ({e}); doing a full read.")
                    reusable = False
            if reusable:
                for r in changed:
                    rows[r["id"]] = r
                library = list(rows.values())
                cache.update(library=library, fetched_at=datetime.now(timezone.utc))
                logger.info(f"RomM library unchanged — reused {len(library):,} ROMs"
                            + (f", refreshed {len(changed)} changed" if changed else "")
                            + f" ({time.perf_counter() - t0:.1f}s).")
                return cls(library=library, platform_map=platform_map)

        if counts is not None:
            total = sum(counts.values())
            logger.info(f"Reading {total:,} ROMs from {len(chosen)} RomM platform(s) that have sync folders "
                        f"(skipping {getattr(cls, '_all_total', total) - total:,} ROMs on other platforms).")
        fetched_at = datetime.now(timezone.utc)
        data = romm_user.get_full_library(platform_ids=chosen, trim=cls._trim, on_progress=progress)
        library = data['items']
        logger.info(f"RomM library read: {len(library):,} ROMs in {time.perf_counter() - t0:.1f}s.")
        if key is not None:
            cls._cache = {"key": key, "counts": counts, "library": library, "at": time.time(), "fetched_at": fetched_at}
        return cls(library=library, platform_map=platform_map)

    def count(self):
        return len(self.library)

    def size_gb(self):
        return round(sum(g['fs_size_bytes'] for g in self.library) / 1e9, 2)

    def __repr__(self):
        platform_count = len({g['platform_slug'] for g in self.library})
        return (f"RetroGameServer(games={self.count()}, "
                f"size_gb={self.size_gb()}, "
                f"platforms={platform_count})")

    def summary(self, verbose=True):
        print(f"Games: {self.count()}, Library Size: {self.size_gb()} GB")
        platforms = defaultdict(list)
        for g in self.library:
            platforms[g['platform_slug']].append(g['name'])
        
        for idx, (platform_slug, games) in enumerate(platforms.items()):
            print(f"┌─ {platform_slug}: {len(games)} game(s)")
            if verbose:
                for game in games:
                    print(f"│  - {game}")
                if idx < len(platforms) - 1:
                    print("│")
                    print("├" + "─" * 40)
                    print("│")


class LocalLibrary:
    """Catalog of games from local sync folder, matched to RetroGameServer library."""

    def __init__(self, sync_folders: List[Path]):
        self.sync_folders: List[Path] = sync_folders
        self.games: Dict[str, Game] = {}

    def _rescan_local_saves_and_states(self, game: Game) -> None:
        """Rescans and refreshes the existing list of save and state files for a particular game."""
        
        logger.debug(f"Refreshing saves and states for {game.name}")

        # Clear existing saves and states
        game.local_save_files = []
        game.local_state_files = {}

        for sync_dir in self.sync_folders:
            try:
                platform_dir = sync_dir / game.platform

                files = LibraryScanner._collect_games(platform_dir)
                games_dict = LibraryScanner._build_games_dict(files, game.platform)

                if game.name in games_dict:
                    file_info = games_dict[game.name]
                    LibraryScanner._load_local_saves_and_states(g=game, file_info=file_info)
                    logger.debug(f"Success! Found {len(game.local_save_files)} save(s), {len(game.local_state_files)} state(s).")
            
            except FileNotFoundError as fe:
                logger.debug(f"Not found in {sync_dir}: {fe}")
            except KeyError as ke:
                logger.debug(f"{ke} (this should never happen).")
            except Exception as e:
                logger.debug(f"Unexpected error: {e}")

    def build_local_library(self):
        """Scan local (syncthing or otherwise) folder and build local game catalog. Public class method.
        Aggregates all save files and state files for each game into a single Game object that is then matched to a romm server-side entry. 
        Game name is derived from base filename. Assumes that ROM's in ROMM are the same as that of the savefiles in the local library.
        """

        # Iterate through platform directories within each sync_folder
        for sync_dir in self.sync_folders:
            for platform_dir in sync_dir.iterdir():
                if not platform_dir.is_dir():
                    continue

                platform_name = platform_dir.name

                # Discover and filter all game files in this platform directory
                files = LibraryScanner._collect_games(platform_dir)

                # Build games dictionary from files
                games_dict = LibraryScanner._build_games_dict(files=files, platform_name=platform_name)

                # Create Game objects for all games in this platform
                logger.debug("Building 'Game' objects for local library...")
                for game_name, file_info in games_dict.items():
                    logger.debug(f"Processing game: {game_name} ({len(file_info['saves'])} saves, {len(file_info['states'])} states)")
                    # Keyed by system folder + name: the same game in saves/gba and states/gba is one game,
                    # but gb/Tetris and gbc/Tetris are two different games.
                    key = self.game_key(platform_name, game_name)
                    if key in self.games:
                        existing_game = self.games[key]
                        LibraryScanner._load_local_saves_and_states(g=existing_game, file_info=file_info)
                    else:
                        new_game = Game(name=game_name, path=file_info['path'], platform=file_info['platform'])
                        LibraryScanner._load_local_saves_and_states(g=new_game, file_info=file_info)
                        self.games[key] = new_game

                    logger.debug(f"  Added {game_name} to library")

    # region Utility Functions
    @staticmethod
    def game_key(platform: str, game_name: str) -> str:
        return f"{platform.lower()}/{game_name}"

    def get_game(self, platform: str, game_name: str) -> Optional[Game]:
        """Get a game from the library by system folder + name."""
        return self.games.get(self.game_key(platform, game_name))

    def get_unmatched_games(self) -> List[Game]:
        """Return list of local games that couldn't be matched to ROMM."""
        return [game for game in self.games.values() if not game.is_matched]

    def get_matched_games(self) -> List[Game]:
        """Return list of local games successfully matched to ROMM."""
        return [game for game in self.games.values() if game.is_matched]

    def summary(self) -> None:
        """Print summary statistics of the catalog."""
        total = len(self.games)
        matched = sum(1 for g in self.games.values() if g.is_matched)
        with_saves = sum(1 for g in self.games.values() if g.local_save_files)

        logger.debug(f"Total games in sync folder: {total}")
        if total > 0:
            logger.debug(f"Matched to ROMM: {matched} ({matched/total*100:.1f}%)")
            logger.debug(f"With local saves: {with_saves}")
            logger.debug(f"Unmatched: {total - matched}.")
        else:
            logger.warning(f"No saves or states were found in {", ".join([str(sf) for sf in self.sync_folders])}.")
    # endregion

    # region File Watcher / Sync Functions
    def _add_new_game_from_watchdog_event(self,
                                          event_file_path: Path,
                                          romm_library: RetroGameServer) -> Game | None:
        """Detect and add a new game based on a watchdog event file path.

        Expects the directory structure: sync_folder/platform_name/game_name.ext
        Follows the same initialization pattern as build_from_scratch for a single game.
        """
        try:
            # Extract platform and game name from path
            # Expected structure: sync_folder/platform_name/game_name.ext
            platform_dir = event_file_path.parent
            platform_name = platform_dir.name
            game_name, _ = LibraryScanner.parse_save_filename(event_file_path.name)

            # Scan platform directory and collect files for this game
            files = LibraryScanner._collect_games(platform_dir)
            games_dict = LibraryScanner._build_games_dict(files, platform_name)

            # Create the Game object
            file_info = games_dict[game_name]
            new_game = Game(name=game_name, path=file_info['path'], platform=file_info['platform'])

            # Match to ROMM (ROMs already claimed by other local games are off limits)
            LibraryScanner.match_to_romm([new_game], romm_library,
                                         taken={g.romm_id for g in self.games.values() if g.is_matched})

            # Load local saves and states
            LibraryScanner._load_local_saves_and_states(g=new_game, file_info=file_info)

            # Add to games dict
            self.games[self.game_key(platform_name, game_name)] = new_game
            logger.info(f"[WATCHDOGSYNC] Added new game to library: {game_name}")

            return new_game

        except Exception as e:
            logger.error(f"[WATCHDOGSYNC] Error adding new game from file change event: {e}")
            return None

    def update_from_watchdog_events(self,
                                    event_paths: List[Path],
                                    romm_library: RetroGameServer) -> List[Game]:
        """
        Identifies which games have had file changes and ensures they're in the
        library (adding new games if needed). The affected games are then rescanned to pick up
        any new save/state files before being returned for sync operations.

        Args:
            event_paths: List of file paths from watchdog events (event.src_path)
            romm_library: RetroGameServer instance for matching new games to ROMM

        Returns:
            List of affected Game objects (deduplicated) ready for sync operations
        """
        game_changed_files = LibraryScanner._parse_watchdog_events(event_paths=event_paths)

        affected_games_dict = {}  # "platform/game" -> Game object

        for (platform_name, game_name), changed_files in game_changed_files.items():
            # Find or create the game
            matching_Game = self.get_game(platform_name, game_name)
            if not matching_Game:
                logger.info(f"[WATCHDOGSYNC] New game detected: {game_name}")
                # Use one of the changed files to add a game. It will be named the same as the ROM by save file/state convention
                matching_Game = self._add_new_game_from_watchdog_event(next(iter(changed_files)), romm_library)
                if matching_Game is None:
                    logger.warning(f"[WATCHDOGSYNC] Failed to add new game: {game_name}")
                    continue
            else:
                logger.info(f"[WATCHDOGSYNC] {game_name} has {len(changed_files)} files to be synced.")

            # Rescan the game's directory to pick up any new/updated save files.
            self._rescan_local_saves_and_states(matching_Game)
            affected_games_dict[self.game_key(platform_name, game_name)] = matching_Game

        return list(affected_games_dict.values())
    # endregion


class LibraryScanner:
    """Utility class for library scanning functions to interface between LocalLibrary, RetroGameServer, and SyncOrchestrator"""

    _warned_unmatched: set = set()  # warn about each unmatched game once, not on every sync

    # region LocalLibrary helpers
    @staticmethod
    def _collect_games(platform_dir: Path) -> List[Path]:
        """Discover and filter all game files in a platform directory.

        Iterates through files in the platform directory and returns a list of valid game files,
        excluding metadata files and directories defined in METADATA_FILE_FILTERS.
        """
        files = []

        for file_path in platform_dir.iterdir():
            if not file_path.is_file():
                continue
            # Skip various metadata files and directories
            if any(filter_str in file_path.name for filter_str in METADATA_FILE_FILTERS):
                continue
            files.append(file_path)

        return files

    @staticmethod
    def _add_game_dict_entry(file_path: Path, platform_name: str, games_dict: dict) -> dict:
        """Process a file and return updated games_dict with its categorized content.

        Extracts game name and file type from filename, then adds the file to the
        appropriate category (save, state, or state_screen) in games_dict.

        The dict has one key per unique file "stem". Retroarch emulator cores should name files with similar patterns:
            - "game(stem)".srm
            - "game(stem).state"
            - "game(stem).
        Specials: 
            - melonDS uses .ml{n}.
            - regular DS is .dsv or .sav. (all "saves" are captured under the else block)

        Args:
            file_path: Path object for the file to process
            platform_name: Name of the platform directory
            games_dict: Dictionary to update with file information

        Returns:
            Updated games_dict with file categorized and added
        """
        game_name, file_type = LibraryScanner.parse_save_filename(file_path.name)
        if file_type is None:
            logger.debug(f"Ignoring {file_path.name} (not a save or state)")
            return games_dict

        # Initialize game entry if not seen before
        if game_name not in games_dict:
            games_dict[game_name] = {
                'path': file_path,
                'platform': platform_name,
                'saves': [],
                'states': [],
                'screens': []
            }

        # Categorize file
        if file_type == 'save':
            games_dict[game_name]['saves'].append(file_path)
        elif file_type == 'state':
            games_dict[game_name]['states'].append(file_path)
        elif file_type == 'state_screen':
            games_dict[game_name]['screens'].append(file_path)

        return games_dict

    # Files in a system folder that are never saves (cover art, notes, temp files...)
    IGNORED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".txt", ".log", ".tmp", ".bak", ".db", ".ini",
                          ".cfg", ".xml", ".json", ".m3u", ".zip", ".7z"}

    @staticmethod
    def parse_save_filename(filename: str):
        """-> (game_name, 'save' | 'state' | 'state_screen' | None).

        The game name is the ROM's file name, which may itself contain dots ("Dr. Mario (USA).srm").
          Game.srm / Game.sav / Game.dsv ...      -> save
          Game.state, Game.state1, Game.state.auto -> state (RetroArch)
          Game.ml0 ...                              -> state (melonDS)
          Game.state1.png / Game.state.auto.png     -> state_screen
        """
        m = re.match(r'^(.+?)\.state(?:\d+|\.auto)?\.png$', filename)
        if m:
            return m.group(1), 'state_screen'
        m = re.match(r'^(.+?)\.state(?:\d+|\.auto)?$', filename) or re.match(r'^(.+?)\.ml\d+$', filename)
        if m:
            return m.group(1), 'state'
        stem, ext = os.path.splitext(filename)
        if not stem or not ext or ext.lower() in LibraryScanner.IGNORED_EXTENSIONS:
            return None, None
        return stem, 'save'

    @staticmethod
    def _load_local_saves_and_states(g: Game, file_info: dict):
        """Loads and populates local save and state files from file_info dict into a Game object."""

        g.add_local_saves(game_dict_entry=file_info)

        g.add_local_states(game_dict_entry=file_info)

    @staticmethod
    def _build_games_dict(files: List[Path], platform_name: str) -> dict:
        """Build a fresh games_dict, a dictionary containing the required data for each game:
        {game_name:
            {'platform': str,
            'saves': [],
            'states': [],
            'screens': []
            }
        }
        This is a factory function that checks all the files given to it.
        """
        games_dict = {}
        for file_path in files:
            games_dict = LibraryScanner._add_game_dict_entry(file_path, platform_name, games_dict)

        return games_dict
    # endregion

    # RomM - LocalLibrary interface
    @staticmethod
    def match_to_romm(games: List[Game],
                      romm_library: RetroGameServer,
                      taken: Optional[set] = None) -> None:
        """Match local games to RomM ROMs, narrowed to the folder's platform.

        Two passes so exact names always win: first every game whose name is exactly a ROM's file name,
        then the rest by prefix (find_rom), never taking a ROM another local game in this batch (or in
        `taken`) already claimed. So local "Fallen" can't grab "Fallen Angel" from local "Fallen Angel".
        """
        taken = set(taken or ())
        by_slug: Dict[Optional[str], List[dict]] = {}
        plans = []
        for game in games:
            if not game.platform:
                logger.warning(f"Failed to match {game.name}. Games must be organized by platform/content directory. Check your local library structure.")
                continue
            slug = romm_library.get_romm_platform(game.platform)
            logger.debug(f"{game.name} -- Matched platform {game.platform} to slug {slug}.")
            key = slug or game.platform
            if key not in by_slug:
                by_slug[key] = [g for g in romm_library.library if g['platform_slug'] == key]
            plans.append((game, by_slug[key]))

        # Pass 0: links made by hand on the Saves page (any platform).
        from . import links as manual_links
        linked = manual_links.load()
        by_id = {r['id']: r for r in romm_library.library} if linked else {}
        rest = []
        for game, platform_games in plans:
            rid = linked.get(manual_links.key(game.platform, game.name))
            if rid is not None and rid in by_id:
                LibraryScanner._set_match(game, by_id[rid], "link")
                taken.add(rid)
            else:
                rest.append((game, platform_games))

        # Pass 1: exact names. Each ROM goes to one local game per system folder pass.
        pending = []
        for game, platform_games in rest:
            match = LibraryScanner.find_rom(game.name, platform_games, exact_only=True)
            if match is not None:
                LibraryScanner._set_match(game, match, "exact")
                taken.add(match['id'])
            else:
                pending.append((game, platform_games))

        # Pass 2: closest prefix match among ROMs nobody claimed exactly.
        still = []
        for game, platform_games in pending:
            match = LibraryScanner.find_rom(game.name, platform_games, exclude_ids=taken)
            if match is not None:
                LibraryScanner._set_match(game, match, "prefix")
                taken.add(match['id'])
            else:
                still.append((game, platform_games))

        # Pass 3: same title, ignoring punctuation, region/version tags and "Legend of Zelda, The"-style
        # articles ("Zelda - Spirit Tracks" won't match, "The Legend of Zelda: Spirit Tracks" will).
        for game, platform_games in still:
            match = LibraryScanner.find_rom_by_title(game.name, platform_games, exclude_ids=taken)
            if match is not None:
                LibraryScanner._set_match(game, match, "title")
                taken.add(match['id'])
                logger.info(f"Matched '{game.name}' to '{match.get('fs_name')}' by title.")
                continue
            game.is_matched = False
            msg = (f"Could not match {game.name} with platform '{game.platform}' to ROMM server. "
                   "Check this folder on the Platforms page and that the game is in your RomM library.")
            warn_key = f"{game.platform}/{game.name}"
            if warn_key in LibraryScanner._warned_unmatched:
                logger.debug(msg)  # already warned once
            else:
                LibraryScanner._warned_unmatched.add(warn_key)
                logger.warning(msg)

    @staticmethod
    def _set_match(game: Game, row: dict, how: str) -> None:
        if how in ("title", "link"):
            # RomM files for this ROM are written under the local game's name, not the ROM file's name,
            # because that's the name the emulator on your devices uses. (A copy of the row, so other
            # users of the shared library row aren't affected.)
            row = dict(row, _local_stem=game.name)
        game.set_romm_data(row)
        game.match_how = how

    _TAG = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
    _ARTICLE = re.compile(r"^(.*?),\s*(the|a|an|le|la|les|l'|der|die|das|el|los|las|il)\b(.*)$", re.I)

    @classmethod
    def title_key(cls, text: str) -> str:
        """'Legend of Zelda, The - Spirit Tracks (USA) (En,Fr)' and 'The Legend of Zelda: Spirit Tracks'
        both become 'the legend of zelda spirit tracks'."""
        t = cls._TAG.sub("", str(text)).strip()
        m = cls._ARTICLE.match(t)
        if m:
            t = f"{m.group(2)} {m.group(1)}{m.group(3)}"
        t = t.casefold().replace("&", " and ")
        t = re.sub(r"[\W_]+", " ", t)
        return " ".join(t.split())

    @classmethod
    def _tags(cls, text: str) -> set:
        return {x.strip().casefold() for tag in re.findall(r"[\(\[]([^\)\]]*)[\)\]]", str(text))
                for x in tag.split(",")}

    @classmethod
    def find_rom_by_title(cls, save_name: str, platform_games: List[dict],
                          exclude_ids: Optional[set] = None) -> Optional[dict]:
        """Unique ROM whose title (file name or RomM name) equals the save's, ignoring tags/punctuation.
        Several candidates: the one sharing most region/version tags with the save; a tie = no match."""
        want = cls.title_key(save_name)
        if not want:
            return None
        cands = []
        for g in platform_games:
            if exclude_ids and g.get('id') in exclude_ids:
                continue
            stem = g.get('fs_name_no_ext') or os.path.splitext(g.get('fs_name') or "")[0]
            if want in (cls.title_key(stem), cls.title_key(g.get('name') or "")):
                cands.append(g)
        if len(cands) <= 1:
            return cands[0] if cands else None
        mine = cls._tags(save_name)
        scored = sorted(((len(mine & cls._tags(g.get('fs_name') or "")), g) for g in cands),
                        key=lambda x: -x[0])
        if scored[0][0] > scored[1][0]:
            return scored[0][1]
        logger.info(f"'{save_name}' could be any of {len(cands)} ROMs ({', '.join(g.get('fs_name', '?') for g in cands[:4])}); "
                    "link it by hand on the Saves page.")
        return None

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(str(text).casefold().split())

    @staticmethod
    def find_rom(save_name: str, platform_games: List[dict], exclude_ids: Optional[set] = None,
                 exact_only: bool = False) -> Optional[dict]:
        """Pick the RomM ROM a save belongs to. RetroArch names saves after the ROM file, so:
          1. exact: ROM file name without extension (or a multi-file ROM's folder name) == save name
          2. prefix: ROM file name starts with the save name followed by a space/bracket/dot
             (e.g. a save "Pokemon Red" for "Pokemon Red (USA, Europe).gb"); the closest one wins
        Substrings in the middle ("Mario" in "Super Mario World") never match.
        """
        want = LibraryScanner._norm(save_name)
        exact = []
        for g in platform_games:
            fs_name = g.get('fs_name') or ""
            stem = g.get('fs_name_no_ext') or os.path.splitext(fs_name)[0]
            if want in (LibraryScanner._norm(stem), LibraryScanner._norm(fs_name)):
                exact.append(g)
        if exact:
            if len(exact) > 1:
                logger.debug(f"{save_name}: {len(exact)} exact ROM matches, using {exact[0]['fs_name']}")
            return exact[0]
        if exact_only:
            return None
        prefix = []
        for g in platform_games:
            if exclude_ids and g.get('id') in exclude_ids:
                continue  # already claimed by another local game whose name matches it exactly
            fs = LibraryScanner._norm(g.get('fs_name') or "")
            if fs.startswith(want) and len(fs) > len(want) and fs[len(want)] in " ([.-_":
                prefix.append(g)
        if not prefix:
            return None
        # Closest (shortest) name wins; ties are broken alphabetically so it's stable, e.g. "(Disc 1)" before "(Disc 2)".
        best = min(prefix, key=lambda g: (len(g.get('fs_name') or ""), g.get('fs_name') or ""))
        if len(prefix) > 1:
            logger.info(f"{save_name}: no exact ROM name match; closest of {len(prefix)} candidates is {best['fs_name']}")
        return best

    # Helpers for watchdog sync
    @staticmethod
    def _parse_watchdog_events(event_paths: List[Path]) -> dict:
        """Parse a list of watchdog events and map them to existing games."""
        game_changed_files = {}  # (platform folder, game_name) -> set of Path objects

        # Pre-filter captured events for the relevant file changes.
        # If we don't do this, every chunk that syncthing pushes will get processed. The sync is robust enough to reject these, but it clutters the logs.
        for event_path in event_paths:
            game_name = LibraryScanner._detect_game_from_watchdog_event(event_path)
            if game_name is not None:
                game_changed_files.setdefault((event_path.parent.name, game_name), set()).add(event_path)

        return game_changed_files

    @staticmethod
    def _detect_game_from_watchdog_event(event_path: Path) -> str | None:
        """Extract game name from watchdog event path.

        Note: Metadata files are already filtered by FileChangeHandler, so this receives only valid game files.
        """
        logger.debug(f"[WATCHDOGSYNC] Found modified savedata: {event_path.name}.")
        # Same parser as the folder scan, so "Dr. Mario (USA).srm" is "Dr. Mario (USA)", not "Dr"
        game_name, _ = LibraryScanner.parse_save_filename(event_path.name)
        return game_name
