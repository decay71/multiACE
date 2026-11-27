"""Manual dryer start refused as busy (unit still cooling down): an amber
notice instead of the red generic error. Every other refusal stays red.
"""
import importlib

from . import harness as H


def _rig(reply):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    M = A.MultiAce

    class Gcmd(object):
        def get_int(self, name, default=None):
            return {'TEMP': 50, 'DURATION': 60}.get(name, default)

    class Gcode(object):
        def __init__(self):
            self.info = []

        def respond_info(self, text):
            self.info.append(text)

    class Ace(object):
        cmd_ACE_START_DRYING = M.cmd_ACE_START_DRYING
        max_dryer_temperature = 70
        dry_auto_roll = False
        _active_device_index = 1

        def __init__(self):
            self.warn, self.err, self.opened = [], [], []
            self.gcode = Gcode()
            self._auto_dry_fault = {}

        def _wait_homing_clear(self):
            pass

        def wait_ace_ready(self):
            pass

        def _t(self, key, **kw):
            return (key, kw)

        def _disp(self, idx):
            return idx + 1

        def _raw(self, idx):
            return idx

        def log_warn(self, msg):
            self.warn.append(msg)

        def log_error(self, msg):
            self.err.append(msg)

        def _schedule_dry_exhaust_open(self, idx, why):
            self.opened.append(idx)

        def send_request(self, request, callback):
            callback(self=self, response=reply)

    ace = Ace()
    ace.cmd_ACE_START_DRYING(Gcmd())
    return ace


def test_busy_refusal_is_an_amber_notice():
    ace = _rig({'code': 1, 'msg': 'busy'})
    assert ace.warn == [('msg.dryer_start_busy', {'ace': 2})]
    assert ace.err == [] and ace.opened == []


def test_other_refusal_stays_red():
    ace = _rig({'code': 3, 'msg': 'faulted'})
    assert ace.warn == []
    assert ace.err == [('msg.ace_error_generic', {'error': 'faulted'})]


def test_accepted_start_reports_and_opens_the_exhaust():
    ace = _rig({'code': 0, 'msg': 'success'})
    assert ace.warn == [] and ace.err == [] and ace.opened == [1]
    assert ace.gcode.info[0][0] == 'msg.drying_ace_at'
