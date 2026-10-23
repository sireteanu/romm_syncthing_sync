"""A tiny in-memory stand-in for the RomM 5.4 API, for local development, tests and README screenshots.

Implements only what romm_sync uses:
  GET  /api/heartbeat
  GET  /api/roms                         (paginated: items/limit/offset)
  GET  /api/saves  | /api/states         (optionally ?rom_id=)
  POST /api/saves  | /api/states         (multipart saveFile/stateFile [+ screenshotFile], ?rom_id=)
  GET  /api/{kind}/{id}
  PUT  /api/{kind}/{id}
  GET  /api/{kind}/{id}/content
  GET  /assets/...                       generated cover / screenshot / platform images

Run:  python -m tests.mock_romm --port 8099 --seed demo
All game names and artwork are invented and procedurally generated.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import random
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

PLATFORMS = {
    1: ("snes", "Super Nintendo Entertainment System", (124, 92, 255)),
    2: ("gba", "Game Boy Advance", (90, 167, 255)),
    3: ("psx", "PlayStation", (242, 179, 61)),
    4: ("n64", "Nintendo 64", (60, 207, 142)),
    5: ("genesis", "Sega Mega Drive/Genesis", (255, 93, 108)),
}


DEMO_T0 = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)  # fixed so demo baselines line up


def now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self):
        self.roms: List[dict] = []
        self.assets: Dict[str, List[dict]] = {"saves": [], "states": []}
        self.blobs: Dict[str, bytes] = {}
        self.next_id = 100

    def add_rom(self, rom_id: int, platform_id: int, fs_name: str, name: str):
        slug, display, _ = PLATFORMS[platform_id]
        stem = fs_name.rsplit(".", 1)[0]
        self.roms.append({
            "id": rom_id, "name": name, "fs_name": fs_name, "fs_name_no_ext": stem,
            "fs_size_bytes": random.randint(1, 600) * 1024 * 1024,
            "platform_id": platform_id, "platform_slug": slug, "platform_fs_slug": slug,
            "platform_display_name": display,
            "path_cover_small": f"/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/small.png",
            "path_cover_large": f"/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png",
            "url_cover": None,
            "merged_screenshots": [f"/assets/romm/resources/roms/{platform_id}/{rom_id}/screenshots/0.png"],
        })

    def add_asset(self, kind: str, rom_id: int, file_name: str, content: bytes,
                  updated_at: Optional[datetime] = None, with_screenshot: bool = False) -> dict:
        self.next_id += 1
        aid = self.next_id
        rom = next(r for r in self.roms if r["id"] == rom_id)
        path = f"users/1/{kind}/{rom['platform_slug']}/{rom_id}"
        ts = (updated_at or now()).isoformat()
        stem, _, ext = file_name.partition(".")
        item = {
            "id": aid, "rom_id": rom_id, "user_id": 1, "file_name": file_name,
            "file_name_no_tags": file_name, "file_name_no_ext": stem, "file_extension": ext.split(".")[-1],
            "file_path": path, "file_size_bytes": len(content), "full_path": f"{path}/{file_name}",
            "download_path": f"/api/raw/assets/{path}/{file_name}", "missing_from_fs": False,
            "created_at": ts, "updated_at": ts, "emulator": None, "screenshot": None,
        }
        if kind == "saves":
            item["content_hash"] = hashlib.md5(content).hexdigest()
            item["slot"] = None
        if with_screenshot:
            item["screenshot"] = {"id": aid + 5000, "download_path": f"/assets/romm/assets/{path}/{file_name}.png",
                                  "file_name": f"{file_name}.png"}
        self.assets[kind].append(item)
        self.blobs[f"{kind}:{aid}"] = content
        return item


S = Store()
app = FastAPI(title="mock RomM")


@app.get("/api/heartbeat")
def heartbeat():
    return {"SYSTEM": {"VERSION": "5.4.0-mock"}}


@app.get("/api/roms")
def roms(limit: int = 50, offset: int = 0):
    items = S.roms[offset:offset + limit]
    return {"items": items, "total": len(S.roms), "limit": limit, "offset": offset}


@app.get("/api/platforms")
def platforms():
    out = []
    for pid, (slug, display, _) in PLATFORMS.items():
        n = sum(1 for r in S.roms if r["platform_id"] == pid)
        if n:
            out.append({"id": pid, "slug": slug, "fs_slug": slug, "name": display, "display_name": display,
                        "custom_name": None, "rom_count": n})
    return out


def _kind(kind: str) -> str:
    if kind not in ("saves", "states"):
        raise HTTPException(404)
    return kind


@app.get("/api/{kind}")
def list_assets(kind: str, rom_id: Optional[int] = None, platform_id: Optional[int] = None):
    items = S.assets[_kind(kind)]
    if rom_id is not None:
        items = [i for i in items if i["rom_id"] == rom_id]
    return items


@app.get("/api/{kind}/{aid}")
def get_asset(kind: str, aid: int):
    for i in S.assets[_kind(kind)]:
        if i["id"] == aid:
            return i
    raise HTTPException(404, "not found")


@app.get("/api/{kind}/{aid}/content")
def content(kind: str, aid: int):
    blob = S.blobs.get(f"{_kind(kind)}:{aid}")
    if blob is None:
        raise HTTPException(404)
    return Response(blob, media_type="application/octet-stream")


@app.post("/api/{kind}")
async def upload(kind: str, request: Request, rom_id: int):
    form = await request.form()
    f = form.get("saveFile") or form.get("stateFile")
    if f is None:
        raise HTTPException(400, "missing file")
    data = await f.read()
    item = S.add_asset(_kind(kind), rom_id, f.filename, data, with_screenshot="screenshotFile" in form)
    return item


@app.put("/api/{kind}/{aid}")
async def replace(kind: str, aid: int, request: Request):
    form = await request.form()
    f = form.get("saveFile") or form.get("stateFile")
    for i in S.assets[_kind(kind)]:
        if i["id"] == aid:
            if f is not None:
                data = await f.read()
                S.blobs[f"{kind}:{aid}"] = data
                i["file_size_bytes"] = len(data)
                if kind == "saves":
                    i["content_hash"] = hashlib.md5(data).hexdigest()
            i["updated_at"] = now().isoformat()
            return i
    raise HTTPException(404)


# ---------- generated artwork ----------
def _art(seed: str, w: int, h: int, color, kind: str) -> bytes:
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    if kind == "cover":
        img = Image.new("RGB", (w, h), tuple(int(c * 0.35) for c in color))
        d = ImageDraw.Draw(img)
        for i in range(6):
            r = rnd.randint(w // 6, w // 2)
            x, y = rnd.randint(0, w), rnd.randint(0, h)
            shade = tuple(min(255, int(c * rnd.uniform(0.5, 1.2))) for c in color)
            d.ellipse([x - r, y - r, x + r, y + r], fill=shade)
        d.rectangle([0, int(h * 0.78), w, h], fill=(15, 16, 22))
        return _png(img)
    # "screenshot": blocky low-res scene upscaled, like an emulator frame
    lw, lh = 64, 48
    img = Image.new("RGB", (lw, lh))
    d = ImageDraw.Draw(img)
    sky = tuple(int(c * 0.45) + 20 for c in color)
    d.rectangle([0, 0, lw, lh], fill=sky)
    ground_y = rnd.randint(30, 38)
    hills = [(x, ground_y - rnd.randint(0, 8)) for x in range(0, lw + 8, 8)]
    d.polygon([(0, lh)] + hills + [(lw, lh)], fill=tuple(int(c * 0.25) for c in color))
    d.rectangle([0, ground_y + 4, lw, lh], fill=(40, 34, 30))
    for _ in range(14):
        x, y = rnd.randint(0, lw), rnd.randint(0, ground_y - 12)
        d.point((x, y), fill=(240, 240, 220))
    px, py = rnd.randint(10, 50), ground_y - 4
    d.rectangle([px, py - 6, px + 3, py], fill=(250, 220, 120))
    for i in range(rnd.randint(2, 4)):
        bx = rnd.randint(4, 56)
        d.rectangle([bx, ground_y - 14 - i * 3, bx + 5, ground_y - 10 - i * 3], fill=tuple(min(255, c + 60) for c in color))
    return _png(img.resize((w, h), Image.NEAREST))


def _png(img) -> bytes:
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


@app.get("/assets/platforms/{name}")
def platform_icon(name: str):
    slug = name.rsplit(".", 1)[0]
    color = next((c for s, _, c in PLATFORMS.values() if s == slug), None)
    if color is None or not name.endswith(".svg"):
        raise HTTPException(404)
    hexc = "#%02x%02x%02x" % color
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect x="3" y="8" width="26" height="16" rx="5" fill="{hexc}"/>'
           f'<circle cx="10" cy="16" r="3" fill="#fff" opacity=".85"/><circle cx="21" cy="14" r="1.6" fill="#fff"/>'
           f'<circle cx="24" cy="18" r="1.6" fill="#fff"/></svg>')
    return Response(svg, media_type="image/svg+xml")


@app.get("/assets/{path:path}")
def assets(path: str):
    seed = path
    parts = path.split("/")
    color = (124, 92, 255)
    try:
        if "roms" in parts:
            pid = int(parts[parts.index("roms") + 1])
            color = PLATFORMS[pid][2]
        else:
            slug = parts[parts.index("states") + 1] if "states" in parts else "snes"
            color = next(c for s, _, c in PLATFORMS.values() if s == slug)
    except Exception:
        pass
    if "cover" in path:
        return Response(_art(seed, 180, 240, color, "cover"), media_type="image/png")
    return Response(_art(seed, 320, 240, color, "shot"), media_type="image/png")


# ---------- demo data ----------
def seed_demo():
    random.seed(7)
    games = [
        (11, 1, "Starfall Odyssey (USA).sfc", "Starfall Odyssey"),
        (12, 1, "Pixel Knights (USA).sfc", "Pixel Knights"),
        (13, 1, "Lantern Keep (USA).sfc", "Lantern Keep"),
        (21, 2, "Neon Drift (Europe).gba", "Neon Drift"),
        (22, 2, "Moonlit Harbor (USA).gba", "Moonlit Harbor"),
        (23, 2, "Clockwork Garden (USA).gba", "Clockwork Garden"),
        (31, 3, "Crystal Caverns (USA).chd", "Crystal Caverns"),
        (32, 3, "Ashen Circuit (USA).chd", "Ashen Circuit"),
        (41, 4, "Thunder Rally 64 (USA).z64", "Thunder Rally 64"),
        (42, 4, "Orbit Golf (USA).z64", "Orbit Golf"),
        (51, 5, "Velocity Fox (USA).md", "Velocity Fox"),
    ]
    for g in games:
        S.add_rom(*g)
    t = DEMO_T0
    # in sync (content identical to the local seed files)
    S.add_asset("saves", 11, "Starfall Odyssey (USA).srm", b"starfall-save-v3", t)
    S.add_asset("states", 11, "Starfall Odyssey (USA).state1", b"starfall-state-1", t, with_screenshot=True)
    S.add_asset("saves", 13, "Lantern Keep (USA).srm", b"lantern-save", t)
    S.add_asset("states", 13, "Lantern Keep (USA).state", b"lantern-state", t, with_screenshot=True)
    S.add_asset("saves", 23, "Clockwork Garden (USA).srm", b"clockwork-save", t)
    S.add_asset("states", 41, "Thunder Rally 64 (USA).state.auto", b"rally-auto", t, with_screenshot=True)
    S.add_asset("saves", 42, "Orbit Golf (USA).srm", b"orbit-save", t)
    S.add_asset("states", 32, "Ashen Circuit (USA).state2", b"ashen-state", t, with_screenshot=True)
    S.add_asset("saves", 51, "Velocity Fox (USA).srm", b"velocity-save", t)
    # newer on RomM (played on another device) -> pull
    S.add_asset("saves", 21, "Neon Drift (Europe).srm", b"neon-drift-save-from-handheld", now() - timedelta(hours=3))
    # only on RomM -> pull_new
    S.add_asset("saves", 22, "Moonlit Harbor (USA).srm", b"moonlit-save", now() - timedelta(hours=20))
    S.add_asset("states", 22, "Moonlit Harbor (USA).state1", b"moonlit-state", now() - timedelta(hours=20), with_screenshot=True)
    # conflict (state store seeds a baseline that both sides have moved past)
    S.add_asset("saves", 31, "Crystal Caverns (USA).srm", b"crystal-save-romm-edit", now() - timedelta(hours=1))
    S.add_asset("states", 31, "Crystal Caverns (USA).state3", b"crystal-state-romm", now() - timedelta(hours=1), with_screenshot=True)


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--seed", default="demo")
    a = ap.parse_args()
    if a.seed == "demo":
        seed_demo()
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
