"""Web GUI for romm_sync (added in the GUI fork; original project by amn-96, MIT License).

Serves a single-page dashboard plus a small JSON API:
    GET  /api/status                 everything the dashboard shows
    POST /api/sync                   run a sync now
    POST /api/actions/{id}           approve / reject / resolve one pending action
    POST /api/actions/bulk           same, for several ids at once
    GET  /img/romm?p=...             authenticated proxy for RomM covers / screenshots
    GET  /img/local?p=...            RetroArch state thumbnails from the sync folders
    GET  /img/platform/{slug}        RomM platform icon
"""
from __future__ import annotations

import base64
import mimetypes
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from loguru import logger
from pydantic import BaseModel

from .sync_state import STATE

STATIC_DIR = Path(__file__).parent / "web_static"
VALID_DECISIONS = {"approve", "reject", "local", "romm", "both", "restore"}


class Decision(BaseModel):
    decision: str


class BulkDecision(BaseModel):
    ids: List[str]
    decision: str


class SavesAction(BaseModel):
    action: str
    ids: List[str]


class MappingUpdate(BaseModel):
    choices: Dict[str, Optional[str]]  # folder -> RomM slug, or None to remove the mapping


class _ImageCache:
    def __init__(self, max_items: int = 400):
        self.max_items = max_items
        self.items: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            v = self.items.get(key)
            if v is not None:
                self.items.move_to_end(key)
            return v

    def put(self, key, value):
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > self.max_items:
                self.items.popitem(last=False)


def create_app(cfg) -> FastAPI:
    app = FastAPI(title="romm_sync", docs_url=None, redoc_url=None)
    romm = cfg.ROMM_CREDENTIALS
    cache = _ImageCache()

    # region optional basic auth
    # Optional basic auth. Credentials are read on every request so a change on the Settings page applies
    # immediately (and clearing them turns auth off).
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        user_cfg, pwd_cfg = cfg.WEB_USERNAME, cfg.WEB_PASSWORD
        if not (user_cfg and pwd_cfg) or request.url.path == "/api/health":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, user_cfg) and secrets.compare_digest(pwd, pwd_cfg)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="romm_sync"'})
        return await call_next(request)
    # endregion

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{name}")
    def static(name: str):
        p = (STATIC_DIR / name).resolve()
        if p.parent != STATIC_DIR.resolve() or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    @app.get("/api/health")
    def health():
        return {"ok": True, "phase": STATE.phase}

    @app.get("/api/status")
    def status():
        snap = STATE.snapshot()
        snap["config"] = {
            "romm_url": romm.url,
            "sync_mode": cfg.SYNC_MODE,
            "push_new_mode": cfg.PUSH_NEW_MODE,
            "pull_mode": cfg.PULL_MODE,
            "pull_writable": cfg.pull_writable(),
            "poll_interval": cfg.ROMM_POLL_INTERVAL_SECONDS,
            "dry_run": cfg.DRY_RUN,
        }
        from .version import get_version, UPDATES, REPO
        from . import recycle as rb
        snap["recycle_count"] = len(rb.entries())
        snap["version"] = {"app": get_version(), "repo": REPO, "romm": STATE.romm_version, **UPDATES.info()}
        return JSONResponse(snap)

    @app.post("/api/sync")
    def sync_now():
        if STATE.request_sync_callback is None:
            raise HTTPException(503, "Sync loop is still starting")
        STATE.request_sync_callback()
        STATE.add_activity("INFO", "Sync requested from the web UI")
        return {"ok": True}

    def _apply(action_id: str, decision: str):
        from .sync_engine import SyncEngine  # late import: engine needs config
        with STATE.lock:
            action = STATE.pending.get(action_id)
        if action is None or action.status != "pending":
            return False, "This item is no longer pending (it may have been handled already)."
        if decision == "reject":
            STATE.dismiss(action)
            STATE.finish_action(action, "dismissed", "Skipped by user")
            return True, "Skipped"
        if action.kind == "conflict":
            if decision not in ("local", "romm", "both"):
                return False, "Conflicts must be resolved with local, romm or both."
            return SyncEngine().execute(action, choice=decision)
        if decision == "restore" and action.kind in ("delete_romm", "delete_local"):
            return SyncEngine().execute(action, choice="restore")
        if decision != "approve":
            return False, f"'{decision}' is not valid for this item."
        return SyncEngine().execute(action)

    # NOTE: must be registered before "/api/actions/{action_id}", otherwise "bulk" is taken as an action id.
    @app.post("/api/actions/bulk")
    def decide_bulk(body: BulkDecision):
        if body.decision not in VALID_DECISIONS:
            raise HTTPException(400, "Invalid decision")

        def run():
            done = 0
            for aid in body.ids:
                ok, _ = _apply(aid, body.decision)
                done += ok
            STATE.add_activity("INFO", f"Bulk {body.decision}: {done}/{len(body.ids)} item(s) completed")
            if STATE.request_sync_callback and body.decision != "reject":
                STATE.request_sync_callback()

        threading.Thread(target=run, name="bulk-actions", daemon=True).start()
        return {"ok": True, "queued": len(body.ids)}


    @app.post("/api/actions/{action_id}")
    def decide(action_id: str, body: Decision):
        if body.decision not in VALID_DECISIONS:
            raise HTTPException(400, "Invalid decision")
        ok, msg = _apply(action_id, body.decision)
        if ok and STATE.request_sync_callback and body.decision != "reject":
            STATE.request_sync_callback()  # refresh the dashboard numbers
        return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 409)

    @app.post("/api/errors/retry")
    def errors_retry():
        """Clear the error list and run a sync; anything still failing shows up again."""
        with STATE.lock:
            n = len(STATE.errors)
            STATE.errors.clear()
        if STATE.request_sync_callback:
            STATE.request_sync_callback()
        STATE.add_activity("INFO", f"Retrying {n} failed file(s)")
        return {"ok": True}

    @app.post("/api/errors/dismiss")
    def errors_dismiss(body: Dict[str, str]):
        STATE.clear_error(body.get("key", ""))
        return {"ok": True}

    # region platforms
    from . import platforms as pf
    plat_cache = {"t": 0.0, "data": None}

    def _library_platforms():
        import time
        if plat_cache["data"] is None or time.time() - plat_cache["t"] > 60:
            try:
                plat_cache["data"] = romm.list_platforms()
            except Exception as e:
                logger.warning(f"Could not list RomM platforms: {e}")
                plat_cache["data"] = plat_cache["data"] or []
            plat_cache["t"] = time.time()
        return plat_cache["data"]

    def _sync_dirs():
        return [d for d in (cfg.SAVE_SYNC_DIR, cfg.STATE_SYNC_DIR, cfg.ALL_SYNC_DIR) if d is not None]

    def _platforms_payload():
        catalog = pf.get_catalog()
        lib = _library_platforms()
        lib_by_slug = {p["slug"]: p for p in lib}
        lib_slugs = set(lib_by_slug)
        lib_fs = {str(p.get("fs_slug", "")).lower(): p["slug"] for p in lib if p.get("fs_slug")}
        path = pf.effective_mapping_path(cfg.PLATFORM_MAP_PATH)
        try:
            mapping = pf.load_mapping(path)
            mapping_error = None
        except Exception as e:
            mapping, mapping_error = {}, str(e)

        names = dict(catalog.get("platforms", {}))
        for p in lib:  # your RomM's names (incl. custom names) win
            names[p["slug"]] = p.get("custom_name") or p.get("display_name") or p.get("name") or names.get(p["slug"], p["slug"])

        with STATE.lock:
            games = list(STATE.games.values())
        folders = []
        for folder in pf.local_platform_folders(_sync_dirs()):
            slug, how = pf.resolve(folder, mapping, lib_slugs, lib_fs, catalog.get("aliases", {}))
            fg = [g for g in games if g["platform"].lower() == folder.lower()]
            folders.append({
                "folder": folder, "slug": slug, "how": how,
                "name": names.get(slug) if slug else None,
                "in_library": slug in lib_slugs if slug else False,
                "rom_count": lib_by_slug.get(slug, {}).get("rom_count") if slug else None,
                "known_slug": bool(slug) and (slug in names),
                "games": len(fg), "unmatched": sum(1 for g in fg if not g.get("matched")),
            })
        platforms = [{"slug": slug, "name": name, "in_library": slug in lib_slugs,
                      "rom_count": lib_by_slug.get(slug, {}).get("rom_count", 0)}
                     for slug, name in names.items()]
        platforms.sort(key=lambda p: (not p["in_library"], p["name"].lower()))
        return {
            "folders": folders,
            "platforms": platforms,
            "catalog": {"source": catalog.get("source"), "fetched_at": catalog.get("fetched_at"),
                        "count": len(catalog.get("platforms", {})), "aliases": len(catalog.get("aliases", {}))},
            "mapping_file": str(path) if path else None,
            "ui_mapping_file": str(pf.ui_mapping_file()),
            "using_ui_file": path == pf.ui_mapping_file(),
            "mounted_file": str(cfg.PLATFORM_MAP_PATH) if cfg.PLATFORM_MAP_PATH else None,
            "mapping_error": mapping_error,
            "sync_folders": pf.sync_folder_report(_sync_dirs()),
        }

    @app.get("/api/platforms")
    def platforms_get():
        return _platforms_payload()

    @app.post("/api/platforms/refresh")
    def platforms_refresh():
        try:
            pf.fetch_catalog(romm.version())
        except Exception as e:
            raise HTTPException(502, f"Could not download the platform list from GitHub: {e}")
        plat_cache["data"] = None
        return _platforms_payload()

    @app.put("/api/platforms/mapping")
    def platforms_save(body: MappingUpdate):
        known = set(pf.get_catalog().get("platforms", {})) | {p["slug"] for p in _library_platforms()}
        folders = {f.lower() for f in pf.local_platform_folders(_sync_dirs())}
        for folder, slug in body.choices.items():
            if folder.lower() not in folders:
                raise HTTPException(400, f"Unknown folder: {folder}")
            if slug is not None and (not isinstance(slug, str) or (known and slug not in known)):
                raise HTTPException(400, f"Unknown RomM platform: {slug}")
        current_path = pf.effective_mapping_path(cfg.PLATFORM_MAP_PATH)
        try:
            current = pf.load_mapping(current_path)
        except Exception:
            current = {}
        path = pf.save_mapping(pf.apply_folder_choices(current, body.choices))
        STATE.add_activity("INFO", f"Platform mapping saved ({len(body.choices)} folder(s) changed) → {path}")
        if STATE.request_sync_callback:
            STATE.request_sync_callback()
        return _platforms_payload()
    # endregion

    # region logs
    from .sync_state import read_log, log_file

    @app.get("/api/logs")
    def logs(level: str = "INFO", q: str = "", limit: int = 1000):
        limit = max(50, min(int(limit), 5000))
        with STATE.lock:
            history = list(STATE.history)
        return {"entries": read_log(level, q, limit), "history": history, "file": str(log_file())}

    @app.get("/api/logs/download")
    def logs_download():
        f = log_file()
        if not f.exists():
            raise HTTPException(404, "No log file yet")
        return FileResponse(f, filename="romm_sync.log", media_type="text/plain")
    # endregion

    # region saves page
    from . import manage

    @app.get("/api/saves")
    def saves_list():
        try:
            rows = manage.inventory()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except Exception as e:
            raise HTTPException(502, f"Couldn't read the saves list: {e}")
        return {"rows": rows, "job": manage.job_status(), "dry_run": bool(cfg.DRY_RUN)}

    @app.get("/api/saves/job")
    def saves_job():
        return manage.job_status()

    @app.post("/api/saves/action")
    def saves_action(body: SavesAction):
        try:
            return manage.start(body.action, body.ids)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    # region recycle bin
    from . import recycle as rb

    def _chown(p):
        if cfg.PUID is not None and cfg.PGID is not None:
            try:
                import os as _os
                _os.chown(p, cfg.PUID, cfg.PGID)
            except OSError:
                pass

    @app.get("/api/recycle")
    def recycle_list():
        return {"entries": rb.entries(), "retention_days": int(cfg.RECYCLE_RETENTION_DAYS),
                "delete_mode": cfg.DELETE_MODE}

    @app.post("/api/recycle/empty")
    def recycle_empty():
        n = rb.empty()
        STATE.add_activity("INFO", f"Recycle bin emptied ({n} item(s) deleted for good)")
        return {"ok": True, "removed": n}

    @app.post("/api/recycle/{eid}/restore")
    def recycle_restore(eid: str):
        try:
            dest = rb.restore(eid, cfg.FILE_MODE, _chown)
        except ValueError as e:
            raise HTTPException(409, str(e))
        STATE.add_activity("INFO", f"Restored {dest} from the recycle bin")
        if STATE.request_sync_callback:
            STATE.request_sync_callback()  # uploads it to RomM again
        return {"ok": True, "message": f"Restored to {dest}. It will be uploaded to RomM on this sync."}

    @app.post("/api/recycle/{eid}/delete")
    def recycle_delete(eid: str):
        if not rb.remove(eid):
            raise HTTPException(404, "Not in the recycle bin")
        return {"ok": True}

    @app.get("/api/recycle/{eid}/download")
    def recycle_download(eid: str):
        e = rb.get(eid)
        if e is None:
            raise HTTPException(404)
        return FileResponse(rb.path_of(e), filename=e["file_name"], media_type="application/octet-stream")

    @app.get("/api/recycle/{eid}/screenshot")
    def recycle_shot(eid: str):
        e = rb.get(eid)
        p = rb.path_of(e).with_name(e["file_name"] + ".png") if e else None
        if not p or not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/png")
    # endregion

    # region settings & notifications
    from . import settings as st
    from . import notify

    def _settings_payload():
        return {
            **st.describe(cfg),
            "notifications": st.notify_public(),
            "connection": {
                "romm_url": romm.url, "auth_type": romm.auth_type, "sync_mode": cfg.SYNC_MODE,
                "sync_folders": [str(d) for d in _sync_dirs()],
                "pull_writable": cfg.pull_writable(),
            },
        }

    @app.get("/api/settings")
    def settings_get():
        return _settings_payload()

    @app.put("/api/settings")
    def settings_put(body: Dict[str, Any]):
        try:
            st.update(cfg, body.get("values", {}), body.get("reset", []))
        except ValueError as e:
            raise HTTPException(400, str(e))
        STATE.add_activity("INFO", "Settings updated from the dashboard")
        return _settings_payload()

    @app.put("/api/notifications")
    def notifications_put(body: Dict[str, Any]):
        try:
            st.notify_update(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        STATE.add_activity("INFO", "Notification settings updated")
        return _settings_payload()

    @app.post("/api/notifications/test")
    def notifications_test():
        chans = st.notify_config()["channels"]
        if not chans:
            raise HTTPException(400, "Add and save a notification service first.")
        failures = notify.send("romm_sync test notification",
                               "If you can read this, notifications from romm_sync are working.", "normal", chans)
        if failures:
            raise HTTPException(502, "; ".join(f"{label}: {err}" for label, err in failures))
        return {"ok": True, "sent": len(chans)}
    # endregion

    # region images
    def _img(content: bytes, ctype: str) -> Response:
        return Response(content, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/img/romm")
    def img_romm(p: str):
        # Only RomM-relative paths: this proxy must never fetch arbitrary URLs.
        if not p.startswith("/") or p.startswith("//") or ".." in p:
            raise HTTPException(400)
        hit = cache.get(p)
        if hit:
            return _img(*hit)
        try:
            r = romm.get_raw(p)
        except Exception:
            raise HTTPException(502)
        if not r.ok:
            raise HTTPException(404)
        ctype = r.headers.get("content-type", "image/png").split(";")[0]
        if not ctype.startswith("image/"):
            raise HTTPException(404)
        cache.put(p, (r.content, ctype))
        return _img(r.content, ctype)

    @app.get("/img/local")
    def img_local(p: str):
        path = Path(p).resolve()
        roots = [d.resolve() for d in (cfg.SAVE_SYNC_DIR, cfg.STATE_SYNC_DIR, cfg.ALL_SYNC_DIR) if d is not None]
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg") or not any(root in path.parents for root in roots):
            raise HTTPException(404)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=300"})

    @app.get("/img/platform/{slug}")
    def img_platform(slug: str):
        if not slug.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400)
        key = f"platform:{slug}"
        hit = cache.get(key)
        if hit:
            return _img(*hit)
        for ext in ("svg", "ico", "png"):
            try:
                r = romm.get_raw(f"/assets/platforms/{slug}.{ext}", timeout=8)
            except Exception:
                break
            ctype = r.headers.get("content-type", "").split(";")[0] or mimetypes.guess_type(f"x.{ext}")[0] or ""
            if r.ok and (ctype.startswith("image/") or ext == "svg" and r.content.lstrip().startswith(b"<")):
                ctype = "image/svg+xml" if ext == "svg" else ctype
                cache.put(key, (r.content, ctype))
                return _img(r.content, ctype)
        raise HTTPException(404)
    # endregion

    return app


def start_web_server(cfg) -> Optional[threading.Thread]:
    app = create_app(cfg)
    config = uvicorn.Config(app, host="0.0.0.0", port=cfg.WEB_PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="web-ui", daemon=True)
    t.start()
    # Only report success once the port is actually bound.
    for _ in range(100):
        if server.started:
            logger.info(f"Web UI ready on port {cfg.WEB_PORT} (inside the container). "
                        f"Open http://<server-ip>:<mapped port>/")
            return t
        if not t.is_alive():
            break
        time.sleep(0.1)
    logger.error(f"Web UI failed to start on port {cfg.WEB_PORT} — is something else using that port? "
                 "Sync keeps running without the UI.")
    return t
