"""FA gate after a load that ran inside a print: the real helper of
filament_feed_ace.py on stubs.

HW 2026-10-24: the start block of a raw Orca file loaded heads with
ACE_LOAD_HEAD while the print was already running. The load's finally closed
the FA gate to idle, so every later tool change skipped feed assist for the
rest of the print.
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
    def __init__(self, head_source, uses_ace=True):
        self._auto_feed_enabled = False
        self._fa_context = 'load'
        self._head_source = head_source
        self.uses_ace = uses_ace
        self.armed = []

    def _fa_trace(self, msg):
        pass

    def head_uses_ace(self, head):
        return self.uses_ace

    def _arm_fa_for(self, idx, slot):
        self.armed.append((idx, slot))


class FakeExtruder:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class FakeToolhead:
    def __init__(self, name):
        self.ext = FakeExtruder(name)

    def get_extruder(self):
        return self.ext


def _rig(state, active='extruder1', head_source=None, uses_ace=True):
    F = _feed_module()

    class Feed(object):
        _restore_print_fa_gate = F.FilamentFeed._restore_print_fa_gate
    feed = Feed()
    feed.ace = FakeAce(head_source if head_source is not None else
                       {1: {'ace_index': 1, 'slot': 2}}, uses_ace)
    feed.toolhead = FakeToolhead(active)
    feed.channel_state = {0: state}
    return F, feed


def test_finished_load_reopens_the_print_gate_and_arms_the_active_head():
    F, feed = _rig(None)
    feed.channel_state[0] = F.FEED_STA_LOAD_FINISH
    feed._restore_print_fa_gate(0)
    assert feed.ace._auto_feed_enabled is True
    assert feed.ace._fa_context == 'print'
    assert feed.ace.armed == [(1, 2)]


def test_failed_load_reopens_the_gate_without_arming():
    F, feed = _rig('load_fail')
    feed._restore_print_fa_gate(0)
    assert feed.ace._fa_context == 'print'
    assert feed.ace.armed == []


def test_active_head_without_ace_is_not_armed():
    F, feed = _rig(None, uses_ace=False)
    feed.channel_state[0] = F.FEED_STA_LOAD_FINISH
    feed._restore_print_fa_gate(0)
    assert feed.ace._fa_context == 'print'
    assert feed.ace.armed == []
