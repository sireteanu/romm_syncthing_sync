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


def test_deleted_on_romm_moves_local_copy_to_bin():
    d = decide(local(), None, base(), remote_ids=set())
    assert d.kind == "delete_local" and d.auto


def test_deleted_on_romm_asks_when_mode_is_ask():
    d = decide(local(), None, base(), delete_mode="ask", remote_ids=set())
    assert d.kind == "delete_local" and not d.auto


def test_deleted_on_romm_but_changed_locally_asks_to_upload_again():
    d = decide(local("changed"), None, base("aaa"), remote_ids=set())
    assert d.kind == "push_new" and not d.auto


def test_renamed_on_romm_is_not_a_deletion():
    d = decide(local(), None, base(rid=5), remote_ids={5})  # entry still exists under another name
    assert d.kind == "push_new" and not d.auto


def test_delete_mode_off_keeps_old_behaviour():
    assert decide(local(), None, base(), delete_mode="off", remote_ids=set()).kind == "push_new"
    assert decide(None, remote(), base(), delete_mode="off").kind == "pull_new"


def test_new_remote_file_respects_pull_mode():
    assert decide(None, remote(), None, pull_mode="off").kind is None
    d = decide(None, remote(), None, pull_mode="confirm")
    assert d.kind == "pull_new" and d.auto is False
    assert decide(None, remote(), None).auto is True  # default: automatic


def test_deleted_locally_moves_romm_copy_to_bin():
    d = decide(None, remote(t=T0), base("aaa", T0))
    assert d.kind == "delete_romm" and d.auto


def test_deleted_locally_but_changed_on_romm_asks_to_download_again():
    d = decide(None, remote(t=T0 + timedelta(hours=1)), base("aaa", T0))
    assert d.kind == "pull_new" and not d.auto


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


# --- matching (src.library_classes) ---------------------------------------
from src.library_classes import LibraryScanner, LocalLibrary  # noqa: E402


def test_parse_save_filename_handles_dots_and_types():
    p = LibraryScanner.parse_save_filename
    assert p("Dr. Mario (USA).srm") == ("Dr. Mario (USA)", "save")
    assert p("Super Mario Bros. 3 (USA).state1") == ("Super Mario Bros. 3 (USA)", "state")
    assert p("Super Mario Bros. 3 (USA).state.auto") == ("Super Mario Bros. 3 (USA)", "state")
    assert p("Super Mario Bros. 3 (USA).state.auto.png") == ("Super Mario Bros. 3 (USA)", "state_screen")
    assert p("Pokemon Diamond.ml2") == ("Pokemon Diamond", "state")
    assert p("cover.png") == (None, None)
    assert p("notes.txt") == (None, None)


def test_watchdog_detects_dotted_names():
    assert LibraryScanner._detect_game_from_watchdog_event(Path("/s/nes/Dr. Mario (USA).srm")) == "Dr. Mario (USA)"


def test_watchdog_events_are_grouped_per_system():
    ev = LibraryScanner._parse_watchdog_events([Path("/s/gb/Tetris (World).srm"), Path("/s/gbc/Tetris (World).srm")])
    assert set(ev) == {("gb", "Tetris (World)"), ("gbc", "Tetris (World)")}


def _rom(fs_name, rid=1):
    return {"id": rid, "fs_name": fs_name, "fs_name_no_ext": fs_name.rsplit(".", 1)[0] if "." in fs_name else fs_name}


def test_find_rom_prefers_exact_and_rejects_middle_substrings():
    roms = [_rom("Super Mario World (USA).sfc", 1), _rom("Mario Paint (USA).sfc", 2), _rom("Mario (USA).sfc", 3)]
    assert LibraryScanner.find_rom("Mario (USA)", roms)["id"] == 3
    assert LibraryScanner.find_rom("Super Mario World (USA)", roms)["id"] == 1
    assert LibraryScanner.find_rom("World (USA)", roms) is None  # middle-of-name never matches


def test_find_rom_prefix_fallback_picks_closest():
    roms = [_rom("Pokemon Red (USA, Europe) (Rev 1).gb", 1), _rom("Pokemon Red (USA, Europe).gb", 2)]
    assert LibraryScanner.find_rom("Pokemon Red", roms)["id"] == 2
    assert LibraryScanner.find_rom("Pokemon Re", roms) is None  # must end at a word boundary


def test_same_game_in_two_systems_stays_separate(tmp_path):
    for sysdir in ("gb", "gbc"):
        (tmp_path / sysdir).mkdir()
        (tmp_path / sysdir / "Tetris (World).srm").write_bytes(sysdir.encode())
    states = tmp_path / "states"
    lib = LocalLibrary([tmp_path])
    lib.build_local_library()
    assert {g.platform for g in lib.games.values()} == {"gb", "gbc"}
    assert all(len(g.local_save_files) == 1 for g in lib.games.values())
    assert states.exists() is False


def test_saves_and_states_folders_still_merge(tmp_path):
    sv, st = tmp_path / "saves" / "gba", tmp_path / "states" / "gba"
    sv.mkdir(parents=True), st.mkdir(parents=True)
    (sv / "Game (USA).srm").write_bytes(b"s")
    (st / "Game (USA).state1").write_bytes(b"t")
    lib = LocalLibrary([tmp_path / "saves", tmp_path / "states"])
    lib.build_local_library()
    (game,) = lib.games.values()
    assert len(game.local_save_files) == 1 and len(game.local_state_files) == 1


# --- settings & notifications ----------------------------------------------
import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from src import settings as st  # noqa: E402
from src import notify  # noqa: E402
from src.sync_state import STATE, PendingAction  # noqa: E402


def _cfg():
    c = SimpleNamespace(**{k: None for k in st.FIELDS})
    c.PUSH_NEW_MODE, c.PULL_MODE, c.WEB_USERNAME, c.WEB_PASSWORD = "auto", "auto", "", ""
    return c


def test_invalid_settings_change_nothing():
    cfg = _cfg()
    st.apply_to_config(cfg)
    with pytest.raises(ValueError):
        st.update(cfg, {"WEB_USERNAME": "me"}, [])  # password missing
    assert cfg.WEB_USERNAME == ""
    with pytest.raises(ValueError):
        st.update(cfg, {"ROMM_POLL_INTERVAL_SECONDS": -1}, [])
    st.update(cfg, {"PULL_MODE": "off"}, [])
    assert cfg.PULL_MODE == "off"
    st.update(cfg, {}, ["PULL_MODE"])
    assert cfg.PULL_MODE == "auto"


def test_notifications_are_not_repeated(monkeypatch):
    sent = []
    monkeypatch.setattr(st, "notify_config", lambda: {"channels": [{"type": "webhook", "url": "x"}],
                                                      "events": {"questions": True, "errors": True,
                                                                 "activity": False, "problems": True},
                                                      "dashboard_url": ""})
    monkeypatch.setattr(notify, "send", lambda title, body, prio="normal", channels=None: sent.append(title))
    monkeypatch.setattr(notify.threading, "Thread",
                        lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args)))
    a = PendingAction(kind="conflict", file_type="save", game="G", platform="snes", rom_id=1, file_name="G.srm")
    with STATE.lock:
        STATE.pending[a.id] = a
    try:
        notify.after_sync({"pushed": 0, "pulled": 0})
        notify.after_sync({"pushed": 0, "pulled": 0})
        assert len(sent) == 1 and "conflict" in sent[0]
    finally:
        with STATE.lock:
            STATE.pending.pop(a.id, None)


# --- RomM 5.x file names from other devices (real examples) ---------------------------
def test_romm_names_are_renamed_to_the_rom_file_name():
    picross = {"fs_name": "Mario's Picross (USA, Europe).gb", "fs_name_no_ext": "Mario's Picross (USA, Europe)"}
    panzer = {"fs_name": "Panzer Dragoon Saga (USA) (Disc 1).chd", "fs_name_no_ext": "Panzer Dragoon Saga (USA) (Disc 1)"}
    metroid = {"fs_name": "Super Metroid (Japan, USA).sfc", "fs_name_no_ext": "Super Metroid (Japan, USA)"}
    n = local_name_for_remote
    assert n({"file_name": "autosave [2026-08-17_19-02-17].srm"}, picross) == "Mario's Picross (USA, Europe).srm"
    assert n({"file_name": "Mario's Picross (USA, Europe) [2026-09-19_06-12-01].srm"}, picross) == "Mario's Picross (USA, Europe).srm"
    assert n({"file_name": "Mario's Picross (USA, Europe) [2026-08-17_15-51-41].autosave.state0"}, picross) == "Mario's Picross (USA, Europe).state"  # RetroArch slot 0
    assert n({"file_name": "Mario's Picross (USA, Europe) [2026-09-08_17-34-35].autosave.state.auto"}, picross) == "Mario's Picross (USA, Europe).state.auto"
    assert n({"file_name": "Mario's Picross (USA, Europe).state1"}, picross) == "Mario's Picross (USA, Europe).state1"
    assert n({"file_name": "freegosy [2026-10-18_00-15-29].srm"}, metroid) == "Super Metroid (Japan, USA).srm"
    assert n({"file_name": "Super Metroid (Japan, USA) [2026-10-17_05-17-38].freegosy.state.auto"}, metroid) == "Super Metroid (Japan, USA).state.auto"
    assert n({"file_name": "Panzer Dragoon Saga (USA).bcr"}, panzer) == "Panzer Dragoon Saga (USA).bcr"  # m3u name kept


def test_multi_disc_prefix_tie_break_prefers_disc_1():
    roms = [_rom("Panzer Dragoon Saga (USA) (Disc 2).chd", 2), _rom("Panzer Dragoon Saga (USA) (Disc 1).chd", 1)]
    assert LibraryScanner.find_rom("Panzer Dragoon Saga (USA)", roms)["id"] == 1


def test_bulk_route_is_not_swallowed_by_single_action_route():
    from fastapi.testclient import TestClient
    from src.web import create_app
    cfg = SimpleNamespace(ROMM_CREDENTIALS=SimpleNamespace(url="http://x", auth_type="api"), WEB_USERNAME="",
                          WEB_PASSWORD="", SAVE_SYNC_DIR=None, STATE_SYNC_DIR=None, ALL_SYNC_DIR=None, PLATFORM_MAP_PATH=None)
    r = TestClient(create_app(cfg)).post("/api/actions/bulk", json={"ids": [], "decision": "approve"})
    assert r.status_code == 200 and r.json().get("queued") == 0


def test_state_and_state0_are_both_kept(tmp_path):
    d = tmp_path / "snes"
    d.mkdir()
    (d / "Game (USA).state").write_bytes(b"a")
    (d / "Game (USA).state0").write_bytes(b"b")
    lib = LocalLibrary([tmp_path])
    lib.build_local_library()
    (game,) = lib.games.values()
    assert len(game.local_state_files) == 2


# --- regressions from a real RomM 5.4 library -------------------------------------------
def test_exact_name_wins_rom_over_prefix_match():
    from src.games_class import Game
    lib = SimpleNamespace(library=[{"id": 81, "fs_name": "Fallen Angel.crt", "fs_name_no_ext": "Fallen Angel",
                                    "platform_slug": "c64", "name": "Fallen Angel", "platform_id": 8, "fs_size_bytes": 1}],
                          get_romm_platform=lambda folder: "c64")
    fallen = Game(name="Fallen", path=Path("/x"), platform="c64")
    angel = Game(name="Fallen Angel", path=Path("/x"), platform="c64")
    LibraryScanner.match_to_romm([fallen, angel], lib)   # order must not matter
    assert angel.romm_id == 81 and not fallen.is_matched


def test_states_in_romms_saves_list_are_classified_by_name():
    from src.sync_engine import SyncEngine
    row = {"fs_name": "Mario's Picross (USA, Europe).gb", "fs_name_no_ext": "Mario's Picross (USA, Europe)"}
    remote = {"save": {71: [{"id": 1, "file_name": "Mario's Picross (USA, Europe).state1", "updated_at": T0.isoformat(), "_api": "saves"},
                            {"id": 2, "file_name": "Mario's Picross (USA, Europe).srm", "updated_at": T0.isoformat(), "_api": "saves"}]},
              "state": {}}
    out = SyncEngine._remote_for_rom(remote, 71, row)
    assert set(out["state"]) == {"Mario's Picross (USA, Europe).state1"}
    assert out["state"]["Mario's Picross (USA, Europe).state1"]["_api"] == "saves"  # updates go back to that list
    assert set(out["save"]) == {"Mario's Picross (USA, Europe).srm"}


def test_written_files_and_new_folders_get_file_mode(tmp_path, monkeypatch):
    import stat
    from src.sync_engine import SyncEngine
    eng = SyncEngine.__new__(SyncEngine)
    eng.cfg = SimpleNamespace(FILE_MODE=0o777, PUID=None, PGID=None)
    old_umask = os.umask(0o022)
    try:
        dest = tmp_path / "saves" / "newsys" / "Game (USA).srm"
        eng._write_local(dest, b"data", None)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o777
    assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o777


def test_file_mode_setting_accepts_numeric_choice():
    cfg = _cfg()
    cfg.FILE_MODE = 0o777
    st.apply_to_config(cfg)
    st.update(cfg, {"FILE_MODE": "438"}, [])
    assert cfg.FILE_MODE == 0o666
    st.update(cfg, {}, ["FILE_MODE"])


# --- quiet RomM polling (watch mode) ------------------------------------
def _poller(monkeypatch):
    import threading as _t
    from src import sync_orchestrator as so

    class FakeApi:
        def __init__(self):
            self.fp = {("saves", 1): ("t1", 10)}
            self.own_changes = {}
            self._own_lock = _t.Lock()

        def fingerprint(self):
            return dict(self.fp)

        def take_own(self, snap):
            for k, v in snap.items():
                if self.own_changes.get(k, object()) == v:
                    del self.own_changes[k]

    class FakeCfg:
        FULL_SCAN_INTERVAL_HOURS = 0
        def __init__(self, api):
            self.ROMM_CREDENTIALS = api

    class FakeOrch:
        def __init__(self):
            self.full, self.targeted = 0, []
        def full_sync(self, force_sync=False):
            self.full += 1
        def romm_sync(self, rom_ids):
            self.targeted.append(set(rom_ids))

    api, orch = FakeApi(), FakeOrch()
    monkeypatch.setattr(so, "get_config", lambda: FakeCfg(api))
    return so.RommPoller(orch, 600), api, orch


def test_quiet_poll_only_syncs_what_changed_on_romm(monkeypatch):
    p, api, orch = _poller(monkeypatch)
    p.check()                                   # first check: no fingerprint yet -> full sync
    assert orch.full == 1
    p.check()                                   # nothing changed on RomM -> folders untouched
    assert orch.full == 1 and orch.targeted == []
    api.fp[("saves", 2)] = ("t2", 10)           # our own upload
    api.own_changes[("saves", 2)] = "t2"
    p.check()
    assert orch.targeted == [] and not api.own_changes
    api.fp[("states", 9)] = ("t3", 42)          # another device uploaded a state for ROM 42
    p.check()
    assert orch.targeted == [{42}]
    del api.fp[("saves", 1)]                    # deleted on RomM (ROM 10)
    p.check()
    assert orch.targeted[-1] == {10}
    assert orch.full == 1                       # never re-read everything


def test_own_change_overwritten_by_another_device_still_counts(monkeypatch):
    p, api, orch = _poller(monkeypatch)
    p.check()
    api.fp[("saves", 1)] = ("t5", 10)           # we updated save 1 (RomM says t5)...
    api.own_changes[("saves", 1)] = "t4"        # ...but our upload got t4: someone changed it after us
    p.check()
    assert orch.targeted == [{10}]


def test_own_delete_is_ignored(monkeypatch):
    p, api, orch = _poller(monkeypatch)
    p.check()
    del api.fp[("saves", 1)]
    api.own_changes[("saves", 1)] = None
    p.check()
    assert orch.targeted == [] and orch.full == 1


def test_missing_romm_table_is_explained():
    from src.sync_engine import explain_error
    from src.sync_state import PendingAction
    a = PendingAction(kind="push_new", file_type="save", game="G", platform="nds", rom_id=1, file_name="G.sav")
    e = RuntimeError("""RomM returned 500: {"detail":"(mariadb.ProgrammingError) Table 'romm.deleted_assets' doesn't exist"}""")
    msg = explain_error(e, a)
    assert "deleted_assets" in msg and "migration" in msg


# --- matching by title / manual links ------------------------------------
def test_title_key_handles_articles_tags_and_punctuation():
    from src.library_classes import LibraryScanner as L
    assert L.title_key("Legend of Zelda, The - Spirit Tracks (USA) (En,Fr,Es)") == \
        L.title_key("The Legend of Zelda: Spirit Tracks")
    assert L.title_key("Mario & Luigi (Europe)") == L.title_key("Mario and Luigi")
    assert L.title_key("starfall_odyssey") == L.title_key("Starfall Odyssey (USA)")


def test_title_match_prefers_matching_region_and_refuses_ties():
    from src.library_classes import LibraryScanner as L
    roms = [{"id": 1, "fs_name": "Spirit Tracks (USA).nds", "name": "Zelda"},
            {"id": 2, "fs_name": "Spirit Tracks (Europe).nds", "name": "Zelda"}]
    assert L.find_rom_by_title("Spirit-Tracks [USA]", roms)["id"] == 1
    assert L.find_rom_by_title("Spirit Tracks!", roms) is None       # can't tell which: no guess


def test_remote_files_follow_local_name_for_title_matched_games():
    row = {"id": 5, "fs_name": "Legend of Zelda, The - Spirit Tracks (USA).nds",
           "fs_name_no_ext": "Legend of Zelda, The - Spirit Tracks (USA)", "_local_stem": "Zelda Spirit Tracks"}
    item = {"file_name": "Legend of Zelda, The - Spirit Tracks (USA) [2026-10-01_10-00-00].sav"}
    assert local_name_for_remote(item, row) == "Zelda Spirit Tracks.sav"
    assert local_name_for_remote({"file_name": "Zelda Spirit Tracks.sav"}, row) == "Zelda Spirit Tracks.sav"


def test_manual_link_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src import links
    from src.games_class import Game
    from src.library_classes import LibraryScanner as L
    links.set_link("nds", "My Zelda Save", 9)

    class Srv:
        library = [{"id": 9, "name": "Zelda", "platform_slug": "nds", "platform_id": 1, "fs_name": "Zelda (USA).nds",
                    "fs_size_bytes": 1}]
        def get_romm_platform(self, folder):
            return "nds"
    g = Game(name="My Zelda Save", path=tmp_path, platform="nds")
    L.match_to_romm([g], Srv())
    assert g.is_matched and g.romm_id == 9 and g.match_how == "link"
