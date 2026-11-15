"""Arrival flow check: skipped when the dock prime read flow and ran to its
full length, kept in every other case and with arrival_check on."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))

from tests.units import harness  # noqa: E402,F401
from extras import ace_swap  # noqa: E402


def _stub(arrival_check=False, dock=None, deficit=None, cleaning=False,
          bg=True):
    wipes = []
    bgobj = types.SimpleNamespace(arrival_check=arrival_check) if bg else None
    s = types.SimpleNamespace(
        printer=types.SimpleNamespace(
            lookup_object=lambda n, d=None: bgobj if n == 'ace_bg_swap' else d),
        _bg_dock_coil={} if dock is None else {0: dict(dock)},
        _bg_prime_deficit={} if deficit is None else {0: deficit},
        _bg_load_unverified={0},
        _pickup_cleaning=cleaning,
        _discard_wipe=lambda head, tag: wipes.append((head, tag)))
    s.notes = []
    s.res_verdict = 'ok'

    def _note(site, head, ace, slot, delta, push=0, noisy=False):
        s.notes.append((site, head, ace, slot, delta, push))
        return (s.res_verdict, 3500., 5.4)
    s._resistance_note = _note
    s._dock_resistance_pause = set()
    s._dock_resistance = types.MethodType(
        ace_swap.SwapMixin._dock_resistance, s)
    return s, wipes


FLOW = {'verdict': 'FLOW', 'dip': 5022., 'primed': 232., 'target': 232.}


def _skip(s):
    return ace_swap.SwapMixin._arrival_check_skipped(s, 0)


def test_clean_dock_result_skips_and_clears():
    s, wipes = _stub(dock=FLOW)
    assert _skip(s) is True
    assert 0 not in s._bg_load_unverified
    assert 0 not in s._bg_dock_coil
    assert wipes == []


def test_pickup_cleaning_still_wipes():
    s, wipes = _stub(dock=FLOW, cleaning=True)
    assert _skip(s) is True
    assert wipes == [(0, 'bg-arrival')]


def test_option_on_keeps_the_check():
    s, _ = _stub(arrival_check=True, dock=FLOW)
    assert _skip(s) is False
    assert 0 in s._bg_load_unverified and 0 in s._bg_dock_coil


def test_no_flow_no_reading_cut_short_keep_the_check():
    for dock, deficit in (
            (dict(FLOW, verdict='NO_FLOW'), None),
            (None, None),
            (dict(FLOW, primed=15.), None),
            (FLOW, 30.),
            (FLOW, 0.0)):
        s, _ = _stub(dock=dock, deficit=deficit)
        assert _skip(s) is False
        assert 0 in s._bg_load_unverified


def test_no_bg_engine_keeps_the_check():
    s, _ = _stub(dock=FLOW, bg=False)
    assert _skip(s) is False


HIGH = dict(FLOW, dip=18947., primed=20., target=20., ace=0, slot=0)


def test_dock_reading_goes_through_the_resistance_watch():
    s, _ = _stub(dock=dict(HIGH, dip=3300.))
    assert _skip(s) is True
    assert s.notes == [('dock', 0, 0, 0, 3300., 20)]


def test_dock_dip_far_above_baseline_keeps_the_check():
    s, _ = _stub(dock=HIGH)
    s.res_verdict = 'suspect'
    assert _skip(s) is False
    assert 0 in s._bg_load_unverified
    assert s._dock_resistance_pause == set()
    assert _skip(s) is False
    assert len(s.notes) == 1


def test_second_strike_at_the_dock_pauses_at_the_arrival():
    s, _ = _stub(dock=HIGH)
    s.res_verdict = 'pause_due'
    assert _skip(s) is False
    assert s._dock_resistance_pause == {0}
    s._bg_pick_flow_check_inner = lambda head, ao: 'ok'
    assert ace_swap.SwapMixin._bg_pick_flow_check(s, 0, 1.0) == 'resistance'
    assert s._dock_resistance_pause == set()
