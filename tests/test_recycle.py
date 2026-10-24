"""Tests for the recycle bin (src.recycle)."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="romm_sync_test_"))

from src import recycle  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_bin(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    yield


def _add(tmp_path, name="Game.srm", content=b"save", shot=None):
    return recycle.add(file_name=name, file_type="save", game="Game", platform="snes", rom_id=1,
                       deleted_from="romm", reason="test", content=content,
                       restore_path=str(tmp_path / "sync" / "snes" / name), screenshot=shot)


def test_add_and_list(tmp_path):
    e = _add(tmp_path)
    assert recycle.path_of(e).read_bytes() == b"save"
    assert [x["id"] for x in recycle.entries()] == [e["id"]]


def test_restore_puts_file_back_and_removes_entry(tmp_path):
    e = _add(tmp_path, shot=b"png")
    dest = recycle.restore(e["id"], file_mode=0o777)
    assert open(dest, "rb").read() == b"save"
    assert os.path.exists(dest + ".png")
    assert oct(os.stat(dest).st_mode & 0o777) == "0o777"
    assert recycle.entries() == []


def test_restore_refuses_to_overwrite(tmp_path):
    e = _add(tmp_path)
    dest = tmp_path / "sync" / "snes" / "Game.srm"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"newer")
    with pytest.raises(ValueError):
        recycle.restore(e["id"])
    assert dest.read_bytes() == b"newer"
    assert len(recycle.entries()) == 1


def test_purge_only_removes_old_entries(tmp_path):
    old, new = _add(tmp_path, "Old.srm"), _add(tmp_path, "New.srm")
    idx = recycle.root() / "index.json"
    items = json.loads(idx.read_text())
    for it in items:
        if it["id"] == old["id"]:
            it["deleted_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    idx.write_text(json.dumps(items))
    assert recycle.purge(0) == 0          # 0 = keep forever
    assert recycle.purge(30) == 1
    assert [x["id"] for x in recycle.entries()] == [new["id"]]
    assert not (recycle.root() / old["id"]).exists()


def test_remove_and_empty(tmp_path):
    a, _ = _add(tmp_path, "A.srm"), _add(tmp_path, "B.srm")
    assert recycle.remove(a["id"]) is True
    assert recycle.remove(a["id"]) is False
    assert recycle.empty() == 1
    assert recycle.entries() == []
