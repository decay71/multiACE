"""Swap gate check after a reconnect: the real helper of ace_swap.py on
stubs (no printer).

A reconnect resets every gate to unknown until the next status frame.
HW 2026-10-23: four units went silent behind the hub, every reopen reset
the gates, and the next swap paused with "slot empty" for a full slot.
"""
import importlib

from . import harness as H


class FakeReactor:
    def __init__(self):
        self.now = 0.0
        self.on_pause = None

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = until
        if self.on_pause is not None:
            self.on_pause(self.now)


def _rig():
    H.load_ace()
    S = importlib.import_module(H.PKG + '.ace_swap')
    P = importlib.import_module(H.PKG + '.ace_protocol')

    class Ace(object):
        GATE_KNOWN_WAIT = S.SwapMixin.GATE_KNOWN_WAIT
        _await_known_gate = S.SwapMixin._await_known_gate
    ace = Ace()
    ace.reactor = FakeReactor()
    return ace, P


def test_known_gate_returns_at_once():
    ace, P = _rig()
    gates = [P.GATE_AVAILABLE, P.GATE_EMPTY]
    assert ace._await_known_gate(gates, 0) == P.GATE_AVAILABLE
    assert ace._await_known_gate(gates, 1) == P.GATE_EMPTY
    assert ace.reactor.now == 0.0


def test_unknown_gate_waits_for_status():
    ace, P = _rig()
    gates = [P.GATE_UNKNOWN]

    def status_frame(now):
        if now >= 1.0:
            gates[0] = P.GATE_AVAILABLE
    ace.reactor.on_pause = status_frame
    assert ace._await_known_gate(gates, 0) == P.GATE_AVAILABLE
    assert 1.0 <= ace.reactor.now < 1.5


def test_unknown_gate_gives_up():
    ace, P = _rig()
    gates = [P.GATE_UNKNOWN]
    assert ace._await_known_gate(gates, 0) == P.GATE_UNKNOWN
    assert ace.reactor.now >= ace.GATE_KNOWN_WAIT
