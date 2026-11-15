"""A follow that ends by itself on a lane the host still wants (ace2k:
stuck, tangled, ...) is reported: the link puts 'follow_lost' into the
slot's status, ace.py says it once on the console and forgets the slot in
its FA cache, so the next arm is sent instead of skipped as 'already
running'. Before, the link dropped the follow silently and the host kept
believing it ran (HW 2026-11-09 08:11 UTC stuck, 08:35 UTC tangled); the
unit does not freewheel, so the head got no filament.

Also here: the load retry stops when the unit blocks at the same place
again.
"""
import importlib
import sys
import types

from . import harness as H


def _link(start_ok=True):
    H.load_ace()
    L = importlib.import_module(H.PKG + '.ace_protocol_ace2k')

    class Link(L.Ace2kLink):
        def __init__(self):
            L.Ace2kLink.__init__(self, raw=None, clock=lambda: 100.0,
                                 label='0:1.3.3:1.0')
            self.cleared = []
            self.starts = []
            self.start_ok = start_ok
            self._sensors = (100.0, 0b1111)
            self._feed = (100.0, {'mode': bytes([0, 0, 8, 0])})

        def _clear_lane(self, lane, why):
            self.cleared.append((lane, why))

        def _stop_lane(self, lane):
            pass

        def _pump(self, timeout=0.0):
            pass

        def _start(self, lane, mode, length_mm, speed_mm_s, free_tip=0):
            self.starts.append((lane, mode))
            return (9, 'ok') if self.start_ok else (None, 'no_filament')

    return Link(), L


def _status_slot(link, lane):
    out = link._status(lambda r: r)
    return out['slots'][lane]


def _event(lane, kind_name, seq, L):
    kind = [k for k, v in L.KIND_NAMES.items() if v == kind_name][0]
    return {'lane': lane, 'kind': kind, 'seq': seq, 'filament_um': 0}


def test_follow_ending_stuck_is_reported_once_per_loss():
    link, L = _link()
    link._follow_want.add(2)
    link._follow[2] = 7
    link._on_event(_event(2, 'stuck', 7, L))
    assert 2 not in link._follow
    assert link.cleared == [(2, 'follow stuck')]
    slot = _status_slot(link, 2)
    assert slot['follow_lost'] == {'why': 'stuck', 'n': 1}
    # other lanes say nothing
    assert 'follow_lost' not in _status_slot(link, 0)


def test_host_stop_is_not_a_loss():
    link, L = _link()
    link._follow_want.add(2)
    link._follow[2] = 7
    link._on_event(_event(2, 'stopped', 7, L))
    assert 'follow_lost' not in _status_slot(link, 2)


def test_link_stop_restarts_and_reports_nothing():
    link, L = _link()
    link._follow_want.add(2)
    link._follow[2] = 7
    link._on_event(_event(2, 'stopped_link', 7, L))
    assert link._follow.get(2) == 9          # started again
    assert 'follow_lost' not in _status_slot(link, 2)


def test_link_stop_that_cannot_restart_is_a_loss():
    link, L = _link(start_ok=False)
    link._follow_want.add(2)
    link._follow[2] = 7
    link._on_event(_event(2, 'stopped_link', 7, L))
    assert _status_slot(link, 2)['follow_lost']['why'] == 'stopped_link'


def test_next_start_and_host_stop_clear_the_report():
    link, L = _link()
    link._follow_want.add(2)
    link._follow[2] = 7
    link._on_event(_event(2, 'tangled', 7, L))
    assert 'follow_lost' in _status_slot(link, 2)
    resp = link._motion('start_feed_assist', {'index': 2}, 1)
    assert resp['code'] == 0
    assert 'follow_lost' not in _status_slot(link, 2)
    link._follow.pop(2)
    link._on_event(_event(2, 'tangled', 9, L))      # stale seq: ignored
    link._follow[2] = 9
    link._on_event(_event(2, 'tangled', 9, L))
    assert _status_slot(link, 2)['follow_lost']['n'] == 2
    link._motion('stop_feed_assist', {'index': 2}, 2)
    assert 'follow_lost' not in _status_slot(link, 2)


def _host():
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    M = A.MultiAce

    class Ace(object):
        _note_follow_lost = M._note_follow_lost
        _maybe_pause_follow_lost = M._maybe_pause_follow_lost

        def __init__(self):
            self.warned = []
            self.paused = []
            self._follow_lost_seen = {}
            self._feed_assist_per_ace = {0: 2, 1: 2}
            self._feed_assist_index = 2
            self._active_device_index = 0
            self.printing = False
            self._fa_context = 'idle'
            self._swap_in_progress = False
            self._follow_pause_sent = set()
            self._loading_heads = set()
            self._head_source = {}
            outer = self

            class _Ext(object):
                extruder_index = 1

            class _Th(object):
                def get_extruder(self):
                    return _Ext()

            class _Reactor(object):
                NOW = 0.0
                NEVER = 1e9

                def register_timer(self, cb, when):
                    outer.paused.append(cb)

            self.toolhead = _Th()
            self.reactor = _Reactor()

        def _cap(self, idx, name):
            return name == 'no_freewheel'

        def _is_actively_printing(self):
            return self.printing

        def _t(self, key, **kw):
            return '%s %s' % (key, sorted(kw.items()))

        def _disp(self, n):
            return n + 1

        def log_warn(self, msg):
            self.warned.append(msg)

    return Ace()


def _info(lost=None):
    slots = [{'index': i} for i in range(4)]
    if lost is not None:
        slots[2]['follow_lost'] = lost
    return {'status': 'ready', 'slots': slots}


def test_host_warns_once_and_forgets_the_slot():
    ace = _host()
    ace._note_follow_lost(0, _info({'why': 'stuck', 'n': 1}))
    ace._note_follow_lost(0, _info({'why': 'stuck', 'n': 1}))   # heartbeat
    assert len(ace.warned) == 1
    assert 'msg.fa_lost_unit' in ace.warned[0]
    assert "('slot', 3)" in ace.warned[0] and "('why', 'stuck')" in ace.warned[0]
    assert ace._feed_assist_per_ace == {0: -1, 1: 2}
    assert ace._feed_assist_index == -1
    # a second loss after a re-arm is said again
    ace._note_follow_lost(0, _info())
    ace._note_follow_lost(0, _info({'why': 'tangled', 'n': 2}))
    assert len(ace.warned) == 2


def test_host_keeps_the_cache_of_another_slot():
    ace = _host()
    ace._feed_assist_per_ace[0] = 1
    ace._feed_assist_index = 1
    ace._note_follow_lost(0, _info({'why': 'stuck', 'n': 1}))
    assert ace._feed_assist_per_ace[0] == 1
    assert ace._feed_assist_index == 1
    assert len(ace.warned) == 1


def _printing_host():
    ace = _host()
    ace.printing = True
    ace._fa_context = 'print'
    ace._head_source = {1: {'ace_index': 0, 'slot': 2}}
    return ace


def test_loss_on_the_printing_lane_pauses_once():
    ace = _printing_host()
    ace._note_follow_lost(0, _info({'why': 'stuck', 'n': 1}))
    assert len(ace.paused) == 1
    ace._note_follow_lost(0, _info())
    ace._note_follow_lost(0, _info({'why': 'stuck', 'n': 2}))
    assert len(ace.paused) == 1 and len(ace.warned) == 2


def test_tail_out_warns_but_never_pauses():
    ace = _printing_host()
    ace._note_follow_lost(0, _info({'why': 'tail_out', 'n': 1}))
    assert ace.paused == []
    assert len(ace.warned) == 1 and 'msg.fa_lost_tail_out' in ace.warned[0]
    assert ace._feed_assist_per_ace[0] == -1


def test_loss_does_not_pause_outside_the_printing_lane():
    for change in ('idle', 'swap', 'other_lane', 'other_unit', 'loading'):
        ace = _printing_host()
        if change == 'idle':
            ace.printing = False
        elif change == 'swap':
            ace._swap_in_progress = True
        elif change == 'other_lane':
            ace._head_source = {1: {'ace_index': 0, 'slot': 1}}
        elif change == 'other_unit':
            ace._head_source = {1: {'ace_index': 1, 'slot': 2}}
        else:
            ace._loading_heads = {1}
        ace._note_follow_lost(0, _info({'why': 'stuck', 'n': 1}))
        assert ace.paused == [], change
        assert len(ace.warned) == 1


def test_inline_retry_rule():
    H.load_ace()
    name = H.PKG + '.pulse_counter'
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    F = importlib.import_module(H.PKG + '.filament_feed_ace')
    cls = [c for c in vars(F).values() if isinstance(c, type)
           and '_blocked_same_place' in vars(c)][0]
    obj = object.__new__(cls)
    same = obj._blocked_same_place
    assert same(('blocked', 784.0), ('blocked', 51.0), 50)
    assert same(('slip', 0.0), ('blocked', 52.0), 50)
    assert not same(('blocked', 35.0), ('blocked', 1849.0), 50)
    assert not same(('distance', 1918.0), ('blocked', 51.0), 50)
    assert not same(None, ('blocked', 51.0), 50)
    assert not same(('blocked', 784.0), None, 50)
