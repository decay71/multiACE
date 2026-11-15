"""ace2k link: a follow whose own lane stays full through repeated
take-backs (plunger stuck at the full end) is stopped and reported."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))

from tests.units import harness  # noqa: E402,F401
from extras import ace_protocol_ace2k as K  # noqa: E402


def _link(follow=(0,), jobs=()):
    stopped, lost = [], []
    s = types.SimpleNamespace(
        _follow={ln: 1 for ln in follow}, _jobs={ln: {} for ln in jobs},
        _follow_full={}, _bursts=[0, 0, 0, 0], _lp='', full={})
    s._lane_full = lambda lane: s.full.get(lane, False)

    def stop(lane):
        stopped.append(lane)
        s._follow.pop(lane, None)
    s._stop_follow = stop
    s._note_follow_lost = lambda lane, why: lost.append((lane, why))
    return s, stopped, lost


def _run(s, now):
    K.Ace2kLink._follow_full_watch(s, now)


def test_stuck_plunger_stops_the_follow():
    s, stopped, lost = _link()
    s.full[0] = True
    for i in range(7):                 # one take-back per second, never rests
        s._bursts[0] = i
        _run(s, 100.0 + i)
    assert stopped == [0]
    assert lost == [(0, 'buffer_full')]


def test_full_that_clears_is_normal_regulation():
    s, stopped, lost = _link()
    for i in range(20):
        s.full[0] = (i % 4 != 3)       # rests every fourth second
        s._bursts[0] = i
        _run(s, 100.0 + i)
    assert stopped == [] and lost == []


def test_full_without_take_backs_is_left_alone():
    # the lane is full but the follow does not pull back (e.g. shared
    # reading while another piece moves): nothing leaves the toolhead
    s, stopped, _ = _link()
    s.full[0] = True
    for i in range(30):
        _run(s, 100.0 + i)
    assert stopped == []


def test_a_running_job_on_the_lane_pauses_the_watch():
    s, stopped, _ = _link(jobs=(0,))
    s.full[0] = True
    for i in range(10):
        s._bursts[0] = i
        _run(s, 100.0 + i)
    assert stopped == []


def test_counter_restart_starts_the_watch_over():
    s, stopped, _ = _link()
    s.full[0] = True
    for i, b in enumerate((5, 6, 7, 0, 1, 2, 3)):
        s._bursts[0] = b
        _run(s, 100.0 + i)
    assert stopped == []               # only 3 take-backs since the restart
    s._bursts[0] = 5
    _run(s, 109.0)
    assert stopped == [0]


def test_other_lane_untouched():
    s, stopped, _ = _link(follow=(0, 3))
    s.full[0] = True
    for i in range(8):
        s._bursts = [i, i, i, i]
        _run(s, 100.0 + i)
    assert stopped == [0] and 3 in s._follow
