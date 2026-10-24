import os
from pathlib import Path
from typing import List, Dict, TYPE_CHECKING
import requests
from requests.auth import HTTPBasicAuth
from loguru import logger

if TYPE_CHECKING:
    from src.games_class import SaveFile, SaveState


class RommSaves:
    """Handle save file operations for ROMM API (upload/download/manage saves)."""

    def __init__(self, romm_user: 'RommUser'):
        """Initialize with a RommUser instance for API access."""
        self.romm_user = romm_user

    def get(self,
            rom_id: int,
            platform_id: int) -> List[Dict]:
        """Generic wrapper for ROMM API GET requests.
        Args:
            creds: RommUser credentials object
            endpoint: API endpoint (e.g., "/api/states")
            params: Optional query parameters
        Returns:
            Parsed JSON response as a dictionary
        """
        url = f"{self.romm_user.url}/api/saves/"
        params = {
            "rom_id": rom_id,
            "platform_id": platform_id
        }
        response = requests.get(url, params=params, timeout=60, **self.romm_user._auth_kwargs())
        logger.debug(f"GET {url} - {response.status_code}")
        return response.json()

    def add(self,
            obj: 'SaveFile',
            rom_id: int):
        """Upload a new save file to ROMM. Pulls the relevant info from the SaveFile object."""
        local_filepath: Path = obj.path

        if not local_filepath.exists():
            logger.error(f"Save file not found: {local_filepath}")
            return {"error": f"Save file not found: {local_filepath}"}

        with open(local_filepath, 'rb') as f:
            files = {'saveFile': (local_filepath.name, f)}
            params = {"rom_id": rom_id}

            url = f"{self.romm_user.url}/api/saves/"
            response = requests.post(
                url,
                timeout=300,
                params=params,
                files=files,
                **self.romm_user._auth_kwargs()
            )
            logger.debug(f"POST {url} - {response.status_code}")
            return response.json()

    def update(self,
               obj: 'SaveFile',
               rom_id: int,
               id: int) -> Dict:
        """Update an existing save file in ROMM.

        Args:
            local_filepath: LOCAL LIBRARY PATH to the save file
            id: ROMM Save ID (obtained from get() method). this is ".id" attr for the romm save's instance.

        Returns:
            Parsed JSON response from ROMM API
        """
        local_filepath = obj.path

        if not local_filepath.exists():
            logger.error(f"Save file not found: {local_filepath}")
            return {"error": f"Save file not found: {local_filepath}"}

        with open(local_filepath, 'rb') as f:
            files = {'saveFile': (local_filepath.name, f)}

            url = f"{self.romm_user.url}/api/saves/{id}"
            response = requests.put(
                url,
                timeout=300,
                files=files,
                **self.romm_user._auth_kwargs()
            )
            logger.debug(f"PUT {url} - {response.status_code}")
            return response.json()


class RommStates:
    """Handle state file operations for ROMM API (upload/download/manage states)."""

    def __init__(self, romm_user: 'RommUser'):
        """Initialize with a RommUser instance for API access."""
        self.romm_user = romm_user

    def get(self,
            rom_id: int,
            platform_id: int) -> List[Dict]:
        """Generic wrapper for ROMM API GET requests.
        Args:
            creds: RommUser credentials object
            endpoint: API endpoint (e.g., "/api/states")
            params: Optional query parameters
        Returns:
            Parsed JSON response as a dictionary
        """
        url = f"{self.romm_user.url}/api/states/"
        params = {
            "rom_id": rom_id,
            "platform_id": platform_id
        }
        response = requests.get(url, params=params, timeout=60, **self.romm_user._auth_kwargs())
        logger.debug(f"GET {url} - {response.status_code}")

        return response.json()

    def add(self,
            obj: 'SaveState',
            rom_id: int,
            emulator: str | None = None) -> Dict:
        """Upload a new state file to ROMM.

        Args:
            local_filepath: Path to the state file to upload
            rom_id: ROMM ROM ID

        Returns:
            Parsed JSON response from ROMM API
        """
        local_filepath = obj.path
        screenshot = obj.screenshot

        with open(local_filepath, 'rb') as state_f:
            files = {'stateFile': (local_filepath.name, state_f)}

            if screenshot:
                screenshot_f = open(screenshot, 'rb')
                files['screenshotFile'] = (screenshot.name, screenshot_f)
            else:
                screenshot_f = None

            try:
                url = f"{self.romm_user.url}/api/states/"
                response = requests.post(
                    url,
                    timeout=300,
                    params={"rom_id": rom_id, 'emulator': emulator},
                    files=files,
                    **self.romm_user._auth_kwargs()
                )
                logger.debug(f"POST {url} - {response.status_code}")
                return response.json()
            finally:
                if screenshot_f:
                    screenshot_f.close()

    def update(self,
               obj: 'SaveState',
               rom_id: int,
               id: int,
               emulator: str | None = None) -> Dict:
        """Update an existing state file in ROMM.

        Args:
            obj: SaveState object containing path and screenshot
            rom_id: ROMM ROM ID
            id: ROMM State ID (obtained from get() method)
            emulator: Optional emulator name

        Returns:
            Parsed JSON response from ROMM API
        """
        local_filepath = obj.path
        screenshot = obj.screenshot

        with open(local_filepath, 'rb') as state_f:
            files = {'stateFile': (local_filepath.name, state_f)}

            if screenshot:
                screenshot_f = open(screenshot, 'rb')
                files['screenshotFile'] = (screenshot.name, screenshot_f)
            else:
                screenshot_f = None

            try:
                url = f"{self.romm_user.url}/api/states/{id}"
                response = requests.put(
                    url,
                    timeout=300,
                    files=files,
                    **self.romm_user._auth_kwargs()
                )
                logger.debug(f"PUT {url} - {response.status_code}")
                return response.json()
            finally:
                if screenshot_f:
                    screenshot_f.close()


class RommUnreachable(RuntimeError):
    """RomM could not be contacted (wrong URL, network, firewall, RomM down)."""


class RommUser:
    """API settings and authentication credentials for a ROMM user.

    The lazy-loaded saves and states properties are used as default factories for SaveState/SaveFile dataclass instantiation for nice Save and State file api request syntax.
    """
    def __init__(self,
                 romm_url: str = os.getenv("ROMM_URL", ""),
                 romm_username: str = os.getenv("ROMM_USERNAME", ""),
                 romm_password: str = os.getenv("ROMM_PASSWORD", ""),
                 romm_base_dir: Path = Path(os.getenv("ROMM_BASE_DIR", "")),
                 romm_container_name: str = os.getenv("ROMM_CONTAINER_NAME", "rommapp"),
                 api_limit: int = int(os.getenv("ROMM_API_LIMIT", "500")),
                 auth_type: str = os.getenv("ROMM_AUTH_TYPE", "http"),
                 romm_api_token: str = os.getenv("ROMM_API_TOKEN", "")):

        self.url = romm_url
        self.user = romm_username
        self.password = romm_password
        self.romm_base_dir = romm_base_dir
        self.romm_container_name = romm_container_name
        self.api_limit = api_limit
        self.auth_type = auth_type.split("#")[0].strip().lower()  # tolerate "api  # comment" from .env files
        self.api_token = romm_api_token
        self._saves = None
        self._states = None

    def _auth_kwargs(self) -> dict:
        if self.auth_type == "api":
            return {"headers": {"Authorization": f"Bearer {self.api_token}"}}
        return {"auth": HTTPBasicAuth(self.user, self.password)}

    # region Two-way sync helpers (romm_sync GUI fork)
    def list_all(self, kind: str) -> List[Dict]:
        """All saves or states owned by this user, in ONE request.
        kind: 'saves' or 'states'. Works on RomM 4.5+ (rom_id is optional on these endpoints)."""
        url = f"{self.url}/api/{kind}"
        response = requests.get(url, timeout=60, **self._auth_kwargs())
        logger.debug(f"GET {url} - {response.status_code}")
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "items" in data:  # tolerate a paginated shape
            data = data["items"]
        return data if isinstance(data, list) else []

    def download_asset(self, kind: str, item: Dict) -> bytes:
        """Download a save/state's content.
        RomM 5.x: GET /api/{kind}/{id}/content. Older RomM: fall back to the item's download_path."""
        url = f"{self.url}/api/{kind}/{item['id']}/content"
        response = requests.get(url, timeout=120, **self._auth_kwargs())
        logger.debug(f"GET {url} - {response.status_code}")
        if response.status_code in (404, 405) and item.get("download_path"):
            url = self.absolute(item["download_path"])
            response = requests.get(url, timeout=120, **self._auth_kwargs())
            logger.debug(f"GET {url} - {response.status_code}")
        response.raise_for_status()
        return response.content

    def upload(self, kind: str, local_path: Path, rom_id: int, romm_id: int | None = None,
               screenshot: Path | None = None, upload_name: str | None = None) -> Dict:
        """Create (romm_id=None) or replace (romm_id set) a save/state. Raises on HTTP errors.
        upload_name lets us store the file under a different name (used for conflict copies)."""
        field_name = "saveFile" if kind == "saves" else "stateFile"
        name = upload_name or local_path.name
        handles = []
        try:
            fh = open(local_path, "rb")
            handles.append(fh)
            files = {field_name: (name, fh)}
            if screenshot and Path(screenshot).exists():
                sh = open(screenshot, "rb")
                handles.append(sh)
                shot_name = f"{name}.png" if upload_name else Path(screenshot).name
                files["screenshotFile"] = (shot_name, sh)
            if romm_id is None:
                url = f"{self.url}/api/{kind}"
                response = requests.post(url, params={"rom_id": rom_id}, files=files, timeout=300, **self._auth_kwargs())
            else:
                url = f"{self.url}/api/{kind}/{romm_id}"
                response = requests.put(url, files=files, timeout=300, **self._auth_kwargs())
            logger.debug(f"{'POST' if romm_id is None else 'PUT'} {url} - {response.status_code}")
            if response.status_code >= 400:
                raise RuntimeError(f"RomM returned {response.status_code}: {response.text[:300]}")
            return response.json()
        finally:
            for h in handles:
                h.close()

    def delete_assets(self, kind: str, ids: List[int]) -> None:
        """Delete saves/states on RomM (POST /api/{kind}/delete). kind: 'saves' or 'states'."""
        if not ids:
            return
        url = f"{self.url}/api/{kind}/delete"
        response = requests.post(url, json={kind: list(ids)}, timeout=60, **self._auth_kwargs())
        logger.debug(f"POST {url} {ids} - {response.status_code}")
        if response.status_code >= 400:
            raise RuntimeError(f"RomM returned {response.status_code}: {response.text[:300]}")

    def absolute(self, path: str) -> str:
        """Turn a RomM-relative path (/assets/..., /api/raw/...) into a full URL."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.url.rstrip('/')}/{path.lstrip('/')}"

    def get_raw(self, path_or_url: str, timeout: int = 20) -> requests.Response:
        """Authenticated GET of any RomM asset (covers, screenshots, platform icons) for the GUI image proxy."""
        return requests.get(self.absolute(path_or_url), timeout=timeout, **self._auth_kwargs())

    def version(self) -> str | None:
        """RomM server version from the heartbeat, e.g. '5.4.0'."""
        try:
            hb = self.heartbeat()
            return (hb.get("SYSTEM") or {}).get("VERSION") or hb.get("VERSION")
        except Exception:
            return None

    def list_platforms(self) -> List[Dict]:
        """Platforms in this RomM library (slug, fs_slug, name, rom_count...)."""
        response = requests.get(f"{self.url}/api/platforms", timeout=30, **self._auth_kwargs())
        response.raise_for_status()
        return response.json()

    def heartbeat(self) -> Dict:
        response = requests.get(f"{self.url}/api/heartbeat", timeout=10)
        response.raise_for_status()
        return response.json()
    # endregion

    @property
    def saves(self) -> RommSaves:
        """Lazy-loaded RommSaves instance for handling save file operations."""
        if self._saves is None:
            self._saves = RommSaves(self)
        return self._saves

    @property
    def states(self) -> RommStates:
        """Lazy-loaded RommSaves instance for handling save file operations."""
        if self._states is None:
            self._states = RommStates(self)
        return self._states

    def _validate_library_response(self, response) -> dict:
        """Validate and parse ROMM API library response."""
        try:
            data = response.json()
        except ValueError as e:
            logger.critical(f"Failed to decode JSON response: {e}")
            raise RuntimeError(f"Invalid JSON from ROMM API: {e}")

        if "items" not in data:
            logger.critical(f"Malformed API response: missing 'items' key. Got keys: {list(data.keys())}")
            raise RuntimeError("ROMM API response missing 'items' key")

        if not isinstance(data["items"], list):
            logger.critical(f"Malformed API response: 'items' is not a list, got {type(data['items'])}")
            raise RuntimeError("ROMM API response 'items' is not a list")

        return data

    def _get_rom_page(self, params: dict):
        url = f"{self.url}/api/roms"
        try:
            response = requests.get(url, params=params, timeout=(10, 180), **self._auth_kwargs())
        except requests.exceptions.RequestException as e:
            raise RommUnreachable(f"Can't reach RomM at {self.url} ({type(e).__name__}: {e}). "
                                  "Check ROMM_URL is reachable from inside this container.") from e
        if response.status_code in (401, 403):
            raise RuntimeError(f"RomM rejected the credentials ({response.status_code}). "
                               "Check ROMM_AUTH_TYPE / ROMM_API_TOKEN (needs roms + platforms read access).")
        logger.debug(f"GET {url} - {response.status_code} ({params})")
        if response.status_code in (400, 404, 422):
            logger.critical(f"RomM returned {response.status_code}: {response.text[:500]}")
            raise RuntimeError(f"ROMM API returned {response.status_code}")
        response.raise_for_status()
        return self._validate_library_response(response)

    def _fetch_roms(self, extra: dict, label: str, trim, on_progress) -> list:
        """Page through /api/roms. Pages are trimmed as they arrive to keep memory low."""
        items, offset, total = [], 0, None
        limit = self.api_limit
        while True:
            params = {
                "limit": limit, "offset": offset,
                # Ask RomM not to compute the gallery extras on every page (RomM 5.x; ignored by older versions).
                # These are what made big libraries slow to read.
                "with_char_index": "false", "with_filter_values": "false", "with_rom_id_index": "false",
                **extra,
            }
            if offset:
                params["with_total"] = "false"
            data = self._get_rom_page(params)
            if total is None and isinstance(data.get("total"), int):
                total = data["total"]
            page = data.get("items") or []
            items.extend(trim(page) if trim else page)
            if on_progress:
                on_progress(label, len(items), total)
            if len(page) < limit:
                break
            offset += limit
        return items

    def get_full_library(self, platform_ids: list | None = None, trim=None, on_progress=None) -> dict:
        """Gets the list of ROMs in RomM, page by page.

        platform_ids: only read these platforms (much faster when your sync folders only cover a few systems).
        trim: function applied to each page (keep only the fields we use).
        on_progress: callback(label, fetched, total) for logging / the dashboard.
        """
        if not platform_ids:
            return {"items": self._fetch_roms({}, "all platforms", trim, on_progress)}

        all_items = []
        for pid, label in platform_ids:
            # 4.x takes platform_id, 5.x platform_ids; send both, then verify RomM really filtered.
            page_items = self._fetch_roms({"platform_id": pid, "platform_ids": pid}, label, trim, on_progress)
            if any(i.get("platform_id") not in (pid, None) for i in page_items):
                logger.warning("RomM ignored the platform filter; reading the whole library instead.")
                return {"items": self._fetch_roms({}, "all platforms", trim, on_progress)}
            all_items.extend(page_items)
        return {"items": all_items}
