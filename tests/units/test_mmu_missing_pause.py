"""A missing MMU during a print pauses instead of cancelling: the real
helper of ace_swap.py on stubs (no printer).

HW 2026-11-02: after a power loss the hub was not on the bus, the resumed
print reached its first swap, "MMU 4 not available" raised with the
default cancel action, and the print with its power-loss data was gone.
"""
import importlib

import pytest

from . import harness as H


class Paused(Exception):
    pass


class Refused(Exception):
    def __init__(self, keep_print):
        Exception.__init__(self)
        self.keep_print = keep_print


def _rig(state):
    H.load_ace()
    S = importlib.import_module(H.PKG + '.ace_swap')

    class Ace(object):
        _mmu_missing_stop = S.SwapMixin._mmu_missing_stop

        def __init__(self):
            self.calls = []

        def _is_actively_printing(self):
            return state == 'printing'

        def _is_print_active_or_paused(self):
            return state in ('printing', 'paused')

        def _disp(self, i):
            return i + 1

        def _t(self, key, **kw):
            return '%s %s' % (key, kw)

        def _pause_for_recovery(self, gcmd, detail_msg, recovery_steps,
                                code=210):
            self.calls.append(('pause', detail_msg, recovery_steps))
            raise Paused()

        def _ace_error(self, gcmd, text, code, head=None, keep_print=False):
            self.calls.append(('error', text, code, keep_print))
            return Refused(keep_print)
    return Ace()


def test_printing_pauses_after_hand_back():
    ace = _rig('printing')
    order = []
    with pytest.raises(Paused):
        ace._mmu_missing_stop(None, 3, 3, 'MMU 4 not available',
                              before_pause=lambda: order.append('back'))
    assert order == ['back']
    kind, msg, steps = ace.calls[0]
    assert kind == 'pause'
    assert 'pause_mmu_missing' in msg and "'ace': 4" in msg
    assert any('RESUME' in s for s in steps)


def test_paused_print_is_kept():
    ace = _rig('paused')
    with pytest.raises(Refused) as e:
        ace._mmu_missing_stop(None, 0, 0, 'MMU 1 not available')
    assert e.value.keep_print is True
    assert ace.calls == [('error', 'MMU 1 not available', 208, True)]


def test_idle_is_a_plain_refusal():
    ace = _rig('standby')
    called = []
    with pytest.raises(Refused) as e:
        ace._mmu_missing_stop(None, 0, 0, 'MMU 1 not available',
                              before_pause=lambda: called.append(1))
    assert e.value.keep_print is False
    assert called == []
