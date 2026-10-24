"""Unit tests for the two-way sync decision rules (src.sync_engine.decide)."""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="romm_sync_test_"))

from src.sync_engine import LocalFile, decide, local_name_for_remote  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def local(h="aaa", t=T0):
    return LocalFile("save", Path("/x/Game.srm"), h, 10, t)


def remote(h=None, t=T0, rid=5):
    return {"id": rid, "file_name": "Game.srm", "updated_at": t.isoformat(), "content_hash": h}


def base(lh="aaa", rt=T0, rid=5, rh=None):
    return {"local_hash": lh, "romm_updated_at": rt.isoformat(), "romm_id": rid, "romm_hash": rh}


# --- one side only -------------------------------------------------------
def test_new_local_file_uploads_automatically_by_default():
    d = decide(local(), None, None)
    assert d.kind == "push_new" and d.auto is True


def test_new_local_file_confirm_mode():
    assert decide(local(), None, None, push_new_mode="confirm").auto is False


def test_local_file_deleted_on_romm_never_auto_uploads():
    d = decide(local(), None, base(), push_new_mode="auto")
    assert d.kind == "push_new" and d.auto is False


def test_new_remote_file_respects_pull_mode():
    assert decide(None, remote(), None, pull_mode="off").kind is None
    d = decide(None, remote(), None, pull_mode="confirm")
    assert d.kind == "pull_new" and d.auto is False
    assert decide(None, remote(), None).auto is True  # default: automatic


def test_remote_file_deleted_locally_never_auto_downloads():
    d = decide(None, remote(), base(), pull_mode="auto")
    assert d.kind == "pull_new" and d.auto is False


# --- both sides, no history -----------------------------------------------
def test_identical_content_is_adopted():
    assert decide(local("abc"), remote("abc"), None).kind == "adopt"


def test_no_baseline_local_newer_pushes_like_original_app():
    d = decide(local(t=T0 + timedelta(hours=1)), remote(t=T0), None)
    assert d.kind == "push_update" and d.auto


def test_no_baseline_remote_newer_without_hash_is_adopted():
    # upgrade from 1.x: everything RomM holds was uploaded by us, so it looks newer
    assert decide(local(t=T0), remote(t=T0 + timedelta(hours=1)), None).kind == "adopt"


def test_no_baseline_remote_newer_with_different_hash_always_asks():
    # ambiguous (no history): asked even when downloads are automatic
    d = decide(local("aaa", T0), remote("bbb", T0 + timedelta(hours=1)), None, pull_mode="auto")
    assert d.kind == "pull_update" and not d.auto and d.reason


def test_only_remote_changed_downloads_automatically_by_default():
    d = decide(local("aaa"), remote(t=T0 + timedelta(minutes=5)), base("aaa", T0))
    assert d.kind == "pull_update" and d.auto


# --- both sides, with history ---------------------------------------------
def test_nothing_changed():
    assert decide(local("aaa"), remote(t=T0), base("aaa", T0)).kind is None


def test_only_local_changed_pushes_automatically():
    d = decide(local("new"), remote(t=T0), base("aaa", T0))
    assert d.kind == "push_update" and d.auto


def test_only_remote_changed_pulls():
    d = decide(local("aaa"), remote(t=T0 + timedelta(minutes=5)), base("aaa", T0), pull_mode="confirm")
    assert d.kind == "pull_update" and not d.auto


def test_only_remote_changed_with_pull_off_does_nothing():
    assert decide(local("aaa"), remote(t=T0 + timedelta(minutes=5)), base("aaa", T0), pull_mode="off").kind is None


def test_both_changed_is_a_conflict_even_in_auto_modes():
    d = decide(local("new"), remote(t=T0 + timedelta(minutes=5)), base("aaa", T0), push_new_mode="auto", pull_mode="auto")
    assert d.kind == "conflict" and d.auto is False


def test_force_push_overrides_conflict():
    d = decide(local("new"), remote(t=T0 + timedelta(minutes=5)), base("aaa", T0), force=True)
    assert d.kind == "push_update" and d.auto


def test_remote_replaced_by_new_id_counts_as_change():
    assert decide(local("aaa"), remote(t=T0, rid=99), base("aaa", T0, rid=5), pull_mode="confirm").kind == "pull_update"


def test_remote_metadata_touch_with_same_hash_is_not_a_change():
    d = decide(local("new"), remote("h1", T0 + timedelta(minutes=5)), base("aaa", T0, rh="h1"))
    assert d.kind == "push_update"


def test_timestamp_formats_compare_equal():
    b = {"local_hash": "aaa", "romm_updated_at": "2026-10-01T12:00:00Z", "romm_id": 5, "romm_hash": None}
    assert decide(local("aaa"), remote(t=T0), b).kind is None


def test_romm_version_tag_is_stripped_from_local_name():
    assert local_name_for_remote({"file_name": "Game (USA) [2026-10-01 12-00-00-000].srm"}) == "Game (USA).srm"
    assert local_name_for_remote({"file_name": "Game (USA).state1"}) == "Game (USA).state1"


# --- platform resolution / catalog parsing (src.platforms) ------------------
from src import platforms as pf  # noqa: E402


def test_platform_resolution_order():
    mapping = {"snes": ["SFC"], "gba": ["gba"]}
    lib, lib_fs, aliases = {"snes", "gba", "genesis", "psx"}, {"playstation": "psx"}, {"megadrive": "genesis"}
    assert pf.resolve("sfc", mapping, lib, lib_fs, aliases) == ("snes", "mapping")
    assert pf.resolve("GBA", mapping, lib, lib_fs, aliases) == ("gba", "exact")
    assert pf.resolve("genesis", mapping, lib, lib_fs, aliases) == ("genesis", "exact")
    assert pf.resolve("PlayStation", mapping, lib, lib_fs, aliases) == ("psx", "romm-folder")
    assert pf.resolve("megadrive", mapping, lib, lib_fs, aliases) == ("genesis", "romm-alias")
    assert pf.resolve("GameBoy Color", mapping, lib, lib_fs, aliases) == (None, "none")


def test_apply_folder_choices_moves_and_clears():
    m = {"snes": ["sfc", "snes"], "genesis": ["md"]}
    out = pf.apply_folder_choices(m, {"SFC": "sfam", "md": None})
    assert out == {"snes": ["snes"], "sfam": ["SFC"]}


def test_parse_github_sources():
    slugs = pf.parse_slugs('class UniversalPlatformSlug(enum.StrEnum):\n    _3DO = "3do"\n    GENESIS = "genesis"\n')
    assert slugs == {"_3DO": "3do", "GENESIS": "genesis"}
    aliases = pf.parse_aliases('PLATFORM_FS_ALIASES: Final[dict[str, UPS]] = {\n    "megadrive": UPS.GENESIS,\n    "x": UPS.NOPE,\n}\n', slugs)
    assert aliases == {"megadrive": "genesis"}
    names = pf.parse_docs_names("| Name | Folder | x |\n| --- | --- | --- |\n| Sega Mega Drive/Genesis | `genesis` | ... |\n")
    assert names == {"genesis": "Sega Mega Drive/Genesis"}
