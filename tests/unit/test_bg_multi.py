"""Background swaps in multi mode on lane-concurrent units (ace2k): the
engine gate, the per-lane FA cache rule, ace.py's per-lane readiness and
the preflight stamps, driven with the real methods on stubs.

In multi mode one MMU feeds several heads (slot N -> head N), so a bg op
moves a lane of a unit that may feed the printing head on another lane at
the same time. Only units with caps lanes_concurrent (ace2k) do that; a
swap moves two units (old and new slot of the head) and both must qualify.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
EXTRAS = os.path.join(ROOT, 'multiace', 'klipper', 'extras')
PP = os.path.join(ROOT, 'multiace', 'tools',
                  'post_process_virtual_toolheads.py')
CORE = os.path.join(ROOT, 'multiace', 'web', 'backend', 'preflight_core.py')
PKG = 'multiace_bg_multi_extras'


def _bg_module():
    name = PKG + '.ace_bg_swap'
    if name in sys.modules:
        return sys.modules[name]
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [EXTRAS]
        sys.modules[PKG] = pkg
    if 'chelper' not in sys.modules:
        sys.modules['chelper'] = types.ModuleType('chelper')
    fm = PKG + '.force_move'
    if fm not in sys.modules:
        stub = types.ModuleType(fm)
        stub.calc_move_time = None
        sys.modules[fm] = stub
        setattr(sys.modules[PKG], 'force_move', stub)
    return importlib.import_module(name)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BG = _bg_module()

ACE2K = 'ace2k'
V2 = 'v2'


class Ace(object):
    """ace.py side as the engine reads it."""
    def __init__(self, mode='multi', kinds=None, head_source=None,
                 cache=None):
        self._ace_mode = mode
        self.kinds = kinds or {0: ACE2K, 1: ACE2K}
        self._head_source = dict(head_source or {})
        self._feed_assist_per_ace = dict(cache or {})
        self._serial_failed_per_ace = {}
        self._reconnecting_per_ace = {}
        self._swap_in_progress = False
        self.swap_anti_ooze_retract = 10
        self.EXTRUDER_MAP = {}

    def _cap(self, idx, name):
        if name == 'lanes_concurrent':
            return self.kinds.get(idx) == ACE2K
        return False

    def head_uses_ace(self, head):
        return True

    def head_ace_for(self, head):
        return head


class Ext(object):
    def __init__(self, head):
        self.name = 'extruder' if head == 0 else 'extruder%d' % head

    def get_name(self):
        return self.name


class Toolhead(object):
    def __init__(self, active):
        self.active = active

    def get_extruder(self):
        return self.active


class Reactor(object):
    def __init__(self):
        self.cbs = []

    def monotonic(self):
        return 100.0

    def register_async_callback(self, cb):
        self.cbs.append(cb)


class Engine(object):
    cmd_ACE_BG_SWAP = BG.AceBgSwap.cmd_ACE_BG_SWAP
    cmd_ACE_BG_UNLOAD = BG.AceBgSwap.cmd_ACE_BG_UNLOAD
    _cap = BG.AceBgSwap._cap
    _multi_units_ok = BG.AceBgSwap._multi_units_ok
    _lanes_of_ops = BG.AceBgSwap._lanes_of_ops
    lanes_in_use = BG.AceBgSwap.lanes_in_use
    _finish = BG.AceBgSwap._finish
    _fa_cache_mark = BG.AceBgSwap._fa_cache_mark
    _fa_cache_clear = BG.AceBgSwap._fa_cache_clear

    def __init__(self, ace):
        self.ace = ace
        self.reactor = Reactor()
        self.exts = dict((h, Ext(h)) for h in range(4))
        self.toolhead = Toolhead(self.exts[0])  # head 0 prints
        self.enabled_heads = set([0, 1, 2, 3])
        self._busy = set()
        self._deferred = []
        self._pick_pending = set()
        self._abort_requested = set()
        self._op_start_time = {}
        self._heat_start_time = {}
        self._pull_start_time = {}
        self.state = {}
        self.swap_queue = True
        self.load_mode = 'full'
        self.said = []
        self.printer = self

    def lookup_object(self, name, default=None):
        if name == 'ace':
            return self.ace
        if name == 'toolhead':
            return self.toolhead
        if name.startswith('extruder'):
            n = name[len('extruder'):]
            return self.exts[int(n) if n else 0]
        return default

    def _ext_name(self, head):
        return 'extruder' if head == 0 else 'extruder%d' % head

    def _say(self, msg):
        self.said.append(msg)

    def _dh(self, i):
        return i + 1

    def _set_state(self, head, st):
        self.state[head] = st

    def _bg_error(self, gcmd, text, head=None):
        return RuntimeError(text)


class Cmd(object):
    def __init__(self, **kw):
        self.kw = kw

    def get_int(self, name, default=None, minval=None, maxval=None):
        return self.kw.get(name, default)

    def get_float(self, name, default=None, minval=None, maxval=None):
        return self.kw.get(name, default)

    def get(self, name, default=None):
        return self.kw.get(name, default)


def _swap(eng, **kw):
    args = dict(HEAD=1, SLOT=1, ACE=1, QUIET=1, TEMP=220., ANTI_OOZE=1.)
    args.update(kw)
    eng.cmd_ACE_BG_SWAP(Cmd(**args))


# -- engine gate ------------------------------------------------------------

def test_multi_swap_between_two_concurrent_units_queues():
    ace = Ace(head_source={1: {'ace_index': 0, 'slot': 1},
                           0: {'ace_index': 0, 'slot': 0}})
    eng = Engine(ace)
    _swap(eng)
    assert eng._busy == set([1])
    assert eng.lanes_in_use() == set([(0, 1), (1, 1)])
    assert len(eng.reactor.cbs) == 1


def test_multi_swap_touching_a_v2_is_skipped():
    ace = Ace(kinds={0: ACE2K, 1: V2},
              head_source={1: {'ace_index': 0, 'slot': 1}})
    eng = Engine(ace)
    _swap(eng)
    assert eng._busy == set()
    assert any('lane-concurrent' in s and 'MMU 2' in s for s in eng.said)


def test_multi_swap_needs_ace_and_the_head_slot():
    ace = Ace(head_source={1: {'ace_index': 0, 'slot': 1}})
    eng = Engine(ace)
    _swap(eng, ACE=-1)
    _swap(eng, SLOT=2)
    assert eng._busy == set()
    assert sum('needs ACE= and SLOT=1' in s for s in eng.said) == 2


def test_swap_onto_a_head_with_another_slot_staged_is_refused():
    # HW 2026-11-10: a manual swap back onto MMU 1 while the strand of
    # MMU 2 sat staged at the toolhead was reported as a 'stale latch'.
    ace = Ace(head_source={0: {'ace_index': 0, 'slot': 0}})
    ace._bg_staged = {1: (1, 1)}
    eng = Engine(ace)
    _swap(eng, ACE=0)
    assert eng._busy == set()
    assert any('has MMU 2 slot 2 staged' in s and 'unload head 2 first' in s
               for s in eng.said)


def test_swap_onto_the_staged_slot_has_nothing_to_do():
    ace = Ace(head_source={0: {'ace_index': 0, 'slot': 0}})
    ace._bg_staged = {1: (1, 1)}
    eng = Engine(ace)
    _swap(eng, ACE=1)
    assert eng._busy == set()
    assert any('already has MMU 2 slot 2 staged' in s for s in eng.said)


def test_multi_unload_on_a_v2_is_skipped():
    ace = Ace(kinds={0: V2, 1: ACE2K},
              head_source={1: {'ace_index': 0, 'slot': 1}})
    eng = Engine(ace)
    eng.cmd_ACE_BG_UNLOAD(Cmd(HEAD=1, QUIET=1, TEMP=220.))
    assert eng._busy == set()
    assert any('lane-concurrent' in s for s in eng.said)


def test_head_mode_keeps_the_printing_unit_check():
    # head mode: the bg unit must not feed the printing head (1:1 wiring),
    # whatever the caps say.
    ace = Ace(mode='head', head_source={1: {'ace_index': 1, 'slot': 2},
                                        0: {'ace_index': 1, 'slot': 0}})
    eng = Engine(ace)
    _swap(eng, SLOT=3)
    assert eng._busy == set()
    assert any('also feeds the printing head' in s for s in eng.said)


def test_finish_releases_the_lanes():
    ace = Ace(head_source={1: {'ace_index': 0, 'slot': 1}})
    eng = Engine(ace)
    _swap(eng)
    eng._finish(1)
    assert eng.lanes_in_use() == set()


# -- FA cache ---------------------------------------------------------------

def test_concurrent_unit_keeps_the_printing_lane_in_the_cache():
    ace = Ace(cache={0: 0})            # head 0 follows on lane 0
    eng = Engine(ace)
    eng._fa_cache_mark(ace, 0, 1)      # bg forward assist on lane 1
    assert ace._feed_assist_per_ace[0] == 0
    eng._fa_cache_clear(ace, 0, 1)
    assert ace._feed_assist_per_ace[0] == 0
    eng._fa_cache_clear(ace, 0, 0)     # the bg lane's own entry goes
    assert ace._feed_assist_per_ace[0] == -1


def test_one_lane_unit_keeps_the_old_cache_rule():
    ace = Ace(kinds={0: V2}, cache={0: 2})
    eng = Engine(ace)
    eng._fa_cache_mark(ace, 0, 1)
    assert ace._feed_assist_per_ace[0] == 1
    eng._fa_cache_clear(ace, 0, 3)
    assert ace._feed_assist_per_ace[0] == -1


# -- ace.py per-lane readiness ----------------------------------------------

def _ace_module():
    sys.path.insert(0, os.path.join(ROOT, 'tests'))
    try:
        from units import harness as H
    finally:
        sys.path.pop(0)
    return importlib.import_module(H.load_ace().__name__)


class _Bg(object):
    def __init__(self, lanes):
        self.lanes = lanes

    def lanes_in_use(self):
        return set(self.lanes)


class _Printer(object):
    def __init__(self, bg):
        self.bg = bg

    def lookup_object(self, name, default=None):
        return self.bg if name == 'ace_bg_swap' else default


def _ready(kind, info, lanes):
    mod = _ace_module()

    class A(object):
        _bg_lanes_on = mod.MultiAce._bg_lanes_on
        _host_unit_ready = mod.MultiAce._host_unit_ready

        def _cap(self, idx, name):
            return name == 'lanes_concurrent' and kind == ACE2K
    a = A()
    a.printer = _Printer(_Bg(lanes))
    return a._host_unit_ready(0, info)


def test_bg_lanes_do_not_hold_up_a_concurrent_unit():
    info = {'status': 'busy', 'busy_lanes': [1]}
    assert _ready(ACE2K, info, [(0, 1)])
    assert not _ready(ACE2K, info, [(1, 1)])     # bg on another unit
    assert not _ready(ACE2K, {'status': 'busy', 'busy_lanes': [1, 2]},
                      [(0, 1)])                # lane 2 is the host's move


def test_other_units_wait_for_ready_as_before():
    info = {'status': 'busy', 'busy_lanes': [1]}
    assert not _ready(V2, info, [(0, 1)])
    assert _ready(V2, {'status': 'ready'}, [])
    assert not _ready(ACE2K, {'status': 'busy'}, [(0, 1)])  # no lane list


# -- ace2k link status ------------------------------------------------------

def test_link_status_names_busy_lanes_and_per_lane_motion():
    sys.path.insert(0, os.path.join(ROOT, 'tests'))
    try:
        from units import harness as H
    finally:
        sys.path.pop(0)
    H.load_ace()
    L = importlib.import_module(H.PKG + '.ace_protocol_ace2k')
    link = L.Ace2kLink.__new__(L.Ace2kLink)
    link._sensors = (0.0, 0b0110)
    link._feed = (0.0, {'mode': bytes([0, 2, 0, 0])})
    link._env = None
    link._rfid = None
    link._tag_uid = [b'', b'', b'', b'']
    link._tag_rec = [None] * 4
    link._tag_reads = {}
    link._tag_search_n = [0] * 4
    link._tag_search_end = [None] * 4
    link._moved_mm = [0.0] * 4
    link._follow_lost = {}
    link._jobs = {1: {'kind': 'retract', 'slot': 1, 'target': 500.0,
                      'done': 120.0, 'mseq': 7}}
    link._last_motion = None
    link._lane_motion = [None, None,
                         {'seq': 5, 'slot': 2, 'kind': 'feed', 'mm': 30.0,
                          'running': False, 'why': 'done'}, None]
    link._fresh = lambda rep: rep is not None
    out = link._status(lambda d: d)
    assert out['status'] == 'busy'
    assert out['busy_lanes'] == [1]
    assert out['slots'][1]['motion']['running'] is True
    assert out['slots'][2]['motion']['seq'] == 5
    assert 'motion' not in out['slots'][0]


# -- preflight stamps -------------------------------------------------------

PP_MOD = _load(PP, 'multiace_bg_multi_pp')


def _gcode(tools):
    lines = ['; HEADER_BLOCK_START', '; filament_colour = '
             + ';'.join(['#FF0000', '#00FF00', '#0000FF', '#FFFFFF']),
             '; HEADER_BLOCK_END', 'T0', ';LAYER_CHANGE', ';Z:0.2']
    r = 60
    for t in tools:
        lines.append('M73 P0 R%d' % r)
        lines.append('; Change Tool 0 -> Tool %d' % t)
        lines.append('T%d' % t)
        lines.append('G1 X10 Y10 E1 F1200')
        r -= 5
    return '\n'.join(lines) + '\n'


def _multi_rewrite(tools, assignment, bg_units):
    d = tempfile.mkdtemp()
    src = os.path.join(d, 'in.gcode')
    dst = os.path.join(d, 'out.gcode')
    with open(src, 'w') as f:
        f.write(_gcode(tools))
    PP_MOD.rewrite_head_mode_to_file(src, dst, assignment, None,
                                     bg_heads=[0, 1], bg_stamps=True,
                                     bg_units=bg_units)
    return [l for l in open(dst).read().splitlines()
            if 'ACE_BG_SWAP' in l or 'bg-swap' in l]


# Multi topology: head 0 prints T0 (MMU 1 slot 0) and T2 (MMU 2 slot 0),
# head 1 prints T1 (MMU 1 slot 1). Each return of head 0 changes units.
ASSIGN = {
    0: {'kind': 'ace', 'head': 0, 'ace': 0, 'slot': 0},
    1: {'kind': 'ace', 'head': 1, 'ace': 0, 'slot': 1},
    2: {'kind': 'ace', 'head': 0, 'ace': 1, 'slot': 0},
}
TOOLS = [0, 1, 2, 1, 0, 1]


def test_multi_stamps_between_concurrent_units():
    lines = _multi_rewrite(TOOLS, ASSIGN, [0, 1])
    stamps = [l for l in lines if l.startswith('ACE_BG_SWAP')]
    assert stamps and all('HEAD=0' in l for l in stamps)
    assert all('SLOT=0' in l for l in stamps)


def test_multi_skips_a_swap_on_a_unit_without_lanes():
    lines = _multi_rewrite(TOOLS, ASSIGN, [0])
    assert not [l for l in lines if l.startswith('ACE_BG_SWAP')]
    assert any('not lane-concurrent' in l for l in lines)


def test_head_mode_rewrite_unchanged_without_units():
    a = _multi_rewrite(TOOLS, ASSIGN, None)
    assert [l for l in a if l.startswith('ACE_BG_SWAP')]


def test_bg_stats_count_a_unit_swap_as_disabled():
    ev = TOOLS
    st_all = PP_MOD.head_mode_bg_stats(ev, ASSIGN, bg_heads=[0, 1],
                                       bg_units=[0, 1])
    st_one = PP_MOD.head_mode_bg_stats(ev, ASSIGN, bg_heads=[0, 1],
                                       bg_units=[0])
    assert st_all['unloads'] == st_one['unloads'] > 0
    assert st_all['bg_disabled'] == 0
    assert st_one['bg_disabled'] == st_one['unloads']


# -- preflight context ------------------------------------------------------

def _core():
    backend = os.path.join(ROOT, 'multiace', 'web', 'backend')
    sys.path.insert(0, backend)
    try:
        return _load(CORE, 'multiace_bg_multi_core')
    finally:
        sys.path.pop(0)


def _ctx(**kw):
    ctx = {'mode': 'multi', 'bg_available': True, 'bg_heads': [0, 1],
           'concurrent_aces': [0, 1], 'connected_aces': [0, 1]}
    ctx.update(kw)
    return ctx


def test_multi_bg_needs_two_concurrent_units():
    C = _core()
    assert C.multi_bg_possible(_ctx())
    assert not C.multi_bg_possible(_ctx(concurrent_aces=[0]))
    assert not C.multi_bg_possible(_ctx(connected_aces=[0]))
    assert not C.multi_bg_possible(_ctx(bg_heads=[]))
    assert C.bg_heads_of(_ctx()) == [0, 1]
    assert C.bg_heads_of(_ctx(concurrent_aces=[1])) == []


def test_multi_bg_routes_through_the_assignment_pipeline():
    C = _core()
    assert C.multi_assignment_shape(_ctx())
    # every multi print takes the assignment pipeline now
    assert C.multi_assignment_shape(_ctx(concurrent_aces=[]))
    C.MULTI_CLASSIC_PIPELINE = True
    try:
        assert C.multi_assignment_shape(_ctx())
        assert not C.multi_assignment_shape(_ctx(concurrent_aces=[]))
    finally:
        C.MULTI_CLASSIC_PIPELINE = False


def test_head_mode_has_no_unit_limit():
    C = _core()
    head = {'mode': 'head', 'bg_heads': [2], 'bg_available': True}
    assert C.multi_bg_units(head) is None
    assert C.bg_heads_of(head) == [2]
