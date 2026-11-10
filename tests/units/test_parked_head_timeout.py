import pytest
import types
from . import harness as H
from .test_bg_heat_intent import _bg_module, FakeHeater, FakePheaters, FakeReactor, FakePrintStats, FakePrinter


def _rig(parked_timeout=300.0, standby_temp=0.0):
    BG = _bg_module()
    reactor = FakeReactor()
    pheaters = FakePheaters()
    ps = FakePrintStats('printing')

    class FakeExtruder:
        def __init__(self, name, heater, idx):
            self.name = name
            self.heater = heater
            self.extruder_index = idx

        def get_name(self):
            return self.name

        def get_heater(self):
            return self.heater

    heaters = [FakeHeater('extruder', 215.0)] + [FakeHeater('extruder%d' % i, 200.0) for i in range(1, 4)]
    extruders = [FakeExtruder('extruder', heaters[0], 0)] + [FakeExtruder('extruder%d' % i, heaters[i], i) for i in range(1, 4)]

    class FakeToolhead:
        def __init__(self, ext):
            self.ext = ext

        def get_extruder(self):
            return self.ext

    toolhead = FakeToolhead(extruders[0])  # Head 0 is active initially

    objs = {
        'heaters': pheaters,
        'print_stats': ps,
        'toolhead': toolhead,
        'extruder': extruders[0],
    }
    for i in range(1, 4):
        objs['extruder%d' % i] = extruders[i]

    printer = FakePrinter(objs)

    class BgWrapper:
        _ext_name = BG.AceBgSwap._ext_name
        _get_active_head = BG.AceBgSwap._get_active_head
        _parked_timeout_tick = BG.AceBgSwap._parked_timeout_tick
        on_head_active = BG.AceBgSwap.on_head_active
        _set_heat = BG.AceBgSwap._set_heat
        _print_active = BG.AceBgSwap._print_active

        def __init__(self):
            self.printer = printer
            self.reactor = reactor
            self.parked_head_timeout = parked_timeout
            self.parked_standby_temp = standby_temp
            self._parked_since = {}
            self._saved_parked_target = {}
            self._busy = set()
            self.said = []

        def _dh(self, h):
            return h + 1

        def _say(self, msg):
            self.said.append(msg)

    bg = BgWrapper()
    return bg, heaters, toolhead, extruders, reactor, ps


def test_parked_head_timeout_drops_temp_to_standby():
    bg, heaters, toolhead, extruders, reactor, ps = _rig(parked_timeout=300.0, standby_temp=0.0)
    # Head 0 active (215C). Heads 1, 2, 3 are parked at 200C.
    # First tick records park time
    bg._parked_timeout_tick(reactor.now)
    assert 1 in bg._parked_since
    assert 2 in bg._parked_since
    assert 3 in bg._parked_since
    assert 0 not in bg._parked_since  # Head 0 is active

    # Advance clock by 100s (below 300s timeout)
    reactor.now += 100.0
    bg._parked_timeout_tick(reactor.now)
    assert heaters[1].target_temp == 200.0
    assert heaters[2].target_temp == 200.0

    # Advance clock past 300s
    reactor.now += 250.0  # now elapsed = 350s
    bg._parked_timeout_tick(reactor.now)

    # All parked heads (1, 2, 3) drop to 0C
    assert heaters[1].target_temp == 0.0
    assert heaters[2].target_temp == 0.0
    assert heaters[3].target_temp == 0.0
    assert heaters[0].target_temp == 215.0  # Active head untouched!

    # Pre-timeout target recorded
    assert bg._saved_parked_target[1] == 200.0
    assert bg._saved_parked_target[2] == 200.0


def test_activation_reheats_and_clears_timer():
    bg, heaters, toolhead, extruders, reactor, ps = _rig(parked_timeout=300.0, standby_temp=0.0)
    # Park and expire
    bg._parked_timeout_tick(reactor.now)
    reactor.now += 350.0
    bg._parked_timeout_tick(reactor.now)
    assert heaters[1].target_temp == 0.0

    # Head 1 becomes active
    toolhead.ext = extruders[1]
    bg.on_head_active(1)

    # Head 1 is reheated back to saved 200C
    assert heaters[1].target_temp == 200.0
    assert 1 not in bg._saved_parked_target
    assert 1 not in bg._parked_since
    assert any('reheating from standby' in s for s in bg.said)


def test_busy_background_head_exempt_from_timeout():
    bg, heaters, toolhead, extruders, reactor, ps = _rig(parked_timeout=300.0, standby_temp=0.0)
    # Head 1 is busy doing a background swap
    bg._busy.add(1)

    bg._parked_timeout_tick(reactor.now)
    assert 1 not in bg._parked_since

    reactor.now += 350.0
    bg._parked_timeout_tick(reactor.now)
    # Head 1 was not timed out
    assert heaters[1].target_temp == 200.0
