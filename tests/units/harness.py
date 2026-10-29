"""H0 harness: the real ace.py against fake transports on a virtual clock.

UNIT_DRIVER_DESIGN.md section 10 (H0). Nothing in multiace/ is changed for
this: ace.py is imported through a synthetic package (the pattern of
tests/ace_set_humidity_selfcheck.py) and driven through the same seams
Klipper uses - config, printer, reactor, gcode - plus the two device
seams, protocol discovery and open_transport.

What is faked, and how far:
- FakeReactor: Klipper's timer/callback/fd API on a virtual clock. pause()
  runs due work and advances the clock; a nested pause (ace.py pauses
  inside timers and the ready handler) runs other timers inline, the
  paused timer itself is parked at NEVER meanwhile, as in reactor.py.
- time shim: ace.py also reads time.monotonic()/time.time() directly; the
  module attribute is replaced so both clocks are the same virtual one.
- FakeSerial: the pyserial surface ace.py touches. A V2 unit is read by a
  real thread (ace.py starts it); the fake parks that thread on a
  condition and releases bytes only when the device model produced them,
  so the run is deterministic. Error injection: EIO on read/write, silence,
  unplug.
- Device models (fake_v1.py, fake_v2.py) answer on the wire, bytes in,
  bytes out, with a virtual latency.
- Trace: a list of JSON-serialisable entries [t, kind, ...]; the golden
  files are these lists, one JSON object per line.
"""
from __future__ import annotations

import ast
import configparser
import importlib
import json
import logging
import os
import queue as _real_queue
import re
import shutil
import sys
import tempfile
import threading as _real_threading
import time as _real_time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
EXTRAS = os.path.join(REPO, 'multiace', 'klipper', 'extras')
I18N_DIR = os.path.join(REPO, 'multiace', 'i18n')
SHIPPED_ACE_CFG = os.path.join(REPO, 'multiace', 'config', 'extended',
                               'ace.cfg')
GOLDENS = os.path.join(HERE, 'goldens')

PKG = 'multiace_h0_extras'

# Import-time environment of ace.py: the managed marker must not depend on
# the machine the tests run on.
os.environ.setdefault('MULTIACE_MANAGED_MARKER',
                      os.path.join(tempfile.gettempdir(),
                                   'multiace-h0-no-managed-marker'))
os.environ.pop('MULTIACE_MANAGED', None)


def load_ace():
    """Import the real ace.py (and its protocol modules) once."""
    name = PKG + '.ace'
    if name in sys.modules:
        return sys.modules[name]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [EXTRAS]
    sys.modules[PKG] = pkg
    return importlib.import_module(name)


def protocol_modules():
    load_ace()
    return (sys.modules[PKG + '.ace_protocol_v1'],
            sys.modules[PKG + '.ace_protocol_v2'])


# ---------------------------------------------------------------------------
# virtual time

class VirtualClock:
    START = 1000.0
    EPOCH = 1700000000.0

    def __init__(self):
        self.now = self.START

    def monotonic(self):
        return self.now

    def time(self):
        return self.EPOCH + (self.now - self.START)

    def sleep(self, seconds):
        # Only worker threads of ace.py sleep (generic USB probe); the main
        # thread never does. Yield the GIL, do not move the clock.
        _real_time.sleep(0)


def make_os_shim(harness):
    """os with os.path.exists() answering for the fake device paths (the
    idle reconnect and the V2 probe ask the filesystem whether the port is
    back). Everything else is the real os."""
    real_path = os.path
    path_shim = types.ModuleType('os.path')
    for attr in dir(real_path):
        if not attr.startswith('__'):
            setattr(path_shim, attr, getattr(real_path, attr))

    def exists(p):
        u = harness.by_path.get(str(p))
        if u is not None:
            return u.plugged
        return real_path.exists(p)
    path_shim.exists = exists
    shim = types.ModuleType('os')
    for attr in dir(os):
        if not attr.startswith('__'):
            setattr(shim, attr, getattr(os, attr))
    shim.path = path_shim
    return shim


def make_time_shim(clock):
    shim = types.ModuleType('time')
    for attr in dir(_real_time):
        if not attr.startswith('__'):
            setattr(shim, attr, getattr(_real_time, attr))
    shim.monotonic = clock.monotonic
    shim.time = clock.time
    shim.sleep = clock.sleep
    return shim


# ---------------------------------------------------------------------------
# errors and small Klipper stand-ins

class ConfigError(Exception):
    pass


class CommandError(Exception):
    """gcode.CommandError of the U1 fork: a CodedException that takes
    keyword arguments (message=, action=, id=, code=, ...)."""

    def __init__(self, message='', *args, **kwargs):
        super().__init__(message, *args)
        self.message = message
        self.kwargs = kwargs

    def __str__(self):
        return str(self.message)


_SENTINEL = object()


class FakeConfig:
    """One config section. Every option access is recorded (the paragraph 30
    audit: an option present but never read halts Klipper, so the harness
    lists what ace.py reads)."""
    error = ConfigError

    def __init__(self, printer, name, options, sections, accessed):
        self._printer = printer
        self._name = name
        self._options = dict(options)
        self._sections = sections
        self._accessed = accessed

    def get_printer(self):
        return self._printer

    def get_name(self):
        return self._name

    def _read(self, name, default, parser, minval=None, maxval=None,
              above=None, below=None):
        self._accessed.add('%s.%s' % (self._name, name))
        if name not in self._options:
            if default is _SENTINEL:
                raise ConfigError("Option '%s' in section '%s' must be "
                                  "specified" % (name, self._name))
            return default
        raw = self._options[name]
        try:
            value = parser(raw)
        except Exception:
            raise ConfigError("Unable to parse option '%s' in section '%s'"
                              % (name, self._name))
        if minval is not None and value < minval:
            raise ConfigError("Option '%s' in section '%s' must have minimum"
                              " of %s" % (name, self._name, minval))
        if maxval is not None and value > maxval:
            raise ConfigError("Option '%s' in section '%s' must have maximum"
                              " of %s" % (name, self._name, maxval))
        if above is not None and value <= above:
            raise ConfigError("Option '%s' in section '%s' must be above %s"
                              % (name, self._name, above))
        if below is not None and value >= below:
            raise ConfigError("Option '%s' in section '%s' must be below %s"
                              % (name, self._name, below))
        return value

    def get(self, name, default=_SENTINEL, note_valid=True):
        return self._read(name, default, str)

    def getint(self, name, default=_SENTINEL, minval=None, maxval=None,
               note_valid=True):
        return self._read(name, default, int, minval, maxval)

    def getfloat(self, name, default=_SENTINEL, minval=None, maxval=None,
                 above=None, below=None, note_valid=True):
        return self._read(name, default, float, minval, maxval, above, below)

    @staticmethod
    def _parse_bool(raw):
        v = str(raw).strip().lower()
        if v in ('true', '1', 'yes', 'on'):
            return True
        if v in ('false', '0', 'no', 'off'):
            return False
        raise ValueError(raw)

    def getboolean(self, name, default=_SENTINEL, note_valid=True):
        return self._read(name, default, self._parse_bool)

    def getchoice(self, name, choices, default=_SENTINEL, note_valid=True):
        raw = self._read(name, default, str)
        if raw == default and name not in self._options:
            return choices[default] if default in choices else default
        if raw not in choices:
            raise ConfigError("Choice '%s' for option '%s' in section '%s' is"
                              " not a valid choice" % (raw, name, self._name))
        return choices[raw]

    def getlists(self, name, default=_SENTINEL, seps=(',',), count=None,
                 parser=str, note_valid=True):
        raw = self._read(name, default, str)
        if raw is default:
            return default
        return [parser(p.strip()) for p in raw.split(seps[0]) if p.strip()]

    def getlist(self, name, default=_SENTINEL, sep=',', count=None,
                note_valid=True):
        return self.getlists(name, default, seps=(sep,), count=count)

    def has_section(self, name):
        return name in self._sections

    def getsection(self, name):
        return FakeConfig(self._printer, name, self._sections.get(name, {}),
                          self._sections, self._accessed)

    def get_prefix_sections(self, prefix):
        return [self.getsection(n) for n in self._sections
                if n.startswith(prefix)]

    def get_prefix_options(self, prefix):
        return [o for o in self._options if o.startswith(prefix)]


def shipped_sections():
    """The [ace] (and [save_variables]) options of the shipped ace.cfg."""
    cp = configparser.RawConfigParser(strict=False, allow_no_value=True,
                                      comment_prefixes=('#', ';'))
    cp.optionxform = str
    with open(SHIPPED_ACE_CFG, encoding='utf-8') as f:
        cp.read_file(f)
    return {s: {k: (v if v is not None else '')
                for k, v in cp.items(s)} for s in cp.sections()}


class FakeGcmd:
    error = CommandError

    def __init__(self, gcode, command, commandline, params):
        self._gcode = gcode
        self._command = command
        self._commandline = commandline
        self._params = params

    def get_command(self):
        return self._command

    def get_commandline(self):
        return self._commandline

    def get_command_parameters(self):
        return self._params

    def get_raw_command_parameters(self):
        return self._commandline.split(' ', 1)[1] if ' ' in self._commandline else ''

    def _get(self, name, default, parser, minval, maxval, above, below):
        if name not in self._params:
            if default is _SENTINEL:
                raise CommandError("Error on '%s': missing %s"
                                   % (self._commandline, name))
            return default
        try:
            value = parser(self._params[name])
        except Exception:
            raise CommandError("Error on '%s': unable to parse %s"
                               % (self._commandline, self._params[name]))
        if minval is not None and value < minval:
            raise CommandError("Error on '%s': %s must have minimum of %s"
                               % (self._commandline, name, minval))
        if maxval is not None and value > maxval:
            raise CommandError("Error on '%s': %s must have maximum of %s"
                               % (self._commandline, name, maxval))
        if above is not None and value <= above:
            raise CommandError("Error on '%s': %s must be above %s"
                               % (self._commandline, name, above))
        if below is not None and value >= below:
            raise CommandError("Error on '%s': %s must be below %s"
                               % (self._commandline, name, below))
        return value

    def get(self, name, default=_SENTINEL, parser=str, minval=None,
            maxval=None, above=None, below=None):
        return self._get(name, default, parser, minval, maxval, above, below)

    def get_int(self, name, default=_SENTINEL, minval=None, maxval=None):
        return self._get(name, default, int, minval, maxval, None, None)

    def get_float(self, name, default=_SENTINEL, minval=None, maxval=None,
                  above=None, below=None):
        return self._get(name, default, float, minval, maxval, above, below)

    def respond_info(self, msg, log=True):
        self._gcode.respond_info(msg, log)

    def respond_raw(self, msg):
        self._gcode.respond_raw(msg)


class FakeGcode:
    """Stock U1 gcode.py semantics where ace.py relies on them:
    register_command(name, None) unregisters and returns the previous
    handler; a duplicate registration is a config error; scripts run line
    by line, a registered command is dispatched, SAVE_VARIABLE is applied
    to the fake store, everything else is recorded."""

    def __init__(self, harness):
        self._h = harness
        self.handlers = {}
        self.help = {}
        self.mutex = _real_threading.RLock()
        self.hold_until = None

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        if func is None:
            return self.handlers.pop(cmd, None)
        if cmd in self.handlers:
            raise ConfigError('gcode command %s already registered' % cmd)
        self.handlers[cmd] = func
        if desc is not None:
            self.help[cmd] = desc
        return None

    def register_mux_command(self, cmd, key, value, func, desc=None):
        self.handlers.setdefault(cmd, func)

    def get_mutex(self):
        return self.mutex

    @staticmethod
    def _parse(line):
        parts = line.strip().split()
        cmd = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if '=' in p:
                k, v = p.split('=', 1)
                params[k.upper()] = v
            else:
                params[p.upper()] = ''
        return cmd, params

    def _run_line(self, line):
        line = line.strip()
        if not line or line.startswith(';'):
            return
        self._h.trace('gcode', line)
        cmd, params = self._parse(line)
        if cmd == 'SAVE_VARIABLE':
            name = params.get('VARIABLE')
            raw = params.get('VALUE', '')
            try:
                value = ast.literal_eval(raw)
            except Exception:
                value = raw
            self._h.save_variables.allVariables[name] = value
            return
        handler = self.handlers.get(cmd)
        if handler is None:
            self._h.trace('gcode_unknown', cmd)
            return
        try:
            handler(FakeGcmd(self, cmd, line, params))
        except CommandError as e:
            # Klipper answers the console with '!! <message>' and carries
            # on; the structured fields (action=pause, id, code) are what
            # the display and Fluidd react to.
            extra = {k: v for k, v in sorted(e.kwargs.items())
                     if k in ('action', 'id', 'code', 'level')}
            self._h.trace('gcode_error', cmd, self._h.norm(str(e)), extra)

    def run_script_from_command(self, script):
        for line in script.split('\n'):
            self._run_line(line)

    def run_script(self, script):
        # Stock run_script takes the gcode mutex; while a command runs (a
        # swap, minutes long) the caller's greenlet waits and the reactor
        # keeps serving timers. hold_until models that wait.
        if self.hold_until is not None:
            until = self.hold_until
            if self._h.clock.now < until:
                self._h.trace('gcode', 'wait_mutex', round(until, 3))
                self._h.reactor.pause(until)
            if self._h.clock.now >= until:
                self.hold_until = None
        self.run_script_from_command(script)

    def respond_info(self, msg, log=True):
        self._h.trace('respond', 'info', self._h.norm(str(msg)))

    def respond_raw(self, msg):
        self._h.trace('respond', 'raw', self._h.norm(str(msg)))


class FakeSaveVariables:
    def __init__(self, initial):
        self.allVariables = dict(initial)

    def get_status(self, eventtime=None):
        return {'variables': dict(self.allVariables)}


class FakeWebhooks:
    def __init__(self, harness):
        self._h = harness
        self.endpoints = {}

    def register_endpoint(self, path, callback):
        self.endpoints[path] = callback
        self._h.trace('webhook', path)


class FakePrintStats:
    def __init__(self):
        self.state = 'standby'

    def get_status(self, eventtime=None):
        return {'state': self.state, 'filename': '', 'print_duration': 0.0}


class FakeMotionReport:
    """motion_report as ace.py reads it: the live extruder velocity decides
    whether a lost feed-assist arm is worth a recovery (an idle extruder
    means the unit's own inactivity timeout, not a dropped arm)."""

    def __init__(self):
        self.live_extruder_velocity = 0.0

    def get_status(self, eventtime=None):
        return {'live_extruder_velocity': self.live_extruder_velocity,
                'live_velocity': 0.0, 'live_position': [0., 0., 0., 0.]}


class FakeExtruder:
    def __init__(self, index=0):
        self.extruder_index = index
        self.name = 'extruder' if index == 0 else 'extruder%d' % index
        self.last_position = 0.

    def get_name(self):
        return self.name


class FakeToolhead:
    def __init__(self):
        self.extruders = [FakeExtruder(i) for i in range(4)]
        self.extruder = self.extruders[0]

    def set_extruder(self, index):
        self.extruder = self.extruders[index]

    def get_extruder(self):
        return self.extruder

    def get_position(self):
        return [0., 0., 0., 0.]

    def get_status(self, eventtime=None):
        return {'position': self.get_position(), 'homed_axes': ''}

    def wait_moves(self):
        pass

    def move(self, newpos, speed):
        pass


class FakePrinter:
    config_error = ConfigError
    command_error = CommandError

    def __init__(self, harness, reactor, objects, start_args):
        self._h = harness
        self._reactor = reactor
        self.objects = objects
        self._start_args = start_args
        self.event_handlers = {}

    def get_reactor(self):
        return self._reactor

    def get_start_args(self):
        return dict(self._start_args)

    def lookup_object(self, name, default=_SENTINEL):
        if name in self.objects:
            return self.objects[name]
        if default is _SENTINEL:
            raise ConfigError("Unknown config object '%s'" % name)
        return default

    def lookup_objects(self, module=None):
        return list(self.objects.items())

    def load_object(self, config, section, default=_SENTINEL):
        if section in self.objects:
            return self.objects[section]
        if default is _SENTINEL:
            raise ConfigError("Unable to load module '%s'" % section)
        return default

    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *params):
        return [cb(*params) for cb in list(self.event_handlers.get(event, []))]

    def is_shutdown(self):
        return False

    def invoke_shutdown(self, msg):
        self._h.trace('shutdown', self._h.norm(str(msg)))

    def invoke_async_shutdown(self, msg):
        self.invoke_shutdown(msg)


# ---------------------------------------------------------------------------
# reactor

class _Timer:
    __slots__ = ('callback', 'waketime', 'seq', 'name')

    def __init__(self, callback, waketime, seq):
        self.callback = callback
        self.waketime = waketime
        self.seq = seq
        self.name = callback_name(callback)


class _FileHandler:
    __slots__ = ('fd', 'read_callback', 'write_callback')

    def __init__(self, fd, read_callback, write_callback):
        self.fd = fd
        self.read_callback = read_callback
        self.write_callback = write_callback


def callback_name(cb):
    qn = getattr(cb, '__qualname__', None)
    if qn is None:
        func = getattr(cb, '__func__', None)
        qn = getattr(func, '__qualname__', None) or type(cb).__name__
    # R1 split: a method that moved into a mixin keeps its trace name under
    # the canonical class, so a move never changes a golden (the same rule
    # move_check applies through modules.MIXIN_GROUPS).
    try:
        from .tools.modules import MIXIN_GROUPS
    except Exception:  # pragma: no cover - tools missing
        MIXIN_GROUPS = {}
    for mixin, canon in MIXIN_GROUPS.items():
        if qn.startswith(mixin + '.'):
            qn = canon + qn[len(mixin):]
            break
    return qn


class FakeReactor:
    NOW = 0.
    NEVER = 9999999999999999.
    MAX_DEPTH = 24

    def __init__(self, harness):
        self._h = harness
        self._clock = harness.clock
        self._timers = []
        self._seq = 0
        self._async = _real_queue.Queue()
        self._async_seq = 0
        self._fds = []
        self._depth = 0
        self.process_async = True

    def monotonic(self):
        return self._clock.now

    def _rel(self, waketime):
        if waketime >= self.NEVER:
            return 'never'
        if waketime <= self._clock.now:
            return 'now'
        return round(waketime - self._clock.now, 3)

    def register_timer(self, callback, waketime=NEVER):
        self._seq += 1
        t = _Timer(callback, waketime, self._seq)
        self._timers.append(t)
        self._h.trace('timer', 'register', t.name, self._rel(waketime))
        return t

    def update_timer(self, timer, waketime):
        timer.waketime = waketime
        self._h.trace('timer', 'update', timer.name, self._rel(waketime))

    def unregister_timer(self, timer):
        timer.waketime = self.NEVER
        if timer in self._timers:
            self._timers.remove(timer)
        self._h.trace('timer', 'unregister', timer.name)

    def register_callback(self, callback, waketime=NOW):
        holder = {}

        def _once(eventtime):
            self._timers.remove(holder['t'])
            callback(eventtime)
            return self.NEVER
        _once.__qualname__ = 'callback:' + callback_name(callback)
        holder['t'] = self.register_timer(_once, waketime)
        return None

    def register_async_callback(self, callback, waketime=NOW):
        # Tagged with the calling thread: a drain sorts by (thread, arrival),
        # so two threads reporting in the same quiet window (reader and
        # writer of one unit after a USB pull) register in a fixed order -
        # one of the orders the real reactor can see, always the same one.
        with self._h.cv:
            self._async_seq += 1
            self._async.put((_real_threading.current_thread().name,
                             self._async_seq, callback, waketime))
            self._h.cv.notify_all()

    def register_fd(self, fd, read_callback, write_callback=None):
        if self._h.fail_fd_registers > 0:
            # Scenario knob, see _TrackedThread.start: the V1 reader
            # registration is the first step after the connected mark.
            self._h.fail_fd_registers -= 1
            self._h.trace('fault', 'register_fd_failed', self._h.fd_label(fd))
            raise RuntimeError('injected: register_fd failed')
        fh = _FileHandler(fd, read_callback, write_callback)
        self._fds.append(fh)
        self._h.trace('fd', 'register', self._h.fd_label(fd))
        return fh

    def unregister_fd(self, fh):
        if fh in self._fds:
            self._fds.remove(fh)
        self._h.trace('fd', 'unregister', self._h.fd_label(fh.fd))

    def set_fd_wake(self, fh, is_readable=True, is_writable=False):
        pass

    # -- dispatch -------------------------------------------------------

    def _drain_async(self):
        batch = []
        while True:
            try:
                batch.append(self._async.get_nowait())
            except _real_queue.Empty:
                break
        if not batch:
            return False
        batch.sort(key=lambda x: (x[0], x[1]))
        for _thread, _seq, cb, wt in batch:
            if wt == self.NOW:
                wt = self._clock.now
            self.register_callback(cb, wt)
        return True

    def _step(self):
        """Run ONE unit of due work. Returns False when nothing is due."""
        now = self._clock.now
        if self._drain_async():
            return True
        if self._h.run_internal(now):
            return True
        for fh in list(self._fds):
            ser = self._h.serial_for_fd(fh.fd)
            if ser is not None and ser.readable_on_reactor():
                fh.read_callback(now)
                return True
        due = [t for t in self._timers if t.waketime <= now]
        if not due:
            return False
        t = min(due, key=lambda x: (x.waketime, x.seq))
        t.waketime = self.NEVER
        res = t.callback(now)
        if t in self._timers:
            if res is None:
                self._h.trace('timer', 'returned_none', t.name)
                res = self.NEVER
            t.waketime = res
        return True

    def _next_wake(self):
        wakes = [t.waketime for t in self._timers]
        nxt = self._h.next_internal()
        if nxt is not None:
            wakes.append(nxt)
        return min(wakes) if wakes else self.NEVER

    def run_until(self, until):
        self._depth += 1
        if self._depth > self.MAX_DEPTH:
            self._depth -= 1
            raise RuntimeError('reactor pause nested deeper than %d'
                               % self.MAX_DEPTH)
        try:
            while True:
                self._h.settle()
                if self._step():
                    continue
                if self._clock.now >= until:
                    break
                nxt = self._next_wake()
                target = min(nxt, until)
                if target <= self._clock.now:
                    target = until
                self._clock.now = target
                if target >= until:
                    # Run what is due at the end instant, then leave in a
                    # QUIET state: a request the last tick queued must be on
                    # the wire (and traced) before the caller goes on, or
                    # a disconnect right after would race the writer.
                    self._h.settle()
                    while self._step():
                        self._h.settle()
                    break
        finally:
            self._depth -= 1
        return self._clock.now

    def pause(self, waketime):
        return self.run_until(waketime)


# ---------------------------------------------------------------------------
# threads and queues of ace.py, tracked for quiescence

class _TrackedThread(_real_threading.Thread):
    # The harness is bound per thread at start(): a thread of an earlier
    # scenario that is still winding down must never touch the counters of
    # the scenario that runs now.
    harness = None

    def start(self):
        self._h = h = _TrackedThread.harness
        if h.skip_thread_starts > 0:
            h.skip_thread_starts -= 1
        elif h.fail_thread_starts > 0:
            # Scenario knob: the thread never starts (an exception in
            # _open_ace after the unit was marked connected).
            h.fail_thread_starts -= 1
            h.trace('fault', 'thread_start_failed', self.name)
            raise RuntimeError('injected: thread start failed')
        with h.cv:
            h.active += 1
            h.threads.append(self)
        h.trace('thread', 'start', self.name)
        super().start()

    def run(self):
        h = self._h
        try:
            super().run()
        finally:
            with h.cv:
                h.active -= 1
                h.cv.notify_all()


class _TrackedQueue(_real_queue.Queue):
    harness = None

    def __init__(self, maxsize=0):
        super().__init__(maxsize)
        self._h = _TrackedQueue.harness
        self._h.queues.append(self)

    def get(self, block=True, timeout=None):
        if not block:
            return super().get(False)
        h = self._h
        deadline = None if timeout is None else _real_time.monotonic() + timeout
        with h.cv:
            h.active -= 1
            h.cv.notify_all()
        try:
            while True:
                try:
                    return super().get(True, 0.01)
                except _real_queue.Empty:
                    if deadline is not None and _real_time.monotonic() >= deadline:
                        raise
        finally:
            with h.cv:
                h.active += 1

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        with self._h.cv:
            self._h.cv.notify_all()


def make_threading_shim(harness):
    shim = types.ModuleType('threading')
    for attr in dir(_real_threading):
        if not attr.startswith('__'):
            setattr(shim, attr, getattr(_real_threading, attr))
    _TrackedThread.harness = harness
    shim.Thread = _TrackedThread
    return shim


def make_queue_shim(harness):
    shim = types.ModuleType('queue')
    for attr in dir(_real_queue):
        if not attr.startswith('__'):
            setattr(shim, attr, getattr(_real_queue, attr))
    _TrackedQueue.harness = harness
    shim.Queue = _TrackedQueue
    return shim


# ---------------------------------------------------------------------------
# serial

class FakeSerial:
    """pyserial surface as ace.py uses it. `threaded` units (V2) are read
    by a thread that blocks in read(); V1 is polled through the reactor fd."""

    def __init__(self, harness, unit, threaded):
        self._h = harness
        self.unit = unit
        self.threaded = threaded
        self.rx = bytearray()
        self.is_open = True
        self._fd = harness.alloc_fd(self)
        self.fail_read = None
        self.fail_write = None
        self.mute = False
        # A fault the reader thread has been told about but not yet met:
        # counts as pending work, so the main thread does not run on
        # before the reader has reported it (the real reactor would see
        # the poll error before the next heartbeat too).
        self.fault_pending = False

    @property
    def in_waiting(self):
        # pyserial asks the kernel (TIOCINQ); on a vanished tty that raises.
        # A pending one-shot read error reports a byte so the caller goes
        # on to read() and meets the error there, as with a real EIO.
        with self._h.cv:
            if self._gone():
                raise self._h.serial_exception(
                    '[Errno 5] Input/output error')
            if self.fail_read is not None:
                return max(1, len(self.rx))
            return len(self.rx)

    def fileno(self):
        return self._fd

    def reset_input_buffer(self):
        with self._h.cv:
            del self.rx[:]

    def flush(self):
        pass

    def close(self):
        with self._h.cv:
            self.is_open = False
            self._h.cv.notify_all()
        self._h.trace('serial', 'close', self.unit.label)

    def readable_on_reactor(self):
        return (not self.threaded) and self.is_open and (
            bool(self.rx) or self.fail_read is not None or self._gone())

    def _raise_read(self):
        self.fault_pending = False
        err = self.fail_read
        self.fail_read = None
        if err is None:
            err = self._h.serial_exception(
                'device reports readiness to read but returned no data '
                '(device disconnected or multiple access on port?)')
        raise err

    def _gone(self):
        return not self.unit.plugged

    def read(self, size=1):
        h = self._h
        with h.cv:
            if not self.threaded:
                if self.fail_read is not None or self._gone():
                    self._raise_read()
                data = bytes(self.rx[:size])
                del self.rx[:size]
                return data
            waited = 0
            while (self.is_open and not self.rx and self.fail_read is None
                   and not self._gone()):
                h.active -= 1
                h.cv.notify_all()
                try:
                    h.cv.wait(0.05)
                finally:
                    h.active += 1
                waited += 1
                # A CFS link waits for its reply inside the writer thread
                # (half duplex); its misses count, keep them short.
                if waited >= (1 if self.unit.kind == 'cfs' else 10):
                    return b''
            if not self.is_open:
                return b''
            if self.fail_read is not None or self._gone():
                self._raise_read()
            data = bytes(self.rx[:size])
            del self.rx[:size]
            return data

    def write(self, data):
        if not self.is_open:
            raise self._h.serial_exception('Attempting to use a port that is '
                                           'not open')
        if self.fail_write is not None:
            err = self.fail_write
            self.fail_write = None
            raise err
        if self._gone():
            raise self._h.serial_exception('[Errno 5] Input/output error')
        data = bytes(data)
        self._h.trace('tx', self.unit.label, data.hex())
        if self.mute or getattr(self.unit, 'unpowered', False):
            return len(data)
        for latency, reply in self.unit.device.on_bytes(data):
            if self.unit.kind == 'cfs':
                # The CFS link blocks the writer thread on its reply, so
                # the clock cannot move meanwhile: the box answers at once.
                self._inject(reply)
                continue
            self._h.schedule(latency, self._inject, reply, key=self.unit.label)
        return len(data)

    def _inject(self, reply):
        with self._h.cv:
            if not self.is_open:
                return
            self.rx.extend(reply)
            self._h.cv.notify_all()
        self._h.trace('rx', self.unit.label, bytes(reply).hex())


class Unit:
    """One fake device on the bus: a path, a protocol and a model."""

    def __init__(self, label, kind, path, device, usb_ids):
        self.label = label
        self.kind = kind
        self.path = path
        self.device = device
        self.usb_ids = usb_ids
        self.plugged = True
        self.unpowered = False
        self.serial = None


# ---------------------------------------------------------------------------
# log capture

_TS_RE = re.compile(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')
_MS_RE = re.compile(r'time=\d+(\.\d+)?ms')
_ADDR_RE = re.compile(r'0x[0-9a-fA-F]{6,}')
_DROP_MARKERS = ('[DBG]', 'caller chain')


class TraceLogHandler(logging.Handler):
    def __init__(self, harness):
        super().__init__(logging.DEBUG)
        self._h = harness

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        first = msg.split('\n', 1)[0]
        for marker in _DROP_MARKERS:
            if marker in first:
                return
        self._h.trace('log', record.name, record.levelname, self._h.norm(first))


NAMED_LOGGERS = ('multiace_usb', 'multiace_state', 'multiace_telemetry',
                 'multiace_wiggle', 'multiace_fa', 'multiace_feedlog')


# ---------------------------------------------------------------------------
# the harness

class Harness:
    def __init__(self, units, config_overrides=None, save_vars=None,
                 ace_section=None, extra_sections=None):
        self.A = load_ace()
        self._core_sha = self.A.compute_core_sha1()
        self.P1, self.P2 = protocol_modules()
        self.clock = VirtualClock()
        self.cv = _real_threading.Condition()
        self.active = 0
        self.threads = []
        self.queues = []
        self.units = list(units)
        self.by_path = {u.path: u for u in self.units}
        self._fds = {}
        self._next_fd = 100
        self._internal = []
        self._internal_seq = 0
        self.entries = []
        self._thread_entries = []
        self._thread_seq = 0
        self.tmp = tempfile.mkdtemp(prefix='multiace-h0-')
        self._patches = []
        self._log_handler = None
        self.inst = None
        self.config_overrides = dict(config_overrides or {})
        self.save_vars = dict(save_vars or {})
        self.ace_section = ace_section or 'ace'
        self.extra_sections = dict(extra_sections or {})
        self.accessed = set()
        self.contract = None
        self.fail_thread_starts = 0
        self.skip_thread_starts = 0
        self.fail_fd_registers = 0

    # -- trace ----------------------------------------------------------

    def trace(self, kind, *fields):
        entry = [round(self.clock.now, 3), kind] + list(fields)
        if _real_threading.current_thread() is _real_threading.main_thread():
            self.entries.append(entry)
            return
        # Entries from ace.py's own threads (the V2 writer logs tx, a
        # reader error logs) are parked and flushed at the next settle():
        # that point is fixed in the main thread's control flow, so the
        # trace order does not depend on thread scheduling.
        with self.cv:
            self._thread_seq += 1
            self._thread_entries.append(
                (_real_threading.current_thread().name, self._thread_seq,
                 entry))

    def norm(self, text):
        text = text.replace(self.tmp, '<tmp>')
        # The build tag moves with every stamped commit and the version
        # with every release; neither is behaviour.
        text = text.replace(self.A.MULTIACE_BUILD_TAG, '<build>')
        text = text.replace(self.A.MULTIACE_VERSION, '<version>')
        # core= hashes the helper modules on disk, i.e. it moves with every
        # edit to any of them (R0 banner line).
        text = text.replace(self._core_sha, '<core>')
        text = _TS_RE.sub('<ts>', text)
        text = _MS_RE.sub('time=<ms>', text)
        text = _ADDR_RE.sub('0x<addr>', text)
        return text

    # -- fds ------------------------------------------------------------

    def alloc_fd(self, ser):
        fd = self._next_fd
        self._next_fd += 1
        self._fds[fd] = ser
        return fd

    def serial_for_fd(self, fd):
        return self._fds.get(fd)

    def fd_label(self, fd):
        ser = self._fds.get(fd)
        return ser.unit.label if ser is not None else 'fd%d' % fd

    # -- internal (device-side) schedule ----------------------------------

    def schedule(self, delay, fn, *args, key=''):
        """Device-side event at virtual now+delay. `key` (the unit label for
        a reply) orders events that fall on the same instant; the arrival
        sequence alone would follow thread timing."""
        with self.cv:
            self._internal_seq += 1
            self._internal.append((self.clock.now + delay, key,
                                   self._internal_seq, fn, args))
            self.cv.notify_all()

    def next_internal(self):
        with self.cv:
            if not self._internal:
                return None
            return min(e[0] for e in self._internal)

    def run_internal(self, now):
        with self.cv:
            due = [e for e in self._internal if e[0] <= now]
            if not due:
                return False
            e = min(due, key=lambda x: (x[0], x[1], x[2]))
            self._internal.remove(e)
        e[3](*e[4])
        return True

    # -- quiescence -------------------------------------------------------

    def _quiet(self):
        if self.active != 0:
            return False
        for q in self.queues:
            if not q.empty():
                return False
        for u in self.units:
            s = u.serial
            if s is not None and s.threaded and s.is_open and (
                    s.rx or s.fault_pending):
                return False
        return True

    def settle(self, timeout=10.0):
        deadline = _real_time.monotonic() + timeout
        with self.cv:
            while not self._quiet():
                self.cv.wait(0.02)
                if _real_time.monotonic() > deadline:
                    raise RuntimeError('harness did not settle: active=%d '
                                       'queues=%s' % (
                                           self.active,
                                           [q.qsize() for q in self.queues]))
            if self._thread_entries:
                # Several of ace.py's threads may have written between two
                # settle points (an open worker, the V2 readers and
                # writers). Their interleaving is scheduling noise; the
                # order WITHIN one thread is causal and kept: sort by
                # thread name, then arrival.
                self._thread_entries.sort(key=lambda e: (e[0], e[1]))
                self.entries.extend(e[2] for e in self._thread_entries)
                self._thread_entries = []

    # -- pyserial ---------------------------------------------------------

    def serial_exception(self, msg):
        import serial as _pyserial
        return _pyserial.serialutil.SerialException(msg)

    def open_transport(self, path, kind):
        unit = self.by_path.get(path)
        if unit is None or not unit.plugged:
            raise self.serial_exception(
                "[Errno 2] could not open port %s: [Errno 2] No such file or "
                "directory: '%s'" % (path, path))
        if unit.kind != kind:
            # Discovery decided the protocol; a mismatch here is a harness
            # bug, not a device behaviour.
            raise RuntimeError('unit %s opened as %s' % (unit.label, kind))
        ser = FakeSerial(self, unit, threaded=(kind in ('v2', 'cfs')))
        unit.serial = ser
        self.trace('serial', 'open', unit.label)
        return ser

    # -- patches ----------------------------------------------------------

    def _patch(self, obj, name, value):
        self._patches.append((obj, name, obj.__dict__.get(name, _SENTINEL)))
        setattr(obj, name, value)

    def _install(self):
        A, P1, P2 = self.A, self.P1, self.P2
        time_shim = make_time_shim(self.clock)
        self._patch(A, 'time', time_shim)
        self._patch(P2, 'time', time_shim)
        os_shim = make_os_shim(self)
        self._patch(A, 'os', os_shim)
        self._patch(P2, 'os', os_shim)
        self._patch(A, 'threading', make_threading_shim(self))
        self._patch(A, 'queue', make_queue_shim(self))
        PC = sys.modules[PKG + '.ace_protocol_cfs']
        self._patch(PC, 'time', time_shim)
        self._patch(PC, 'queue', make_queue_shim(self))
        h = self

        def discover_v1(cls):
            return [u.path for u in h.units if u.kind == 'v1' and u.plugged]

        def discover_v2(cls):
            return [u.path for u in h.units if u.kind == 'v2' and u.plugged]

        def is_official(cls, path):
            u = h.by_path.get(path)
            return bool(u and u.kind == 'v2' and u.plugged)

        def read_usb_ids(real_dev):
            for u in h.units:
                if os.path.basename(u.path) == real_dev and u.plugged:
                    return u.usb_ids
            return None, None

        def open_v1(cls, path, baud, **kw):
            return h.open_transport(path, 'v1')

        def open_v2(cls, path, baud, **kw):
            return h.open_transport(path, 'v2')

        def probe_uid_v2(cls, path, query_timeout=0.35):
            # D4 startup probe: a plugged, powered ACE 2 answers its uid
            # key; anything else is silent (''), like the real bounded
            # probe on an unpowered or foreign port.
            u = h.by_path.get(path)
            ok = bool(u and u.kind == 'v2' and u.plugged
                      and not getattr(u, 'unpowered', False))
            key = P2.AceProtocolV2.uid_key(u.device.uid) if ok else ''
            h.trace('probe', 'uid', path, key)
            return key, ('' if ok else 'no_response: unit silent')

        self._patch(P1.AceProtocolV1, 'discover', classmethod(discover_v1))
        self._patch(P2.AceProtocolV2, 'discover', classmethod(discover_v2))
        self._patch(P2.AceProtocolV2, 'is_official_usb_path',
                    classmethod(is_official))
        self._patch(sys.modules[PKG + '.ace_protocol'].AceProtocol,
                    '_read_usb_ids', staticmethod(read_usb_ids))
        self._patch(P1.AceProtocolV1, 'open_transport', classmethod(open_v1))
        self._patch(P2.AceProtocolV2, 'open_transport', classmethod(open_v2))
        self._patch(P2.AceProtocolV2, 'probe_uid', classmethod(probe_uid_v2))

        def discover_cfs(cls):
            return [u.path for u in h.units if u.kind == 'cfs' and u.plugged]

        def open_raw_cfs(cls, path, baud):
            return h.open_transport(path, 'cfs')

        def probe_cfs(cls, path, query_timeout=0.4):
            # The pre-claim probe: a plugged, powered, talking box answers
            # its unique id; anything else is silent.
            u = h.by_path.get(path)
            dev = getattr(u, 'device', None)
            ok = bool(u and u.kind == 'cfs' and u.plugged
                      and not getattr(u, 'unpowered', False)
                      and (dev.mute_until is None
                           or h.clock.now >= dev.mute_until))
            words = PC.uid_words(dev.uid) if ok else ()
            h.trace('probe', 'cfs', path, PC.uid_key(words))
            if not ok:
                return {'ok': False, 'category': 'no_response',
                        'error': 'no GET_SLAVE_INFO reply'}
            return {'ok': True, 'category': 'verified', 'model': PC.MODEL,
                    'firmware': '13680000140289S226GSHT', 'uid': words,
                    'addr': 0x35}

        self._patch(PC.AceProtocolCfs, 'discover', classmethod(discover_cfs))
        self._patch(PC.AceProtocolCfs, 'open_raw', classmethod(open_raw_cfs))
        self._patch(PC.AceProtocolCfs, 'probe_device', classmethod(probe_cfs))
        # M2: the request/response contract (contract.py) judged live.
        from .contract import ContractChecker
        self.contract = ContractChecker(self)
        self.contract.install()

    def _uninstall(self):
        for obj, name, old in reversed(self._patches):
            if old is _SENTINEL:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, old)
        self._patches = []

    # -- logging ----------------------------------------------------------

    def _attach_logging(self):
        self._log_handler = TraceLogHandler(self)
        root = logging.getLogger()
        self._root_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(self._log_handler)
        for name in NAMED_LOGGERS:
            lg = logging.getLogger(name)
            for hd in list(lg.handlers):
                lg.removeHandler(hd)
                try:
                    hd.close()
                except Exception:
                    pass
            lg.addHandler(self._log_handler)

    def _detach_logging(self):
        root = logging.getLogger()
        root.removeHandler(self._log_handler)
        root.setLevel(self._root_level)
        for name in NAMED_LOGGERS:
            lg = logging.getLogger(name)
            for hd in list(lg.handlers):
                lg.removeHandler(hd)
                try:
                    hd.close()
                except Exception:
                    pass
            lg.setLevel(logging.NOTSET)
            lg.propagate = True

    # -- build --------------------------------------------------------------

    def _printer_data(self):
        pd = os.path.join(self.tmp, 'printer_data')
        cfg = os.path.join(pd, 'config')
        os.makedirs(os.path.join(cfg, 'extended', 'multiace'), exist_ok=True)
        os.makedirs(os.path.join(pd, 'logs'), exist_ok=True)
        shutil.copy(SHIPPED_ACE_CFG, os.path.join(cfg, 'extended', 'ace.cfg'))
        with open(os.path.join(cfg, 'printer.cfg'), 'w') as f:
            f.write('[include extended/ace.cfg]\n')
        return pd, cfg

    def build(self):
        pd, cfg_dir = self._printer_data()
        os.environ['MULTIACE_CONFIG_DIR'] = cfg_dir
        os.environ['MULTIACE_PRINTER_DATA'] = pd
        self._install()
        self._attach_logging()

        sections = shipped_sections()
        ace_opts = dict(sections.get('ace', {}))
        ace_opts.update({
            'log_dir': os.path.join(pd, 'logs'),
            'web_dir': os.path.join(self.tmp, 'no-web'),
            'i18n_dir': I18N_DIR,
            'homing_flag_path': os.path.join(self.tmp, 'homing_active'),
        })
        ace_opts.update(self.config_overrides)
        sections['ace'] = ace_opts
        sections.setdefault('mcu', {'serial': '/dev/serial/by-id/usb-Klipper_u1-if00'})
        for name, opts in self.extra_sections.items():
            sections[name] = dict(opts)

        self.reactor = FakeReactor(self)
        self.gcode = FakeGcode(self)
        save_vars = {'ace__mode': 'multi'}
        save_vars.update(self.save_vars)
        self.save_variables = FakeSaveVariables(save_vars)
        self.print_stats = FakePrintStats()
        self.toolhead = FakeToolhead()
        self.motion_report = FakeMotionReport()
        objects = {
            'gcode': self.gcode,
            'save_variables': self.save_variables,
            'webhooks': FakeWebhooks(self),
            'print_stats': self.print_stats,
            'toolhead': self.toolhead,
            'motion_report': self.motion_report,
        }
        self.printer = FakePrinter(
            self, self.reactor, objects,
            {'config_file': os.path.join(cfg_dir, 'printer.cfg')})
        config = FakeConfig(self.printer, 'ace', ace_opts, sections,
                            self.accessed)
        self.inst = self.A.load_config(config)
        self.trace('built', 'MultiAce')
        return self.inst

    def ready(self):
        self.printer.send_event('klippy:ready')
        self.trace('ready', 'done')

    # -- printer-side events a scenario raises ------------------------------

    def print_start(self):
        """Stock order: print_stats goes 'printing', then print_stats:start
        fires (virtual_sdcard work handler)."""
        self.print_stats.state = 'printing'
        self.trace('event', 'print_stats:start')
        self.printer.send_event('print_stats:start')

    def print_pause(self):
        """The print is paused (stock PAUSE): print_stats goes 'paused',
        no event; a later print_start() is the RESUME (print_stats:start
        fires again, see virtual_sdcard.work_handler)."""
        self.print_stats.state = 'paused'
        self.trace('event', 'print_stats paused')

    def print_end(self, state='complete'):
        self.print_stats.state = state
        self.trace('event', 'print_stats:stop')
        self.printer.send_event('print_stats:stop')

    def tool_change(self, head):
        """The stock T<n> pick: toolhead extruder switched, then the
        extruder:activate_extruder event."""
        self.toolhead.set_extruder(head)
        self.trace('event', 'extruder:activate_extruder', head)
        self.printer.send_event('extruder:activate_extruder')

    def extruding(self, mm_s):
        """The print extrudes at mm_s (0 = idle), as motion_report reports."""
        self.motion_report.live_extruder_velocity = float(mm_s)
        self.trace('event', 'extruding', float(mm_s))

    def gcode_line(self, line):
        """A console/macro line, as the user or a macro would issue it."""
        self.gcode.run_script(line)

    def hold_gcode(self, seconds):
        """A command holds the gcode mutex for `seconds` from now: every
        run_script of ace.py in that window waits (reactor keeps running),
        like a background timer calling into a printer mid-swap."""
        self.gcode.hold_until = self.clock.now + seconds
        self.trace('fault', 'hold_gcode', seconds)

    def run(self, seconds):
        self.reactor.run_until(self.clock.now + seconds)

    def snapshot(self):
        status = self.inst.get_status(self.clock.now)
        bad = non_str_keys(status)
        self.trace('status', json.loads(json.dumps(status, default=str,
                                                   sort_keys=True)))
        if bad:
            self.trace('status_bad_keys', bad)
        return status

    def at(self, delay, fn, *args):
        """Scenario hook: run fn at virtual now+delay from the main thread,
        between two reactor steps. Fault injections use this so their
        timing does not depend on how deep ace.py is nested in a pause."""
        self.schedule(delay, fn, *args)

    def finish(self):
        self.trace('cfg_read', sorted(self.accessed))
        try:
            self.printer.send_event('klippy:disconnect')
        finally:
            self.settle(timeout=5.0)
        self._detach_logging()
        self._uninstall()
        for t in self.threads:
            t.join(timeout=5.0)
        alive = [t.name for t in self.threads if t.is_alive()]
        if alive:
            raise RuntimeError('threads of ace.py still alive after '
                               'disconnect: %s' % alive)
        shutil.rmtree(self.tmp, ignore_errors=True)
        return self.entries

    # -- fault injection ----------------------------------------------------

    def unplug(self, label):
        """The cable is pulled: every read/write on the open port fails
        with what pyserial reports for a vanished tty, discovery no longer
        lists the path, open() fails until replug()."""
        u = self._unit(label)
        u.plugged = False
        self._arm_fault(u)
        self.trace('fault', 'unplug', label)

    def _arm_fault(self, u):
        with self.cv:
            s = u.serial
            if s is not None and s.threaded and s.is_open:
                s.fault_pending = True
            self.cv.notify_all()

    def inject_read_error(self, label):
        """One EIO on the next read (the device stays plugged in)."""
        u = self._unit(label)
        u.serial.fail_read = self.serial_exception(
            'device reports readiness to read but returned no data '
            '(device disconnected or multiple access on port?)')
        self._arm_fault(u)
        self.trace('fault', 'read_error', label)

    def fail_thread_start(self, count=1, after=0):
        """After `after` normal thread starts the next `count` raise.
        _open_ace starts the port-open worker ('aceN-open') BEFORE the
        connected mark and the V2 reader/writer AFTER it, so after=1 hits
        the late path: a connect that fails on a unit already marked
        connected."""
        self.skip_thread_starts += after
        self.fail_thread_starts += count

    def fail_register_fd(self, count=1):
        """The next `count` reactor.register_fd calls raise (the V1
        reader of _open_ace): same late connect failure for a V1."""
        self.fail_fd_registers += count

    def inject_write_error(self, label):
        """One EIO on the next write (the device stays plugged in)."""
        u = self._unit(label)
        u.serial.fail_write = self.serial_exception(
            '[Errno 5] Input/output error')
        self.trace('fault', 'write_error', label)

    def replug(self, label):
        u = self._unit(label)
        u.plugged = True
        self.trace('fault', 'replug', label)

    def power(self, label, on=True):
        """Unit unpowered behind a live USB link: the port still opens,
        nothing ever answers, also after a reopen (unlike mute, which is
        per open port)."""
        u = self._unit(label)
        u.unpowered = not on
        self.trace('fault', 'power_on' if on else 'power_off', label)

    def mute(self, label, on=True):
        u = self._unit(label)
        if u.serial is not None:
            u.serial.mute = on
        self.trace('fault', 'mute' if on else 'unmute', label)

    def _unit(self, label):
        for u in self.units:
            if u.label == label:
                return u
        raise KeyError(label)


def non_str_keys(obj, path=''):
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                bad.append('%s/%r' % (path, k))
            bad.extend(non_str_keys(v, '%s/%s' % (path, k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            bad.extend(non_str_keys(v, '%s[%d]' % (path, i)))
    return bad


# ---------------------------------------------------------------------------
# goldens

def write_golden(name, entries):
    os.makedirs(GOLDENS, exist_ok=True)
    path = os.path.join(GOLDENS, name + '.jsonl')
    with open(path, 'w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, sort_keys=True, ensure_ascii=False) + '\n')
    return path


def read_golden(name):
    path = os.path.join(GOLDENS, name + '.jsonl')
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def canonical(entries):
    return [json.loads(json.dumps(e, sort_keys=True, ensure_ascii=False))
            for e in entries]


def diff_entries(expected, actual, context=3):
    """A short human-readable account of the first divergence."""
    n = min(len(expected), len(actual))
    for i in range(n):
        if expected[i] != actual[i]:
            lines = ['first divergence at entry %d (expected %d entries, got %d)'
                     % (i, len(expected), len(actual))]
            for j in range(max(0, i - context), min(n, i + context + 1)):
                mark = '!!' if expected[j] != actual[j] else '  '
                lines.append('%s exp[%d] %s' % (mark, j, json.dumps(expected[j], sort_keys=True)[:400]))
                if expected[j] != actual[j]:
                    lines.append('%s act[%d] %s' % (mark, j, json.dumps(actual[j], sort_keys=True)[:400]))
            return '\n'.join(lines)
    if len(expected) != len(actual):
        longer = expected if len(expected) > len(actual) else actual
        who = 'expected' if longer is expected else 'actual'
        return ('same prefix, %s has %d extra entries, first: %s'
                % (who, abs(len(expected) - len(actual)),
                   json.dumps(longer[n], sort_keys=True)[:400]))
    return ''
