"""Manual links between a local game and a RomM ROM (GUI fork).

For saves whose name doesn't resemble the ROM file at all (renamed saves, hacks, a save from another
emulator), link them on the Saves page. Stored in DATA_DIR/game_links.json as
{"<system folder>/<game name>": rom_id}; links win over automatic matching.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, Optional

from .sync_state import data_dir

_lock = threading.Lock()


def _file():
    return data_dir() / "game_links.json"


def key(platform: str, game: str) -> str:
    return f"{platform.lower()}/{game}"


def load() -> Dict[str, int]:
    with _lock:
        try:
            return {k: int(v) for k, v in json.loads(_file().read_text()).items()}
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001
            return {}


def get(platform: str, game: str) -> Optional[int]:
    return load().get(key(platform, game))


def set_link(platform: str, game: str, rom_id: Optional[int]) -> None:
    with _lock:
        try:
            data = json.loads(_file().read_text())
        except Exception:  # noqa: BLE001
            data = {}
        if rom_id is None:
            data.pop(key(platform, game), None)
        else:
            data[key(platform, game)] = int(rom_id)
        tmp = _file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, _file())
