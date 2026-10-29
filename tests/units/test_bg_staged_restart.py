"""STAGED record from the first bg feed command on, and the stop of a
possibly still running feed when a STAGED head is restored after a
restart (real methods on stubs).

Field case (docx-tech, #172): Klipper restart during a pause while the bg
feed ran. The unload half had cleared head_source, the record was only
written at the op's end, the ACE kept feeding (the sensor stop is the
host's job), and the print start found filament with nothing recorded:
ghost, arrival swap refused.
"""
import importlib
import os
import sys
import types


class H:
    EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                          'multiace', 'klipper', 'extras')
    PKG = 'multiace_bg_staged_extras'

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


class FakeAce:
    def __init__(self):
        self._bg_staged = {}
        self._bg_left_empty = set()
        self.saves = 0

    def _save_bg_staged(self):
        self.saves += 1


def _engine():
    BG = _bg_module()

    class Bg(object):
        _mark_staged = BG.AceBgSwap._mark_staged
        _clear_staged = BG.AceBgSwap._clear_staged
    return Bg()


# -- engine side --------------------------------------------------------

def test_mark_records_and_persists():
    bg, ace = _engine(), FakeAce()
    bg._mark_staged(ace, 0, 1, 3)
    assert ace._bg_staged == {0: (1, 3)}
    assert 0 in ace._bg_left_empty
    assert ace.saves == 1


def test_clear_drops_and_persists_only_when_there_was_a_record():
    bg, ace = _engine(), FakeAce()
    bg._clear_staged(ace, 0)                 # nothing to drop, no write
    assert ace.saves == 0
    bg._mark_staged(ace, 0, 1, 3)
    bg._clear_staged(ace, 0)
    assert ace._bg_staged == {} and 0 not in ace._bg_left_empty
    assert ace.saves == 2


def test_helpers_tolerate_an_old_ace():
    bg = _engine()
    bg._mark_staged(object(), 0, 1, 3)       # no attributes at all
    bg._clear_staged(object(), 0)


def test_feed_start_marks_before_the_first_feed_command():
    """Source check: the record is written where the feed loop starts,
    i.e. before _ace_feed_to_gears can run, and cleared in bookkeeping."""
    BG = _bg_module()
    import inspect
    src = inspect.getsource(BG.AceBgSwap._load_core)
    mark = src.index('self._mark_staged(ace, head, ace_idx, slot)')
    feed = src.index('self._ace_feed_to_gears(')
    assert mark < feed
    book = inspect.getsource(BG.AceBgSwap._load_bookkeeping)
    assert 'self._clear_staged(ace, head)' in book
    assert book.index("ace._head_source[head] = _ident") \
        < book.index('self._clear_staged(ace, head)')


# -- ace.py side ----------------------------------------------------------

class FakeReactor:
    NEVER = float('inf')

    def __init__(self):
        self.now = 100.0
        self.timers = []
        self.async_cbs = []

    def monotonic(self):
        return self.now

    def register_timer(self, fn, when):
        self.timers.append((fn, when))
        return fn

    def register_async_callback(self, fn):
        self.async_cbs.append(fn)


class FakeSaveVars:
    def __init__(self, vars_):
        self.allVariables = vars_


def _ace(saved, connected):
    A = H.load_ace()

    class Ace(object):
        VARS_ACE_BG_STAGED = A.MultiAce.VARS_ACE_BG_STAGED
        _restore_bg_staged = A.MultiAce._restore_bg_staged
        _schedule_staged_feed_stop = A.MultiAce._schedule_staged_feed_stop
        _bg_staged_stop_tick = A.MultiAce._bg_staged_stop_tick
        _stop_staged_feed = A.MultiAce._stop_staged_feed

        def __init__(self):
            self.reactor = FakeReactor()
            self.save_variables = FakeSaveVars({'ace__bg_staged': saved})
            self._head_source = {}
            self._bg_staged = {}
            self._bg_left_empty = set()
            self._bg_staged_stop_pending = set()
            self._bg_staged_stop_timer = None
            self._bg_staged_stop_deadline = 0.
            self._connected_per_ace = dict(connected)
            self.stops = []

        def _stop_feeding(self, index, idx=None):
            self.stops.append((idx, index))
            return True
    return Ace(), A


def test_restore_queues_the_stop_until_the_unit_connects():
    ace, A = _ace({'1': [3, 2]}, {3: False})
    ace._restore_bg_staged()
    assert ace._bg_staged == {1: (3, 2)} and 1 in ace._bg_left_empty
    assert ace._bg_staged_stop_pending == {(3, 2)}
    assert len(ace.reactor.timers) == 1
    tick = ace.reactor.timers[0][0]
    # not connected yet: keep polling
    assert tick(101.0) == 102.0 and ace.reactor.async_cbs == []
    ace._connected_per_ace[3] = True
    assert tick(102.0) == FakeReactor.NEVER
    assert ace._bg_staged_stop_pending == set()
    assert len(ace.reactor.async_cbs) == 1
    ace.reactor.async_cbs[0](102.0)
    assert ace.stops == [(3, 2)]
    assert ace._bg_staged_stop_timer is None


def test_restore_gives_up_after_the_window():
    ace, A = _ace({'0': [0, 1]}, {0: False})
    ace._restore_bg_staged()
    tick = ace.reactor.timers[0][0]
    assert tick(100.0 + A.BG_STAGED_STOP_WINDOW) == FakeReactor.NEVER
    assert ace._bg_staged_stop_pending == set() and ace.stops == []


def test_restore_skips_heads_with_a_head_source():
    ace, A = _ace({'0': [0, 1]}, {0: True})
    ace._head_source[0] = {'ace_index': 0, 'slot': 1}
    ace._restore_bg_staged()
    assert ace._bg_staged == {} and ace._bg_staged_stop_pending == set()
    assert ace.reactor.timers == []
