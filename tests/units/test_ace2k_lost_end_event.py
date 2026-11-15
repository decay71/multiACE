"""ace2k link: a piece whose end event was lost is booked from the lane
encoder report instead of reading as not moved."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))

from tests.units import harness  # noqa: E402,F401
from extras import ace_protocol_ace2k as K  # noqa: E402


def _link(now, enc, feed_t=None):
    s = types.SimpleNamespace(_enc=enc, _feed=(feed_t, {}) if feed_t else None)
    s._now = lambda: now
    s._fresh = lambda e: K.Ace2kLink._fresh(s, e)
    return s


def _ran(s, job, lane=2):
    return K.Ace2kLink._lost_piece_mm(s, lane, job)


def test_counters_report_is_parsed_signed_little_endian():
    s = types.SimpleNamespace(_now=lambda: 5.0)
    raw = b''.join(int(v).to_bytes(4, 'little', signed=True)
                   for v in (0, 1000, -950000, 7))
    K.Ace2kLink._dispatch(s, 'ace2k_lane_counters', {'encoder_um': raw})
    assert s._enc == (5.0, [0, 1000, -950000, 7])


def test_full_piece_is_booked_from_the_encoder():
    job = {'t0': 100.0, 'enc0': 1100000, 'piece': 950.0}
    s = _link(113.5, (113.4, [0, 0, 150400, 0]), feed_t=113.0)
    assert abs(_ran(s, job) - 949.6) < 0.01


def test_never_more_than_the_piece():
    job = {'t0': 100.0, 'enc0': 1100000, 'piece': 950.0}
    s = _link(113.5, (113.4, [0, 0, 100000, 0]), feed_t=113.0)
    assert _ran(s, job) == 950.0


def test_no_reading_books_nothing():
    job = {'t0': 100.0, 'enc0': None, 'piece': 950.0}
    assert _ran(_link(113.5, (113.4, [0, 0, 0, 0])), job) is None
    job = {'t0': 100.0, 'enc0': 5, 'piece': 950.0}
    assert _ran(_link(113.5, None), job) is None                 # no report
    assert _ran(_link(120.0, (113.4, [0, 0, 0, 0])), job) is None  # stale
    assert _ran(_link(101.0, (100.5, [0, 0, 0, 0])), job) is None  # too early
    # read before the report that showed the lane idle
    assert _ran(_link(113.5, (112.0, [0, 0, 0, 0]), feed_t=113.0), job) is None
