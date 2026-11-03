"""Load feed deadline of a unit that reports its own motion (the CFS Nano):
the real helpers of filament_feed_ace.py on stubs.

HW 2026-11-01: the Nano first waited ~30 s for its own pull-in of a spool
inserted meanwhile, so the host's feed deadline (load_length / feed_speed +
30 s) ran out while the box still fed. The retry then retracted 50 mm just
after the filament reached the sensor.
"""
import importlib
import sys
import types

from . import harness as H


def _feed_module():
    H.load_ace()
    name = H.PKG + '.pulse_counter'
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return importlib.import_module(H.PKG + '.filament_feed_ace')


def _feed(motion=None):
    F = _feed_module()

    class Feed(object):
        _unit_motion = F.FilamentFeed._unit_motion
        _unit_motion_seq = F.FilamentFeed._unit_motion_seq
        _unit_feed_running = F.FilamentFeed._unit_feed_running

    class Ace(object):
        _active_device_index = 1
        _info_per_ace = {1: {} if motion is None else {'motion': motion}}
    f = Feed()
    f.ace = Ace()
    return f


def _m(seq, kind='feed', slot=3, running=True):
    return {'seq': seq, 'kind': kind, 'slot': slot, 'mm': 10.0,
            'running': running}


def test_running_feed_after_the_start_counts():
    f = _feed(_m(7))
    assert f._unit_feed_running(3, 6)


def test_older_or_finished_or_other_motion_does_not_count():
    assert not _feed(_m(6))._unit_feed_running(3, 6)
    assert not _feed(_m(7, running=False))._unit_feed_running(3, 6)
    assert not _feed(_m(7, kind='retract'))._unit_feed_running(3, 6)
    assert not _feed(_m(7, slot=2))._unit_feed_running(3, 6)


def test_unit_without_motion_report_never_extends():
    f = _feed(None)
    assert f._unit_motion_seq() == 0
    assert not f._unit_feed_running(3, 0)


def test_seq_before_the_feed():
    assert _feed(_m(5, running=False))._unit_motion_seq() == 5


def _feed_short(motion):
    f = _feed(motion)
    F = _feed_module()
    f._unit_retract_short = F.FilamentFeed._unit_retract_short.__get__(f)
    return f


def test_short_retract_fails_the_unload():
    f = _feed_short({'seq': 8, 'kind': 'retract', 'slot': 1, 'mm': 700.0,
                     'running': False, 'target': 872.0, 'short': 172.0})
    assert f._unit_retract_short(3, 1, 7)


def test_full_or_old_or_other_retract_passes():
    full = {'seq': 8, 'kind': 'retract', 'slot': 1, 'mm': 872.0,
            'running': False, 'target': 872.0, 'short': 0.0}
    assert not _feed_short(full)._unit_retract_short(3, 1, 7)
    assert not _feed_short(dict(full, short=50.0, seq=7))._unit_retract_short(3, 1, 7)
    assert not _feed_short(dict(full, short=50.0, slot=2))._unit_retract_short(3, 1, 7)
    assert not _feed_short(None)._unit_retract_short(3, 1, 7)
