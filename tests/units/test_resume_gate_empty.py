"""RESUME after a strand broke above the gate: the real helper of
filament_feed_ace.py on stubs.

Stock INNER_RESUME reads filament_exist of the paused head; with an empty
toolhead that is the feed module's filament_detected = the gate of the old,
empty slot. The helper lets the RESUME through so the replayed swap loads.
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


class FakeAce:
    def __init__(self, src, loads=True, uses_ace=True):
        self._head_source = {1: src}
        self._print_has_gcode_loads = loads
        self.uses_ace = uses_ace

    def head_uses_ace(self, head):
        return self.uses_ace


class FakePS:
    def __init__(self, state):
        self.state = state

    def get_status(self, _t):
        return {'state': self.state}


class FakePrinter:
    def __init__(self, state):
        self.ps = FakePS(state)

    def lookup_object(self, name, default=None):
        return self.ps if name == 'print_stats' else default


class FakeReactor:
    def monotonic(self):
        return 0.0


def _feed(ace, state='paused'):
    m = _feed_module()
    f = object.__new__(m.FilamentFeed)
    f.ace = ace
    f.printer = FakePrinter(state)
    f.reactor = FakeReactor()
    return f


MARKED = {'ace_index': 0, 'slot': 1, 'gate_empty': True}


def test_paused_marked_head_passes():
    assert _feed(FakeAce(MARKED))._resume_past_gate_empty(1)


def test_needs_mark():
    src = {'ace_index': 0, 'slot': 1}
    assert not _feed(FakeAce(src))._resume_past_gate_empty(1)


def test_needs_pause():
    assert not _feed(FakeAce(MARKED), 'printing')._resume_past_gate_empty(1)


def test_needs_gcode_loads():
    assert not _feed(FakeAce(MARKED, loads=False))._resume_past_gate_empty(1)


def test_not_for_feeder_head():
    assert not _feed(FakeAce(MARKED, uses_ace=False))._resume_past_gate_empty(1)


def test_no_head_source():
    assert not _feed(FakeAce(None))._resume_past_gate_empty(1)
