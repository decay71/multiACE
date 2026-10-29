"""_bg_staged_sweep on stubs: a STAGED record goes when its toolhead sensor
reads free outside a print, and stays while printing/paused, while the
bg engine works the head, while the post-restart feed stop is pending and
while the sensor reads filament or cannot be read.

HW 2026-10-26/27: MMU 2 / Slot 3 was staged at the sensor of head 2, the
strand was pulled out by hand ten minutes later (sensor remove event),
and the record came back at every start for a day: it refused the driver
switch and would have refused a swap to another slot of that head.
"""
import importlib

from . import harness as H


class FakePrintStats:
    def __init__(self, state):
        self.state = state

    def get_status(self, eventtime):
        return {'state': self.state}


class FakeBg:
    def __init__(self, busy):
        self.busy = set(busy)

    def is_busy(self, head):
        return head in self.busy


class FakePrinter:
    def __init__(self, state, busy):
        self.objs = {'print_stats': FakePrintStats(state),
                     'ace_bg_swap': FakeBg(busy)}

    def lookup_object(self, name, default=None):
        return self.objs.get(name, default)


def _rig(state='standby', sensors=None, busy=(), pending=(),
         staged=None):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')

    class Ace(object):
        _bg_staged_sweep = A.MultiAce._bg_staged_sweep

        def _head_sensor_present(self, head):
            return (sensors or {}).get(head)

        def _disp(self, idx):
            return idx + 1

        def _save_bg_staged(self):
            self.saved += 1

        def log_always(self, msg):
            self.said.append(msg)

    ace = Ace()
    ace.printer = FakePrinter(state, busy)
    ace._bg_staged = dict(staged if staged is not None else {1: (1, 2)})
    ace._bg_left_empty = set(ace._bg_staged)
    ace._bg_staged_stop_pending = set(pending)
    ace.saved = 0
    ace.said = []
    return ace


def test_free_sensor_outside_a_print_drops_the_record():
    ace = _rig(sensors={1: False})
    ace._bg_staged_sweep(1.0)
    assert ace._bg_staged == {} and ace._bg_left_empty == set()
    assert ace.saved == 1
    assert ace.said == ['[multiACE] head 2: STAGED record MMU 2 / Slot 3 '
                        'dropped - toolhead sensor free outside a print']


def test_record_kept_while_printing_or_paused():
    for state in ('printing', 'paused'):
        ace = _rig(state=state, sensors={1: False})
        ace._bg_staged_sweep(1.0)
        assert 1 in ace._bg_staged and ace.saved == 0


def test_record_kept_while_filament_sits_at_the_sensor():
    ace = _rig(sensors={1: True})
    ace._bg_staged_sweep(1.0)
    assert 1 in ace._bg_staged


def test_record_kept_without_a_readable_sensor():
    ace = _rig(sensors={})
    ace._bg_staged_sweep(1.0)
    assert 1 in ace._bg_staged


def test_record_kept_while_the_bg_engine_works_the_head():
    ace = _rig(sensors={1: False}, busy={1})
    ace._bg_staged_sweep(1.0)
    assert 1 in ace._bg_staged


def test_record_kept_while_the_restart_feed_stop_is_pending():
    ace = _rig(sensors={1: False}, pending={(1, 2)})
    ace._bg_staged_sweep(1.0)
    assert 1 in ace._bg_staged


def test_only_the_free_head_goes():
    ace = _rig(sensors={1: False, 3: True}, staged={1: (1, 2), 3: (3, 0)})
    ace._bg_staged_sweep(1.0)
    assert ace._bg_staged == {3: (3, 0)} and ace._bg_left_empty == {3}
