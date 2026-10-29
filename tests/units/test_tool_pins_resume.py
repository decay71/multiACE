"""Filament pins of a raw Orca print across a RESUME: the real
_resolve_tool_pins of ace.py on stubs.

HW 2026-10-24: a pause emptied head 1, the RESUME fired print_stats:start
and the pins were resolved again. Filament 1 was pinned to the slot head 1
now showed (black) instead of the one chosen at the start (orange), and the
print stopped switching between the two colours.
"""
from . import harness as H


class FakePrintStats:
    def __init__(self, duration):
        self.duration = duration

    def get_status(self, eventtime):
        return {'print_duration': self.duration}


class FakePrinter:
    def __init__(self, objects):
        self.objects = objects

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class FakeReactor:
    def monotonic(self):
        return 0.


def _rig(duration, pins):
    A = H.load_ace()

    class Ace(object):
        _resolve_tool_pins = A.MultiAce._resolve_tool_pins

        def _sniff_virtual_tool_file(self):
            return (['#000000'], ['pla'])

        def head_uses_ace(self, head):
            return True

        def _slot_declared_identity(self, a, sl):
            return None

        def _disp(self, i):
            return i + 1
    ace = Ace()
    ace._ace_mode = 'head'
    ace._print_tool_pins = pins
    ace._head_source = {0: {'ace_index': 0, 'slot': 0}}
    ace._gate_status_per_ace = {}
    ace.reactor = FakeReactor()
    ace.printer = FakePrinter({'print_stats': FakePrintStats(duration)})
    return ace


def test_resume_keeps_the_pins_of_the_print_start():
    ace = _rig(600., {0: (0, 0, 1)})
    assert ace._resolve_tool_pins() == {0: (0, 0, 1)}


def test_fresh_start_resolves_from_the_loaded_slot():
    ace = _rig(0., {0: (0, 0, 1)})
    assert ace._resolve_tool_pins() == {0: (0, 0, 0)}


def test_resume_without_earlier_pins_resolves():
    ace = _rig(600., {})
    assert ace._resolve_tool_pins() == {0: (0, 0, 0)}
