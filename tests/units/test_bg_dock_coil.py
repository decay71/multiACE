"""Dock coil log: sampling during the background prime, log only."""
import types

from .test_bg_pick_notice import _bg_module

BG = _bg_module()


class Reactor:
    def __init__(self, step=0.0):
        self.now, self.step = 100.0, step

    def monotonic(self):
        self.now += self.step
        return self.now


def ext_with(values):
    it = iter(values)
    sensor = types.SimpleNamespace(get_coil_freq=lambda: next(it))
    return types.SimpleNamespace(
        binding_probe=types.SimpleNamespace(sensor=sensor))


def engine(step=0.0):
    e = BG.AceBgSwap.__new__(BG.AceBgSwap)
    e.reactor = Reactor(step)
    return e


def test_runs_without_the_airlog_switch():
    ace = types.SimpleNamespace(airlog_enable=False)
    assert engine()._dock_coil_start(ace, ext_with([1.0])) is not None


def _stored(values):
    ace = types.SimpleNamespace(
        _bg_dock_coil={}, _wiggle_log=types.SimpleNamespace(info=lambda l: None))
    e = engine()
    ext = ext_with(values)
    d = e._dock_coil_start(ace, ext)
    for _ in values[1:]:
        e._dock_coil_sample(d, ext)
    e._dock_coil_report(ace, d, 2, 1, 3, 20.0, 80.0, 4.0, True)
    return ace._bg_dock_coil


def test_reading_is_kept_for_the_arrival_with_a_verdict():
    assert _stored([1485000.0, 1479800.0])[2]['verdict'] == 'FLOW'
    air = _stored([1485730.0, 1485700.0])[2]
    assert air['verdict'] == 'NO_FLOW' and air['dip'] == 30.0
    assert air['primed'] == 20.0 and air['target'] == 80.0


def test_no_sample_leaves_nothing_for_the_arrival():
    ace = types.SimpleNamespace(_bg_dock_coil={2: {'verdict': 'FLOW'}})
    e = engine()
    d = e._dock_coil_start(ace, ext_with([1.0]))
    e._dock_coil_report(ace, d, 2, 1, 3, 0.0, 80.0, 4.0, True)
    assert 2 not in ace._bg_dock_coil


def test_samples_min_max_and_reports():
    lines = []
    ace = types.SimpleNamespace(
        airlog_enable=True,
        _wiggle_log=types.SimpleNamespace(info=lines.append))
    e = engine()
    ext = ext_with([1000.0, 990.0, 400.0, 1005.0])
    d = e._dock_coil_start(ace, ext)
    for _ in range(3):
        e._dock_coil_sample(d, ext)
    e._dock_coil_report(ace, d, 2, 1, 3, 80.0, 80.0, 7.5, False)
    assert d['n'] == 3 and d['min'] == 400.0 and d['max'] == 1005.0
    assert len(lines) == 1 and 'dip=600 up=5 cut=0' in lines[0]
    assert lines[0].startswith('dockcoil head=2 ace=1 slot=3')


def test_slow_or_failing_read_disables_sampling():
    ace = types.SimpleNamespace()
    assert engine(step=0.5)._dock_coil_start(ace, ext_with([1.0])) is None
    broken = types.SimpleNamespace(binding_probe=None)
    assert engine()._dock_coil_start(ace, broken) is None
    engine()._dock_coil_sample(None, broken)       # no-op, no raise


class _Mcu:
    def estimated_print_time(self, now):
        return now


class _Gcmd:
    def __init__(self, **p):
        self.p, self.out = p, []

    def get_int(self, name, default=None, **kw):
        return int(self.p.get(name, default))

    def get_float(self, name, default=None, **kw):
        return float(self.p.get(name, default))

    def respond_info(self, msg):
        self.out.append(msg)


def _test_engine(values, active=False, printing=False, hot=True):
    e = engine()
    heater = types.SimpleNamespace(can_extrude=hot, smoothed_temp=220.0)
    ext = ext_with(values)
    ext.get_heater = lambda: heater
    other = object()
    toolhead = types.SimpleNamespace(
        get_extruder=lambda: ext if active else other, mcu=_Mcu())
    ace = types.SimpleNamespace(
        _head_sensor_present=lambda h: False,
        _wiggle_log=types.SimpleNamespace(info=lambda l: None))
    objs = {'extruder2': ext, 'toolhead': toolhead, 'ace': ace}
    e.printer = types.SimpleNamespace(
        lookup_object=lambda n, d=None: objs.get(n, d))
    e._busy = set()
    e._print_active = lambda: printing
    e.moves = []

    def queue_move(x, dist, speed, accel):
        e.moves.append((dist, speed))
        return (0., 0., False)
    e.queue_move = queue_move
    e._wait_lead = lambda th, end: None
    e._wait_move = lambda th, end: None
    e._bg_error = lambda gcmd, msg, head=None: RuntimeError(msg)
    return e


def test_coil_test_runs_the_prime_in_chunks_and_reports():
    e = _test_engine([1000.0, 990.0, 985.0, 980.0, 1001.0])
    g = _Gcmd(HEAD=2, LENGTH=20, SPEED=4, FORCE=0)
    e.cmd_ACE_BG_COIL_TEST(g)
    assert e.moves == [(5.0, 4.0)] * 4
    assert len(g.out) == 1
    assert 'filament=none len=20 speed=4.0' in g.out[0]
    assert 'n=4' in g.out[0] and 'dip=20 up=1' in g.out[0]


def test_coil_test_refuses_active_head_print_and_cold_head():
    import pytest
    for kw in ({'active': True}, {'printing': True}, {'hot': False}):
        e = _test_engine([1000.0] * 6, **kw)
        with pytest.raises(RuntimeError):
            e.cmd_ACE_BG_COIL_TEST(_Gcmd(HEAD=2, LENGTH=20, SPEED=4, FORCE=0))
        assert e.moves == []
