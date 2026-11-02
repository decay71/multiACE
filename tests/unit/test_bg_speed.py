"""Background swap speed-ups: chained stealth moves, the early-start and
shortened cold-pull table, the per-swap load depth (LOAD=) and the held
swap, driven with the real engine methods on stubs, plus the preflight
stamp that picks the load depth from the parked window."""
import importlib
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
EXTRAS = os.path.join(ROOT, 'multiace', 'klipper', 'extras')
PP = os.path.join(ROOT, 'multiace', 'tools',
                  'post_process_virtual_toolheads.py')
PKG = 'multiace_bg_speed_extras'


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


BG = _bg_module()


class Clock(object):
    """Reactor + MCU clock in one: pause() advances time."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = max(self.now, until)

    def estimated_print_time(self, t):
        return t


class FakeToolhead(object):
    def __init__(self, clk):
        self.mcu = clk


# -- helpers --------------------------------------------------------------

class TestLoadMode(object):
    def test_parse(self):
        assert BG.parse_load_mode('transport') == 'transport'
        assert BG.parse_load_mode(' FULL ') == 'full'
        assert BG.parse_load_mode('1') == 'full'
        assert BG.parse_load_mode('none') == 'none'
        assert BG.parse_load_mode('') is None
        assert BG.parse_load_mode('banana') is None

    def test_a_stamp_only_lowers_the_configured_mode(self):
        e = BG.effective_load_mode
        assert e('full', 'transport') == 'transport'
        assert e('full', 'none') == 'none'
        assert e('full', None) == 'full'
        # a dock that cannot take the prime is never upgraded by a file
        assert e('none', 'full') == 'none'
        assert e('transport', 'full') == 'transport'
        assert e('transport', 'none') == 'none'


class TestPushShortened(object):
    TABLE = [('move', 57., 400.), ('move', 3., 1500.), ('move', -27., 2700.)]

    def test_off_by_default(self):
        assert BG.push_shortened(self.TABLE, 0) is self.TABLE

    def test_cuts_only_the_opening_push(self):
        out = BG.push_shortened(self.TABLE, 35)
        assert out[0] == ('move', 35., 400.)
        assert out[1:] == self.TABLE[1:]

    def test_never_lengthens_and_skips_other_openers(self):
        assert BG.push_shortened(self.TABLE, 57) == self.TABLE
        assert BG.push_shortened([('move', 20., 400.)], 35) == [
            ('move', 20., 400.)]
        assert BG.push_shortened([('pause', 100)], 35) == [('pause', 100)]
        assert BG.push_shortened([], 35) == []

    def test_the_stock_tables_open_with_the_push(self):
        assert BG.COLD_PULL_NORMAL[0] == (57.0, 400.)
        assert BG.COLD_PULL_SOFT[0][0] > 0


def test_the_speed_knobs_ship_off():
    """The faster prime and the early pull start change what the hardware
    does: they stay opt-in until they have run on a printer."""
    assert BG.BG_PULL_EARLY_START_C == 0
    assert BG.BG_LOAD_PRIME_SPEED_DEFAULT == BG.BG_LOAD_PRIME_SPEED == 4.


def test_chaining_alone_is_already_shorter_than_the_old_engine():
    """The shipped defaults still chain the moves (no settle gap per move,
    no fixed pause per retract): the old engine takes 131 s in the
    calibrated simulation, this ~108 s; with both opt-ins ~96 s."""
    off = _simulate()
    fast = _simulate(**FAST)
    assert off[3] < 112
    assert fast[3] < off[3] - 10


# -- chained moves --------------------------------------------------------

class Engine(object):
    _wait_lead = BG.AceBgSwap._wait_lead
    _wait_move = BG.AceBgSwap._wait_move

    def __init__(self, clk):
        self.reactor = clk

    def _pause(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)


def test_wait_lead_returns_one_lead_before_the_end():
    clk = Clock()
    eng = Engine(clk)
    th = FakeToolhead(clk)
    end = clk.now + 5.0
    eng._wait_lead(th, end)
    assert end - BG.BG_CHAIN_LEAD - 0.06 <= clk.now <= end - BG.BG_CHAIN_LEAD + 0.06


def test_the_lead_beats_the_settle_gap_it_replaces():
    """Per move: old = wait(end + MOVE_SETTLE) then SCHEDULE_DELAY + EPS
    before the next starts; new = queue at end - LEAD, chained on the end."""
    old_gap = BG.MOVE_SETTLE + BG.SCHEDULE_DELAY + BG.SCHEDULE_EPS
    new_gap = BG.SCHEDULE_EPS
    assert old_gap > 0.6 and new_gap < 0.1
    # the lead must exceed the scheduling delay, else the next move would
    # start from "now" instead of from the previous end
    assert BG.BG_CHAIN_LEAD > BG.SCHEDULE_DELAY


def test_prime_time_for_a_142mm_prime():
    chunks = 142 / BG.BG_LOAD_PRIME_CHUNK
    old = 142 / BG.BG_LOAD_PRIME_SPEED + chunks * (
        BG.MOVE_SETTLE + BG.SCHEDULE_DELAY + BG.SCHEDULE_EPS)
    new = 142 / FAST['prime_speed'] + chunks * BG.SCHEDULE_EPS \
        + BG.MOVE_SETTLE
    assert old > 50 and new < 28


# -- held swaps -----------------------------------------------------------

class Held(object):
    cmd_ACE_BG_SWAP = BG.AceBgSwap.cmd_ACE_BG_SWAP
    _finish = BG.AceBgSwap._finish
    _defer_swap = BG.AceBgSwap._defer_swap
    _drop_deferred = BG.AceBgSwap._drop_deferred
    _run_deferred = BG.AceBgSwap._run_deferred
    request_pick_abort = BG.AceBgSwap.request_pick_abort

    def __init__(self, clk, queue=True):
        self.reactor = clk
        self.reactor.register_async_callback = self._async
        self._deferred = []
        self._busy = set([2])
        self._pick_pending = set()
        self.state = {}
        self.enabled_heads = set([1, 2])
        self.load_mode = 'full'
        self.swap_queue = queue
        self.said = []
        self.replayed = []
        self.printing = True
        self.printer = self

    def _async(self, cb):
        self.cb = cb

    def lookup_object(self, name, default=None):
        return object() if name == 'ace' else default

    def _say(self, msg):
        self.said.append(msg)

    def _dh(self, i):
        return i + 1

    def _print_active(self):
        return self.printing

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


def _stamp(**extra):
    kw = dict(HEAD=1, SLOT=3, ACE=1, QUIET=1, ANTI_OOZE=10.)
    kw.update(extra)
    return Cmd(**kw)


def test_a_stamped_swap_behind_a_running_op_is_held():
    eng = Held(Clock())
    eng.cmd_ACE_BG_SWAP(_stamp(LOAD='transport'))
    assert len(eng._deferred) == 1
    d = eng._deferred[0]
    assert (d['HEAD'], d['SLOT'], d['ACE'], d['LOAD']) == (1, 3, 1, 'transport')
    assert any('held' in s for s in eng.said)


def test_a_second_stamp_for_the_same_head_replaces_the_first():
    eng = Held(Clock())
    eng.cmd_ACE_BG_SWAP(_stamp(SLOT=2))
    eng.cmd_ACE_BG_SWAP(_stamp(SLOT=3))
    assert [d['SLOT'] for d in eng._deferred] == [3]


def test_not_held_when_the_queue_is_off_or_the_call_is_manual():
    eng = Held(Clock(), queue=False)
    eng.cmd_ACE_BG_SWAP(_stamp())
    assert eng._deferred == []
    eng = Held(Clock())
    with pytest.raises(RuntimeError):
        eng.cmd_ACE_BG_SWAP(_stamp(QUIET=0))
    assert eng._deferred == []


def test_the_end_of_an_op_starts_the_held_swap():
    clk = Clock()
    eng = Held(clk)
    eng.cmd_ACE_BG_SWAP(_stamp())
    eng._finish(2)
    assert eng._busy == set()
    assert eng.cb is not None
    seen = []
    eng.cmd_ACE_BG_SWAP = lambda g: seen.append(
        (g.get_int('HEAD'), g.get_int('SLOT'), g.get('LOAD', '')))
    eng.cb(0)
    assert seen == [(1, 3, None)] or seen == [(1, 3, '')]
    assert eng._deferred == []


def test_a_stale_held_swap_is_dropped_not_started():
    clk = Clock()
    eng = Held(clk)
    eng.cmd_ACE_BG_SWAP(_stamp())
    clk.now += BG.BG_DEFER_MAX_AGE_S + 1
    eng._busy.clear()
    seen = []
    eng.cmd_ACE_BG_SWAP = lambda g: seen.append(g)
    eng._run_deferred()
    assert seen == [] and eng._deferred == []
    assert any('stale' in s for s in eng.said)


def test_no_held_swap_once_the_print_is_over():
    eng = Held(Clock())
    eng.cmd_ACE_BG_SWAP(_stamp())
    eng._busy.clear()
    eng.printing = False
    seen = []
    eng.cmd_ACE_BG_SWAP = lambda g: seen.append(g)
    eng._run_deferred()
    assert seen == []


def test_the_pick_notice_drops_a_held_swap_for_that_head():
    eng = Held(Clock())
    eng.cmd_ACE_BG_SWAP(_stamp())
    assert eng.request_pick_abort(1) is False       # head 1 runs nothing
    assert eng._deferred == []
    assert any('held swap dropped' in s for s in eng.said)


def test_replay_serves_the_stored_parameters():
    cmd = BG._ReplayCmd({'HEAD': 1, 'SLOT': 3, 'ACE': -1, 'TEMP': 0.,
                         'PURGE': None, 'LOAD': 'transport'})
    assert cmd.get_int('HEAD', minval=0) == 1
    assert cmd.get_int('ACE', -1) == -1
    assert cmd.get_float('PURGE', None) is None
    assert cmd.get_float('TEMP', 0.) == 0.
    assert cmd.get('LOAD', '') == 'transport'
    assert cmd.get('MISSING', 'x') == 'x'


# -- preflight stamp ------------------------------------------------------

@pytest.fixture(scope='module')
def pp():
    spec = importlib.util.spec_from_file_location('pp_bg_speed', PP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ace(head, ace, slot):
    return {'kind': 'ace', 'head': head, 'ace': ace, 'slot': slot}


def _stamps(pp, tmp_path, window_min):
    """T1 on head 1 (slot 0), T2 on head 2, T3 back on head 1 with another
    slot: head 1 is parked for `window_min` minutes in between."""
    src = tmp_path / 'in.gcode'
    r_t2 = 50
    r_t3 = r_t2 - window_min
    src.write_text(
        '; generated by OrcaSlicer\n'
        'T0\n'
        '; Change Tool 0 -> Tool 1\n'
        'M73 P5 R60\n'
        'T1\n'
        'G1 X1 Y1 E1\n'
        '; Change Tool 1 -> Tool 2\n'
        'M73 P10 R%d\n'
        'T2\n'
        'G1 X2 Y2 E1\n'
        '; Change Tool 2 -> Tool 3\n'
        'M73 P20 R%d\n'
        'T3\n'
        'G1 X3 Y3 E1\n' % (r_t2, r_t3))
    out = tmp_path / 'out.gcode'
    assignment = {0: _ace(0, 0, 0), 1: _ace(1, 1, 0), 2: _ace(2, 2, 0),
                  3: _ace(1, 1, 1)}
    pp.rewrite_head_mode_to_file(str(src), str(out), assignment)
    return [l for l in out.read_text().splitlines()
            if l.startswith('ACE_BG_SWAP')]


def test_the_stamp_format_is_versioned(pp):
    # 7: ACE_BG_SWAP LOAD=transport on short windows
    assert pp.PP_FORMAT_VERSION >= 8


def test_a_long_window_stamps_the_full_load(pp, tmp_path):
    lines = _stamps(pp, tmp_path, 5)
    assert len(lines) == 1
    assert 'LOAD=' not in lines[0] and lines[0].endswith('QUIET=1')


def test_no_load_stamp_by_default(pp, tmp_path):
    # ACE_BG_WAIT decides at runtime; the preflight does not guess from
    # the window unless the threshold is raised.
    assert pp.BG_FULL_LOAD_MIN_WINDOW_MIN == 0
    lines = _stamps(pp, tmp_path, 1)
    assert len(lines) == 1
    assert 'LOAD=' not in lines[0] and lines[0].endswith('QUIET=1')


def test_a_short_window_stamps_transport_when_asked(pp, tmp_path, monkeypatch):
    monkeypatch.setattr(pp, 'BG_FULL_LOAD_MIN_WINDOW_MIN', 2)
    lines = _stamps(pp, tmp_path, 1)
    assert len(lines) == 1
    assert 'LOAD=transport' in lines[0] and lines[0].endswith('QUIET=1')


def test_the_threshold_is_the_first_full_minute(pp, tmp_path, monkeypatch):
    monkeypatch.setattr(pp, 'BG_FULL_LOAD_MIN_WINDOW_MIN', 2)
    assert 'LOAD=' not in _stamps(pp, tmp_path, 2)[0]


# -- the whole swap on a simulated clock -----------------------------------

class _Heater(object):
    RATE = 2.5

    def __init__(self, clk):
        self.clk, self.target_temp, self.t0, self.v0 = clk, 200., 0., 200.

    def _cur(self):
        d = self.target_temp - self.v0
        step = self.RATE * (self.clk.now - self.t0)
        return self.v0 + (min(step, d) if d > 0 else max(-step, d))

    def set(self, t):
        self.v0, self.t0, self.target_temp = self._cur(), self.clk.now, t

    def get_temp(self, et):
        return self._cur(), self.target_temp


FAST = dict(prime_speed=6., early=10.)     # the opt-in speed settings


def _simulate(soft=False, **knobs):
    """Run the real _unload_core + _load_core of one swap on stubs: ACE
    calls cost 0.2 s, the stealth moves are timed like queue_move does.
    Returns (bg, events, phase marks, total seconds)."""
    clk = Clock()
    clk.now = 0.
    heater = _Heater(clk)
    ev, marks = [], {}
    th = types.SimpleNamespace(mcu=clk, print_time=0., step_gen_time=0.,
                               get_extruder=lambda: None)
    ace = types.SimpleNamespace(
        _runout_suppress_heads=set(), _feed_assist_per_ace={1: -1},
        EXTRUDER_MAP={1: ('m', 0)}, unload_gpio=False, _head_source={1: None},
        get_retract_speed=lambda i: 80, get_swap_retract_length=lambda i, s: 900,
        get_retract_length=lambda i, s: 1900, get_feed_speed=lambda i: 80,
        get_load_length=lambda i, s: 1800, get_purge_length=lambda: 80,
        _retract_with_decoder_span=lambda i, s, fn: (fn(), (10000, 1, 0, 1))[1],
        book_spool_use=lambda *a: None, _is_actively_printing=lambda: True,
        _is_empty_status=lambda st: False, log_warn=lambda m: None)
    if 'tip_f' in knobs:        # [ace_tipform] bgprimespeed: (mm/min)
        ace.tipform_bg_prime_speed_for = lambda head, soft=False: (
            knobs['tip_f'])
    bg = object.__new__(BG.AceBgSwap)
    bg.reactor = clk
    bg.state, bg._busy, bg._pick_pending = {}, set([1]), set()
    bg._heat_claims, bg._heat_start_time, bg._pull_start_time = {}, {}, {}
    bg._last_end = {}
    bg.prime_speed = knobs.get('prime_speed', BG.BG_LOAD_PRIME_SPEED_DEFAULT)
    bg.prime_extra = knobs.get('prime_extra', BG.BG_LOAD_PRIME_EXTRA)
    bg.pull_early_start = knobs.get('early', BG.BG_PULL_EARLY_START_C)
    bg.pull_push = knobs.get('push', 0.)
    bg.gcode = types.SimpleNamespace(respond_raw=lambda m: None)
    bg.printer = types.SimpleNamespace(
        lookup_object=lambda n, d=None: th if n == 'toolhead' else d)

    def set_state(head, s):
        marks.setdefault(s, clk.now)
        bg.state[head] = s
    bg._set_state = set_state
    bg._say = lambda m: None
    bg._dh = lambda i: i + 1
    for name in ('_check_docked', '_dwell_fan', '_bg_dec_log', '_mark_staged',
                 '_clear_staged', '_load_bookkeeping', '_release_heat',
                 '_heat_claim'):
        setattr(bg, name, lambda *a, **k: None)
    bg._gpio_diag = lambda *a: False
    bg._cap = lambda *a: True
    bg._slot_status = lambda *a: 'ready'
    bg._fa_on = lambda *a, **k: True
    bg._set_heat = lambda a, p, h, t: h.set(t)
    bg._pause = lambda s: clk.pause(clk.now + s)

    def send(a, idx, req):
        ev.append((clk.now, 'ACE ' + req['method']))
        clk.pause(clk.now + 0.2)
        return {'code': 0, 'msg': 'OK'}
    bg._ace_send = send

    def unwind(a, idx, slot, length, speed, wait=True, retries=3):
        ev.append((clk.now, 'UNWIND %d' % length))
        clk.pause(clk.now + 0.4)
        return True
    bg._ace_unwind = unwind
    bg._ace_feed_to_gears = lambda *a: (
        clk.pause(clk.now + 0.2 + 1200. / 80), ('ok', (None, 0, None, None)))[1]

    def queue_move(ext, dist, speed, accel):
        start = max(th.print_time, clk.now + BG.SCHEDULE_DELAY,
                    bg._last_end.get('e', 0.)) + BG.SCHEDULE_EPS
        end = start + abs(dist) / speed + speed / accel
        bg._last_end['e'] = end
        ev.append((start, 'MOVE %+.1f' % dist))
        return start, end, False
    bg.queue_move = queue_move
    ext = types.SimpleNamespace(get_heater=lambda: heater)
    ph = types.SimpleNamespace()
    bg._unload_core(1, ace, th, ext, heater, ph, 1, 2, 215., soft,
                    {'v': False})
    pull_end = clk.now
    bg._load_core(1, ace, th, ext, heater, ph, 1, 3, 215., 10., soft=soft)
    return bg, ev, marks, clk.now, pull_end


def test_the_whole_swap_is_much_shorter_than_the_measured_one():
    # klippy.log, 12:20 swap: 146 s queued -> primed on the printer. The
    # same stubs run the OLD engine in ~120 s (20 s of it prime gaps).
    bg, ev, marks, total, _ = _simulate(**FAST)
    assert total < 100
    primes = [e for e in ev if e[1] == 'MOVE +5.0']
    assert len(primes) == 24                       # 80 + 40 mm in 5 mm chunks
    gaps = [b[0] - a[0] for a, b in zip(primes, primes[1:])]
    assert max(gaps) < 0.95                        # 0.83 s chunk + 0.05 gap


def test_the_pull_starts_before_the_heater_is_at_target():
    bg, ev, marks, total, _ = _simulate(**FAST)
    first_move = next(t for t, e in ev if e.startswith('MOVE'))
    late = _simulate(early=0.)[1]
    late_move = next(t for t, e in late if e.startswith('MOVE'))
    assert late_move - first_move > 1.5            # 5 of the 15 degrees
    # ... and the table is still the stock one in the same order
    moves = [e for t, e in ev if e.startswith('MOVE')][:5]
    assert moves == ['MOVE +57.0', 'MOVE +3.0', 'MOVE -27.0', 'MOVE -5.5',
                     'MOVE -37.5']


def test_the_soft_table_does_not_start_early():
    bg, ev, marks, total, _ = _simulate(soft=True, early=10.)
    first_move = next(t for t, e in ev if e.startswith('MOVE'))
    off = _simulate(soft=True, early=0.)[1]
    assert first_move == next(t for t, e in off if e.startswith('MOVE'))


def test_early_start_can_be_switched_off():
    on = next(t for t, e in _simulate(early=10.)[1] if e.startswith('MOVE'))
    off = next(t for t, e in _simulate(early=0.)[1] if e.startswith('MOVE'))
    assert off > on


def test_a_shorter_push_saves_its_time_and_nothing_else_changes():
    base = _simulate()
    short = _simulate(push=35.)
    assert [e for t, e in short[1] if e.startswith('MOVE')][0] == 'MOVE +35.0'
    assert 2.5 < base[3] - short[3] < 5.0          # 22 mm at 6.7 mm/s ~ 3.3 s


def test_the_push_is_the_only_forward_motion_before_the_retracts():
    """FA is armed once, before the push; an unwind never starts while a
    forward push is still running."""
    bg, ev, marks, total, _ = _simulate()
    pull = [(t, e) for t, e in ev if t < marks['LD_FEED']]
    assert pull[0][1] == 'ACE start_feed_assist'
    assert sum(1 for t, e in pull if e == 'ACE start_feed_assist') == 1
    last_push = [t for t, e in pull if e == 'MOVE +3.0'][0]
    first_unwind = [t for t, e in pull if e.startswith('UNWIND')][0]
    # at most one chain lead early, i.e. not into the middle of a push
    assert first_unwind >= last_push - BG.BG_CHAIN_LEAD


def test_soft_filament_primes_at_the_slow_speed():
    fast = _simulate(prime_speed=6.)
    soft = _simulate(soft=True, prime_speed=6.)
    prime = lambda r: r[3] - r[2]['LD_PRIME']
    assert prime(soft) - prime(fast) > 8           # 120 mm at 4 instead of 6


def test_the_tipform_prime_speed_beats_the_config_key():
    prime = lambda r: r[3] - r[2]['LD_PRIME']
    base = _simulate()                              # 4 mm/s
    tip = _simulate(tip_f=360.)                     # 6 mm/s from the table
    assert prime(base) - prime(tip) > 8
    # the same speed as the config key gives the same prime
    assert abs(prime(tip) - prime(_simulate(prime_speed=6.))) < 0.3


def test_the_tipform_prime_speed_never_speeds_up_soft_filament():
    prime = lambda r: r[3] - r[2]['LD_PRIME']
    assert abs(prime(_simulate(soft=True, tip_f=360.))
               - prime(_simulate(soft=True))) < 0.3
