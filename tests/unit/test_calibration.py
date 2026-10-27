"""Bowden-path calibration (ace.py ACE_CALIBRATION_*), driven with the real
MultiAce methods against a fake ACE.

The fake ACE models what the review of PR #168 ran into on hardware: motion
starts late, 'ready' is reported late, the V2 lane encoder restarts with
every motion command, and on V1 (ACE Pro) `stop_feed_filament` stops a feed
but not an unwind - that takes `stop_unwind_filament`. A fake can also
ignore stops altogether, which is what the calibration has to survive when
a firmware does not know the stop command it is sent.
"""
import importlib
import os
import sys
import types

import pytest

EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                      'multiace', 'klipper', 'extras')
PKG = 'multiace_calibration_extras'

START_LATENCY = 0.8     # s between a motion command and the first motion
READY_LAG = 1.0         # s between the end of motion and status 'ready'
STOP_READY_LAG = 0.2    # s between an obeyed stop and status 'ready'
DECODER_RESET_LAG = 0.02
REPLY_LAG = 0.03
SPEED = 30


def load_ace():
    name = PKG + '.ace'
    if name in sys.modules:
        return sys.modules[name]
    if 'serial' not in sys.modules:
        try:
            importlib.import_module('serial')
        except ImportError:
            stub = types.ModuleType('serial')
            stub.SerialException = Exception
            sys.modules['serial'] = stub
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [EXTRAS]
    sys.modules[PKG] = pkg
    return importlib.import_module(name)


class Reactor:
    NEVER = 9e99
    NOW = 0.0

    def __init__(self):
        self.t = 100.0
        self.callbacks = []

    def monotonic(self):
        return self.t

    def register_timer(self, callback, when):
        return object()

    def unregister_timer(self, timer):
        pass

    def register_callback(self, callback, when=0.0):
        self.callbacks.append((when, callback))

    def pause(self, waketime):
        while self.t < waketime:
            self.stepper(0.01)
        return self.t

    def run_due(self):
        due = sorted((c for c in self.callbacks if c[0] <= self.t),
                     key=lambda c: c[0])
        self.callbacks = [c for c in self.callbacks if c[0] > self.t]
        for _, callback in due:
            callback(self.t)


class GCmd:
    class error(Exception):
        pass

    def __init__(self, **params):
        self.params = params

    def get(self, key, default=None):
        return self.params.get(key, default)

    def get_int(self, key, default=None):
        value = self.params.get(key, default)
        return None if value is None else int(value)


class FakePrinter:
    command_error = Exception

    def lookup_object(self, name, default=None):
        return default


def make_ace(true_mm, proto, stop_mode='honest'):
    """A MultiAce wired to one fake ACE (index 0, slot 0, head 0) whose
    toolhead sensor trips once the filament tip reaches `true_mm`."""
    MultiAce = load_ace().MultiAce
    a = MultiAce.__new__(MultiAce)
    a.reactor = Reactor()
    a.printer = FakePrinter()
    a.unload_decoder_gate = True
    a._connected_per_ace = {0: True}
    a._info_per_ace = {0: {'status': 'ready'}}
    a._active_device_index = 0
    a._calibration_prev_ace = None
    a._calibration_timer = None
    a._calibration_move = None
    a._calibration_seq = 1
    a._swap_in_progress = False
    a.logs = []
    a.log_always = a.logs.append
    a.log_error = lambda message: a.logs.append('ERR ' + message)
    a.get_retract_speed = lambda idx: SPEED
    a.get_feed_speed = lambda idx: SPEED
    a._set_active_idx = lambda idx: None
    a._ensure_ace_available = lambda idx: True
    a._switch_ace_for_head_target = lambda idx: True
    a._disable_feed_assist_all = lambda: None
    a.wait_ace_ready_on = lambda idx: None
    ace = {'pos': 0.0, 'move': None, 'dec': 0.0, 'dec_reset_at': None,
           'ready_at': None, 'replies': [], 'sent': []}
    a.fake = ace

    def obeys(method):
        if stop_mode == 'ignore' or ace['move'] is None:
            return False
        if proto == 'v2':
            return method == 'stop_feed_filament'
        direction = ace['move']['dir']
        return ((method == 'stop_feed_filament' and direction > 0)
                or (method == 'stop_unwind_filament' and direction < 0))

    def send_request_to(idx, request, callback):
        method = request['method']
        params = request.get('params', {})
        now = a.reactor.t
        ace['sent'].append((method, params.get('length')))
        if method in ('feed_filament', 'unwind_filament'):
            ace['move'] = {'dir': 1 if method == 'feed_filament' else -1,
                           'left': float(params['length']),
                           'speed': float(params['speed']),
                           'go_at': now + START_LATENCY}
            ace['dec_reset_at'] = now + DECODER_RESET_LAG
            ace['ready_at'] = None
            a._info_per_ace[0]['status'] = 'busy'
        elif method in ('stop_feed_filament', 'stop_unwind_filament'):
            if obeys(method):
                ace['move'] = None
                ace['ready_at'] = now + STOP_READY_LAG
        elif method == 'get_feed_info':
            ace['replies'].append((now + REPLY_LAG, callback))
    a.send_request_to = send_request_to
    a._calibration_sensor = lambda head: ace['pos'] >= true_mm - 1e-6

    def step(dt):
        a.reactor.t += dt
        now = a.reactor.t
        if ace['dec_reset_at'] is not None and now >= ace['dec_reset_at']:
            ace['dec'] = 0.0
            ace['dec_reset_at'] = None
        move = ace['move']
        if move and now >= move['go_at']:
            dist = min(move['left'], move['speed'] * dt)
            ace['pos'] += move['dir'] * dist
            ace['dec'] += move['dir'] * dist
            move['left'] -= dist
            if move['left'] <= 1e-9:
                ace['move'] = None
                ace['ready_at'] = now + READY_LAG
        if ace['ready_at'] is not None and now >= ace['ready_at']:
            a._info_per_ace[0]['status'] = 'ready'
            ace['ready_at'] = None
        due = [r for r in ace['replies'] if r[0] <= now]
        ace['replies'] = [r for r in ace['replies'] if r[0] > now]
        for _, callback in due:
            row = {'index': 0}
            if proto == 'v2':
                value = int(round(ace['dec']))
                row['decoder'] = value + (1 << 64) if value < 0 else value
            callback(a, {'result': {'feed_info': [row]}})
        a.reactor.run_due()
    a.step = step
    a.reactor.stepper = step
    # what ACE_CALIBRATION_START reads besides the session
    a._ace_devices = [object()]
    a._ace_mode = 'head'
    a._gate_status_per_ace = {0: [load_ace().GATE_AVAILABLE] * 4}
    a._protocols = {}
    a.head_is_manual = lambda head: False
    a.head_uses_ace = lambda head: True
    a.head_ace_for = lambda head: 0
    a.get_load_length = lambda idx, slot: true_mm + 150
    a.get_swap_retract_length = lambda idx, slot: 400
    a.get_retract_length = lambda idx, slot: true_mm
    return a


def _tick(a, st):
    timer = a._calibration_timer
    if timer is None:
        st['timer'] = None
        return
    if timer is not st.get('timer'):
        st['timer'] = timer
        st['next'] = a.reactor.t
    if a.reactor.t >= st['next']:
        st['next'] = a._calibration_timer_tick(a.reactor.t)


def run_for(a, seconds):
    end = a.reactor.t + seconds
    st = {}
    while a.reactor.t < end:
        a.step(0.01)
        _tick(a, st)


def pump(a, until, limit_s=600):
    """Advance until the session rests in one of `until` (or fails)."""
    st = {}
    end = a.reactor.t + limit_s
    while a.reactor.t < end:
        a.step(0.01)
        _tick(a, st)
        c = a._calibration
        if (c['state'] in until and a._calibration_timer is None
                and a._calibration_move is None and not a.reactor.callbacks):
            return c['state']
        if c['state'] in ('failed', 'cancelled'):
            return c['state']
    return 'timeout:' + a._calibration['state']


def phys(a):
    return a.fake['pos']


def at_sensor(a):
    """A measuring session fed to the toolhead sensor."""
    c = a._calibration_idle_state()
    c.update({'state': 'prepared', 'session_id': 1, 'ace': 0, 'slot': 0,
              'head': 0, 'protocol': a.proto if hasattr(a, 'proto') else '',
              'speed_mm_s': SPEED, 'max_feed_mm': 3000, 'tip_position_mm': 0})
    a._calibration = c
    a.cmd_ACE_CALIBRATION_FEED(GCmd())
    assert pump(a, ('at_sensor',)) == 'at_sensor'
    return c


def new_session(true_mm, proto, stop_mode='honest'):
    a = make_ace(true_mm, proto, stop_mode)
    a.proto = proto
    c = at_sensor(a)
    # a unit that ignores stops keeps feeding past the sensor; the next
    # calibration move is only accepted once it has finished
    run_until(a, lambda: a._info_per_ace[0]['status'] == 'ready')
    return a, c


# ---- V1 unwinds (review point 2) ------------------------------------------

def test_v1_stop_unwind_stops():
    a, c = new_session(1543, 'v1')
    a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=c['load_length_mm']))
    run_for(a, 6)
    a.cmd_ACE_CALIBRATION_STOP(GCmd())
    assert pump(a, ('retract_ready',)) == 'retract_ready'
    run_for(a, 60)      # an unwind that was not stopped would run on here
    assert phys(a) > 1000, 'the unwind kept running after the stop'
    assert abs(c['tip_position_mm'] - phys(a)) <= 60
    assert ('stop_unwind_filament', None) in a.fake['sent']


def test_v1_stop_ignored_books_full_length():
    a, c = new_session(1543, 'v1', stop_mode='ignore')
    load = c['load_length_mm']
    a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=load))
    run_for(a, 6)
    a.cmd_ACE_CALIBRATION_STOP(GCmd())
    assert pump(a, ('retract_ready',)) == 'retract_ready'
    assert c['commanded_retract_mm'] == load
    assert c['tip_position_mm'] == 0
    with pytest.raises(Exception, match='no calibration filament'):
        a.cmd_ACE_CALIBRATION_HOME(GCmd())
    # only the V1 time-estimate bias of the measurement itself, no second
    # unwind on top of the first one
    assert phys(a) >= -60


def test_v1_early_stop_not_falsely_confirmed():
    a, c = new_session(1543, 'v1', stop_mode='ignore')
    load = c['load_length_mm']
    a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=load))
    run_for(a, 0.3)
    a.cmd_ACE_CALIBRATION_STOP(GCmd())
    assert pump(a, ('retract_ready',)) == 'retract_ready'
    assert c['commanded_retract_mm'] == load


def test_v1_stop_confirm_disconnect_fails():
    a, c = new_session(1543, 'v1', stop_mode='ignore')
    a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=c['load_length_mm']))
    run_for(a, 3)
    a.cmd_ACE_CALIBRATION_STOP(GCmd())
    run_for(a, 0.5)
    a._connected_per_ace[0] = False
    run_for(a, 5)
    assert c['state'] == 'failed'
    assert 'disconnected' in (c['error'] or '')


def test_v2_stop_unchanged():
    a, c = new_session(1543, 'v2')
    a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=c['load_length_mm']))
    run_for(a, 8)
    a.cmd_ACE_CALIBRATION_STOP(GCmd())
    assert pump(a, ('retract_ready',)) == 'retract_ready'
    assert abs(c['tip_position_mm'] - phys(a)) <= 10


def complete_session(true_mm, proto, stop_mode='honest', swap=400):
    """A finished measurement parked at the ACE, ready for verification."""
    a = make_ace(true_mm, proto, stop_mode)
    a.proto = proto
    c = a._calibration_idle_state()
    c.update({'state': 'complete', 'session_id': 1, 'ace': 0, 'slot': 0,
              'head': 0, 'protocol': proto, 'speed_mm_s': SPEED,
              'load_length_mm': true_mm, 'retract_length_mm': true_mm,
              'swap_retract_length_mm': swap, 'tip_position_mm': 0,
              'verify_position_mm': 0})
    a._calibration = c
    return a, c


def run_until(a, predicate, limit_s=300):
    st = {}
    end = a.reactor.t + limit_s
    while a.reactor.t < end and not predicate():
        a.step(0.01)
        _tick(a, st)
    assert predicate(), 'condition not reached'


def test_v1_verify_pause_ignored_stop_books_full_move():
    a, c = complete_session(1543, 'v1', stop_mode='ignore')
    a.cmd_ACE_CALIBRATION_VERIFY(GCmd(ACTION='start'))
    run_until(a, lambda: c['state'] == 'verifying_splitter'
              and a._calibration_move is not None)
    run_for(a, 3)
    a.cmd_ACE_CALIBRATION_VERIFY(GCmd(ACTION='pause'))
    assert pump(a, ('verify_paused',)) == 'verify_paused'
    run_for(a, 60)
    assert abs(c['verify_position_mm'] - phys(a)) <= 60


# ---- clean reset, no blocking, plain errors (review point 3) ---------------

def test_retract_refused_while_ace_busy():
    a, c = new_session(1543, 'v1')
    a._info_per_ace[0]['status'] = 'busy'      # still running a lost move
    a.fake['ready_at'] = None
    started = a.reactor.t
    with pytest.raises(Exception, match='still moving'):
        a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=100))
    assert a.reactor.t - started <= 3, 'the command blocked on a busy ACE'
    assert c['state'] == 'at_sensor'


def test_home_refused_while_ace_busy():
    a, c = new_session(1543, 'v2')
    c['state'] = 'cancelled'
    a._info_per_ace[0]['status'] = 'busy'
    with pytest.raises(Exception, match='still moving'):
        a.cmd_ACE_CALIBRATION_HOME(GCmd())
    assert c['state'] == 'cancelled'


def test_mark_at_park_position_explains():
    a, c = new_session(1543, 'v2')
    a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=c['load_length_mm']))
    assert pump(a, ('retract_ready',)) == 'retract_ready'
    assert c['commanded_retract_mm'] == c['load_length_mm']
    with pytest.raises(Exception, match='all the way back'):
        a.cmd_ACE_CALIBRATION_MARK(GCmd(MARK='swap'))


def test_start_after_failed_run_is_clean():
    a, c = new_session(1543, 'v2')
    a.cmd_ACE_CALIBRATION_RETRACT(GCmd(LENGTH=c['load_length_mm']))
    assert pump(a, ('retract_ready',)) == 'retract_ready'
    a._calibration_fail('test failure')          # tip is back at 0 mm
    a.cmd_ACE_CALIBRATION_START(GCmd(ACE=0, SLOT=0, HEAD=0))
    fresh = a._calibration
    assert fresh['state'] == 'prepared'
    assert fresh['commanded_retract_mm'] == 0
    assert fresh['load_length_mm'] is None
    assert fresh['swap_retract_length_mm'] is None


def test_moonraker_error_text():
    import ast
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                        'multiace', 'web', 'backend', 'main.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == '_moonraker_error_text')
    ns = {'json': __import__('json')}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, 'exec'), ns)
    text = ns['_moonraker_error_text']
    raw = ns['json'].dumps({'error': {
        'code': 400,
        'message': '[multiACE] splitter checkpoint must be before the ACE '
                   'preload reference',
        'traceback': 'Traceback (most recent call last):\n  File ...'}})
    assert text(raw) == ('[multiACE] splitter checkpoint must be before the '
                         'ACE preload reference')
    assert text('plain text') == 'plain text'
    assert text('{"error": "boom"}') == 'boom'
