"""A slot that went empty under a demonstrably empty head ends its mapping,
and a retract the unit ended with runout counts as back (real methods on
stubs).

HW 2026-11-09 (multi, ace2k): the bulk retract of MMU 1 / Slot 1 held after
176 mm, the strand was then pulled out of the MMU by hand, but head_source
kept pointing at the slot. The spool inserted next was parked 300 mm deep
by the insert read; an unload of the empty head pulled it back out of the
MMU (retract ended runout after 146 mm) and reported it as "did not pull
the filament all the way back", keeping the mapping a second time.
"""
import importlib
import sys
import types

from . import harness as H


def _modules():
    H.load_ace()
    name = H.PKG + '.pulse_counter'
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    A = importlib.import_module(H.PKG + '.ace')
    F = importlib.import_module(H.PKG + '.filament_feed_ace')
    return A, F


class Reactor(object):
    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now


# ---- feed module: runout end and slot-left check -------------------------

def _feed(motion=None, gates=None, flip_ts=None, source=None):
    A, F = _modules()

    class Ace(object):
        pass
    ace = Ace()
    ace._active_device_index = 0
    ace._head_source = {0: source}
    ace._gate_status_per_ace = {0: list(gates or [1, 1, 1, 1])}
    ace._gate_flip_ts = dict(flip_ts or {})

    class Feed(object):
        _unit_retract_short = F.FilamentFeed._unit_retract_short
        _slot_left_unit = F.FilamentFeed._slot_left_unit

        def _unit_motion(self, slot=None):
            return motion
    f = Feed()
    f.ace = ace
    f.reactor = Reactor()
    return f


def _retract(stop, mm=146.0, target=1618.0, seq=9):
    return {'seq': seq, 'slot': 0, 'kind': 'retract', 'running': False,
            'mm': mm, 'target': target, 'short': target - mm, 'stop': stop}


def test_runout_end_counts_as_back():
    f = _feed(motion=_retract('runout'))
    assert f._unit_retract_short(0, 0, 8) is False


def test_held_retract_still_counts_as_short():
    f = _feed(motion=_retract('stuck', mm=176.0))
    assert f._unit_retract_short(0, 0, 8) is True


def test_slot_emptied_during_the_unload_verifies_it():
    f = _feed(gates=[0, 1, 1, 1], flip_ts={(0, 0): 105.0},
              source={'ace_index': 0, 'slot': 0})
    assert f._slot_left_unit(0, 0, 100.0) is True


def test_slot_empty_from_before_does_not_count():
    f = _feed(gates=[0, 1, 1, 1], flip_ts={(0, 0): 90.0},
              source={'ace_index': 0, 'slot': 0})
    assert f._slot_left_unit(0, 0, 100.0) is False


def test_slot_still_filled_does_not_count():
    f = _feed(gates=[1, 1, 1, 1], flip_ts={(0, 0): 105.0},
              source={'ace_index': 0, 'slot': 0})
    assert f._slot_left_unit(0, 0, 100.0) is False


def test_slot_left_reads_the_source_unit():
    # head 0 sourced from MMU 1: the gate of MMU 1 decides, not the active
    f = _feed(gates=[1, 1, 1, 1], source={'ace_index': 1, 'slot': 0})
    f.ace._gate_status_per_ace[1] = [0, 1, 1, 1]
    f.ace._gate_flip_ts[(1, 0)] = 105.0
    assert f._slot_left_unit(0, 0, 100.0) is True


# ---- ace.py: clear the mapping on the slot-empty transition --------------

class Sensor(object):
    def __init__(self, pin):
        self.runout_buttun_state = pin

    def get_status(self, eventtime):
        return {'filament_detected': True}      # stale motion helper


class FeedObj(object):
    def __init__(self, state):
        self.channel_state = [state, state]


class Bg(object):
    def __init__(self, busy=()):
        self.busy = set(busy)

    def is_busy(self, head):
        return head in self.busy


class Printer(object):
    def __init__(self, pins, feed_state='wait_insert', bg=None):
        self.pins = pins
        self.feed = FeedObj(feed_state)
        self.bg = bg

    def lookup_object(self, name, default=None):
        if name.startswith('filament_motion_sensor e'):
            head = int(name.split(' e')[1].split('_')[0])
            if head in self.pins:
                return Sensor(self.pins[head])
            return default
        if name.startswith('filament_feed '):
            return self.feed
        if name == 'ace_bg_swap':
            return self.bg if self.bg is not None else default
        return default


def _ace(sources, pins, feed_state='wait_insert', bg=None, swap=False,
         loading=(), uses_ace=True):
    A, F = _modules()

    class Ace(object):
        _clear_sources_on_slot_empty = A.MultiAce._clear_sources_on_slot_empty
        _feed_op_running = A.MultiAce._feed_op_running
        _FEED_OP_STATES = A.MultiAce._FEED_OP_STATES
        _head_filament_present = A.MultiAce._head_filament_present
        EXTRUDER_MAP = A.MultiAce.EXTRUDER_MAP
        unload_gpio = True

        def head_uses_ace(self, head):
            return uses_ace

        def _save_head_source(self):
            self.saved += 1

        def _t(self, key, **kw):
            return '%s %s' % (key, sorted(kw.items()))

        def _disp(self, i):
            return int(i) + 1

        def log_always(self, msg):
            self.console.append(msg)
    ace = Ace()
    ace.printer = Printer(pins, feed_state, bg)
    ace._head_source = dict(sources)
    ace._swap_in_progress = swap
    ace._connected_per_ace = {0: True, 1: True}
    ace._reconnecting_per_ace = {}
    ace._loading_heads = set(loading)
    ace.saved = 0
    ace.console = []
    return ace


SRC = {'ace_index': 0, 'slot': 0, 'type': 'PETG'}


def test_empty_head_loses_the_mapping():
    ace = _ace({0: dict(SRC), 1: {'ace_index': 1, 'slot': 1}},
               pins={0: False, 1: False})
    ace._clear_sources_on_slot_empty(0, 0)
    assert ace._head_source[0] is None
    assert ace._head_source[1] == {'ace_index': 1, 'slot': 1}
    assert ace.saved == 1
    assert len(ace.console) == 1
    assert 'head_source_cleared_slot_empty' in ace.console[0]


def test_runout_with_filament_in_the_head_keeps_it():
    ace = _ace({0: dict(SRC)}, pins={0: True})
    ace._clear_sources_on_slot_empty(0, 0)
    assert ace._head_source[0] == SRC
    assert ace.saved == 0


def test_running_operations_keep_it():
    for kw in ({'feed_state': 'unload_doing'}, {'feed_state': 'load_feeding'},
               {'swap': True}, {'loading': (0,)}, {'bg': Bg(busy=(0,))}):
        ace = _ace({0: dict(SRC)}, pins={0: False}, **kw)
        ace._clear_sources_on_slot_empty(0, 0)
        assert ace._head_source[0] == SRC, kw


def test_other_slot_or_unit_keeps_it():
    ace = _ace({0: dict(SRC)}, pins={0: False})
    ace._clear_sources_on_slot_empty(0, 1)
    ace._clear_sources_on_slot_empty(1, 0)
    assert ace._head_source[0] == SRC


def test_disconnected_unit_keeps_it():
    ace = _ace({0: dict(SRC)}, pins={0: False})
    ace._connected_per_ace[0] = False
    ace._clear_sources_on_slot_empty(0, 0)
    assert ace._head_source[0] == SRC
    ace = _ace({0: dict(SRC)}, pins={0: False})
    ace._reconnecting_per_ace[0] = True
    ace._clear_sources_on_slot_empty(0, 0)
    assert ace._head_source[0] == SRC


def test_unreadable_sensor_keeps_it():
    ace = _ace({0: dict(SRC)}, pins={})
    ace._clear_sources_on_slot_empty(0, 0)
    assert ace._head_source[0] == SRC


def test_feeder_head_is_not_touched():
    ace = _ace({0: dict(SRC)}, pins={0: False}, uses_ace=False)
    ace._clear_sources_on_slot_empty(0, 0)
    assert ace._head_source[0] == SRC
