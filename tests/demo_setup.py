"""Create a demo sync folder + state file that pairs with `tests/mock_romm.py --seed demo`.

    python -m tests.demo_setup /tmp/romm-demo
Then run the app with SAVE_SYNC_FOLDER/STATE_SYNC_FOLDER pointing at <dir>/saves and <dir>/states
(see README "Developing"). Produces: in-sync games, a new local state (upload approval), a save that
is newer on RomM (download approval), a RomM-only game, a conflict, an automatic upload and an
unmatched game.
"""
import hashlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from tests.mock_romm import DEMO_T0, _art, PLATFORMS


def write(path: Path, data: bytes, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def main(root: Path):
    sv, st = root / "saves", root / "states"
    old = DEMO_T0 - timedelta(hours=1)
    recent = DEMO_T0 + timedelta(days=2, hours=10)
    files = [
        (sv / "snes/Starfall Odyssey (USA).srm", b"starfall-save-v3", old),
        (st / "snes/Starfall Odyssey (USA).state1", b"starfall-state-1", old),
        (st / "snes/Pixel Knights (USA).state1", b"pixel-knights-new-state", recent),
        (sv / "snes/Lantern Keep (USA).srm", b"lantern-save-new-progress", recent),
        (st / "snes/Lantern Keep (USA).state", b"lantern-state", old),
        (sv / "gba/Neon Drift (Europe).srm", b"neon-drift-old-save", old),
        (sv / "gba/Clockwork Garden (USA).srm", b"clockwork-save", old),
        (sv / "psx/Crystal Caverns (USA).srm", b"crystal-save-local-edit", recent),
        (st / "psx/Crystal Caverns (USA).state3", b"crystal-state-local", recent),
        (st / "psx/Ashen Circuit (USA).state2", b"ashen-state", old),
        (st / "n64/Thunder Rally 64 (USA).state.auto", b"rally-auto", old),
        (sv / "n64/Orbit Golf (USA).srm", b"orbit-save", old),
        (sv / "md/Velocity Fox (USA).srm", b"velocity-save", old),
        (sv / "md/Glyph Hunter (USA).srm", b"glyph-save", old),
    ]
    for p, data, when in files:
        write(p, data, when)
    # RetroArch-style state thumbnails
    for p, color in [(st / "snes/Pixel Knights (USA).state1.png", PLATFORMS[1][2]),
                     (st / "psx/Crystal Caverns (USA).state3.png", PLATFORMS[3][2]),
                     (st / "snes/Starfall Odyssey (USA).state1.png", PLATFORMS[1][2])]:
        write(p, _art(p.name + "local", 320, 240, color, "shot"), recent)
    (st / "gba").mkdir(parents=True, exist_ok=True)

    def md5(b):
        return hashlib.md5(b).hexdigest()
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    baselines = {
        # Lantern Keep: synced before, changed locally since -> automatic upload
        "save:13:Lantern Keep (USA).srm": {"local_hash": md5(b"lantern-save"), "romm_updated_at": DEMO_T0.isoformat(),
                                           "romm_id": 103, "romm_hash": md5(b"lantern-save"), "synced_at": DEMO_T0.isoformat()},
        # Crystal Caverns: changed on both sides since the last sync -> conflicts
        "save:31:Crystal Caverns (USA).srm": {"local_hash": md5(b"crystal-save-original"), "romm_updated_at": DEMO_T0.isoformat(),
                                              "romm_id": 113, "romm_hash": md5(b"crystal-save-original"), "synced_at": DEMO_T0.isoformat()},
        "state:31:Crystal Caverns (USA).state3": {"local_hash": md5(b"crystal-state-original"), "romm_updated_at": DEMO_T0.isoformat(),
                                                  "romm_id": 114, "romm_hash": None, "synced_at": DEMO_T0.isoformat()},
    }
    (data / "romm_sync_state.json").write_text(json.dumps({"version": 1, "baselines": baselines, "pending": [], "dismissed": {}}, indent=1))
    print(f"Demo folders ready in {root}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/romm-demo"))
