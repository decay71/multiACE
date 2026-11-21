"""The 5 mm rollback that switches the feed assist off is an ACE Pro
trick (cap fa_stop_rollback). A unit that ends its assist on
stop_feed_assist alone (ace2k) must not get it: the rollback wound a loose
strand out of the inlet (HW 2026-11-09 09:12 local) and it ran after every
load against a tip the extruder held (once 'timeout after 4 mm').
"""
import importlib

from . import harness as H


def _rig(caps):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    M = A.MultiAce

    class Ace(object):
        _disable_feed_assist_all = M._disable_feed_assist_all
        _disable_feed_assist = M._disable_feed_assist
        _enable_feed_assist = M._enable_feed_assist
        _disarm_fa_for = M._disarm_fa_for

        def __init__(self):
            self.sent = []
            self.retracts = []
            self.armed = []
            self._active_device_index = 0
            self._feed_assist_per_ace = {0: 2}
            self._feed_assist_index = 2
            self._connected_per_ace = {0: True}
            self._gate_status_per_ace = {0: [A.GATE_AVAILABLE] * 4}
            self._fa_last_active_ts = 0.0

        def _t(self, key, **kw):
            return key

        def _disp(self, n):
            return n + 1

        def log_error(self, msg):
            pass

        def _cap(self, idx, name):
            return caps.get(name, False)

        def wait_ace_ready_on(self, idx, *a, **kw):
            pass

        def wait_ace_ready(self, *a, **kw):
            pass

        def dwell(self, delay=1.0):
            pass

        def send_request_to(self, idx, request, callback=None):
            self.sent.append((idx, request['method'],
                              dict(request.get('params') or {})))

        def _retract(self, index, length, speed, **kw):
            self.retracts.append((index, length, speed))

        def _arm_fa_for(self, idx, slot, from_recovery=False):
            self.armed.append((idx, slot))

    return Ace()


def _methods(ace):
    return [m for (_i, m, _p) in ace.sent]


def test_all_off_on_the_ace_pro_keeps_the_rollback():
    ace = _rig({'fa_stop_rollback': True})
    ace._disable_feed_assist_all()
    assert _methods(ace) == ['unwind_filament', 'stop_feed_assist']
    assert ace.sent[0][2] == {'index': 2, 'length': 5, 'speed': 80}
    assert ace._feed_assist_per_ace[0] == -1


def test_all_off_on_the_ace2k_only_stops_the_assist():
    ace = _rig({'fa_stop_rollback': False})
    ace._disable_feed_assist_all()
    assert _methods(ace) == ['stop_feed_assist']
    assert ace.sent[0][2] == {'index': 2}
    assert ace._feed_assist_per_ace[0] == -1
    assert ace._feed_assist_index == -1


def test_all_off_on_the_ace_2_stays_armed():
    ace = _rig({'has_rollback_assist': True, 'fa_stop_rollback': False})
    ace._disable_feed_assist_all()
    assert ace.sent == []
    assert ace._feed_assist_per_ace[0] == 2


def test_single_off_rolls_back_only_with_the_cap():
    ace = _rig({'fa_stop_rollback': True})
    ace._disable_feed_assist(2)
    assert ace.retracts == [(2, 5, 80)]
    ace = _rig({'fa_stop_rollback': False})
    ace._disable_feed_assist(2)
    assert ace.retracts == []
    assert _methods(ace) == ['stop_feed_assist']


def test_slot_change_rolls_back_only_with_the_cap():
    ace = _rig({'fa_stop_rollback': True})
    ace._enable_feed_assist(1)
    assert ace.retracts == [(2, 5, 80)]
    assert ace.armed == [(0, 1)]
    ace = _rig({'fa_stop_rollback': False})
    ace._enable_feed_assist(1)
    assert ace.retracts == []
    assert ace.armed == [(0, 1)]


def test_every_codec_declares_the_cap():
    H.load_ace()
    want = {'ace_protocol_v1': True, 'ace_protocol_cfs': False,
            'ace_protocol_v2': False, 'ace_protocol_ace2k': False}
    for mod, val in want.items():
        m = importlib.import_module(H.PKG + '.' + mod)
        tables = [getattr(c, 'CAPS_STATIC') for c in vars(m).values()
                  if isinstance(c, type) and vars(c).get('CAPS_STATIC')]
        assert tables and all(t['fa_stop_rollback'] is val for t in tables), mod
