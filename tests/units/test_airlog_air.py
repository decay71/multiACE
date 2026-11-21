"""Airlog AIR detector: sustained low back-pressure while extruding."""
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXTRAS = os.path.join(ROOT, 'multiace', 'klipper', 'extras')


def _load():
    pkg = types.ModuleType('ma_air_pkg')
    pkg.__path__ = [EXTRAS]
    sys.modules['ma_air_pkg'] = pkg
    spec = importlib.util.spec_from_file_location(
        'ma_air_pkg.ace_resistance', os.path.join(EXTRAS, 'ace_resistance.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load()


class Stub(R.ResistanceMixin):
    def __init__(self, pause=True):
        self._airlog_air_run = {}
        self._airlog_air_seen = {}
        self._airlog_air_latched = set()
        self._swap_in_progress = False
        self._pickcheck_active = False
        self._swap_phase = 'idle'
        self.resistance_pause = pause
        self.printing = True
        self._loading_heads = set()
        self.events = []
        self.pauses = []
        self.alerts = []

    def _is_actively_printing(self):
        return self.printing

    def _ace_event(self, name, **kw):
        self.events.append((name, kw))

    def _airlog_pause(self, head, detail, what):
        self.pauses.append((head, what))

    def log_warn(self, text):
        self.alerts.append(text)

    def _t(self, key, **kw):
        return key

    def _disp(self, n):
        return n + 1


def feed(stub, head, windows, t0=100.0, coil=(1000000.0, None)):
    """coil = (coil_min, rest): the window's coil_max is rest when given,
    else coil_min + delta (an air window sits at the rest level)."""
    t = t0
    for delta, nmove, vel in windows:
        cmin = coil[0]
        cmax = coil[1] if coil[1] is not None else cmin + delta
        stub._airlog_air_eval(head, delta, nmove, vel, t, cmin, cmax)
        t += 12.0
    return t


# The windows of the print that ran dry: head 1, 10:40:18 - 10:41:54 UTC.
DRY = [(140, 2, 2.0), (42, 3, 0.4), (407, 6, 1.8), (336, 6, 3.2),
       (140, 6, 1.9), (476, 6, 2.7), (294, 6, 4.2), (266, 6, 2.9),
       (378, 6, 3.0)]


def test_dry_head_warns_on_the_third_counted_window():
    s = Stub()
    feed(s, 1, DRY[:6])
    assert s.alerts == []
    feed(s, 1, DRY[6:7], t0=100.0 + 6 * 12)
    assert [e[0] for e in s.events] == ['airlog_air']
    feed(s, 1, DRY[7:], t0=100.0 + 7 * 12)
    assert len(s.events) == 1          # latched, once per head


def test_slow_windows_neither_count_nor_reset():
    s = Stub()
    # three slow windows first establish the rest level (AIRLOG_REST_MIN_S)
    feed(s, 0, [(100, 6, 1.0)] * 3 + [
        (300, 6, 3.0), (100, 6, 1.0), (300, 6, 3.0),
        (50, 2, 0.5), (300, 6, 3.0)])
    assert [e[0] for e in s.events] == ['airlog_air']


def test_slow_print_never_fires():
    s = Stub()
    feed(s, 0, [(300, 6, 1.5)] * 20)
    assert s.alerts == [] and s.events == []


def test_normal_window_resets_the_run():
    s = Stub()
    feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0), (4000, 6, 3.0),
                (300, 6, 3.0), (300, 6, 3.0)])
    assert s.alerts == []


def test_head_away_resets_the_run():
    s = Stub()
    t = feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0)])
    feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0)], t0=t + 300)
    assert s.alerts == []


def test_swap_window_is_not_counted():
    s = Stub()
    feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0)])
    s._swap_in_progress = True
    s._airlog_air_eval(0, 300, 6, 3.0, 124.0)
    s._swap_in_progress = False
    assert s.alerts == [] and 0 not in s._airlog_air_run


def test_detection_off_logs_only():
    s = Stub(pause=False)
    feed(s, 1, DRY)
    assert s.alerts == []
    assert [e[0] for e in s.events] == ['airlog_air']


def test_paused_print_and_loading_head_are_no_flow_reading():
    # a manual unload in a pause runs tip-form moves with nothing flowing
    s = Stub()
    s.printing = False
    for i in range(6):
        s._airlog_air_eval(0, 150, 6, 4.0, 100.0 + 12 * i)
    assert s.alerts == [] and s._airlog_air_run == {}
    s.printing = True
    s._loading_heads = {0}
    for i in range(6):
        s._airlog_air_eval(0, 150, 6, 4.0, 200.0 + 12 * i)
    assert s.alerts == []
    # back in the print the count starts from zero
    s._loading_heads = set()
    # (the rest level needs AIRLOG_REST_MIN_S of windows first)
    for i in range(7):
        s._airlog_air_eval(0, 150, 6, 4.0, 300.0 + 12 * i,
                           1000000.0, 1000150.0)
    assert len(s.events) == 1


def test_even_path_far_below_rest_is_not_air():
    # 2026-11-15 PETG: long even path at 2 mm/s, delta under 500 for
    # minutes, but coil_min ~5900 below the head's rest level.
    s = Stub()
    feed(s, 0, [(9000, 6, 4.0)] * 3, coil=(1424000.0, 1430000.0))
    feed(s, 0, [(400, 6, 2.1)] * 10, t0=136.0, coil=(1424000.0, 1424400.0))
    assert s.alerts == [] and s._airlog_air_run.get(0) is None


def test_no_rest_level_yet_counts_nothing():
    s = Stub()
    feed(s, 0, [(300, 6, 3.0)] * 2)
    assert s._airlog_air_run.get(0) is None


def test_air_never_pauses():
    """HW 2026-11-20: the air detector paused a print that flowed fine
    (even path, coil_min equal to the flowing windows). It only logs:
    no pause, no web notice."""
    s = Stub()
    feed(s, 1, DRY)
    assert [e[0] for e in s.events] == ['airlog_air']
    assert s.alerts == [] and s.pauses == []
