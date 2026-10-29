"""_head_is_loaded on stubs: the presence pin decides, the motion helper is
only its fallback.

HW 2026-10-25 07:06 (head mode): after a hand removal the motion helper
still read True, the swap held the empty head for loaded, unloaded it with
no head_source and the guessed slot pulled the freshly inserted spool out
of the ACE.
"""
import importlib

from . import harness as H


class FakeSensor:
    def __init__(self, motion, pin=None):
        self.motion = motion
        if pin is not None:
            self.runout_buttun_state = pin

    def get_status(self, eventtime):
        return {'filament_detected': self.motion}


class FakePrinter:
    def __init__(self, sensor):
        self.sensor = sensor

    def lookup_object(self, name, default=None):
        return self.sensor if name == 'filament_motion_sensor e2_filament' else default


def _rig(motion, pin=None, source=None, uses_ace=True):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')

    class Ace(object):
        _head_is_loaded = A.MultiAce._head_is_loaded
        _head_filament_present = A.MultiAce._head_filament_present
        unload_gpio = True

        def head_uses_ace(self, head):
            return uses_ace
    ace = Ace()
    ace.printer = FakePrinter(FakeSensor(motion, pin))
    ace._head_source = {2: source}
    return ace


def test_pin_clear_beats_stale_motion_helper():
    assert _rig(motion=True, pin=False)._head_is_loaded(2) is False


def test_pin_present_counts_as_loaded():
    assert _rig(motion=False, pin=True)._head_is_loaded(2) is True


def test_without_pin_the_motion_helper_decides():
    assert _rig(motion=True)._head_is_loaded(2) is True
    assert _rig(motion=False)._head_is_loaded(2) is False


def test_head_source_still_counts_and_failed_load_on_empty_head_does_not():
    assert _rig(motion=True, pin=False, source={'ace_index': 2, 'slot': 0})._head_is_loaded(2) is True
    assert _rig(motion=True, pin=False,
                source={'ace_index': 2, 'slot': 0, 'load_failed': True})._head_is_loaded(2) is False


def test_gate_empty_mark_on_an_empty_head_does_not_count():
    # HW 2026-10-25 09:11: an unload found the gate empty (strand broke
    # above it), head_source stayed; the user pulled the rest by hand and
    # inserted a fresh spool; the next unload treated the head as loaded
    # and retracted the fresh spool. With the pin clear the mark ends it.
    src = {'ace_index': 0, 'slot': 2, 'gate_empty': True}
    assert _rig(motion=True, pin=False, source=src)._head_is_loaded(2) is False
    # filament still in the head (pin present): the entry still counts
    assert _rig(motion=True, pin=True, source=src)._head_is_loaded(2) is True
    # pin unreadable: the motion helper is the fallback verdict (same rule
    # as load_failed), a stale True keeps the entry
    assert _rig(motion=True, source=src)._head_is_loaded(2) is True
