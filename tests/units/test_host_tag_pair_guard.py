"""Antenna pair guard for units whose reader the host drives (ace2k): two
lanes share one antenna, and the firmware gives a tag to the lane that
moves. HW 2026-11-09 06:37 UTC: slot 3 read the Bambu chip of the spool
being hung up in bay 4 (bay 4 gate flipped 1.0 s after the read), then
slot 4 read the parked tag of slot 3 - the two spools were swapped. A read
now waits HOST_TAG_PAIR_SETTLE_S and is dropped when the partner gate
changed during it, when its UID is the partner's, or (automatic reads)
when the partner's own read was dropped. Real methods on a stub."""
import importlib
import os
import sys
import types


class H:
    EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                          '..', 'multiace', 'klipper', 'extras')
    PKG = 'multiace_pair_guard_extras'

    @staticmethod
    def load_ace():
        name = H.PKG + '.ace'
        if name in sys.modules:
            return sys.modules[name]
        pkg = types.ModuleType(H.PKG)
        pkg.__path__ = [H.EXTRAS]
        sys.modules[H.PKG] = pkg
        return importlib.import_module(name)


class Reactor(object):
    def __init__(self):
        self.now = 100.0
        self.calls = []

    def monotonic(self):
        return self.now

    def register_callback(self, cb, waketime):
        self.calls.append((waketime, cb))

    def run_until(self, t):
        self.now = t
        due = [c for c in self.calls if c[0] <= t]
        self.calls = [c for c in self.calls if c[0] > t]
        for _w, cb in sorted(due, key=lambda c: c[0]):
            cb(_w)


def _rig():
    A = H.load_ace()
    M = A.MultiAce
    names = ('_host_tag_hold', '_host_tag_pair_doubt', '_host_tag_settle',
             'HOST_TAG_PAIR_SETTLE_S', 'HOST_TAG_PAIR_PRE_S',
             'HOST_TAG_READ_WAIT_S')

    class Ace(object):
        locals().update({n: M.__dict__[n] for n in names})

        def __init__(self):
            self.reactor = Reactor()
            self._host_tag_search = {}
            self._host_tag_pending = {}
            self._host_tag_contested = set()
            self._host_tag_verdict = {}
            self._host_tag_seq = 0
            self._gate_flip_ts = {}
            self._rc_last_uid = {}
            self._gate_status_per_ace = {1: [A.GATE_AVAILABLE] * 4}
            self.taken = []
            self.warned = []

        def _host_tag_take(self, idx, slot, rec):
            self.taken.append((idx, slot, rec['uid']))
            self._rc_last_uid[(idx, slot)] = rec['uid']

        def _disp(self, i):
            return i + 1

        def _t(self, key, **kw):
            return '%s %s' % (key, sorted(kw.items()))

        def _is_actively_printing(self):
            return False

        def log_warn(self, msg):
            self.warned.append(msg)

        def flip(self, slot, t):
            self._gate_flip_ts[(1, slot)] = t
            self._host_tag_contested.discard((1, slot))

        def search(self, slot, t, manual=False):
            self._host_tag_search[(1, slot)] = (t, manual)

        def read(self, slot, t, uid):
            self.reactor.now = t
            self._host_tag_hold(1, slot, {'uid': uid, 'type': 'PLA',
                                          'fmt': 'bambu'})
    return Ace


def test_field_case_both_slots_unsure():
    """06:37 field case: slot 2 read at 44.78, slot 3 inserted at 45.80,
    slot 3 then read slot 2's parked tag. Neither read is used."""
    ace = _rig()()
    ace.flip(2, 136.77)
    ace.search(2, 136.87)
    ace.read(2, 144.78, '3A5D69F3')
    ace.flip(3, 145.80)
    ace.search(3, 146.94)
    ace.reactor.run_until(150.0)
    ace.read(3, 151.84, '53D36EF4620001')
    ace.reactor.run_until(160.0)
    assert ace.taken == []
    assert (1, 2) in ace._host_tag_contested
    assert (1, 3) in ace._host_tag_contested
    assert 'host_tag_pair_changed' in ace.warned[0]
    assert 'host_tag_pair_open' in ace.warned[1]


def test_read_without_partner_change_is_used():
    ace = _rig()()
    ace.flip(3, 50.0)
    ace.search(2, 100.0)
    ace.read(2, 108.0, 'AA')
    ace.reactor.run_until(120.0)
    assert ace.taken == [(1, 2, 'AA')]
    assert ace._host_tag_verdict[(1, 2)][1] is True


def test_partner_flip_after_settle_does_not_drop():
    ace = _rig()()
    ace.search(0, 100.0)
    ace.read(0, 110.0, 'AA')
    ace.reactor.run_until(115.0)
    ace.flip(1, 116.0)
    ace.reactor.run_until(130.0)
    assert ace.taken == [(1, 0, 'AA')]


def test_partner_uid_is_rejected():
    ace = _rig()()
    ace._rc_last_uid[(1, 1)] = 'BB'
    ace.search(0, 100.0)
    ace.read(0, 108.0, 'BB')
    ace.reactor.run_until(120.0)
    assert ace.taken == []
    assert 'host_tag_pair_same_uid' in ace.warned[0]


def test_manual_read_ignores_contested_partner():
    """The Read button is the way out of a doubted pair: it is not blocked
    by the partner's dropped read (both slots would stay '?' forever)."""
    ace = _rig()()
    ace._host_tag_contested.update({(1, 0), (1, 1)})
    ace.search(0, 100.0, manual=True)
    ace.read(0, 108.0, 'AA')
    ace.reactor.run_until(120.0)
    assert ace.taken == [(1, 0, 'AA')]
    assert (1, 0) not in ace._host_tag_contested


def test_read_without_seen_search_looks_back():
    """A session the unit opened on its own: the look-back window counts."""
    ace = _rig()()
    ace.flip(1, 95.0)
    ace.read(0, 100.0, 'AA')
    ace.reactor.run_until(120.0)
    assert ace.taken == []


def test_slot_emptied_while_waiting():
    ace = _rig()()
    ace.search(0, 100.0)
    ace.read(0, 108.0, 'AA')
    ace._gate_status_per_ace[1][0] = 0
    ace.reactor.run_until(120.0)
    assert ace.taken == [] and ace.warned == []
    assert ace._host_tag_verdict[(1, 0)][1] is False
