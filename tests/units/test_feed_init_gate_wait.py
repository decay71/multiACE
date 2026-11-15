"""Start state of a feed channel and the raw-print skip for a loaded head:
the real helpers of filament_feed_ace.py on stubs.

HW 2026-11-08: the init timer set the start state of an ACE head before its
MMU had connected. The gate read GATE_UNKNOWN, the loaded head landed on
wait_insert, and the start gcode's load of that head (SM_PRINT_AUTO_FEED ->
FEED_AUTO PRINTING=1) then hit the raw-print refusal (code 212).
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


class _Reactor(object):
    NEVER = 9.0e99

    def __init__(self):
        self.unregistered = False

    def unregister_timer(self, t):
        self.unregistered = True


class _Detect(object):
    def is_startup_stay(self):
        return True


class _Ace(object):
    _ace_mode = 'head'

    def __init__(self, gate, src=None):
        self._gate = [gate]
        self._head_source = {0: src, 1: None}
        self._gate_status_per_ace = {0: self._gate}
        self.gate_status = [-1, -1, -1, -1]

    def head_uses_ace(self, head):
        return head == 0

    def _ace_slot_for_head(self, head):
        return 0

    def head_ace_for(self, head):
        return 0


def _feed(gate, load_finish=True, src=None):
    F = _feed_module()

    class Feed(object):
        _check_init_state_timer_handler = \
            F.FilamentFeed._check_init_state_timer_handler

        def _set_channel_state(self, ch, state, save=False):
            self.channel_state[ch] = state

    f = Feed()
    f.ace = _Ace(gate, src)
    f.reactor = _Reactor()
    f._check_init_state_timer = object()
    f.filament_detect = _Detect()
    f.filament_ch = [0, 1]
    f.channel_state = [F.FEED_STA_INITED, F.FEED_STA_INITED]
    f.module_exist = [False, False]
    # channel 0 = the ACE head, channel 1 = a feeder head on its port
    f.config = {'auto_mode': [True, True], 'load_finish': [load_finish, False]}
    ports = []
    for ch in range(2):
        p = F.FeedPort.__new__(F.FeedPort)
        p.ace = f.ace
        p.index = ch
        p._filament_detected = (ch == 1)
        p.get_adc_value = lambda: 0.0
        ports.append(p)
    f._port = ports
    f._init_state_deadline = None
    f._init_state_done = [False, False]
    f._init_state_waited = [False, False]
    return F, f


def test_unknown_gate_waits_then_restores_load_finish():
    F, f = _feed(gate=-1)
    nxt = f._check_init_state_timer_handler(10.0)
    assert nxt == 10.0 + F.FEED_INIT_GATE_RETRY
    assert f.channel_state[0] == F.FEED_STA_INITED
    assert f.channel_state[1] == F.FEED_STA_PRELOAD_FINISH  # port head: now
    assert not f.reactor.unregistered
    f.ace._gate[0] = 1  # the MMU reported
    assert f._check_init_state_timer_handler(11.0) == f.reactor.NEVER
    assert f.channel_state[0] == F.FEED_STA_LOAD_FINISH
    assert f.reactor.unregistered


def test_known_gate_decides_at_once():
    F, f = _feed(gate=0)
    assert f._check_init_state_timer_handler(10.0) == f.reactor.NEVER
    assert f.channel_state[0] == F.FEED_STA_WAIT_INSERT


def test_gate_still_unknown_after_the_wait_decides_from_the_reading():
    F, f = _feed(gate=-1)
    f._check_init_state_timer_handler(10.0)
    t = 10.0 + F.FEED_INIT_GATE_WAIT
    assert f._check_init_state_timer_handler(t) == f.reactor.NEVER
    assert f.channel_state[0] == F.FEED_STA_WAIT_INSERT


def test_operation_during_the_wait_keeps_its_state():
    F, f = _feed(gate=-1)
    f._check_init_state_timer_handler(10.0)
    f.channel_state[0] = 'load_feeding'
    f.ace._gate[0] = 1
    assert f._check_init_state_timer_handler(11.0) == f.reactor.NEVER
    assert f.channel_state[0] == 'load_feeding'


def test_gate_value_matches_the_old_reading():
    F, f = _feed(gate=1)
    p = f._port[0]
    assert p.get_filament_detected() is True and p.gate_known()
    f.ace._gate[0] = 0
    assert p.get_filament_detected() is False and p.gate_known()
    f.ace._gate[0] = -1
    assert p.get_filament_detected() is False and not p.gate_known()
    q = f._port[1]  # feeder head: its port, never waits
    assert q.get_filament_detected() is True and q.gate_known()


class _Sensor(object):
    def __init__(self, pin):
        self.runout_buttun_state = pin

    def get_status(self, eventtime):
        return {'filament_detected': self.runout_buttun_state}


class _Printer(object):
    def __init__(self, sensor):
        self.sensor = sensor

    def lookup_object(self, name, default=None):
        if name == 'filament_motion_sensor e0_filament':
            return self.sensor
        return default


def _holder(src, pin):
    F = _feed_module()
    H.load_ace()
    ace_mod = importlib.import_module(H.PKG + '.ace')

    class Feed(object):
        _ace_head_holds_filament = F.FilamentFeed._ace_head_holds_filament

    class Ace(object):
        unload_gpio = True
        _head_filament_present = ace_mod.MultiAce._head_filament_present

    f = Feed()
    f.ace = Ace()
    f.ace._head_source = {0: src}
    f.printer = _Printer(_Sensor(pin))
    return f


def test_loaded_head_holds_filament():
    src = {'ace_index': 0, 'slot': 1}
    assert _holder(src, True)._ace_head_holds_filament(0)


def test_no_source_or_empty_pin_or_failed_load_needs_a_load():
    src = {'ace_index': 0, 'slot': 1}
    assert not _holder(None, True)._ace_head_holds_filament(0)
    assert not _holder(src, False)._ace_head_holds_filament(0)
    failed = dict(src, load_failed=True)
    assert not _holder(failed, True)._ace_head_holds_filament(0)
    gate_empty = dict(src, gate_empty=True)
    assert not _holder(gate_empty, True)._ace_head_holds_filament(0)
