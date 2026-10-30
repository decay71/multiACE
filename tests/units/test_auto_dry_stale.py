"""A booked auto-dry cycle the device no longer runs (ACE power cycle,
firmware flash, device backstop) kept its ownership note forever: the start
branch needs `not ours`, the stop branch only fires below rh_end, so the
unit never dried again (field report 2026-10-30, 21 %rH for 39 h). The real
_auto_dry_tick releases the booking after AUTO_DRY_STALE_S of 'not drying'
while the reading still asks for drying, and starts a fresh cycle in the
same tick."""
import importlib
import types

from . import harness as H


def _rig(rh, drying):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    started = []
    ace = types.SimpleNamespace(
        _auto_dry_started={0}, _external_rh_cycle=set(),
        _auto_dry_seen={}, _auto_dry_stale_since={}, _auto_dry_ramp={},
        _auto_dry_follow_until={}, _ace_devices=['p0'],
        _connected_per_ace={0: True}, auto_dry_while_printing=False,
        save_variables=None)
    ace._auto_dry_ramp_tick = lambda et: None
    ace._is_actively_printing = lambda: False
    ace._auto_dry_for = lambda idx: {'enabled': True, 'rh_start': 20.0,
                                     'rh_end': 15.0, 'temp': 50,
                                     'master': -1}
    ace._cap = lambda idx, name: True
    ace._external_rh_get = lambda idx, now=None: None
    ace._ace_humidity = lambda idx: rh
    ace._ace_is_drying = lambda idx: drying
    ace._raw = lambda idx: idx
    ace._auto_dry_persist = lambda: None
    ace._auto_dry_persist_ext = lambda: None
    ace._auto_dry_persist_follow = lambda: None
    ace._auto_dry_followers = lambda idx: []
    ace._auto_dry_start = lambda idx, temp, why, external=False: \
        started.append(idx)
    ace._auto_dry_stop = lambda idx, why: None
    ace._auto_dry_release = types.MethodType(
        A.MultiAce._auto_dry_release, ace)
    tick = types.MethodType(A.MultiAce._auto_dry_tick, ace)
    return A, ace, tick, started


def test_stale_booking_is_released_and_restarted():
    A, ace, tick, started = _rig(rh=21.0, drying=False)
    tick(1000.)
    assert 0 in ace._auto_dry_started and started == []
    tick(1000. + A.AUTO_DRY_STALE_S)
    assert started == [0]


def test_running_cycle_keeps_its_booking():
    A, ace, tick, started = _rig(rh=21.0, drying=True)
    tick(1000.)
    tick(1000. + A.AUTO_DRY_STALE_S * 2)
    assert 0 in ace._auto_dry_started and started == []


def test_below_start_is_left_alone():
    A, ace, tick, started = _rig(rh=17.0, drying=False)
    tick(1000.)
    tick(1000. + A.AUTO_DRY_STALE_S * 2)
    assert 0 in ace._auto_dry_started and started == []
