"""Buffer relax after a bg transport stages a lane (real method on stubs).

HW 2026-11-09 (multi mode, two ace2k units): the transport stopped MMU 1 /
Slot 3 at the toolhead sensor of the parked head 3, the lane's plunger
stood full from then on, and because the four pulled switches share one
input the unit paused the follow of the printing lane MMU 1 / Slot 1
(taut and starved 100 %) for about a minute of printing. The engine now
rolls a staged lane back until the unit reports its plunger at rest.
"""
import importlib
import os
import sys
import types


class H:
    EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                          '..', 'multiace', 'klipper', 'extras')
    PKG = 'multiace_bg_relax_extras'

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


class Reactor(object):
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class FakeAce(object):
    """send_request_to answers relax_buffer like the ace2k link; the
    slot's motion report ends after `end_after` seconds of polling."""

    def __init__(self, reactor, caps=True, result=None, code=0,
                 end_stop='at_rest', end_after=2.0, mm=12.0):
        self.reactor = reactor
        self.caps = caps
        self.result = result if result is not None else {
            'started': True, 'seq': 4}
        self.code = code
        self.end_stop = end_stop
        self.end_after = end_after
        self.mm = mm
        self.sent = []
        self.t_start = None
        self._info_per_ace = {1: {'slots': [{}, {}, {
            'motion': {'seq': 3, 'slot': 2, 'kind': 'feed', 'mm': 1050.0,
                       'running': False}}, {}]}}

    def _cap(self, idx, name):
        return self.caps if name == 'buffer_relax' else False

    def send_request_to(self, idx, request, cb):
        self.sent.append((idx, request))
        self.t_start = self.reactor.now
        cb(self=self, response={'code': self.code, 'msg': 'success'
                                if self.code == 0 else 'no_stop_when',
                                'result': self.result})

    def tick(self):
        if self.t_start is None or not self.result.get('started'):
            return
        slot = self._info_per_ace[1]['slots'][2]
        if self.reactor.now - self.t_start >= self.end_after:
            slot['motion'] = {'seq': 4, 'slot': 2, 'kind': 'retract',
                              'mm': self.mm, 'running': False,
                              'stop': self.end_stop, 'why': self.end_stop}
        else:
            slot['motion'] = {'seq': 4, 'slot': 2, 'kind': 'retract',
                              'mm': 3.0, 'running': True}


def _engine(ace):
    BG = _bg_module()

    class Bg(object):
        _relax_buffer = BG.AceBgSwap._relax_buffer
        _slot_motion = BG.AceBgSwap._slot_motion
        _ace_send = BG.AceBgSwap._ace_send
        _cap = BG.AceBgSwap._cap

        def __init__(self):
            self.reactor = ace.reactor
            self.said = []
            self.diag = []

        def _pause(self, dt):
            self.reactor.now += dt
            ace.tick()

        def _say(self, msg):
            self.said.append(msg)

        def _dh(self, i):
            return i + 1

        def _gpio_diag(self, head, where):
            self.diag.append((head, where))
    return Bg()


def test_relax_ends_at_rest():
    ace = FakeAce(Reactor())
    bg = _engine(ace)
    bg._relax_buffer(ace, 1, 2, 2)
    req = ace.sent[0][1]
    assert req['method'] == 'relax_buffer'
    assert req['params']['index'] == 2
    assert any('buffer relaxed after 12 mm (at_rest)' in m for m in bg.said)
    assert bg.diag == [(2, 'after buffer relax')]


def test_not_full_does_nothing_more():
    ace = FakeAce(Reactor(), result={'started': False, 'full': False})
    bg = _engine(ace)
    bg._relax_buffer(ace, 1, 2, 2)
    assert len(ace.sent) == 1
    assert bg.said == [] and bg.diag == []


def test_no_cap_sends_nothing():
    ace = FakeAce(Reactor(), caps=False)
    bg = _engine(ace)
    bg._relax_buffer(ace, 1, 2, 2)
    assert ace.sent == [] and bg.said == []


def test_refused_is_reported_not_raised():
    ace = FakeAce(Reactor(), code=-1, result={})
    bg = _engine(ace)
    bg._relax_buffer(ace, 1, 2, 2)
    assert any('not started (no_stop_when)' in m for m in bg.said)


def test_still_full_after_the_bound_is_said():
    ace = FakeAce(Reactor(), end_stop='distance', mm=40.0)
    bg = _engine(ace)
    bg._relax_buffer(ace, 1, 2, 2)
    assert any('still full after 40 mm back (distance)' in m
               for m in bg.said)


def test_no_end_report_times_out():
    ace = FakeAce(Reactor(), end_after=1e9)
    bg = _engine(ace)
    t0 = ace.reactor.now
    bg._relax_buffer(ace, 1, 2, 2)
    assert any('did not report an end' in m for m in bg.said)
    assert ace.reactor.now - t0 < 20.0
