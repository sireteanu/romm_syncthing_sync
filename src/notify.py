"""Notifications: Discord, ntfy, Gotify, Pushbullet, generic webhook (GUI fork).

One message per sync, only for things that are new since the last message (no repeat alerts).
Events: questions (needs a decision), errors (files that failed), problems (sync failed / RomM
unreachable), activity (files uploaded/downloaded).
"""
from __future__ import annotations

import threading
from typing import List, Optional, Tuple

import requests
from loguru import logger

from . import settings
from .sync_state import STATE

_lock = threading.Lock()
_notified_pending: set = set()
_notified_errors: dict = {}      # key -> message last notified
_last_problem: Optional[str] = None


def seed_from_state():
    """Called once at startup (before the first sync): items already waiting from before a restart
    were announced back then, so don't announce them again."""
    with _lock, STATE.lock:
        _notified_pending.update(a.key for a in STATE.pending.values())
        _notified_errors.update({k: e["message"] for k, e in STATE.errors.items()})


# region senders
def _send_one(ch: dict, title: str, body: str, priority: str, url: Optional[str]) -> None:
    t = ch["type"]
    if t == "discord":
        color = {"high": 0xFF5D6C, "normal": 0x7C5CFF, "low": 0x3CCF8E}[priority]
        embed = {"title": title, "description": body[:4000], "color": color}
        if url:
            embed["url"] = url
        r = requests.post(ch["webhook_url"], json={"username": "romm_sync", "embeds": [embed]}, timeout=15)
    elif t == "ntfy":
        headers = {"Title": title.encode("utf-8"), "Priority": {"high": "4", "normal": "3", "low": "2"}[priority],
                   "Tags": {"high": "warning", "normal": "video_game", "low": "floppy_disk"}[priority]}
        if url:
            headers["Click"] = url
        if ch.get("token"):
            headers["Authorization"] = f"Bearer {ch['token']}"
        r = requests.post(f"{ch['server'].rstrip('/')}/{ch['topic']}", data=body.encode("utf-8"),
                          headers=headers, timeout=15)
    elif t == "gotify":
        payload = {"title": title, "message": body + (f"\n\n{url}" if url else ""),
                   "priority": {"high": 8, "normal": 5, "low": 2}[priority]}
        if url:
            payload["extras"] = {"client::notification": {"click": {"url": url}}}
        r = requests.post(f"{ch['server'].rstrip('/')}/message", params={"token": ch["app_token"]},
                          json=payload, timeout=15)
    elif t == "pushbullet":
        payload = {"type": "link", "title": title, "body": body, "url": url} if url else \
                  {"type": "note", "title": title, "body": body}
        r = requests.post("https://api.pushbullet.com/v2/pushes", json=payload,
                          headers={"Access-Token": ch["access_token"]}, timeout=15)
    elif t == "webhook":
        r = requests.post(ch["url"], json={"title": title, "message": body, "priority": priority,
                                           "url": url, "source": "romm_sync"}, timeout=15)
    else:
        raise ValueError(f"Unknown channel {t}")
    if r.status_code >= 400:
        raise RuntimeError(f"{settings.CHANNEL_FIELDS[t]['label']} returned {r.status_code}: {r.text[:200]}")


def send(title: str, body: str, priority: str = "normal", channels: Optional[list] = None) -> List[Tuple[str, str]]:
    """Send to every configured channel. Returns [(channel label, error)] for failures."""
    cfg = settings.notify_config()
    url = cfg.get("dashboard_url") or None
    failures = []
    for ch in (channels if channels is not None else cfg["channels"]):
        label = settings.CHANNEL_FIELDS.get(ch.get("type"), {}).get("label", ch.get("type"))
        try:
            _send_one(ch, title, body, priority, url)
        except Exception as e:
            failures.append((label, str(e)))
            logger.warning(f"Notification via {label} failed: {e}")
    return failures
# endregion


def _bullets(items: List[str], limit: int = 8) -> str:
    out = [f"• {i}" for i in items[:limit]]
    if len(items) > limit:
        out.append(f"• …and {len(items) - limit} more")
    return "\n".join(out)


def after_sync(summary: Optional[dict], error: Optional[str] = None) -> None:
    """Called when a sync finishes. Sends at most one message per enabled event type."""
    cfg = settings.notify_config()
    if not cfg["channels"]:
        return
    ev = cfg["events"]
    global _last_problem
    with _lock:
        messages = []

        if error:
            if ev.get("problems") and error != _last_problem:
                messages.append(("romm_sync: sync problem", error, "high"))
            _last_problem = error
        else:
            if _last_problem and ev.get("problems"):
                messages.append(("romm_sync: back to normal", "The last sync completed without problems.", "low"))
            _last_problem = None

        with STATE.lock:
            pending = list(STATE.pending.values())
            errors = list(STATE.errors.values())

        new_q = [a for a in pending if a.key not in _notified_pending]
        _notified_pending.intersection_update({a.key for a in pending})
        _notified_pending.update(a.key for a in new_q)
        if new_q and ev.get("questions"):
            conflicts = [a for a in new_q if a.kind == "conflict"]
            n = len(new_q)
            title = f"romm_sync: {n} file{'s' if n != 1 else ''} need{'s' if n == 1 else ''} your decision"
            if conflicts:
                title += f" ({len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''})"
            body = _bullets([f"{a.game} ({a.platform}) — {a.file_name}: {a.reason}" for a in new_q])
            messages.append((title, body, "high" if conflicts else "normal"))

        new_e = [e for e in errors if _notified_errors.get(e["key"]) != e["message"]]
        for k in list(_notified_errors):
            if k not in {e["key"] for e in errors}:
                del _notified_errors[k]
        for e in new_e:
            _notified_errors[e["key"]] = e["message"]
        if new_e and ev.get("errors"):
            body = _bullets([f"{e['game']} ({e['platform']}) — {e['file_name']}: {e['message']}" for e in new_e])
            messages.append((f"romm_sync: {len(new_e)} file{'s' if len(new_e) != 1 else ''} failed to sync", body, "high"))

        if summary and ev.get("activity") and (summary.get("pushed") or summary.get("pulled")):
            parts = []
            if summary.get("pushed"):
                parts.append(f"⬆ {summary['pushed']} uploaded to RomM")
            if summary.get("pulled"):
                parts.append(f"⬇ {summary['pulled']} downloaded from RomM")
            messages.append(("romm_sync: synced", "\n".join(parts), "low"))

    for title, body, prio in messages:
        threading.Thread(target=send, args=(title, body, prio), daemon=True, name="notify").start()
