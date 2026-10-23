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
        response = requests.get(url, params=params, **self.romm_user._auth_kwargs())
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
        response = requests.get(url, params=params, **self.romm_user._auth_kwargs())
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
                    files=files,
                    **self.romm_user._auth_kwargs()
                )
                logger.debug(f"PUT {url} - {response.status_code}")
                return response.json()
            finally:
                if screenshot_f:
                    screenshot_f.close()


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
                 api_limit: int = int(os.getenv("ROMM_API_LIMIT", "50")),
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

    def get_full_library(self) -> dict:
        """Gets the full list of ROM's in the ROMM.app database, with arg support for pagination."""

        all_items = []
        offset = 0  # start offset at 0.
        # ROMM API default return limit is 50, which is also the default here. This can be controlled with docker env variable
        limit = self.api_limit

        while True:
            params = {"limit": limit, "offset": offset}

            url = f"{self.url}/api/roms/"
            response = requests.get(
                url,
                params=params,
                **self._auth_kwargs()
            )
            logger.debug(f"GET {url} - {response.status_code} (offset: {offset}, limit: {limit})")

            if response.status_code == 400:
                logger.critical(f"Bad request (400): {response.text}")
                raise RuntimeError("ROMM API returned 400 - Bad request")
            elif response.status_code == 404:
                logger.critical(f"Not found (404): {response.text}")
                raise RuntimeError("ROMM API returned 404 - Not found")
            elif response.status_code == 422:
                logger.critical(f"Unprocessable entity (422): {response.text}")
                raise RuntimeError("ROMM API returned 422 - Invalid data")

            response.raise_for_status()  # Raise for any other HTTP errors
            data = self._validate_library_response(response)

            if not data.get("items"):
                logger.debug("No more items to fetch")
                break

            items_count = len(data["items"])
            all_items.extend(data["items"])
            logger.debug(f"Fetched {items_count} items (total so far: {len(all_items)})")

            if items_count < limit:
                logger.debug("Reached end of records")
                break

            offset += limit

        logger.debug(f"Total items retrieved: {len(all_items)}")

        return {"items": all_items}
