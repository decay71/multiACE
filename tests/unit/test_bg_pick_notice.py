"""Toolchange notice to the bg engine (ace.py T0-T3 wrapper ->
ace_bg_swap.request_pick_abort), driven with the real methods on stubs.

The field case (docx-tech, full bg swap): the stock T macro lifts the
bg head out of the dock and activates its extruder ~2.4 s later; the
engine's _check_docked only sees the activation, so a prime still
running kept extruding while the head was carried to the wipe tower
(blob in front of it, no anti-ooze cushion). The notice arrives BEFORE
the macro: the engine stops at its next move boundary while the head is
still docked, the T waits for the handover.
"""
import importlib
import os
import sys
import types

import pytest


class H:
    EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                          'multiace', 'klipper', 'extras')
    PKG = 'multiace_bg_pick_extras'

    @staticmethod
    def load_ace():
        name = H.PKG + '.ace'
        if name in sys.modules:
            return sys.modules[name]
        pkg = types.ModuleType(H.PKG)
        pkg.__path__ = [H.EXTRAS]
        sys.modules[H.PKG] = pkg
        return importlib.import_module(name)


def _bg_module():
    name = H.PKG + '.ace_bg_swap'
    if name in sys.modules:
        return sys.modules[name]
    H.load_ace()
    if 'chelper' not in sys.modules:
        sys.modules['chelper'] = types.ModuleType('chelper')
    fm = H.PKG + '.force_move'
    if fm not in sys.modules:
        stub = types.ModuleType(fm)
        stub.calc_move_time = None
        sys.modules[fm] = stub
        setattr(sys.modules[H.PKG], 'force_move', stub)
    return importlib.import_module(name)


class FakeReactor:
    def __init__(self):
        self.now = 100.0
        self.on_pause = None

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = max(self.now, until)
        if self.on_pause is not None:
            self.on_pause(self)


class FakeToolhead:
    def __init__(self, ext=None):
        self.ext = ext

    def get_extruder(self):
        return self.ext


def _engine(phase=None, busy=True):
    BG = _bg_module()

    class Bg(object):
        request_pick_abort = BG.AceBgSwap.request_pick_abort
        _check_docked = BG.AceBgSwap._check_docked
        is_busy = BG.AceBgSwap.is_busy

        def __init__(self):
            self.said = []
            self._busy = set()
            self._pick_pending = set()
            self.state = {}

        def _say(self, msg):
            self.said.append(msg)

        def _dh(self, i):
            return i + 1
    bg = Bg()
    if busy:
        bg._busy.add(0)
        bg.state[0] = phase
    return bg


def _ace(bg):
    A = H.load_ace()

    class Printer:
        def lookup_object(self, name, default=None):
            return bg if name == 'ace_bg_swap' else default

    class Ace(object):
        _notify_bg_pick = A.MultiAce._notify_bg_pick

        def __init__(self):
            self.printer = Printer()
            self.reactor = FakeReactor()
            self.said = []

        def _disp(self, i):
            return i + 1

        def log_always(self, msg):
            self.said.append(msg)
    return Ace(), A


# -- engine side --------------------------------------------------------

def test_notice_in_prime_marks_pending_and_check_docked_aborts():
    bg = _engine('LD_PRIME')
    assert bg.request_pick_abort(0) is True
    assert 0 in bg._pick_pending
    assert any('toolchange announced during LD_PRIME' in s for s in bg.said)
    ext = object()
    with pytest.raises(RuntimeError) as ei:
        bg._check_docked(FakeToolhead(None), ext, 0)   # still docked
    assert 'before the toolchange' in str(ei.value)
    # a second notice for the same op says nothing new
    n = len(bg.said)
    assert bg.request_pick_abort(0) is True
    assert len(bg.said) == n


@pytest.mark.parametrize('phase', ['HEAT', 'PULL', 'LD_FEED', 'LD_GRIP'])
def test_notice_in_every_polling_phase(phase):
    bg = _engine(phase)
    assert bg.request_pick_abort(0) is True


@pytest.mark.parametrize('phase', ['QUEUED', 'FA_STOP', 'RETRACT', 'BOOKKEEP',
                                   'DONE'])
def test_notice_outside_polling_phases_is_ignored(phase):
    bg = _engine(phase)
    assert bg.request_pick_abort(0) is False
    assert 0 not in bg._pick_pending
    bg._check_docked(FakeToolhead(None), object(), 0)   # no raise


def test_notice_on_idle_head_is_a_noop():
    bg = _engine(busy=False)
    bg._pick_pending.add(0)                           # stale leftover
    assert bg.request_pick_abort(0) is False
    assert 0 not in bg._pick_pending


def test_real_pick_still_aborts_without_notice():
    bg = _engine('LD_PRIME')
    ext = object()
    with pytest.raises(RuntimeError) as ei:
        bg._check_docked(FakeToolhead(ext), ext, 0)
    assert 'PICKED mid-sequence' in str(ei.value)


# -- ace.py side ----------------------------------------------------------

def test_t_wrapper_waits_for_the_handover():
    bg = _engine('LD_PRIME')
    ace, A = _ace(bg)

    def handover(reactor):
        # the engine finishes its chunk and hands over 1.3 s later
        if reactor.now >= 101.3:
            bg._busy.discard(0)
            bg._pick_pending.discard(0)
    ace.reactor.on_pause = handover
    ace._notify_bg_pick(0)
    assert not bg.is_busy(0)
    assert ace.reactor.now < 100.0 + A.BG_PICK_NOTICE_WAIT
    assert ace.said == []                             # no timeout line


def test_t_wrapper_gives_up_after_the_budget():
    bg = _engine('PULL')
    ace, A = _ace(bg)
    ace._notify_bg_pick(0)
    assert bg.is_busy(0)
    assert ace.reactor.now >= 100.0 + A.BG_PICK_NOTICE_WAIT
    assert any('did not hand over' in s for s in ace.said)


def test_t_wrapper_does_not_wait_during_bulk_retract():
    bg = _engine('RETRACT')
    ace, A = _ace(bg)
    ace._notify_bg_pick(0)
    assert ace.reactor.now == 100.0
    assert ace.said == []


def test_t_wrapper_without_engine_or_old_engine():
    class OldBg:
        def is_busy(self, head):
            return True
    ace, A = _ace(OldBg())
    ace._notify_bg_pick(0)                            # no request_pick_abort
    ace2, _ = _ace(None)
    ace2._notify_bg_pick(0)
    assert ace.reactor.now == 100.0 and ace2.reactor.now == 100.0
