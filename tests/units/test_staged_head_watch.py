"""A head picked in a print whose filament is only staged at the toolhead
(bg transport, not gripped, no head_source) reads 'filament present', so no
runout fires and it prints air (HW 2026-11-10 07:00 UTC, manual ACE_BG_SWAP
on a head the file treated as loaded). The watch pauses once the head
really extrudes; a swap, a bg op or a load on the head owns it instead."""
import importlib

from . import harness as H


class Reactor(object):
    NEVER = 9e99
    NOW = 0.0

    def __init__(self):
        self.now = 100.0
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, cb, when):
        self.timers.append([cb, when])

    def run(self, seconds):
        end = self.now + seconds
        while True:
            due = [t for t in self.timers if t[1] <= end]
            if not due:
                break
            t = min(due, key=lambda x: x[1])
            self.now = max(self.now, t[1])
            nxt = t[0](self.now)
            if nxt >= self.NEVER:
                self.timers.remove(t)
            else:
                t[1] = nxt
        self.now = end


def _rig(staged=(1, 3), source=None, vel=2.0):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    M = A.MultiAce

    class Ext(object):
        extruder_index = 3

    class Ace(object):
        _staged_head_watch = M._staged_head_watch
        _staged_head_pause = M._staged_head_pause

        def __init__(self):
            self.reactor = Reactor()
            self.printing = True
            self.vel = vel
            self.ext = Ext()
            self.toolhead = self
            self.printer = self
            self.gcode = self
            self.scripts = []
            self.bg_busy = False
            self._bg_staged = {3: staged} if staged else {}
            self._head_source = {3: source}
            self._staged_pause_sent = set()
            self._swap_in_progress = False
            self._loading_heads = set()

        def get_extruder(self):
            return self.ext

        def _is_actively_printing(self):
            return self.printing

        def lookup_object(self, name, default=None):
            if name == 'motion_report':
                return self
            if name == 'ace_bg_swap':
                return self
            return default

        def is_busy(self, head):
            return self.bg_busy

        def get_status(self, eventtime):
            return {'live_extruder_velocity': self.vel}

        def run_script(self, script):
            self.scripts.append(script)

        def _t(self, key, **kw):
            return '%s %s' % (key, sorted(kw.items()))

        def _disp(self, n):
            return n + 1

    return Ace()


def test_staged_head_that_extrudes_is_paused_once():
    ace = _rig()
    ace._staged_head_watch()
    ace.reactor.run(3.0)
    assert ace.scripts == []                    # inside the grace
    ace.reactor.run(10.0)
    assert ace.scripts[-1] == 'PAUSE'
    assert 'msg.staged_head_print_pause' in ace.scripts[0]
    assert "('ace', 2)" in ace.scripts[0] and "('slot', 4)" in ace.scripts[0]
    n = len(ace.scripts)
    ace._staged_head_watch()
    ace.reactor.run(20.0)
    assert len(ace.scripts) == n                # once per print


def test_arrival_swap_owns_the_head():
    ace = _rig()
    ace._swap_in_progress = True
    ace._staged_head_watch()
    ace.reactor.run(8.0)
    assert ace.scripts == []
    # the swap loaded it: staged entry gone, source stamped
    ace._bg_staged.pop(3)
    ace._head_source[3] = {'ace_index': 1, 'slot': 3}
    ace._swap_in_progress = False
    ace.reactor.run(30.0)
    assert ace.scripts == []
    assert ace.reactor.timers == []


def test_no_pause_without_extrusion_or_outside_a_print():
    ace = _rig(vel=0.0)
    ace._staged_head_watch()
    ace.reactor.run(60.0)
    assert ace.scripts == []
    ace = _rig()
    ace.printing = False
    ace._staged_head_watch()
    assert ace.reactor.timers == []


def test_loaded_or_unstaged_head_is_not_watched():
    ace = _rig(source={'ace_index': 0, 'slot': 3})
    ace._staged_head_watch()
    assert ace.reactor.timers == []
    ace = _rig(staged=None)
    ace._staged_head_watch()
    assert ace.reactor.timers == []


def test_running_bg_op_on_the_head_defers_the_verdict():
    ace = _rig()
    ace.bg_busy = True
    ace._staged_head_watch()
    ace.reactor.run(20.0)
    assert ace.scripts == []
    ace.bg_busy = False
    ace.reactor.run(5.0)
    assert ace.scripts[-1] == 'PAUSE'
