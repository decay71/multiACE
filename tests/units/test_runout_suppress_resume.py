"""Runout suppression across a print start: the real helper of ace.py on
stubs (no printer).

print_stats:start fires on RESUME too. HW 2026-10-22: a comms-loss pause
landed in the middle of a background unload, the RESUME cleared the
engine's suppression, and the engine's own "head empty" note at the end
of the pull paused the print as a runout on a parked head.
"""
from . import harness as H


class FakeBg:
    def __init__(self, busy=(), broken=False):
        self.busy = set(busy)
        self.broken = broken

    def is_busy(self, head):
        if self.broken:
            raise RuntimeError('boom')
        return head in self.busy


class FakePrinter:
    def __init__(self, objects):
        self.objects = objects

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


def _rig(suppressed, bg=None):
    A = H.load_ace()

    class Ace(object):
        _runout_suppress_kept = A.MultiAce._runout_suppress_kept

        def _disp(self, i):
            return i + 1
    ace = Ace()
    ace._runout_suppress_heads = set(suppressed)
    ace.printer = FakePrinter({'ace_bg_swap': bg} if bg is not None else {})
    return ace


def test_busy_bg_head_keeps_its_suppression():
    ace = _rig({0, 2}, FakeBg(busy={0}))
    assert ace._runout_suppress_kept() == {0}


def test_idle_engine_clears_everything():
    ace = _rig({0, 2}, FakeBg())
    assert ace._runout_suppress_kept() == set()


def test_no_bg_module_clears_everything():
    ace = _rig({1})
    assert ace._runout_suppress_kept() == set()


def test_busy_head_without_suppression_is_not_added():
    ace = _rig(set(), FakeBg(busy={3}))
    assert ace._runout_suppress_kept() == set()


def test_engine_error_clears():
    ace = _rig({0}, FakeBg(busy={0}, broken=True))
    assert ace._runout_suppress_kept() == set()


def test_bg_unload_lifts_suppression_for_a_head_picked_mid_retract():
    """The bulk retract of a background unload never aborts on a pick. A
    head picked in that window prints empty afterwards, and the engine's
    own suppression (step 0) swallowed the runout until the head's next
    arrival (HW 2026-10-26: manual ACE_BG_UNLOAD, pick at 09:43:29,
    runout pause only at 09:49). Before the empty mark the engine must
    lift its suppression for a head that is active and printing, so the
    mark fires the runout now. The lift sits between the BOOKKEEP state
    and the note_filament_present(False) call, guarded on the active
    extruder and a running print."""
    import ast
    import os
    path = os.path.join(H.EXTRAS, 'ace_bg_swap.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_unload_core')
    src = ast.unparse(fn)
    i_book = src.index("self._set_state(head, 'BOOKKEEP')")
    i_note = src.index('note_filament_present(False, True)', i_book)
    window = src[i_book:i_note]
    assert '_runout_suppress_heads.discard(head)' in window
    guard = next(n for n in ast.walk(fn)
                 if isinstance(n, ast.If)
                 and '_runout_suppress_heads.discard' in ast.unparse(n)
                 and 'get_extruder() is ext' in ast.unparse(n.test))
    assert '_is_actively_printing()' in ast.unparse(guard.test)


def test_bg_unload_keeps_suppression_while_the_arrival_waits():
    """A stamped bg swap's arrival T picks the head before its
    ACE_SWAP_HEAD line, which then waits in _wait_bg_op and loads right
    after the op. Lifting the suppression there fired a runout and paused
    after every arrival whose pick fell into the bulk retract (HW
    2026-10-29, six pauses in one print). The lift is skipped for a head
    in _bg_arrival_waiting."""
    import ast
    import os
    path = os.path.join(H.EXTRAS, 'ace_bg_swap.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_unload_core')
    guard = next(n for n in ast.walk(fn)
                 if isinstance(n, ast.If)
                 and '_runout_suppress_heads.discard' in ast.unparse(n)
                 and 'get_extruder() is ext' in ast.unparse(n.test))
    assert '_bg_arrival_waiting' in ast.unparse(guard.test)


def test_wait_bg_op_marks_the_waiting_head():
    import importlib
    import types
    H.load_ace()
    SW = importlib.import_module(H.PKG + '.ace_swap')
    seen = []

    class Bg:
        def __init__(self):
            self.n = 0

        def is_busy(self, head):
            self.n += 1
            seen.append(set(stub._bg_arrival_waiting))
            return self.n < 3

    class Reactor:
        t = 0.

        def monotonic(self):
            return self.t

        def pause(self, until):
            self.t = until

    bg = Bg()
    stub = types.SimpleNamespace(
        printer=types.SimpleNamespace(lookup_object=lambda *a: bg),
        reactor=Reactor(), _bg_arrival_waiting=set(),
        log_always=lambda *a: None, _disp=lambda h: h + 1,
        _rearm_fa_after_bg_wait=lambda *a: None,
        _bg_wait_park=lambda *a: None)
    SW.SwapMixin._wait_bg_op(stub, 2)
    assert seen[1] == {2}
    assert stub._bg_arrival_waiting == set()
