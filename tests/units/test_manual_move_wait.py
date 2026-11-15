"""ACE_FEED / ACE_RETRACT WAIT=0: the real _feed/_retract of ace.py on
stubs. WAIT=0 sends the move and returns - no wait for the unit's ready,
no dwell (the dwell holds the gcode queue, so a print stopped while the
command waited, HW 2026-11-08 16:12). WAIT=1, the default, is unchanged.
"""
import importlib

import pytest

from . import harness as H


class Refused(Exception):
    pass


def _rig():
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    M = A.MultiAce

    class Ace(object):
        _feed = M._feed
        _retract = M._retract
        _manual_move_wait = M._manual_move_wait
        _tipform_send = M._tipform_send
        _retract_send_wait = M._retract_send_wait

        def _retract_pending_mark(self, idx, slot, length, head=None):
            return None

        def _retract_pending_clear(self, key):
            pass
        _tipform_rejected = M._tipform_rejected

        def __init__(self, replies=None):
            self.calls = []
            # Replies to unwind_filament, in order; the last one repeats.
            # None = the unit does not answer.
            self.replies = list(replies if replies is not None
                                else [{'code': 0, 'msg': 'success'}])
            self.reactor = _Reactor()
            self._active_device_index = 0
            self._feed_assist_per_ace = {}
            self._feed_assist_index = -1

        def _t(self, key, **kw):
            return key

        def log_error(self, msg):
            self.calls.append(('error', msg))

        def head_is_manual(self, head):
            return False

        def _cap(self, idx, name):
            return False

        def _fa_trace(self, msg):
            pass

        def wait_ace_ready_on(self, idx):
            self.calls.append(('wait', idx))

        def send_request_to(self, idx, request, callback):
            if request['method'] == 'stop_feed_assist':
                self.calls.append(('prestop', idx))
                return
            self.calls.append(('send', idx, request['method'],
                               request['params']['length']))
            if request['method'] == 'unwind_filament':
                reply = (self.replies.pop(0) if len(self.replies) > 1
                         else self.replies[0])
                if reply is not None:
                    callback(self=self, response=reply)

        def dwell(self, delay=1.0):
            self.calls.append(('dwell', round(delay, 2)))

        def _ace_error(self, gcmd, text, code=0):
            return Refused(text)
    return Ace


class _Reactor(object):
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = max(self.now, until) + 0.001


@pytest.mark.parametrize('fn', ['_feed', '_retract'])
def test_wait_default_waits_and_dwells(fn):
    ace = _rig()()
    getattr(ace, fn)(1, 400, 80, ace=1)
    method = 'feed_filament' if fn == '_feed' else 'unwind_filament'
    assert ace.calls == [('wait', 1), ('send', 1, method, 400),
                         ('dwell', 5.1)]


@pytest.mark.parametrize('fn', ['_feed', '_retract'])
def test_wait0_sends_and_returns(fn):
    ace = _rig()()
    getattr(ace, fn)(1, 400, 80, ace=1, wait=False)
    method = 'feed_filament' if fn == '_feed' else 'unwind_filament'
    assert ace.calls == [('send', 1, method, 400)]


class _Gcmd(object):
    def __init__(self, **params):
        self.params = params

    def get_int(self, name, default=None):
        return int(self.params.get(name, default))


@pytest.mark.parametrize('params,expect', [({}, True), ({'WAIT': 1}, True),
                                           ({'WAIT': 0}, False)])
def test_wait_param(params, expect):
    assert _rig()()._manual_move_wait(_Gcmd(**params)) is expect


def test_wait_param_refuses_other_values():
    with pytest.raises(Refused):
        _rig()()._manual_move_wait(_Gcmd(WAIT=2))


REFUSED = {'code': 1, 'msg': 'error_2'}
OK = {'code': 0, 'msg': 'success'}


def test_refused_unwind_is_sent_again():
    ace = _rig()([REFUSED, REFUSED, OK])
    ace._retract(1, 150, 80, ace=1)
    sends = [c for c in ace.calls if c[0] == 'send']
    assert len(sends) == 3
    assert ace.calls[-1] == ('dwell', 1.98)
    assert not [c for c in ace.calls if c[0] == 'error']


def test_forbidden_counts_as_refused():
    ace = _rig()([{'code': 0, 'msg': 'FORBIDDEN'}, OK])
    ace._retract(1, 150, 80, ace=1)
    assert len([c for c in ace.calls if c[0] == 'send']) == 2


def test_unwind_refused_every_time_reports_and_does_not_dwell():
    ace = _rig()([REFUSED])
    ace._retract(1, 150, 80, ace=1)
    assert len([c for c in ace.calls if c[0] == 'send']) == 4
    assert [c for c in ace.calls if c[0] == 'error']
    assert not [c for c in ace.calls if c[0] == 'dwell']


def test_no_reply_is_taken_as_accepted():
    ace = _rig()([None])
    ace._retract(1, 150, 80, ace=1)
    assert len([c for c in ace.calls if c[0] == 'send']) == 1
    assert ace.calls[-1] == ('dwell', 1.98)
