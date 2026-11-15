"""bg engine on units that report their own moves (real methods on stubs).

HW 2026-11-09 (multi mode, two ace2k units): the bulk retract of MMU 1 /
Slot 1 ended 'stuck' after 21 mm and then 0 mm, the toolhead pin already
read clear, and the open-loop pacing counted the unload as done. The
transport of MMU 2 / Slot 1 then ran into the strand still in the shared
path, ended 'blocked' after 175 mm, and the engine waited out the whole
feed window plus an FA rescue before anything noticed. The engine now reads
the unit's end report for both.
"""
import importlib
import os
import sys
import types

import pytest


class H:
    EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                          '..', 'multiace', 'klipper', 'extras')
    PKG = 'multiace_bg_motion_extras'

    @staticmethod
    def bg_module():
        name = H.PKG + '.ace_bg_swap'
        if name in sys.modules:
            return sys.modules[name]
        if H.PKG not in sys.modules:
            pkg = types.ModuleType(H.PKG)
            pkg.__path__ = [H.EXTRAS]
            sys.modules[H.PKG] = pkg
        importlib.import_module(H.PKG + '.ace')
        if 'chelper' not in sys.modules:
            sys.modules['chelper'] = types.ModuleType('chelper')
        fm = H.PKG + '.force_move'
        if fm not in sys.modules:
            stub = types.ModuleType(fm)
            stub.calc_move_time = None
            sys.modules[fm] = stub
            setattr(sys.modules[H.PKG], 'force_move', stub)
        return importlib.import_module(name)


class Reactor(object):
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class FakeUnit(object):
    """send_request_to answers like the ace2k link (or a CFS Nano with
    per_lane=False): a move starts a job whose report ends `end_after`
    seconds later with `end`."""

    def __init__(self, reactor, per_lane=True, reports=True,
                 end_after=1.0, end=None):
        self.reactor = reactor
        self.per_lane = per_lane
        self.reports = reports
        self.end_after = end_after
        self.end = end or {}
        self.sent = []
        self.seq = 7
        self.job = None
        self._info_per_ace = {1: {'slots': [{}, {}, {}, {}]}}

    def _cap(self, idx, name):
        return False      # no slot status truth, no decoder

    def send_request_to(self, idx, request, cb):
        self.sent.append(request['method'])
        m = request['method']
        if m in ('unwind_filament', 'feed_filament') and self.reports:
            self.seq += 1
            p = request['params']
            self.job = {'seq': self.seq, 'slot': p['index'],
                        'kind': 'retract' if m == 'unwind_filament'
                        else 'feed', 'target': float(p['length']),
                        't0': self.reactor.now}
            self._publish(running=True, mm=0.0)
        cb(self=self, response={'code': 0, 'msg': 'success', 'result': {}})

    def _publish(self, running, mm, extra=None):
        j = self.job
        m = {'seq': j['seq'], 'slot': j['slot'], 'kind': j['kind'],
             'mm': mm, 'running': running}
        if j['kind'] == 'retract':
            m['target'] = j['target']
        if extra:
            m.update(extra)
        info = self._info_per_ace[1]
        if self.per_lane:
            info['slots'][j['slot']]['motion'] = m
        else:
            info['motion'] = m

    def tick(self):
        j = self.job
        if j is None or self.reactor.now - j['t0'] < self.end_after:
            return
        mm = self.end.get('mm', j['target'])
        extra = dict(self.end)
        extra.pop('mm', None)
        self._publish(running=False, mm=mm, extra=extra)
        self.job = None


class Sensor(object):
    def get_status(self, ev):
        return {'filament_detected': False}


class Printer(object):
    def lookup_object(self, name, default=None):
        return Sensor() if name.startswith('filament_motion_sensor') \
            else default


def _engine(unit):
    BG = H.bg_module()

    class Bg(object):
        _move_report = BG.AceBgSwap._move_report
        _move_seq = BG.AceBgSwap._move_seq
        _new_move_end = BG.AceBgSwap._new_move_end
        _await_unwind_end = BG.AceBgSwap._await_unwind_end
        _retract_held = BG.AceBgSwap._retract_held
        _slot_motion = BG.AceBgSwap._slot_motion
        _ace_unwind = BG.AceBgSwap._ace_unwind
        _ace_feed_to_gears = BG.AceBgSwap._ace_feed_to_gears
        _ace_send = BG.AceBgSwap._ace_send
        _resp_rejected = BG.AceBgSwap._resp_rejected
        _cap = BG.AceBgSwap._cap

        def __init__(self):
            self.reactor = unit.reactor
            self.printer = Printer()
            self.said = []

        def _pause(self, dt):
            self.reactor.now += dt
            unit.tick()

        def _say(self, msg):
            self.said.append(msg)

        def _dh(self, i):
            return i + 1

        def _fa_cache_mark(self, *a):
            pass

        def _fa_cache_clear(self, *a):
            pass
    return Bg(), BG


def test_held_short_retract_is_seen_early():
    unit = FakeUnit(Reactor(), end_after=0.4,
                    end={'mm': 21.0, 'short': 129.0, 'stop': 'stuck'})
    bg, BG = _engine(unit)
    rep = {}
    t0 = unit.reactor.now
    assert bg._ace_unwind(unit, 1, 0, 150, 80, wait=True, report=rep)
    assert unit.reactor.now - t0 < 1.5          # not the 3.9 s pacing
    assert BG.AceBgSwap._retract_held(rep['end']) == '21 of 150 mm, stuck'


def test_full_retract_is_not_held():
    unit = FakeUnit(Reactor(), end_after=1.0, end={'short': 0.0,
                                                   'stop': 'distance'})
    bg, BG = _engine(unit)
    rep = {}
    assert bg._ace_unwind(unit, 1, 0, 150, 80, wait=True, report=rep)
    assert rep['end']['mm'] == 150.0
    assert BG.AceBgSwap._retract_held(rep['end']) is None


def test_runout_end_counts_as_back():
    m = {'kind': 'retract', 'running': False, 'mm': 400.0, 'target': 883.0,
         'short': 483.0, 'stop': 'runout'}
    assert H.bg_module().AceBgSwap._retract_held(m) is None


def test_cfs_unit_report_is_used():
    unit = FakeUnit(Reactor(), per_lane=False, end_after=0.4,
                    end={'mm': 50.0, 'short': 100.0})
    bg, BG = _engine(unit)
    rep = {}
    bg._ace_unwind(unit, 1, 2, 150, 80, wait=True, report=rep)
    assert BG.AceBgSwap._retract_held(rep['end']) == '50 of 150 mm, short'


def test_no_report_keeps_open_loop_pacing():
    unit = FakeUnit(Reactor(), reports=False)
    bg, BG = _engine(unit)
    rep = {}
    t0 = unit.reactor.now
    assert bg._ace_unwind(unit, 1, 0, 160, 80, wait=True, report=rep)
    assert rep['end'] is None
    assert unit.reactor.now - t0 == pytest.approx(4.0, abs=0.25)


def test_long_running_retract_waits_for_its_end():
    unit = FakeUnit(Reactor(), end_after=9.0, end={'short': 0.0})
    bg, BG = _engine(unit)
    rep = {}
    t0 = unit.reactor.now
    bg._ace_unwind(unit, 1, 0, 160, 80, wait=True, report=rep)
    assert rep['end'] is not None and not rep['end']['running']
    assert unit.reactor.now - t0 == pytest.approx(9.0, abs=0.3)


def test_feed_ended_by_the_unit_stops_the_wait():
    unit = FakeUnit(Reactor(), end_after=3.7,
                    end={'mm': 175.0, 'stop': 'blocked'})
    bg, BG = _engine(unit)
    t0 = unit.reactor.now
    res, _span = bg._ace_feed_to_gears(unit, 1, 0, 1918, 80, head=0)
    assert res == 'partial'
    assert unit.reactor.now - t0 < 8.0          # not 39 s + FA rescue
    assert 'start_feed_assist' not in unit.sent
    assert any('feed ended by the MMU after 175 mm (blocked)' in m
               for m in bg.said)
    # kept for the retry loop's same-place check
    assert bg._feed_unit_end[0] == ('blocked', 175.0)


def test_feed_slip_without_movement_is_none():
    unit = FakeUnit(Reactor(), end_after=1.0,
                    end={'mm': 16.0, 'stop': 'slip'})
    bg, BG = _engine(unit)
    res, _span = bg._ace_feed_to_gears(unit, 1, 0, 1918, 80, head=0)
    assert res == 'none'
    assert bg._feed_unit_end[0] == ('slip', 16.0)


def test_retry_blocked_at_the_same_place_is_recognised():
    unit = FakeUnit(Reactor(), end_after=1.0, end={'mm': 0.0})
    _bg, BG = _engine(unit)
    same = BG.AceBgSwap._blocked_same_place
    # HW 2026-11-09: 370 mm blocked, then 51 / 52 / 51 after 50 mm back
    assert same(('blocked', 370.0), ('blocked', 51.0), 50)
    assert same(('slip', 11.0), ('blocked', 52.0), 50)
    # a retry that got further, or ended another way, goes on
    assert not same(('blocked', 35.0), ('blocked', 500.0), 50)
    assert not same(('distance', 1918.0), ('blocked', 51.0), 50)
    assert not same(('blocked', 370.0), ('slip', 5.0), 50)
    assert not same(('blocked', 370.0), ('distance', 1918.0), 50)
    # no motion report (ACE Pro, ACE 2): every retry runs
    assert not same(None, None, 50)
    assert not same(('blocked', 370.0), None, 50)
