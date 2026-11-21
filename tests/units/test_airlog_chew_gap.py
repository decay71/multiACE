"""CHEW detector: only back-to-back windows of a head form a run. HW
2026-11-19: head 0 printed ~25 s every 4.5 min and read ~30k in the
window after each pick (pick-up travel, vel_max 30); two visits 4.5 min
apart were counted as a run and paused the print."""
from .test_airlog_air import R, Stub

E = R.AIRLOG_EMIT_S


def _stub():
    s = Stub()
    s._airlog_chew_run = {}
    s._airlog_chew_latched = set()
    return s


def test_windows_across_a_park_do_not_pause():
    s = _stub()
    s._airlog_chew_eval(0, 30772, 6, 1000.0)
    s._airlog_chew_eval(0, 38665, 6, 1000.0 + 274.0)
    assert s.pauses == []
    assert s._airlog_chew_run[0] == 1


def test_back_to_back_windows_still_pause():
    s = _stub()
    s._airlog_chew_eval(0, 40000, 6, 1000.0)
    s._airlog_chew_eval(0, 45000, 6, 1000.0 + E + 2)
    assert s.pauses == [(0, 'airlog-chew')]
