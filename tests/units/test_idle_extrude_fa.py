"""Feed assist outside a print: an ACE 2 or ace2k does not freewheel, so an
extruder that pulls on an ACE head outside a print (a PA calibration from the
console or the display, a manual extrude) dragged the strand through the
stopped unit (field report 2026-11-22, adaptive PA macros on an ace2k head).
The poll arms the assist while that head extrudes and drops it after the
extruder has been idle for the grace period."""
import importlib

from . import harness as H


def _rig(no_freewheel=True, state='standby', source=None):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    M = A.MultiAce

    class Ext(object):
        extruder_index = 3

    class Ace(object):
        _idle_fa_step = M._idle_fa_step
        _idle_fa_release = M._idle_fa_release

        def __init__(self):
            self.vel = 0.0
            self.state = state
            self.ext = Ext()
            self.toolhead = self
            self.printer = self
            self.bg_busy = False
            self.feed_op = False
            self.armed = []
            self.disarmed = []
            self._idle_fa = None
            self._auto_feed_enabled = False
            self._fa_context = 'idle'
            self._swap_in_progress = False
            self._loading_heads = set()
            self._head_source = {3: source if source is not None
                                 else {'ace_index': 0, 'slot': 2}}
            self._connected_per_ace = {0: True}
            self._feed_assist_per_ace = {0: -1}
            self._nf = no_freewheel

        def get_extruder(self):
            return self.ext

        def lookup_object(self, name, default=None):
            if name in ('motion_report', 'ace_bg_swap', 'print_stats'):
                return self
            return default

        def get_status(self, eventtime):
            return {'live_extruder_velocity': self.vel}

        def is_busy(self, head):
            return self.bg_busy

        def _feed_op_running(self, head):
            return self.feed_op

        def head_uses_ace(self, head):
            return True

        def _cap(self, idx, name):
            return self._nf if name == 'no_freewheel' else None

        def _arm_fa_for(self, idx, slot):
            self.armed.append((idx, slot))
            self._feed_assist_per_ace[idx] = slot

        def _disarm_fa_for(self, idx):
            self.disarmed.append(idx)
            self._feed_assist_per_ace[idx] = -1

    return Ace()


def test_arms_while_extruding_and_drops_after_idle():
    a = _rig()
    a._idle_fa_step(1.0)
    assert a.armed == []
    a.vel = 3.0
    a._idle_fa_step(2.0)
    assert a.armed == [(0, 2)]
    assert a._auto_feed_enabled and a._fa_context == 'extrude'
    a._idle_fa_step(2.5)
    assert a.armed == [(0, 2)]
    a.vel = 0.0
    a._idle_fa_step(6.0)
    assert a.disarmed == []
    a._idle_fa_step(8.0)
    assert a.disarmed == [0]
    assert not a._auto_feed_enabled and a._fa_context == 'idle'
    assert a._idle_fa is None


def test_freewheeling_unit_untouched():
    a = _rig(no_freewheel=False)
    a.vel = 3.0
    a._idle_fa_step(1.0)
    assert a.armed == [] and not a._auto_feed_enabled


def test_print_owns_the_gate():
    a = _rig(state='printing')
    a.vel = 3.0
    a._idle_fa_step(1.0)
    assert a.armed == []


def test_load_and_bg_and_swap_block():
    for attr in ('feed_op', 'bg_busy', '_swap_in_progress'):
        a = _rig()
        a.vel = 3.0
        setattr(a, attr, True)
        a._idle_fa_step(1.0)
        assert a.armed == [], attr


def test_failed_load_and_no_source_skip():
    a = _rig(source={'ace_index': 0, 'slot': 2, 'load_failed': True})
    a.vel = 3.0
    a._idle_fa_step(1.0)
    assert a.armed == []
    a = _rig(source={})
    a.vel = 3.0
    a._idle_fa_step(1.0)
    assert a.armed == []


def test_other_path_takes_the_gate():
    a = _rig()
    a.vel = 3.0
    a._idle_fa_step(1.0)
    a._fa_context = 'load'
    a._idle_fa_step(1.5)
    assert a._idle_fa is None and a._auto_feed_enabled
    assert a.disarmed == []


def test_handed_back_context_is_released():
    a = _rig()
    a._auto_feed_enabled = True
    a._fa_context = 'extrude'
    a._feed_assist_per_ace[0] = 2
    a._idle_fa_step(1.0)
    assert a.disarmed == [0] and not a._auto_feed_enabled


def test_head_change_releases():
    a = _rig()
    a.vel = 3.0
    a._idle_fa_step(1.0)
    a.ext.extruder_index = 1
    a._idle_fa_step(1.5)
    assert a.disarmed == [0] and a._idle_fa is None
