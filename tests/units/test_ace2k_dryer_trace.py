"""ace2k dryer trace: one log line per dryer report for the first two
minutes of a cycle and one at a fault, with the numbers the unit's rise
protection judges (duty, drive target, both outlet NTCs).
"""
import importlib
import logging

from . import harness as H


def _link():
    H.load_ace()
    P = importlib.import_module(H.PKG + '.ace_protocol_ace2k')

    class Link(object):
        _dryer_trace = P.Ace2kLink._dryer_trace
        _lp = 'ace2k x: '

        def __init__(self):
            self._dryer_trace_t0 = None
            self._dryer_trace_prev = 0
            self._env = (0.0, {'ptc_left_mc': 41200, 'ptc_right_mc': 40800,
                               'chamber_mc': 24100, 'valid': 7})
    return Link()


def _run(link, caplog, reports):
    with caplog.at_level(logging.INFO):
        for now, state, fault in reports:
            link._dryer_trace(now, {'state': state, 'target_c': 50,
                                    'drive_dc': 570, 'duty': 90,
                                    'fault': fault})
    return [r.getMessage() for r in caplog.records if 'dryer +' in r.getMessage()]


def test_cycle_start_is_logged_once_a_second(caplog):
    link = _link()
    lines = _run(link, caplog, [(100.0, 0, 0), (101.0, 1, 0), (102.0, 2, 0),
                                (103.0, 2, 0)])
    assert len(lines) == 3
    assert lines[0].startswith('ace2k x: dryer +0s starting target=50 '
                               'drive=57.0 duty=90 ntc_l=41.2 ntc_r=40.8 '
                               'chamber=24.1 fault=none')
    assert '+2s heating' in lines[2]


def test_fault_line_and_silence_after_the_window(caplog):
    link = _link()
    lines = _run(link, caplog, [(0.0, 2, 0), (60.0, 2, 0), (121.0, 2, 0),
                                (300.0, 2, 0), (301.0, 4, 2), (302.0, 4, 2),
                                (400.0, 0, 0)])
    assert [ln.split(' target')[0] for ln in lines] == [
        'ace2k x: dryer +0s heating', 'ace2k x: dryer +60s heating',
        'ace2k x: dryer +301s fault']
    assert lines[-1].endswith('fault=response')


def test_invalid_sensor_reads_na(caplog):
    link = _link()
    link._env = (0.0, {'ptc_left_mc': 0, 'ptc_right_mc': 40800, 'valid': 2})
    lines = _run(link, caplog, [(5.0, 1, 0)])
    assert 'ntc_l=n/a ntc_r=40.8 chamber=n/a' in lines[0]
