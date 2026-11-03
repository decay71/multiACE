"""Negative cache of the generic USB probes: a port that answered nothing is
asked again after a growing pause (PROBE_MISS_BACKOFF), so a unit that was
still booting at the first probe is found within seconds, while a foreign
device on the same chip backs off to 30 s. Real method on a stub."""
import importlib
import logging
import os
import sys
import threading
import time as _real_time
import types

EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                      'multiace', 'klipper', 'extras')
PKG = 'multiace_probe_backoff_extras'


def load_ace():
    name = PKG + '.ace'
    if name in sys.modules:
        return sys.modules[name]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [EXTRAS]
    sys.modules[PKG] = pkg
    return importlib.import_module(name)


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class Reactor(object):
    def __init__(self, clock):
        self.clock = clock

    def monotonic(self):
        return self.clock.t

    def pause(self, waketime):
        _real_time.sleep(0.001)
        return self.clock.t


def _rig(monkeypatch, answers):
    A = load_ace()
    clock = Clock()
    monkeypatch.setattr(A, 'time', types.SimpleNamespace(
        monotonic=clock.monotonic, time=_real_time.time,
        sleep=_real_time.sleep))
    V2 = A.AceProtocolV2
    calls = []

    def probe(cls, path, query_timeout=0.35):
        calls.append(clock.t)
        return answers.pop(0) if answers else {
            'ok': False, 'category': 'no_response', 'error': 'silent'}
    monkeypatch.setattr(V2, 'probe_device', classmethod(probe))
    monkeypatch.setattr(V2, '_read_usb_ids',
                        staticmethod(lambda dev: ('1a86', '7523')))
    monkeypatch.setattr(V2, 'serial_port_in_use',
                        staticmethod(lambda path: False))
    a = A.MultiAce.__new__(A.MultiAce)
    a._enable_ace_v2 = True
    a._v2_probe_generic_usb = True
    a._v2_generic_probe_cache = {}
    a._v2_generic_probe_in_progress = set()
    a._v2_generic_probe_cursor = None
    a._usb_log = logging.getLogger('test-probe-backoff')
    a.reactor = Reactor(clock)
    a._configured_serial_path_set = lambda: set()
    a._is_printer_idle_for_generic_probe = lambda: True
    return A, a, clock, calls


OK = {'ok': True, 'uid': (1, 2, 3), 'model': 'ACE', 'firmware': 'V1.1.31'}
MISS = {'ok': False, 'category': 'no_response', 'error': 'silent'}


def _scan(a):
    return a._probe_v2_generic_candidate('/dev/ttyUSB9', set(), [1])


def test_ttl_ladder():
    A = load_ace()
    assert A.probe_cache_ttl(True, None, 0) == A.PROBE_VERIFIED_TTL
    assert A.probe_cache_ttl(False, 'port_in_use', 3) == A.PROBE_TRANSIENT_TTL
    got = [A.probe_cache_ttl(False, 'no_response', n) for n in range(1, 7)]
    assert got == [5.0, 10.0, 20.0, 30.0, 30.0, 30.0]
    assert A.probe_misses_after(2, False, 'port_in_use') == 2
    assert A.probe_misses_after(2, False, 'probe_timeout') == 3
    assert A.probe_misses_after(4, True, None) == 0


def test_late_unit_found_after_five_seconds(monkeypatch):
    A, a, clock, calls = _rig(monkeypatch, [dict(MISS), dict(OK)])
    assert _scan(a) is False
    clock.t += 4.0
    assert _scan(a) is False          # still inside the first 5 s
    assert len(calls) == 1
    clock.t += 1.5
    assert _scan(a) is True           # asked again, the unit now answers
    assert len(calls) == 2


def test_silent_port_backs_off_to_thirty_seconds(monkeypatch):
    A, a, clock, calls = _rig(monkeypatch, [])
    times = []
    for _ in range(80):               # one scan per second, 80 s
        _scan(a)
        clock.t += 1.0
    times = [round(t - calls[0]) for t in calls]
    gaps = [b - a2 for a2, b in zip(times, times[1:])]
    assert gaps[:4] == [5, 10, 20, 30]
