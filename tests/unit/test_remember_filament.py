"""Remember last filament (opt-in, default off): a slot that goes empty keeps
its hand-set label and its spool binding. The switch lives in the [ace]
config line only (write-through) - no save-variable twin."""
import importlib
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXTRAS = ROOT / "multiace" / "klipper" / "extras"
BACKEND = ROOT / "multiace" / "web" / "backend"
PKG = "multiace_remember_extras"


def _ace_modules():
    if PKG + ".ace" in sys.modules:
        return sys.modules[PKG + ".ace"], sys.modules[PKG + ".ace_settings"]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(EXTRAS)]
    sys.modules[PKG] = pkg
    A = importlib.import_module(PKG + ".ace")
    S = importlib.import_module(PKG + ".ace_settings")
    return A, S


# -- engine ---------------------------------------------------------------

class _Ace(object):
    def __init__(self, remember, connected=True, reconnecting=False,
                 feeds=False):
        A, S = _ace_modules()
        self._remember_filament = remember
        self._connected_per_ace = {0: connected}
        self._reconnecting_per_ace = {0: reconnecting}
        self._feeds = feeds
        self.said, self.persisted = [], []
        self._empty = A.MultiAce._empty_slot_releases_spool
        self._cmd = S.SettingsMixin.cmd_ACE_SET_REMEMBER_FILAMENT

    def _slot_still_feeds_filament(self, idx, slot):
        return self._feeds

    def releases(self):
        return self._empty(self, 0, 1)

    def log_always(self, m):
        self.said.append(m)

    def _wt_persist(self, gcmd, option, value, legacy, section='ace',
                    shadow_attr=None, shadow_val=None):
        self.persisted.append((option, value, legacy, section, shadow_attr,
                               shadow_val))
        if shadow_attr:
            setattr(self, shadow_attr, shadow_val)
        return ''


def test_default_off_keeps_todays_behaviour():
    assert _Ace(remember=False).releases() is True


def test_on_keeps_the_binding_of_an_empty_slot():
    assert _Ace(remember=True).releases() is False


@pytest.mark.parametrize("kw", [dict(connected=False),
                                dict(reconnecting=True),
                                dict(feeds=True)])
def test_the_other_guards_still_hold_with_the_switch_off(kw):
    assert _Ace(remember=False, **kw).releases() is False


class _Cmd(object):
    def __init__(self, enable):
        self.enable = enable

    def get_int(self, name, default=None, minval=None, maxval=None):
        return self.enable if name == 'ENABLE' else default


def test_setter_writes_the_config_line_only():
    ace = _Ace(remember=False)
    ace._cmd(ace, _Cmd(1))
    assert ace._remember_filament is True
    # (option, value, legacy save variable, section, shadow, shadow value)
    assert ace.persisted == [('remember_filament', 'true', None, 'ace',
                              '_remember_filament_cfg', True)]
    ace._cmd(ace, _Cmd(0))
    assert ace._remember_filament is False
    assert ace.persisted[-1][1] == 'false'


def test_no_save_variable_twin():
    """Write-through switches keep the choice in the config line only; the
    old twins are migrated and deleted at startup."""
    for name in ("ace.py", "ace_settings.py"):
        assert "ace__remember_filament" not in (EXTRAS / name).read_text(
            encoding="utf-8")


def test_the_option_is_read_from_the_config_and_defaults_off():
    src = (EXTRAS / "ace.py").read_text(encoding="utf-8")
    assert "config.getboolean('remember_filament',\n" in src
    assert re.search(r"config\.getboolean\('remember_filament',\s*False\)", src)
    cfg = (ROOT / "multiace" / "config" / "extended" / "ace.cfg").read_text(
        encoding="utf-8")
    assert re.search(r"^remember_filament: false$", cfg, re.M)


# -- web: the hand-set label -----------------------------------------------

@pytest.fixture
def web(monkeypatch, tmp_path):
    cfg = tmp_path / "ace.cfg"
    cfg.write_text("[ace]" + chr(10) + "ace_device_count: 1" + chr(10),
                   encoding="utf-8")
    monkeypatch.setenv("MULTIACE_CFG_PATH", str(cfg))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    monkeypatch.delenv("MULTIACE_MOCK_MODE", raising=False)
    monkeypatch.syspath_prepend(str(BACKEND))
    sys.modules.pop("main", None)
    mod = importlib.import_module("main")
    ov = tmp_path / "slot_overrides.json"
    ov.write_text(json.dumps({"0_1": {"material": "PETG",
                                      "color": "ff0000"}}), encoding="utf-8")
    monkeypatch.setattr(mod, "OVERRIDE_FILE", str(ov))
    mod._slot_overrides.clear()
    mod._eject_pending_since.clear()
    mod._last_rfid_status.clear()
    mod._kept_slot_emptied.clear()
    mod._overrides_mtime = 0.0
    return mod


def _status(web, remember, gate1=0, rfid1=0):
    ace = {"device_count": 1, "active_device": 0,
           "gate_status": [1, gate1, 1, 1], "aces": [],
           "remember_filament": remember}
    if rfid1:
        ace["aces"] = [{"index": 0, "slots": [
            {"index": 1, "rfid": rfid1, "type": "PLA", "material": "PLA",
             "color": [0, 0, 255], "status": "ready"}]}]
    return {"ace": ace}


def _poll_twice(web, monkeypatch, status):
    t = [1000.0]
    monkeypatch.setattr(web.time, "time", lambda: t[0])
    web._parse_state(status)
    t[0] += web.EJECT_DEBOUNCE_S + 0.1
    return web._parse_state(status)


def test_an_emptied_slot_drops_its_label_by_default(web, monkeypatch):
    _poll_twice(web, monkeypatch, _status(web, remember=False))
    assert web._override_for(0, 1) is None


def test_with_remember_on_the_label_stays(web, monkeypatch):
    _poll_twice(web, monkeypatch, _status(web, remember=True))
    assert web._override_for(0, 1) is not None
    assert web._override_for(0, 1)["material"] == "PETG"


def test_an_engine_without_the_field_means_off(web, monkeypatch):
    st = _status(web, remember=False)
    del st["ace"]["remember_filament"]
    _poll_twice(web, monkeypatch, st)
    assert web._override_for(0, 1) is None


def test_the_state_reports_the_switch(web):
    assert web._parse_state(_status(web, True))["remember_filament"] is True
    assert web._parse_state(_status(web, False))["remember_filament"] is False


def test_a_fresh_rfid_read_replaces_a_kept_label(web, monkeypatch):
    # kept: the slot went empty and the label stayed
    _poll_twice(web, monkeypatch, _status(web, remember=True))
    assert web._override_for(0, 1) is not None
    # a tagged spool is put in: the first successful read wins
    web._parse_state(_status(web, True, gate1=1, rfid1=2))
    assert web._override_for(0, 1) is None


def test_a_label_on_a_slot_that_keeps_its_tag_is_not_dropped_again(
        web, monkeypatch):
    web._parse_state(_status(web, True, gate1=1, rfid1=2))     # first read
    web._slot_overrides["0_1"] = {"material": "ABS", "color": "00ff00"}
    web._parse_state(_status(web, True, gate1=1, rfid1=2))     # same read
    assert web._override_for(0, 1) is not None


def test_a_restart_does_not_drop_a_hand_set_label(web):
    # first poll after a restart / reconnect: the tag reads 2 for the first
    # time, but the slot was never seen empty
    web._parse_state(_status(web, True, gate1=1, rfid1=2))
    assert web._override_for(0, 1)["material"] == "PETG"


def test_a_label_set_after_the_refill_survives_a_restart(web, monkeypatch):
    _poll_twice(web, monkeypatch, _status(web, remember=True))     # emptied
    web._parse_state(_status(web, True, gate1=1, rfid1=2))         # refill
    assert web._override_for(0, 1) is None
    web._slot_overrides["0_1"] = {"material": "ABS", "color": "00ff00"}
    web._last_rfid_status.clear()                                  # restart
    web._parse_state(_status(web, True, gate1=1, rfid1=2))
    assert web._override_for(0, 1)["material"] == "ABS"


def test_a_label_set_by_hand_while_empty_is_not_dropped_by_a_later_label(
        web, monkeypatch):
    _poll_twice(web, monkeypatch, _status(web, remember=True))     # emptied
    import asyncio
    req = web.SlotOverride(ace=0, slot=1, material="ABS", color="00ff00")
    asyncio.run(web.set_slot_override(req))
    web._parse_state(_status(web, True, gate1=1, rfid1=2))
    assert web._override_for(0, 1)["material"] == "ABS"


def test_with_remember_off_a_read_never_drops_a_label(web):
    web._parse_state(_status(web, False, gate1=1, rfid1=2))
    assert web._override_for(0, 1)["material"] == "PETG"
