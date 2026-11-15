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

    def _is_actively_printing(self):
        return self.printing

    def _ace_event(self, name, **kw):
        self.events.append((name, kw))

    def _airlog_pause(self, head, detail, what):
        self.pauses.append((head, what))

    def _t(self, key, **kw):
        return key

    def _disp(self, n):
        return n + 1


def feed(stub, head, windows, t0=100.0):
    t = t0
    for delta, nmove, vel in windows:
        stub._airlog_air_eval(head, delta, nmove, vel, t)
        t += 12.0
    return t


# The windows of the print that ran dry: head 1, 10:40:18 - 10:41:54 UTC.
DRY = [(140, 2, 2.0), (42, 3, 0.4), (407, 6, 1.8), (336, 6, 3.2),
       (140, 6, 1.9), (476, 6, 2.7), (294, 6, 4.2), (266, 6, 2.9),
       (378, 6, 3.0)]


def test_dry_head_pauses_on_the_third_counted_window():
    s = Stub()
    feed(s, 1, DRY[:6])
    assert s.pauses == []
    feed(s, 1, DRY[6:7], t0=100.0 + 6 * 12)
    assert s.pauses == [(1, 'airlog-air')]
    feed(s, 1, DRY[7:], t0=100.0 + 7 * 12)
    assert len(s.pauses) == 1          # latched, once per head


def test_slow_windows_neither_count_nor_reset():
    s = Stub()
    feed(s, 0, [(300, 6, 3.0), (100, 6, 1.0), (300, 6, 3.0),
                (50, 2, 0.5), (300, 6, 3.0)])
    assert s.pauses == [(0, 'airlog-air')]


def test_slow_print_never_fires():
    s = Stub()
    feed(s, 0, [(300, 6, 1.5)] * 20)
    assert s.pauses == [] and s.events == []


def test_normal_window_resets_the_run():
    s = Stub()
    feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0), (4000, 6, 3.0),
                (300, 6, 3.0), (300, 6, 3.0)])
    assert s.pauses == []


def test_head_away_resets_the_run():
    s = Stub()
    t = feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0)])
    feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0)], t0=t + 300)
    assert s.pauses == []


def test_swap_window_is_not_counted():
    s = Stub()
    feed(s, 0, [(300, 6, 3.0), (300, 6, 3.0)])
    s._swap_in_progress = True
    s._airlog_air_eval(0, 300, 6, 3.0, 124.0)
    s._swap_in_progress = False
    assert s.pauses == [] and 0 not in s._airlog_air_run


def test_detection_off_logs_only():
    s = Stub(pause=False)
    feed(s, 1, DRY)
    assert s.pauses == []
    assert [e[0] for e in s.events] == ['airlog_air']


def test_paused_print_and_loading_head_are_no_flow_reading():
    # a manual unload in a pause runs tip-form moves with nothing flowing
    s = Stub()
    s.printing = False
    for i in range(6):
        s._airlog_air_eval(0, 150, 6, 4.0, 100.0 + 12 * i)
    assert s.pauses == [] and s._airlog_air_run == {}
    s.printing = True
    s._loading_heads = {0}
    for i in range(6):
        s._airlog_air_eval(0, 150, 6, 4.0, 200.0 + 12 * i)
    assert s.pauses == []
    # back in the print the count starts from zero
    s._loading_heads = set()
    for i in range(4):
        s._airlog_air_eval(0, 150, 6, 4.0, 300.0 + 12 * i)
    assert len(s.pauses) == 1
