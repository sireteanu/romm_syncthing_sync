"""App version (stamped into the Docker image by the release workflow) and a light update check."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

REPO = "sireteanu/romm_syncthing_sync"
_ROOT = Path(__file__).resolve().parent.parent


def get_version() -> str:
    v = os.getenv("APP_VERSION", "").strip()
    if not v:
        try:
            v = (_ROOT / "VERSION").read_text().strip()
        except OSError:
            v = ""
    return v.lstrip("v") or "dev"


def _key(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


class UpdateChecker:
    """Asks GitHub for the latest release at most every 6 hours (in a background thread)."""

    INTERVAL = 6 * 3600

    def __init__(self):
        self.latest: Optional[str] = None
        self.url: Optional[str] = None
        self._checked = 0.0
        self._lock = threading.Lock()

    def _check(self):
        try:
            r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=10,
                             headers={"Accept": "application/vnd.github+json"})
            if r.ok:
                data = r.json()
                self.latest = str(data.get("tag_name", "")).lstrip("v") or None
                self.url = data.get("html_url")
        except Exception as e:
            logger.debug(f"Update check failed: {e}")

    def info(self) -> dict:
        with self._lock:
            if time.time() - self._checked > self.INTERVAL:
                self._checked = time.time()
                threading.Thread(target=self._check, name="update-check", daemon=True).start()
        current = get_version()
        newer = bool(self.latest) and current != "dev" and _key(self.latest) > _key(current)
        return {"latest": self.latest, "update_available": newer, "latest_url": self.url}


UPDATES = UpdateChecker()
