"""Dock waste: the purge bin counter, the prime_extra knob and the burst
purge of the background prime (real engine methods on stubs, the whole
swap on a simulated clock)."""
import json
import types

import pytest

from . import test_bg_speed as T

BG = T.BG


def _prime_moves(ev, marks):
    """Forward motion from the start of the prime phase on, anti-ooze
    retract excluded."""
    t0 = marks['LD_PRIME']
    return [e for t, e in ev if t >= t0 and e.startswith('MOVE')]


# -- prime_extra ----------------------------------------------------------

def test_prime_extra_default_is_the_old_constant():
    assert BG.BG_LOAD_PRIME_EXTRA == 40.
    bg, ev, marks, total, _ = T._simulate()
    assert len([e for e in _prime_moves(ev, marks) if e == 'MOVE +5.0']) == 24


def test_a_smaller_prime_extra_shortens_the_prime():
    bg, ev, marks, total, _ = T._simulate(extra=0.)
    assert len([e for e in _prime_moves(ev, marks) if e == 'MOVE +5.0']) == 16
    base = T._simulate()
    assert base[3] - total > 6


# -- bin counter ----------------------------------------------------------

def test_the_cold_pull_push_and_the_prime_are_booked():
    bg, ev, marks, total, _ = T._simulate()
    # 57 + 3 mm of pushes, 80 + 40 mm of prime; the retracts are not waste
    assert bg._bin['heads'] == {'1': pytest.approx(180.)}
    assert bg._bin_status()['mm'] == pytest.approx(180.)


def test_a_smaller_prime_extra_is_booked_smaller():
    bg = T._simulate(extra=0.)[0]
    assert bg._bin_total() == pytest.approx(140.)


def test_status_is_str_keyed_and_json_clean():
    """webhooks encodes with a strict orjson: one non-str key is a Klipper
    shutdown at the first status query (see get_status)."""
    bg = T._simulate(warn=100.)[0]
    st = bg._bin_status()
    assert json.loads(json.dumps(st)) == st
    assert all(isinstance(k, str) for k in st['heads'])
    assert st['g'] == pytest.approx(180 * BG.BG_BIN_G_PER_MM, abs=0.1)
    assert st['warn'] is True and st['warn_mm'] == 100.


def test_no_warning_below_the_level_or_when_off():
    assert T._simulate(warn=1000.)[0]._bin_status()['warn'] is False
    off = T._simulate(warn=0.)[0]
    assert off._bin_status()['warn'] is False and not off._bin_warned


class Bin(object):
    """Just the counter methods, with a recording ace behind them."""
    for _n in ('_bin_restore', '_bin_total', '_bin_add', '_bin_save',
               '_bin_status', 'cmd_ACE_BG_BIN', 'cmd_ACE_BG_SET_BURST',
               '_finish'):
        locals()[_n] = getattr(BG.AceBgSwap, _n)
    del _n

    def __init__(self):
        self.said, self.saved, self.persisted, self.warned = [], [], [], []
        self._bin = {'since': 0., 'swaps': 0, 'heads': {}}
        self._bin_op, self._bin_warned = {}, False
        self.bin_warn_mm = 0.
        self.burst_prime = False
        self._busy, self._pick_pending, self._deferred = set([0]), set(), []
        ace = types.SimpleNamespace(
            save_variable=lambda k, v, write=False: self.saved.append((k, v)),
            log_warn=lambda m: self.warned.append(m),
            _wt_persist=lambda g, opt, val, legacy, section='ace': (
                self.persisted.append((opt, val, section)) or ''))
        self.printer = types.SimpleNamespace(
            lookup_object=lambda n, d=None: ace if n == 'ace' else d)

    def _say(self, m):
        self.said.append(m)

    def _dh(self, i):
        return i + 1


class Cmd(T.Cmd):
    def __init__(self, **kw):
        T.Cmd.__init__(self, **kw)
        self.out = []

    def respond_info(self, m, log=True):
        self.out.append(m)


def test_the_end_of_a_swap_counts_it_and_persists():
    b = Bin()
    b._bin_add(0, 150.)
    b._finish(0)
    assert b._bin['swaps'] == 1
    key, data = b.saved[-1]
    assert key == 'ace__bg_bin'
    assert data['heads'] == {'0': 150.0} and data['swaps'] == 1
    # an op that pushed nothing is not a swap into the bin
    b._busy.add(1)
    b._finish(1)
    assert b._bin['swaps'] == 1 and len(b.saved) == 1


def test_the_warning_fires_once_when_the_level_is_crossed():
    b = Bin()
    b.bin_warn_mm = 100.
    b._bin_add(0, 60.)
    assert b.warned == []
    b._bin_add(0, 60.)
    b._bin_add(0, 60.)
    assert len(b.warned) == 1 and 'ACE_BG_BIN RESET=1' in b.warned[0]


def test_reset_clears_persists_and_rearms_the_warning():
    b = Bin()
    b.bin_warn_mm = 50.
    b._bin_add(0, 80.)
    cmd = Cmd(RESET=1)
    b.cmd_ACE_BG_BIN(cmd)
    assert b._bin_total() == 0 and b._bin['swaps'] == 0
    assert b.saved[-1][1]['heads'] == {}
    assert b._bin_warned is False
    assert any('reset' in o for o in cmd.out)


def test_warn_level_is_written_through_to_the_config():
    b = Bin()
    b.cmd_ACE_BG_BIN(Cmd(WARN=2500.))
    assert b.bin_warn_mm == 2500.
    assert b.persisted == [('bin_warn_mm', '2500', 'ace_bg_swap')]
    b.cmd_ACE_BG_BIN(Cmd(WARN=0.))
    assert b.bin_warn_mm == 0. and b._bin_warned is False


def test_the_counters_survive_a_restart():
    b = Bin()
    b._bin_add(2, 321.)
    b._bin_save()
    saved = b.saved[-1][1]
    c = Bin()
    c._bin_restore(saved)
    assert c._bin_total() == pytest.approx(321.)
    c._bin_restore({'heads': {'x': 'bad'}})            # garbage: start clean
    assert c._bin_total() == 0


def test_burst_toggle_is_written_through():
    b = Bin()
    b.cmd_ACE_BG_SET_BURST(Cmd(ENABLE=1))
    assert b.burst_prime is True
    b.cmd_ACE_BG_SET_BURST(Cmd(ENABLE=0))
    assert b.burst_prime is False
    assert b.persisted == [('burst_prime', 'true', 'ace_bg_swap'),
                           ('burst_prime', 'false', 'ace_bg_swap')]


# -- burst purge ----------------------------------------------------------

def test_burst_is_off_by_default():
    bg, ev, marks, total, _ = T._simulate()
    assert bg.burst_prime is False
    assert 'MOVE -2.0' not in [e for t, e in ev]


def test_burst_pushes_the_same_net_filament():
    plain = T._simulate()
    burst = T._simulate(burst=True)
    net = lambda r: sum(float(e.split()[1]) for e in
                        _prime_moves(r[1], r[2]))
    # the same travel in the prime phase (the closing anti-ooze retract
    # included) and the same amount booked into the bin
    assert net(burst) == pytest.approx(net(plain))
    assert burst[0]._bin_total() == pytest.approx(plain[0]._bin_total())


def test_burst_comes_in_push_and_pull_back_pairs():
    bg, ev, marks, total, _ = T._simulate(burst=True)
    moves = _prime_moves(ev, marks)
    pushes = [e for e in moves if e == 'MOVE +12.0']
    backs = [e for e in moves if e == 'MOVE -2.0']
    assert len(pushes) == len(backs) == 12             # 120 mm in 10 mm
    # each push is followed by its pull back
    i = moves.index('MOVE +12.0')
    assert moves[i + 1] == 'MOVE -2.0'


def test_burst_has_a_pause_between_bursts_and_costs_time():
    # at the faster prime speed; at the default 4 mm/s the chained prime is
    # nearly as slow as the bursts
    plain = T._simulate(prime_speed=6.)
    burst = T._simulate(burst=True, prime_speed=6.)
    prime = lambda r: r[3] - r[2]['LD_PRIME']
    # 12 bursts: ~1.1 s of motion + 0.8 s pause each, vs ~20 s chained
    assert 8 < prime(burst) - prime(plain) < 20
    starts = [t for t, e in burst[1] if e == 'MOVE +12.0']
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) > BG.BG_BURST_PAUSE


def test_a_burst_is_the_abort_granularity():
    """_check_docked and the pick notice run between bursts, so the blind
    extrusion after a pick is at most one burst."""
    assert BG.BG_BURST_MM + BG.BG_BURST_BACK <= 12.0


# -- web backend ----------------------------------------------------------

@pytest.fixture
def web(monkeypatch, tmp_path):
    import importlib
    import os
    import sys
    from pathlib import Path
    cfg = tmp_path / "ace.cfg"
    cfg.write_text("[ace]" + chr(10) + "ace_device_count: 1" + chr(10),
                   encoding="utf-8")
    monkeypatch.setenv("MULTIACE_CFG_PATH", str(cfg))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    monkeypatch.delenv("MULTIACE_MOCK_MODE", raising=False)
    backend = Path(__file__).resolve().parents[2] / "multiace" / "web" / "backend"
    monkeypatch.syspath_prepend(str(backend))
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def test_state_carries_the_bin_and_the_burst_flag(web):
    bin_ = {"mm": 1200.0, "g": 3.6, "swaps": 7, "since": 1.0,
            "heads": {"0": 1200.0}, "warn_mm": 4000.0, "warn": False}
    st = web._parse_state({"ace": {}, "ace_bg_swap": {
        "version": "v0.9", "enabled_heads": [0], "busy": [],
        "burst_prime": True, "bin": bin_}})
    assert st["bg_swap"]["burst_prime"] is True
    assert st["bg_swap"]["bin"] == bin_


def test_state_of_an_older_engine_or_no_module_is_unchanged(web):
    old = web._parse_state({"ace": {}, "ace_bg_swap": {
        "version": "v0.9", "enabled_heads": [0], "busy": []}})
    assert set(old["bg_swap"]) == {"available", "version", "enabled_heads",
                                   "busy"}
    none = web._parse_state({"ace": {}})
    assert none["bg_swap"]["available"] is False
    assert "bin" not in none["bg_swap"]
