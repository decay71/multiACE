"""Unload: how much of the short probe retract the MMU measured. Only a
measured move can prove that the strand came back while the head sensor
still reads filament (a remnant at the head inlet)."""
import importlib
import sys
import types

from . import harness as H


def _mod():
    H.load_ace()
    name = H.PKG + '.pulse_counter'
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return importlib.import_module(H.PKG + '.filament_feed_ace')


F = _mod()


def _feed(motion=None):
    s = types.SimpleNamespace()
    s._unit_motion = lambda slot=None: motion
    return lambda span, seq0=0, cmd=150: F.FilamentFeed._short_retract_moved(
        s, 0, span, seq0, cmd)


def test_ace_pro_measures_nothing():
    moved = _feed(None)
    assert moved((None, 0, None, None)) == 0.0
    assert moved(None) == 0.0


def test_ace2_decoder_span_counts_capped_at_the_command():
    moved = _feed(None)
    assert moved((131.0, 5, 0, 0)) == 131.0
    assert moved((1083.0, 873, 0, 0)) == 150.0     # span window anomaly


def test_unit_motion_report_counts_for_its_own_retract_only():
    rep = {'kind': 'retract', 'slot': 0, 'seq': 7, 'mm': 151.0,
           'running': False}
    assert _feed(rep)((None, 0, None, None), seq0=6) == 150.0
    assert _feed(rep)((None, 0, None, None), seq0=7) == 0.0   # an older move
    assert _feed(dict(rep, running=True))((None, 0, None, None), 6) == 0.0
    assert _feed(dict(rep, kind='feed'))((None, 0, None, None), 6) == 0.0
    assert _feed(dict(rep, slot=2))((None, 0, None, None), 6) == 0.0


def test_two_measured_probe_retracts_reach_the_limit_one_does_not():
    assert 150.0 < F.UNLOAD_REMNANT_MIN_MM <= 300.0
