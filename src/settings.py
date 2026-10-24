"""Settings editable from the dashboard (GUI fork).

Stored in DATA_DIR/settings.json. A value saved here overrides the container environment; "reset"
removes the override. Connection settings (RomM URL/token, sync folders, sync mode) stay in the
container config because changing them needs a restart anyway.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict

from loguru import logger

from .sync_state import data_dir

_lock = threading.RLock()

# key -> spec. "attr" is the Config attribute the value is applied to.
FIELDS: Dict[str, dict] = {
    "PUSH_NEW_MODE": {"type": "choice", "choices": ["auto", "confirm"], "group": "sync",
                      "label": "New files in your sync folder → RomM",
                      "help": "auto: upload right away. confirm: ask before uploading any new file."},
    "PULL_MODE": {"type": "choice", "choices": ["auto", "confirm", "off"], "group": "sync",
                  "label": "New or changed files on RomM → sync folder",
                  "help": "auto: download right away. confirm: ask first. off: never write to the sync folder."},
    "DELETE_MODE": {"type": "choice", "choices": ["auto", "ask", "off"], "group": "sync",
                    "labels": {"auto": "Recycle bin", "ask": "Ask first", "off": "Never"},
                    "label": "When a synced file is deleted on one side",
                    "help": "Recycle bin: remove it on the other side too, keeping a copy in the recycle bin. "
                            "Ask first: wait for your decision. Never: offer to bring it back instead."},
    "ROMM_POLL_INTERVAL_SECONDS": {"type": "int", "min": 0, "max": 86400, "unit": "seconds", "group": "timing",
                                   "label": "Check RomM for changes every",
                                   "help": "Watch mode: how often changes made on RomM (other devices) are picked up. "
                                           "0 = only when you press Sync now. Applies from the next check. "
                                           "This only asks RomM; your sync folders are read only when RomM has changes."},
    "FULL_SCAN_INTERVAL_HOURS": {"type": "int", "min": 0, "max": 720, "unit": "hours", "group": "timing",
                                 "label": "Re-read the sync folders anyway every",
                                 "help": "Watch mode: a safety net in case a file change was missed. Each one can wake "
                                         "the disks the folders live on. 0 = only at startup and when you press Sync now."},
    "WATCHDOG_DELAY_SECONDS": {"type": "int", "min": 0, "max": 3600, "unit": "seconds", "group": "timing",
                               "label": "Wait after a file changes",
                               "help": "Watch mode: quiet time after Syncthing writes a file before syncing it."},
    "SYNC_INTERVAL_SECONDS": {"type": "int", "min": 60, "max": 604800, "unit": "seconds", "group": "timing",
                              "label": "Full sync every", "help": "Periodic mode only."},
    "BACKUP_RETENTION_DAYS": {"type": "int", "min": 0, "max": 3650, "unit": "days", "group": "backups",
                              "label": "Keep backups of overwritten files for",
                              "help": "0 = keep forever. Backups are in /data/backups."},
    "RECYCLE_RETENTION_DAYS": {"type": "int", "min": 0, "max": 3650, "unit": "days", "group": "backups",
                               "label": "Empty the recycle bin after",
                               "help": "Deleted files are kept this long, then removed for good. 0 = keep forever."},
    "FILE_MODE": {"type": "choice", "choices": [0o777, 0o666, 0o775, 0o664], "group": "backups",
                  "labels": {"511": "0777", "438": "0666", "509": "0775", "436": "0664"},
                  "label": "Permissions for files written to your sync folders",
                  "help": "0777 (default) = everyone can read and write, the usual choice on Unraid shares. "
                          "0666 = the same without the executable bit."},
    "WEB_USERNAME": {"type": "str", "group": "access", "label": "Dashboard username",
                     "help": "Set both username and password to require a login. Clear both to turn it off."},
    "WEB_PASSWORD": {"type": "secret", "group": "access", "label": "Dashboard password", "help": ""},
}

NOTIFY_DEFAULTS = {
    "channels": [],          # [{"type": "discord", "webhook_url": "..."}, ...]
    "events": {"questions": True, "errors": True, "activity": False, "problems": True},
    "dashboard_url": "",     # used for "open dashboard" links in messages
}

CHANNEL_FIELDS = {
    "discord": {"label": "Discord", "fields": [("webhook_url", "Webhook URL", True)]},
    "ntfy": {"label": "ntfy", "fields": [("server", "Server", False), ("topic", "Topic", False),
                                         ("token", "Access token (optional)", True)]},
    "gotify": {"label": "Gotify", "fields": [("server", "Server URL", False), ("app_token", "App token", True)]},
    "pushbullet": {"label": "Pushbullet", "fields": [("access_token", "Access token", True)]},
    "webhook": {"label": "Webhook (JSON POST)", "fields": [("url", "URL", True)]},
}


def _file():
    return data_dir() / "settings.json"


def load() -> dict:
    with _lock:
        try:
            raw = json.loads(_file().read_text())
        except FileNotFoundError:
            raw = {}
        except Exception as e:
            logger.warning(f"Ignoring unreadable settings file: {e}")
            raw = {}
        raw.setdefault("values", {})
        n = raw.setdefault("notifications", {})
        for k, v in NOTIFY_DEFAULTS.items():
            n.setdefault(k, json.loads(json.dumps(v)))
        for k, v in NOTIFY_DEFAULTS["events"].items():
            n["events"].setdefault(k, v)
        return raw


def save(data: dict):
    with _lock:
        tmp = _file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        try:
            os.chmod(tmp, 0o600)  # holds tokens
        except OSError:
            pass
        os.replace(tmp, _file())


# Values the container started with, so "reset" can go back to them.
_container_values: Dict[str, Any] = {}


def apply_to_config(cfg):
    """Called once at startup: remember container values, then apply saved overrides."""
    for key in FIELDS:
        _container_values.setdefault(key, getattr(cfg, key, None))
    for key, value in load()["values"].items():
        if key in FIELDS:
            setattr(cfg, key, value)
    logger.debug("Applied dashboard settings overrides")


def validate(key: str, value: Any):
    spec = FIELDS[key]
    if spec["type"] == "choice":
        if spec["choices"] and isinstance(spec["choices"][0], int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
        if value not in spec["choices"]:
            raise ValueError(f"{spec['label']}: must be one of {', '.join(map(str, spec['choices']))}")
        return value
    if spec["type"] == "int":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']}: must be a whole number")
        if not spec["min"] <= v <= spec["max"]:
            raise ValueError(f"{spec['label']}: must be between {spec['min']} and {spec['max']}")
        return v
    return "" if value is None else str(value)


def update(cfg, changes: Dict[str, Any], reset: list) -> None:
    """Validate everything first, then apply; nothing changes if any value is invalid."""
    data = load()
    vals = data["values"]
    clean = {k: validate(k, v) for k, v in changes.items() if k in FIELDS}
    prospective = {k: getattr(cfg, k, None) for k in FIELDS}
    for k in reset:
        if k in FIELDS:
            prospective[k] = _container_values.get(k)
    prospective.update(clean)
    if bool(prospective["WEB_USERNAME"]) != bool(prospective["WEB_PASSWORD"]):
        raise ValueError("Set both a dashboard username and password, or clear both.")
    for k in reset:
        if k in FIELDS:
            vals.pop(k, None)
    vals.update(clean)
    save(data)
    for k, v in prospective.items():
        setattr(cfg, k, v)


def describe(cfg) -> dict:
    """Settings for the UI. Secrets are never sent back, only whether they are set."""
    vals = load()["values"]
    out = []
    for key, spec in FIELDS.items():
        current = getattr(cfg, key, None)
        item = {"key": key, **spec,
                "source": "dashboard" if key in vals else ("container" if os.getenv(key) is not None else "default"),
                "container_value": None if spec["type"] == "secret" else _container_values.get(key)}
        if spec["type"] == "secret":
            item["is_set"] = bool(current)
            item["value"] = ""
        else:
            item["value"] = current
        out.append(item)
    return {"fields": out}


# region notifications
def notify_public() -> dict:
    """Notification settings for the UI, with secrets masked."""
    n = load()["notifications"]
    chans = []
    for ch in n["channels"]:
        spec = CHANNEL_FIELDS.get(ch.get("type"), {"fields": []})
        c = {"type": ch.get("type")}
        for name, _, secret in spec["fields"]:
            if secret:
                c[name] = ""
                c[name + "_set"] = bool(ch.get(name))
            else:
                c[name] = ch.get(name, "")
        chans.append(c)
    return {"channels": chans, "events": n["events"], "dashboard_url": n.get("dashboard_url", ""),
            "channel_types": {k: {"label": v["label"], "fields": [{"name": f, "label": lbl, "secret": sec}
                                                                  for f, lbl, sec in v["fields"]]}
                              for k, v in CHANNEL_FIELDS.items()}}


def notify_update(body: dict) -> None:
    data = load()
    n = data["notifications"]
    old = n["channels"]
    new_channels = []
    for i, ch in enumerate(body.get("channels", [])):
        t = ch.get("type")
        if t not in CHANNEL_FIELDS:
            raise ValueError(f"Unknown notification type: {t}")
        prev = old[i] if i < len(old) and old[i].get("type") == t else {}
        c = {"type": t}
        for name, label, secret in CHANNEL_FIELDS[t]["fields"]:
            v = (ch.get(name) or "").strip()
            if secret and not v:
                v = prev.get(name, "")  # blank secret = keep the saved one
            c[name] = v
        if t == "ntfy":
            c["server"] = c.get("server") or "https://ntfy.sh"
            if not c.get("topic"):
                raise ValueError("ntfy needs a topic")
        required = {"discord": "webhook_url", "gotify": "app_token", "pushbullet": "access_token", "webhook": "url"}
        if t in required and not c.get(required[t]):
            raise ValueError(f"{CHANNEL_FIELDS[t]['label']}: {dict((f, lbl) for f, lbl, _ in CHANNEL_FIELDS[t]['fields'])[required[t]]} is required")
        if t == "gotify" and not c.get("server"):
            raise ValueError("Gotify needs a server URL")
        new_channels.append(c)
    n["channels"] = new_channels
    ev = body.get("events", {})
    n["events"] = {k: bool(ev.get(k, n["events"].get(k))) for k in NOTIFY_DEFAULTS["events"]}
    n["dashboard_url"] = (body.get("dashboard_url") or "").strip()
    save(data)


def notify_config() -> dict:
    return load()["notifications"]
# endregion
