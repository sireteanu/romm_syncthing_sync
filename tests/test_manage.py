"""Tests for the Saves page actions (src.manage): bulk delete, old-version pruning, version history, zip."""
import hashlib
import io
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="romm_sync_test_"))

from src import manage, recycle  # noqa: E402
from src import sync_engine  # noqa: E402
from src.sync_engine import LocalFile, file_key  # noqa: E402
from src.sync_state import STATE  # noqa: E402

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


class FakeRomm:
    def __init__(self, files):
        self.files = dict(files)        # (api, id) -> bytes
        self.deleted = []
        self.fail_delete = False

    def download_asset(self, api, item):
        return self.files[(api, item["id"])]

    def delete_assets(self, api, ids):
        if self.fail_delete:
            raise RuntimeError("RomM returned 500")
        self.deleted += [(api, i) for i in ids]
        for i in ids:
            self.files.pop((api, i), None)

    def get_raw(self, path):
        raise RuntimeError("no screenshots in tests")


class FakeCfg:
    DRY_RUN = False
    FILE_MODE = 0o777

    def __init__(self, root):
        self.root = root

    def sync_dir_for(self, ft):
        return self.root / ("saves" if ft == "save" else "states")


class FakeEng:
    def __init__(self, root, files, remote=None):
        self.cfg = FakeCfg(root)
        self.romm = FakeRomm(files)
        self.remote = remote
        self.backed_up = []

    def fetch_remote(self):
        return self.remote

    def _backup_local(self, path, platform):
        self.backed_up.append(path.read_bytes())

    def _write_local(self, dest, content, mtime):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with STATE.lock:
        STATE.baselines.clear()
    manage._rows.clear()
    manage._job.clear()
    manage._job["running"] = False
    yield


def _local(tmp_path, content=b"local-save", name="Game.srm"):
    p = tmp_path / "saves" / "snes" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return LocalFile("save", p, hashlib.md5(content).hexdigest(), len(content), T)


def _row(tmp_path, lf, rf, group, name="Game.srm", rom_id=7):
    r = {"id": "r1", "game": "Game", "platform": "snes", "system": "SNES", "rom_id": rom_id, "type": "save",
         "name": name, "status": "both", "_lf": lf, "_rf": rf, "_group": group,
         "_dest": tmp_path / "saves" / "snes" / name, "_key": file_key("save", rom_id, name), "_items": {}}
    manage._rows[r["id"]] = r
    return r


def test_delete_removes_both_sides_and_keeps_one_copy_when_identical(tmp_path):
    lf = _local(tmp_path)
    eng = FakeEng(tmp_path, {("saves", 1): b"local-save", ("saves", 2): b"older"})
    rf = {"id": 1, "_api": "saves", "updated_at": T.isoformat()}
    r = _row(tmp_path, lf, rf, [("saves", 1), ("saves", 2)])
    STATE.set_baseline(r["_key"], lf.hash, T.isoformat(), 1, None, persist=False)
    ok, msg = manage._delete(eng, r)
    assert ok
    assert not lf.path.exists()
    assert sorted(eng.romm.deleted) == [("saves", 1), ("saves", 2)]     # every RomM version
    assert len(recycle.entries()) == 1                                  # identical copies stored once
    assert STATE.get_baseline(r["_key"]) is None                        # won't come back on the next sync


def test_delete_keeps_romm_copy_too_when_different(tmp_path):
    lf = _local(tmp_path, b"mine")
    eng = FakeEng(tmp_path, {("saves", 1): b"theirs"})
    r = _row(tmp_path, lf, {"id": 1, "_api": "saves"}, [("saves", 1)])
    manage._delete(eng, r)
    contents = sorted(recycle.path_of(e).read_bytes() for e in recycle.entries())
    assert contents == [b"mine", b"theirs"]


def test_delete_leaves_local_file_if_romm_delete_fails(tmp_path):
    lf = _local(tmp_path)
    eng = FakeEng(tmp_path, {("saves", 1): b"x"})
    eng.romm.fail_delete = True
    r = _row(tmp_path, lf, {"id": 1, "_api": "saves"}, [("saves", 1)])
    with pytest.raises(RuntimeError):
        manage._delete(eng, r)
    assert lf.path.exists()


def test_delete_folder_only_file(tmp_path):
    lf = _local(tmp_path)
    eng = FakeEng(tmp_path, {})
    r = _row(tmp_path, lf, None, [], rom_id=None)
    r["_key"] = None
    assert manage._delete(eng, r)[0]
    assert not lf.path.exists() and len(recycle.entries()) == 1 and eng.romm.deleted == []


def test_prune_keeps_newest_version(tmp_path):
    eng = FakeEng(tmp_path, {("saves", 3): b"new", ("saves", 2): b"mid", ("states", 1): b"old"})
    r = _row(tmp_path, None, {"id": 3, "_api": "saves"}, [("saves", 3), ("saves", 2), ("states", 1)])
    r["_items"] = {("saves", 2): {"id": 2, "updated_at": "2026-09-01T00:00:00+00:00"}, ("states", 1): {"id": 1}}
    manage._prune(eng, r)
    assert sorted(eng.romm.deleted) == [("saves", 2), ("states", 1)]
    assert ("saves", 3) in eng.romm.files
    assert len(recycle.entries()) == 2


def test_auto_prune_keeps_n_newest_and_the_synced_one(tmp_path, monkeypatch):
    def item(i, day):
        return {"id": i, "rom_id": 7, "file_name": "Game.srm", "_api": "saves",
                "updated_at": f"2026-09-{day:02d}T00:00:00+00:00"}
    items = [item(1, 1), item(2, 2), item(3, 3), item(4, 4), item(5, 5)]
    eng = FakeEng(tmp_path, {("saves", i): b"v%d" % i for i in range(1, 6)},
                  remote={"save": {7: items}, "state": {}})
    monkeypatch.setattr(sync_engine, "SyncEngine", lambda: eng)
    STATE.set_baseline(file_key("save", 7, "Game.srm"), "h", items[0]["updated_at"], 1, None, persist=False)

    class Srv:
        library = [{"id": 7, "fs_name": "Game.sfc", "fs_name_no_ext": "Game", "name": "Game", "platform_slug": "snes"}]

    removed = manage.auto_prune(Srv(), None, keep=2)
    assert removed == 2
    assert sorted(i for _, i in eng.romm.deleted) == [2, 3]   # 4 and 5 are newest, 1 is the synced copy


def test_restore_version_backs_up_current_copy(tmp_path, monkeypatch):
    lf = _local(tmp_path, b"current")
    eng = FakeEng(tmp_path, {("saves", 9): b"older"})
    monkeypatch.setattr(sync_engine, "SyncEngine", lambda: eng)
    r = _row(tmp_path, lf, {"id": 9, "_api": "saves"}, [("saves", 9)])
    manage.restore_version(r["id"], "romm", "saves:9")
    assert lf.path.read_bytes() == b"older"
    assert eng.backed_up == [b"current"]


def test_version_content_rejects_foreign_romm_ids(tmp_path, monkeypatch):
    eng = FakeEng(tmp_path, {("saves", 9): b"x", ("saves", 99): b"other game"})
    monkeypatch.setattr(sync_engine, "SyncEngine", lambda: eng)
    r = _row(tmp_path, None, {"id": 9, "_api": "saves"}, [("saves", 9)])
    with pytest.raises(ValueError):
        manage.version_content(r["id"], "romm", "saves:99")
    with pytest.raises(ValueError):
        manage.version_content(r["id"], "backup", "../../etc/passwd")


def test_versions_lists_bin_and_backups(tmp_path):
    from src.sync_state import data_dir
    lf = _local(tmp_path)
    r = _row(tmp_path, lf, None, [], rom_id=None)
    b = data_dir() / "backups" / "2026-10-01" / "snes"
    b.mkdir(parents=True)
    (b / "120000-local-Game.srm").write_bytes(b"bk")
    recycle.add(file_name="Game.srm", file_type="save", game="Game", platform="snes", rom_id=None,
                deleted_from="local", reason="t", content=b"bin", restore_path=str(lf.path))
    sources = sorted(v["source"] for v in manage.versions(r["id"]))
    assert sources == ["backup", "bin", "local"]


def test_zip_uses_folder_copy_or_romm(tmp_path, monkeypatch):
    lf = _local(tmp_path, b"folder")
    eng = FakeEng(tmp_path, {("saves", 5): b"romm-only"})
    monkeypatch.setattr(sync_engine, "SyncEngine", lambda: eng)
    _row(tmp_path, lf, None, [])
    manage._rows["r2"] = dict(manage._rows["r1"], id="r2", name="Other.srm", _lf=None,
                              _rf={"id": 5, "_api": "saves"}, _group=[("saves", 5)])
    f, n, problems = manage.build_zip(["r1", "r2"])
    z = zipfile.ZipFile(io.BytesIO(f.read()))
    assert n == 2 and not problems
    assert z.read("saves/snes/Game.srm") == b"folder"
    assert z.read("saves/snes/Other.srm") == b"romm-only"


def test_start_rejects_bad_requests():
    with pytest.raises(ValueError):
        manage.start("format_disk", ["x"])
    with pytest.raises(ValueError):
        manage.start("delete", ["not-in-list"])
