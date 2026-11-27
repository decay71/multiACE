"""ace2k link: the unit's dryer (start, stop, status, events) and the LED
identify behind the host's drying / drying_stop / flash_led methods."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))

from tests.units import harness  # noqa: E402,F401
from extras import ace_protocol_ace2k as K  # noqa: E402


class Dict(object):
    def __init__(self, cmds, consts=None):
        self.cmds, self.consts = set(cmds), consts or {}

    def has(self, name):
        return name in self.cmds

    def constant(self, key, default=None):
        return self.consts.get(key, default)


ALL = ('ace2k_dryer_start', 'ace2k_dryer_stop', 'ace2k_dryer_clear',
       'ace2k_dryer_event_ack', 'ace2k_identify')


def _link(answers=None, cmds=ALL, state=None, now=100.0):
    calls, sent = [], []
    answers = dict(answers or {})
    s = types.SimpleNamespace(
        dictionary=Dict(cmds), _lp='', _dryer_minutes=0, _dryer_ack=None,
        _dryer_ack_due=False, _gain_due=None, _gain_seen=None,
        _dryer=(now, state) if state is not None else None)
    s._now = lambda: now
    s._fresh = lambda e: K.Ace2kLink._fresh(s, e)

    def call(name, response, **values):
        calls.append((name, values))
        return answers.get(name)
    s._call = call
    s._send = lambda name, **values: sent.append((name, values))
    for m in ('_dryer_state', '_dryer_call', '_dryer_request',
              '_dryer_status', '_dryer_tick', '_identify'):
        setattr(s, m, types.MethodType(getattr(K.Ace2kLink, m), s))
    return s, calls, sent


OK = {'accepted': 1, 'reason': 0}


def test_start_clamps_to_the_units_range():
    s, calls, _ = _link({'ace2k_dryer_start': OK})
    r = s._dryer_request('drying', {'temp': 70, 'duration': 240}, 7)
    assert r['code'] == 0 and r['id'] == 7
    assert calls == [('ace2k_dryer_start', {'target_c': 65, 'minutes': 240})]
    assert s._dryer_minutes == 240


def test_refused_start_names_the_reason():
    s, _, _ = _link({'ace2k_dryer_start': {'accepted': 0, 'reason': 5}})
    r = s._dryer_request('drying', {'temp': 50, 'duration': 60}, 1)
    assert (r['code'], r['msg']) == (-1, 'no_mains')


def test_start_from_fault_tries_a_clear_first():
    s, calls, _ = _link({'ace2k_dryer_start': OK, 'ace2k_dryer_clear': OK},
                        state={'state': 4, 'fault': 10})
    assert s._dryer_request('drying', {'temp': 50, 'duration': 60}, 1)['code'] == 0
    assert [c[0] for c in calls] == ['ace2k_dryer_clear', 'ace2k_dryer_start']


def test_stop_and_stop_in_fault():
    s, calls, _ = _link({'ace2k_dryer_stop': OK})
    assert s._dryer_request('drying_stop', {}, 1)['code'] == 0
    s, calls, _ = _link({'ace2k_dryer_stop': {'accepted': 0, 'reason': 3},
                         'ace2k_dryer_clear': {'accepted': 0, 'reason': 7}})
    r = s._dryer_request('drying_stop', {}, 1)
    assert (r['code'], r['msg']) == (-1, 'not_cool')
    assert [c[0] for c in calls] == ['ace2k_dryer_stop', 'ace2k_dryer_clear']


def test_image_without_a_dryer_is_unsupported():
    s, _, _ = _link(cmds=())
    assert s._dryer_request('drying', {'temp': 50, 'duration': 60}, 1)[
        'code'] == K.UNSUPPORTED
    assert s._identify({'components': 15}, 1)['code'] == K.UNSUPPORTED
    assert s._dryer_status() is None


def test_status_maps_the_report():
    s, _, _ = _link(state={'state': 2, 'target_c': 50, 'remaining_min': 90,
                           'fault': 0})
    s._dryer_minutes = 240
    assert s._dryer_status() == {
        'status': 'drying', 'target_temp': 50, 'duration': 240,
        'remain_time': 5400, 'state': 'heating', 'fault': ''}
    s, _, _ = _link(state={'state': 4, 'target_c': 50, 'remaining_min': 0,
                           'fault': 12})
    d = s._dryer_status()
    assert (d['status'], d['state'], d['fault']) == ('stop', 'fault', 'lid')


def test_events_are_acknowledged_once():
    s, _, sent = _link()
    K.Ace2kLink._dispatch(s, 'ace2k_dryer_event',
                          {'kind': 2, 'arg': 7, 'seq': 4})
    s._dryer_tick()
    s._dryer_tick()
    assert sent == [('ace2k_dryer_event_ack', {'seq': 4})]


def test_heater_rate_is_asked_when_due_and_logged_on_change(caplog):
    s, calls, _ = _link({'ace2k_dryer_gain_query': {'gain': 61, 'source': 2}})
    s._dryer_tick()
    assert calls == []                      # nothing due
    s._gain_due = 50.0
    with caplog.at_level('INFO'):
        s._dryer_tick()
        s._gain_due = 50.0
        s._dryer_tick()                     # same answer: no second line
    assert [c[0] for c in calls] == ['ace2k_dryer_gain_query'] * 2
    lines = [r.getMessage() for r in caplog.records
             if 'heater rate' in r.getMessage()]
    assert lines == ['dryer heater rate: learned (gain 61)']
    assert s._gain_due is None


def test_heater_rate_is_silent_on_an_older_image():
    s, calls, _ = _link()                   # no answer = no such command
    s._gain_due = 50.0
    s._dryer_tick()
    assert s._gain_due is None and s._gain_seen is None


def test_identify_blinks_and_off_needs_no_command():
    s, calls, _ = _link({'ace2k_identify': {'ms': 10000}})
    assert s._identify({'components': 15}, 1)['code'] == 0
    assert s._identify({'components': 0}, 2)['code'] == 0
    assert [c[0] for c in calls] == ['ace2k_identify']
