"""Feed-assist start refused on an emptied spool: note, no pause."""
import logging
import types

from multiace.klipper.extras import ace as ace_mod


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(m)


def _stub(gate):
    o = types.SimpleNamespace()
    o._gate_status_per_ace = {0: [1, gate, 1, 1]}
    o._fa_failed_notified = {}
    o._fa_log = _Log()
    o.warned = []
    o.log_warn = o.warned.append
    o._t = lambda key, **kw: '%s %s' % (key, sorted(kw.items()))
    o._disp = lambda n: n + 1
    o.run = types.MethodType(ace_mod.MultiAce._fa_arm_spool_ended, o)
    return o


def test_empty_inlet_is_a_note_once():
    o = _stub(ace_mod.GATE_EMPTY)
    assert o.run(0, 1, -1, 'no_filament') is True
    assert len(o.warned) == 1 and 'fa_lost_tail_out' in o.warned[0]
    assert o.run(0, 1, -1, 'no_filament') is True
    assert len(o.warned) == 1


def test_loaded_inlet_keeps_the_failure_path():
    o = _stub(ace_mod.GATE_AVAILABLE)
    assert o.run(0, 1, -1, 'no_filament') is False
    assert o.warned == []


def test_other_refusals_keep_the_failure_path():
    o = _stub(ace_mod.GATE_EMPTY)
    assert o.run(0, 1, 0, 'forbidden') is False
    assert o.run(0, 1, -1, 'in_error') is False
