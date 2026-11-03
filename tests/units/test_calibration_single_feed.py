"""Calibration feed of a unit that reports its own distance
(calibration_feed single, the CFS Nano): one feed to the sensor, the
distance taken from the status 'motion' report and carried on to the
sensor moment at the pace of the last two reports. Also the codec side:
the CFS link reports the running and the finished motion.
"""
from . import harness as H
from .test_cfs_codec import _mlink, _run_until_ready


def _rig():
    A = H.load_ace()

    class Ace(object):
        _calibration_motion = A.MultiAce._calibration_motion
        _calibration_note_motion = A.MultiAce._calibration_note_motion
        _calibration_single_at = A.MultiAce._calibration_single_at
        _calibration_move_travel = A.MultiAce._calibration_move_travel
    ace = Ace()
    ace._calibration = {'ace': 0}
    ace._info_per_ace = {0: {}}
    return ace


def _report(ace, seq, mm, kind='feed', running=True):
    ace._info_per_ace[0] = {'motion': {'seq': seq, 'slot': 0, 'kind': kind,
                                       'mm': mm, 'running': running}}


def _move(started=0.0):
    return {'length': 3000, 'speed': 30, 'started': started, 'single': True,
            'samples': [], 'motion_seq0': 4}


def test_distance_carried_on_to_the_sensor_moment():
    ace = _rig()
    mv = _move()
    for t, mm in ((1.0, 40.0), (2.0, 84.0), (3.0, 128.0)):
        _report(ace, 5, mm)
        ace._calibration_note_motion(mv, t)
        ace._calibration_note_motion(mv, t + 0.05)   # same report: no sample
    assert len(mv['samples']) == 3
    at = ace._calibration_single_at(mv, 3.5)
    assert abs(at - (128.0 + 44.0 * 0.5)) < 0.01
    assert ace._calibration['position_source'] == 'wheel'
    assert ace._calibration_move_travel(mv, 3.5) == 128.0


def test_older_motion_is_ignored():
    ace = _rig()
    mv = _move()
    _report(ace, 4, 900.0, running=False)    # the last job before this one
    ace._calibration_note_motion(mv, 1.0)
    _report(ace, 5, 30.0, kind='retract')
    ace._calibration_note_motion(mv, 1.5)
    assert mv['samples'] == []


def test_single_report_uses_the_pace_since_the_start():
    ace = _rig()
    mv = _move(started=10.0)
    _report(ace, 5, 88.0)
    ace._calibration_note_motion(mv, 12.0)
    assert abs(ace._calibration_single_at(mv, 12.5) - (88.0 + 22.0)) < 0.01


def test_no_report_falls_back_to_timing():
    ace = _rig()
    mv = _move(started=0.0)
    assert ace._calibration_single_at(mv, 2.0) == 60.0
    assert ace._calibration['position_source'] == 'timing'


def test_extrapolation_is_capped():
    ace = _rig()
    mv = _move()
    _report(ace, 5, 40.0)
    ace._calibration_note_motion(mv, 1.0)
    _report(ace, 5, 84.0)
    ace._calibration_note_motion(mv, 2.0)
    # 10 s without a report: carried on 2 s at most
    assert abs(ace._calibration_single_at(mv, 12.0) - (84.0 + 88.0)) < 0.01


def test_cfs_link_reports_the_motion():
    C, clock, dev, raw, link, ask = _mlink()
    assert 'motion' not in ask('get_status')['result']
    ask('feed_filament', index=0, length=300, speed=80)
    clock.now += 2.0
    m = ask('get_status')['result']['motion']
    assert m['kind'] == 'feed' and m['running'] and m['slot'] == 0
    assert m['mm'] > 0
    seq = m['seq']
    r = _run_until_ready(clock, ask)
    m = r['result']['motion']
    assert not m['running'] and m['seq'] == seq
    assert abs(m['mm'] - 300.0) < 50.0
    ask('unwind_filament', index=0, length=50, speed=80)
    assert ask('get_status')['result']['motion']['seq'] == seq + 1


def _rig_moving():
    ace = _rig()
    A = H.load_ace()
    ace._calibration_unit_still_moving = (
        A.MultiAce._calibration_unit_still_moving.__get__(ace))
    return ace


def test_running_motion_holds_the_timeout_off():
    ace = _rig_moving()
    _report(ace, 5, 35.0, kind='dosed')
    assert ace._calibration_unit_still_moving(0, 13.0, 1.7)
    # capped: a frozen report still times out
    assert not ace._calibration_unit_still_moving(0, 200.0, 1.7)


def test_finished_or_missing_motion_times_out():
    ace = _rig_moving()
    _report(ace, 5, 50.0, kind='dosed', running=False)
    assert not ace._calibration_unit_still_moving(0, 13.0, 1.7)
    ace._info_per_ace[0] = {}
    assert not ace._calibration_unit_still_moving(0, 13.0, 1.7)


def test_forward_jog_shows_the_reported_distance():
    ace = _rig()
    mv = {'kind': 'feed_jog', 'length': 50, 'speed': 30, 'started': 0.0}
    _report(ace, 5, 12.0, kind='dosed')
    assert ace._calibration_move_travel(mv, 10.0) == 12.0
    assert ace._calibration['position_source'] == 'wheel'
    ace._info_per_ace[0] = {}
    assert ace._calibration_move_travel(mv, 1.0) == 30.0
