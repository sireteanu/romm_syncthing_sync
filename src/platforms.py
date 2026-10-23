"""Platform catalog + platform mapping management (GUI fork).

Resolution order for a local platform folder (e.g. "md", "Nintendo 64", "snes"):
    1. your mapping              platform_mapping.yaml (edited in the web UI or mounted)
    2. exact RomM slug           folder name == a RomM platform slug
    3. your RomM's folder name   folder name == platform fs_slug in your RomM library
    4. RomM's own aliases        PLATFORM_FS_ALIASES from rommapp/romm (Batocera/ES-DE folder names)

The catalog of every platform RomM supports is fetched from GitHub:
    rommapp/romm  backend/utils/platform_slugs.py     -> all slugs (at your RomM's version tag when possible)
    rommapp/romm  backend/utils/platform_aliases.py   -> folder-name aliases
    rommapp/docs  docs/resources/snippets/supported-platforms.md -> display names
and cached in DATA_DIR/romm_platforms.json.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml
from loguru import logger

from .sync_state import data_dir, utcnow_iso

RAW = "https://raw.githubusercontent.com"
ROMM_REPO = "rommapp/romm"
DOCS_REPO = "rommapp/docs"
SLUGS_PATH = "backend/utils/platform_slugs.py"
ALIASES_PATH = "backend/utils/platform_aliases.py"
DOCS_TABLE_PATH = "docs/resources/snippets/supported-platforms.md"
CATALOG_MAX_AGE = timedelta(days=7)

_lock = threading.RLock()
_catalog_cache: Optional[dict] = None


# region catalog (RomM GitHub)
def catalog_file() -> Path:
    return data_dir() / "romm_platforms.json"


def _get(url: str) -> Optional[str]:
    r = requests.get(url, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def parse_slugs(src: str) -> Dict[str, str]:
    """UniversalPlatformSlug enum -> {CONSTANT_NAME: slug}."""
    return dict(re.findall(r'^\s+([A-Z0-9_]+)\s*=\s*"([^"]+)"', src, flags=re.M))


def parse_aliases(src: str, const_to_slug: Dict[str, str]) -> Dict[str, str]:
    """PLATFORM_FS_ALIASES -> {folder_alias: slug}."""
    block = src.split("PLATFORM_FS_ALIASES", 1)[-1].split("\n}", 1)[0]
    out = {}
    for alias, const in re.findall(r'"([^"]+)"\s*:\s*UPS\.([A-Z0-9_]+)', block):
        if const in const_to_slug:
            out[alias.lower()] = const_to_slug[const]
    return out


def parse_docs_names(md: str) -> Dict[str, str]:
    """Supported-platforms markdown table -> {slug: display name}."""
    names = {}
    for name, slug in re.findall(r"^\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|", md, flags=re.M):
        names[slug.strip()] = name.strip()
    return names


def _title_from_const(const: str) -> str:
    return const.lstrip("_").replace("_", " ").title()


def fetch_catalog(romm_version: Optional[str] = None) -> dict:
    """Download the platform catalog from RomM's GitHub. Tries the tag matching your RomM version first."""
    refs = []
    if romm_version:
        v = romm_version.split("-")[0].split("+")[0].strip()
        if re.match(r"^\d+\.\d+\.\d+$", v):
            refs.append(v)
    refs.append("master")

    slugs_src, ref_used = None, None
    for ref in refs:
        slugs_src = _get(f"{RAW}/{ROMM_REPO}/{ref}/{SLUGS_PATH}")
        if slugs_src:
            ref_used = ref
            break
    if not slugs_src:
        raise RuntimeError("Could not find platform_slugs.py in rommapp/romm")

    const_to_slug = parse_slugs(slugs_src)
    aliases_src = _get(f"{RAW}/{ROMM_REPO}/{ref_used}/{ALIASES_PATH}") or ""
    aliases = parse_aliases(aliases_src, const_to_slug)
    try:
        names = parse_docs_names(_get(f"{RAW}/{DOCS_REPO}/HEAD/{DOCS_TABLE_PATH}") or "")
    except Exception as e:
        logger.warning(f"Could not fetch platform display names from rommapp/docs: {e}")
        names = {}

    platforms = {slug: names.get(slug) or _title_from_const(const) for const, slug in const_to_slug.items()}
    catalog = {
        "source": f"github.com/{ROMM_REPO}@{ref_used}",
        "ref": ref_used,
        "fetched_at": utcnow_iso(),
        "platforms": platforms,
        "aliases": aliases,
    }
    if len(platforms) < 50:
        raise RuntimeError(f"Platform list looks wrong ({len(platforms)} entries) — not saving it")
    with _lock:
        catalog_file().write_text(json.dumps(catalog, indent=1))
        global _catalog_cache
        _catalog_cache = catalog
    logger.info(f"Loaded {len(platforms)} RomM platforms and {len(aliases)} folder aliases from {catalog['source']}")
    return catalog


def get_catalog() -> dict:
    """Cached catalog (may be empty if never fetched and offline)."""
    global _catalog_cache
    with _lock:
        if _catalog_cache is None:
            try:
                _catalog_cache = json.loads(catalog_file().read_text())
            except Exception:
                _catalog_cache = {"source": None, "ref": None, "fetched_at": None, "platforms": {}, "aliases": {}}
        return _catalog_cache


def ensure_catalog(romm_version: Optional[str] = None, force: bool = False) -> dict:
    """Refresh the catalog from GitHub if missing, stale, or for a different RomM version. Never raises."""
    cat = get_catalog()
    stale = True
    if cat.get("fetched_at"):
        try:
            stale = datetime.now(timezone.utc) - datetime.fromisoformat(cat["fetched_at"]) > CATALOG_MAX_AGE
        except ValueError:
            stale = True
    version_changed = bool(romm_version) and cat.get("ref") not in (None, "master") and \
        not romm_version.startswith(cat.get("ref") or "")
    if force or stale or version_changed or not cat.get("platforms"):
        try:
            return fetch_catalog(romm_version)
        except Exception as e:
            logger.warning(f"Could not refresh the RomM platform list from GitHub ({e}). Using the cached list.")
    return get_catalog()
# endregion


# region mapping file
def ui_mapping_file() -> Path:
    return data_dir() / "platform_mapping.yaml"


def effective_mapping_path(mounted: Optional[Path]) -> Optional[Path]:
    """The UI-managed file wins once it exists; the mounted file is the starting point."""
    ui = ui_mapping_file()
    if ui.exists():
        return ui
    if mounted is not None and mounted.is_file():
        return mounted
    if mounted is not None and mounted.is_dir() and not getattr(effective_mapping_path, "_warned", False):
        effective_mapping_path._warned = True
        logger.warning(f"{mounted} is a folder, not a file. Docker creates an empty folder when the mapped "
                       "file doesn't exist on the host. Either download platform_mapping.yaml to that host path "
                       "(delete the folder first) or remove the mapping and use the dashboard's Platforms page.")
    return None


def load_mapping(path: Optional[Path]) -> Dict[str, List[str]]:
    if path is None:
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("platform mapping must be a dictionary")
    out = {}
    for slug, names in data.items():
        if isinstance(names, list):
            out[str(slug)] = [str(n) for n in names]
    return out


HEADER = """# Platform mapping for romm_sync — managed by the web UI (Platforms page).
# <romm platform slug>:
#   - <local folder name>
# Folder names are matched case-insensitively. Folders that already match a RomM slug,
# a folder name in your RomM library, or one of RomM's built-in aliases don't need an entry.
"""


def save_mapping(mapping: Dict[str, List[str]]) -> Path:
    clean = {slug: sorted(set(n for n in names if n), key=str.lower)
             for slug, names in sorted(mapping.items()) if names}
    path = ui_mapping_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HEADER + "\n" + yaml.safe_dump(clean, sort_keys=True, allow_unicode=True))
    tmp.replace(path)
    return path


def apply_folder_choices(mapping: Dict[str, List[str]], choices: Dict[str, Optional[str]]) -> Dict[str, List[str]]:
    """Set (or clear, with None) the mapped slug for each folder, keeping all other entries."""
    out = {slug: list(names) for slug, names in mapping.items()}
    for folder, slug in choices.items():
        f = folder.lower()
        for s in list(out):
            out[s] = [n for n in out[s] if n.lower() != f]
            if not out[s]:
                del out[s]
        if slug:
            out.setdefault(slug, []).append(folder)
    return out
# endregion


def resolve(folder: str, mapping: Dict[str, List[str]], library_slugs: set,
            library_fs_slugs: Dict[str, str], aliases: Dict[str, str]):
    """-> (slug or None, how). See module docstring for the order."""
    f = folder.lower()
    for slug, names in mapping.items():
        if any(n.lower() == f for n in names):
            return slug, ("exact" if slug == f else "mapping")
    if f in library_slugs:
        return f, "exact"
    if f in library_fs_slugs:
        return library_fs_slugs[f], "romm-folder"
    if f in aliases:
        return aliases[f], "romm-alias"
    return None, "none"


def local_platform_folders(sync_dirs: List[Path]) -> List[str]:
    seen = {}
    for d in sync_dirs:
        if d is None or not d.exists():
            continue
        for p in d.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                seen.setdefault(p.name.lower(), p.name)
    return sorted(seen.values(), key=str.lower)
