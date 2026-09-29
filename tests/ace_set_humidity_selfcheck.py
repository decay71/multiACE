#!/usr/bin/env python3
"""Self-check for external humidity (ACE_SET_HUMIDITY) in ace.py.

The repository has no test suite (no CI beyond the release tarball), so
this is the test-in-a-script - like
tools/gen1_flasher_selfcheck.py. It imports the REAL multiACE module
(klipper/extras/ace.py, from the repo checkout or from a printer install)
and drives the real methods on a hand-built instance: no Klipper, no
hardware, no serial ports.

Covered, as required for the PR:
  (a) parsing/validation of ACE_SET_HUMIDITY (ranges, clamping, _info_per_ace
      stays untouched);
  (b) a FRESH external reading is preferred over the unit's own sensor;
  (c) a STALE/absent reading falls back to the internal value and the source
      is reported accordingly;
  (d) a cycle started from the external reading is STOPPED when it expires -
      for an ACE 2 (V2) and for an ACE Pro (V1) alike - while a cycle started
      from the internal sensor, or by hand, is not touched.

Run it after touching ace.py:

    python3 tests/ace_set_humidity_selfcheck.py

Exit codes:
    0  every check passed
    1  a check FAILED
    2  usage/setup error (ace.py not found or not importable)
"""

from __future__ import annotations

import copy
import importlib
import logging
import os
import sys
import types

# The INFO trails of ace.py are not the report - the checks are.
logging.disable(logging.INFO)

# --- locate the real module (repo checkout OR printer install) -------------

def find_extras():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("MULTIACE_KLIPPY_EXTRAS"),
        os.path.join(os.path.dirname(here), "multiace",
                     "klipper", "extras"),                        # repo
        "/home/lava/klipper/klippy/extras",                       # printer
        os.path.join(os.path.expanduser("~"), "klipper", "klippy", "extras"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "ace.py")):
            return c
    return None


EXTRAS = find_extras()
if EXTRAS is None:
    print("[FAIL] ace.py not found - set MULTIACE_KLIPPY_EXTRAS to the "
          "klippy/extras directory")
    sys.exit(2)

# Import through a synthetic package whose __path__ is the extras directory:
# ace.py's relative imports resolve, and no real 'extras' package can shadow
# it (an installed Klipper keeps its own; a dev box may have anything).
_pkg = types.ModuleType("multiace_selfcheck_extras")
_pkg.__path__ = [EXTRAS]
sys.modules[_pkg.__name__] = _pkg
try:
    A = importlib.import_module(_pkg.__name__ + ".ace")
except Exception as e:                                      # pragma: no cover
    print("[FAIL] cannot import ace.py from %s: %s" % (EXTRAS, e))
    sys.exit(2)

# The multiACE package directory (this file lives in tests/ at the repo root).
REPO = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "multiace")
CATALOG = A._load_i18n_catalog(os.path.join(REPO, "i18n"), "en")

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("[ok]   %s" % name)
    else:
        print("[FAIL] %s %s" % (name, detail))
        FAILED.append(name)


def _t(key, **params):
    v = CATALOG
    for p in key.split('.'):
        v = v.get(p) if isinstance(v, dict) else None
    if not isinstance(v, str):
        return key
    try:
        return v.format(**params)
    except Exception:
        return v


# --- fakes: just enough of Klipper for the real ace.py methods -------------

class FakeError(Exception):
    pass


class FakeGcmd:
    def __init__(self, **params):
        self.params = {k: str(v) for k, v in params.items()}
        self.info = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        v = self.params.get(name, default)
        if v is None:
            return None
        v = int(v)
        if minval is not None and v < minval:
            raise FakeError('%s below minval' % name)
        if maxval is not None and v > maxval:
            raise FakeError('%s above maxval' % name)
        return v

    def error(self, message=None, **kw):
        return FakeError(message if message is not None else str(kw))

    def respond_info(self, msg):
        self.info.append(msg)


class FakeReactor:
    NEVER = 1e18

    def __init__(self):
        self.now = 1000.0
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, cb, when):
        self.timers.append((cb, when))
        return len(self.timers)

    def pause(self, until):
        pass


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == 'print_stats':
            return types.SimpleNamespace(state='idle')
        return default


class FakeProto:
    def __init__(self, name):
        self.NAME = name


def make_ace(protocols=('v2', 'v1'), humidity=(30.0, None)):
    """A real MultiAce with just the attributes the tested methods use.
    send_request_to records traffic and answers None (an absent device):
    the bookkeeping must be right without any reply."""
    inst = A.MultiAce.__new__(A.MultiAce)
    inst.printer = FakePrinter()
    inst.reactor = inst.printer.reactor
    inst.gcode = types.SimpleNamespace(respond_raw=lambda m: None)
    inst._ace_devices = ['/dev/fake-ace%d' % i for i in range(len(protocols))]
    inst._protocols = {i: FakeProto(n) for i, n in enumerate(protocols)}
    inst._info_per_ace = {}
    for i, h in enumerate(humidity):
        info = {'dryer_status': {'status': 'stop'}}
        if h is not None:
            info['humidity'] = h
        inst._info_per_ace[i] = info
    inst._connected_per_ace = {i: True for i in range(len(protocols))}
    inst._auto_dry_cfg = {}
    inst.auto_dry_default = {
        'enabled': False, 'rh_start': 45., 'rh_end': 35., 'temp': 50,
        'master': -1, 'add_time': 60}
    inst._auto_dry_started = set()
    inst._auto_dry_ramp = {}
    inst._auto_dry_follow_until = {}
    inst._auto_dry_seen = {}
    inst._external_rh = {}
    inst._external_rh_cycle = set()
    inst.save_variables = None
    inst.dry_exhaust_delay = 0.
    inst.dry_auto_roll = False
    inst.max_dryer_temperature = 55
    inst._display_index_base = 0
    inst.auto_dry_while_printing = False
    inst._dry_exhaust_pending = {}
    inst._dry_exhaust_seq = 0
    inst._i18n = CATALOG
    inst.sent = []              # (idx, request) wire trail
    inst.responses = []
    inst.errors = []
    inst._t = _t
    inst.log_always = lambda msg, color=False: inst.responses.append(msg)
    inst.log_error = lambda msg: inst.errors.append(msg)

    def send_request_to(idx, request, callback=None, **kw):
        inst.sent.append((idx, request))
        if callback is not None:
            callback(inst, None)   # no reply, like a reconnecting unit
    inst.send_request_to = send_request_to
    return inst


def push(inst, **params):
    g = FakeGcmd(**params)
    inst.cmd_ACE_SET_HUMIDITY(g)
    return g


def push_error(inst, **params):
    try:
        push(inst, **params)
    except FakeError:
        return True
    return False


def set_cfg(inst, **params):
    g = FakeGcmd(**params)
    inst.cmd_ACE_SET_AUTO_DRY(g)
    return g


def set_cfg_error(inst, **params):
    try:
        set_cfg(inst, **params)
    except FakeError:
        return True
    return False


def methods(inst, idx):
    return [r.get('method') for i, r in inst.sent if i == idx]


# --- 1. module + catalog sanity -------------------------------------------

check("real ace.py imported (%s)" % EXTRAS,
      hasattr(A, 'MultiAce') and hasattr(A.MultiAce, 'cmd_ACE_SET_HUMIDITY'))
check("ACE_SET_HUMIDITY message exists in en.json",
      CATALOG.get('msg', {}).get('ace_humidity_set') is not None)
check("the command is registered by __init__ (source scan)",
      "'ACE_SET_HUMIDITY'," in open(os.path.join(EXTRAS, 'ace.py'),
                                    encoding='utf-8').read())

# --- 2. (a) parsing / validation -------------------------------------------

inst = make_ace()
before_info = copy.deepcopy(inst._info_per_ace)

g = push(inst, ACE='0', RH='42.5', TEMP='23.5', TTL='1800')
st = inst._external_rh.get(0) or {}
resp = inst.responses[-1]
check("ACE_SET_HUMIDITY stores rh/temp/ttl/ts",
      st.get('rh') == 42.5 and st.get('temp') == 23.5
      and st.get('ttl') == 1800.0 and st.get('ts') == inst.reactor.now)
check("... and never writes _info_per_ace (1 Hz heartbeat owns it)",
      inst._info_per_ace == before_info)
check("... response names the ACE, value, TTL and 'external'",
      '42.5' in resp and '1800' in resp and 'external' in resp.lower()
      and ('ACE %d' % inst._disp(0)) in resp, resp)
check("... logs exactly one accepted push per call",
      len([r for r in inst.responses if 'external' in r.lower()]) == 1)

push(inst, ACE='0', RH='42.5', TEMP='23.5', TTL='1800')
check("idempotent re-push leaves the same store",
      inst._external_rh[0]['rh'] == 42.5
      and inst._external_rh[0]['ttl'] == 1800.0)

check("RH above range refused", push_error(inst, ACE='0', RH='101'))
check("RH below range refused", push_error(inst, ACE='0', RH='-0.5'))
check("non-numeric RH refused", push_error(inst, ACE='0', RH='abc'))
check("NaN RH refused", push_error(inst, ACE='0', RH='nan'))
check("missing RH refused", push_error(inst, ACE='0'))
check("implausible TEMP refused", push_error(inst, ACE='0', RH='50',
                                             TEMP='150'))
check("negative TEMP refused", push_error(inst, ACE='0', RH='50',
                                          TEMP='-5'))
check("TTL 0 refused", push_error(inst, ACE='0', RH='50', TTL='0'))
check("negative TTL refused", push_error(inst, ACE='0', RH='50', TTL='-60'))
check("non-numeric TTL refused", push_error(inst, ACE='0', RH='50',
                                            TTL='soon'))
check("unknown ACE index refused", push_error(inst, ACE='3', RH='50'))
check("refused command left the previous store alone",
      inst._external_rh[0]['rh'] == 42.5)

push(inst, ACE='1', RH='55', TTL='10')
check("TTL below MIN clamps up to 60s",
      inst._external_rh[1]['ttl'] == A.EXTERNAL_RH_MIN_TTL == 60.0)
check("clamp is named in the response",
      'clamp' in inst.responses[-1].lower(), inst.responses[-1])
push(inst, ACE='1', RH='55', TTL='999999')
check("TTL above MAX clamps down to 7200s",
      inst._external_rh[1]['ttl'] == A.EXTERNAL_RH_MAX_TTL == 7200.0)
push(inst, ACE='1', RH='55')
check("TTL defaults to 900s when absent",
      inst._external_rh[1]['ttl'] == A.EXTERNAL_RH_DEFAULT_TTL == 900.0)

# --- 3. (b) fresh external reading wins over the internal sensor -----------

inst = make_ace(humidity=(30.0, None))
push(inst, ACE='0', RH='55', TTL='900')
check("_ace_humidity prefers the fresh external reading (55 over 30)",
      inst._ace_humidity(0) == 55.0)
check("source reported as external", inst._ace_humidity_source(0) == 'external')
sts = inst._external_rh_status(0)
check("status: external_humidity_fresh=True, value 55",
      sts['external_humidity_fresh'] is True
      and sts['external_humidity'] == 55.0)
check("status: age climbs from 0, source external, effective 55",
      sts['external_humidity_age'] == 0.0
      and sts['humidity_source'] == 'external'
      and sts['humidity_effective'] == 55.0)

# --- 4. (c) stale reading falls back to internal, with the right labels ----

inst.reactor.now += 901
check("expired reading no longer drives control (30, the sensor)",
      inst._ace_humidity(0) == 30.0)
check("source falls back to internal",
      inst._ace_humidity_source(0) == 'internal')
sts = inst._external_rh_status(0)
check("status: fresh=False, true age reported, last value kept",
      sts['external_humidity_fresh'] is False
      and sts['external_humidity_age'] >= 901.0
      and sts['external_humidity'] == 55.0
      and sts['humidity_source'] == 'internal'
      and sts['humidity_effective'] == 30.0)
check("V1 with no sensor and no/expired external has no reading at all",
      inst._ace_humidity(1) is None and inst._ace_humidity_source(1) is None)

# --- 5. (d) expiry stop: V2 cycle started from external --------------------

inst = make_ace()
inst._auto_dry_cfg = {'0': {'enabled': True}}
push(inst, ACE='0', RH='60', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
check("V2: tick starts a cycle on the fresh external reading (60 >= 45)",
      inst._external_rh_cycle == {0} and 0 in inst._auto_dry_started
      and 'drying' in methods(inst, 0))
check("V2: start reason names the external source",
      any('external' in m for m in inst.responses), inst.responses)
inst.reactor.now += 301
inst._auto_dry_tick(inst.reactor.now)
check("V2: expiry STOPS the cycle we started",
      inst._external_rh_cycle == set()
      and 0 not in inst._auto_dry_started
      and 'drying_stop' in methods(inst, 0))
check("V2: one log line names the expiry reason",
      sum('expired' in m for m in inst.responses) == 1, inst.responses)
inst.sent = []
inst._auto_dry_tick(inst.reactor.now)
check("V2: no further stop traffic after the cycle ended",
      'drying_stop' not in methods(inst, 0))
# The feeder comes back: a fresh push starts a new cycle normally.
push(inst, ACE='0', RH='60', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
check("V2: a refreshed reading starts a new cycle after the expiry stop",
      0 in inst._auto_dry_started and inst._external_rh_cycle == {0}
      and 'drying' in methods(inst, 0))

# An ACE 2 with a valid internal reading: after the external expiry stop the
# normal internal rule applies again - a NEW cycle starts from the sensor,
# marked internal (the external value is gone, not silently reused).
inst = make_ace(humidity=(60.0, None))
inst._auto_dry_cfg = {'0': {'enabled': True}}
push(inst, ACE='0', RH='70', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
inst.reactor.now += 301
inst.sent = []
inst._auto_dry_tick(inst.reactor.now)
check("V2: expiry stops the external cycle; internal rule then re-starts",
      'drying_stop' in methods(inst, 0) and 'drying' in methods(inst, 0)
      and 0 in inst._auto_dry_started and inst._external_rh_cycle == set())

# A refresh WHILE the cycle runs keeps it ours beyond the old TTL.
inst = make_ace()
inst._auto_dry_cfg = {'0': {'enabled': True}}
push(inst, ACE='0', RH='60', TTL='120')
inst._auto_dry_tick(inst.reactor.now)
inst.reactor.now += 100
push(inst, ACE='0', RH='58', TTL='300')
inst.reactor.now += 100          # past the FIRST TTL, inside the second
inst.sent = []
inst._auto_dry_tick(inst.reactor.now)
check("V2: refresh extends the cycle (not stopped at the old TTL)",
      0 in inst._auto_dry_started and inst._external_rh_cycle == {0}
      and 'drying_stop' not in methods(inst, 0))

# --- 6. (d) expiry stop: V1 (ACE Pro) cycle started from external ----------

inst = make_ace()
inst._auto_dry_cfg = {'1': {'enabled': True}}       # master -1
push(inst, ACE='1', RH='70', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
check("V1: no V2-only gate - a Pro starts on its external reading",
      inst._external_rh_cycle == {1} and 1 in inst._auto_dry_started
      and 'drying' in methods(inst, 1))
inst.reactor.now += 301
inst._auto_dry_tick(inst.reactor.now)
check("V1: expiry stops the Pro cycle too",
      inst._external_rh_cycle == set()
      and 1 not in inst._auto_dry_started
      and 'drying_stop' in methods(inst, 1))
check("V1: what we started is what stops it (ownership was ours)",
      inst._external_rh_status(1)['external_humidity_cycle'] is False)

# Self-mastered variant (the convention the local feeder used): the unit
# points at ITSELF, and must not be started twice as its own follower.
inst = make_ace()
inst._auto_dry_cfg = {'1': {'enabled': True, 'master': 1}}
push(inst, ACE='1', RH='70', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
check("V1 self-mastered starts once, not as its own follower",
      1 in inst._auto_dry_started
      and methods(inst, 1).count('drying') == 1
      and inst._auto_dry_followers(1) == [])

# --- 7. (d) only cycles started from the external reading are stopped ------

# A V2 master started from external drives its Pro follower; the follower is
# a follower, never an "external cycle" itself.
inst = make_ace()
inst._auto_dry_cfg = {'0': {'enabled': True},
                      '1': {'enabled': True, 'master': 0}}
push(inst, ACE='0', RH='60', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
check("master start drives the follower, which is NOT marked external",
      0 in inst._auto_dry_started and 1 in inst._auto_dry_started
      and inst._external_rh_cycle == {0})
inst.reactor.now += 301
inst._auto_dry_tick(inst.reactor.now)
check("master expiry stops the master; the follower keeps its add-time",
      0 not in inst._auto_dry_started
      and 1 in inst._auto_dry_started
      and 1 in inst._auto_dry_follow_until)

# A follower driven by another unit's cycle does not self-start on its own
# pushed reading (its master's cycle owns it).
inst = make_ace()
inst._auto_dry_cfg = {'1': {'enabled': True, 'master': 0}}
push(inst, ACE='1', RH='70', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
check("V1 follower does not self-start on its own external reading",
      1 not in inst._auto_dry_started and inst._external_rh_cycle == set())

# A cycle started from the INTERNAL sensor is not killed by an external
# reading expiring (it never depended on it).
inst = make_ace(humidity=(60.0, None))
inst._auto_dry_cfg = {'0': {'enabled': True}}
inst._external_rh[0] = {'rh': 60.0, 'temp': None,
                        'ts': inst.reactor.now - 5000, 'ttl': 300}
inst._auto_dry_tick(inst.reactor.now)
check("internal-sensor cycle starts with no external dependence",
      0 in inst._auto_dry_started and inst._external_rh_cycle == set())
inst.sent = []
inst.reactor.now += 1
inst._auto_dry_tick(inst.reactor.now)
check("expiry leaves an internal-sensor cycle running (60 > stop 35)",
      'drying_stop' not in methods(inst, 0)
      and 0 in inst._auto_dry_started)

# A cycle the USER started (device drying, not ours) is never touched by the
# expiry path.
inst = make_ace()
inst._info_per_ace[1]['dryer_status'] = {'status': 'drying'}
inst._auto_dry_cfg = {'1': {'enabled': True}}
inst._external_rh[1] = {'rh': 70.0, 'temp': None,
                        'ts': inst.reactor.now - 5000, 'ttl': 300}
inst._auto_dry_tick(inst.reactor.now)
check("hand-started cycle untouched by expiry (nothing is ours)",
      'drying_stop' not in methods(inst, 1)
      and 1 not in inst._auto_dry_started)

# Stopping is not gated on auto-dry still being enabled.
inst = make_ace()
inst._auto_dry_cfg = {'1': {'enabled': True}}
push(inst, ACE='1', RH='70', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
check("... started, then auto-dry switched OFF mid-cycle",
      1 in inst._external_rh_cycle)
inst._auto_dry_cfg['1']['enabled'] = False
inst.reactor.now += 301
inst.sent = []
inst._auto_dry_tick(inst.reactor.now)
check("expiry still stops it (enabled=0 is not a way to dry on)",
      'drying_stop' in methods(inst, 1)
      and 1 not in inst._auto_dry_started)

# After a Klipper restart the reading store is empty but the persisted
# ownership note says the cycle came from one - the first tick stops it.
inst = make_ace()
inst._auto_dry_cfg = {'1': {'enabled': True}}
inst._auto_dry_started = {1}
inst._external_rh_cycle = {1}
inst.sent = []
inst._auto_dry_tick(inst.reactor.now)
check("restart: persisted external ownership stops the orphaned cycle",
      'drying_stop' in methods(inst, 1)
      and 1 not in inst._auto_dry_started
      and 1 not in inst._external_rh_cycle)
check("restart: reason names the missing reading",
      any('gone' in m for m in inst.responses), inst.responses)

# --- 8. relaxed config gates: a Pro with an external source is self-driven -

inst = make_ace()
check("ENABLE refused while the Pro has no master and no reading",
      set_cfg_error(inst, ACE='1', ENABLE='1'))
check("RH_START refused while the Pro has no reading",
      set_cfg_error(inst, ACE='1', RH_START='50'))
check("MASTER pointing at a unit without a reading refused",
      set_cfg_error(inst, ACE='1', MASTER='1'))
push(inst, ACE='1', RH='55')
set_cfg(inst, ACE='1', ENABLE='1')
check("ENABLE accepted once an external reading exists (own master)",
      inst._auto_dry_for(1)['enabled'] is True)
set_cfg(inst, ACE='1', RH_START='50', RH_END='40')
check("RH_START/RH_END accepted once an external reading exists",
      inst._auto_dry_for(1)['rh_start'] == 50.0
      and inst._auto_dry_for(1)['rh_end'] == 40.0)
check("hysteresis still enforced (END must stay below START)",
      set_cfg_error(inst, ACE='1', RH_END='60'))
# A V2 still follows nobody.
inst2 = make_ace()
check("ACE 2 still refuses follower settings",
      set_cfg_error(inst2, ACE='0', MASTER='0'))

# --- 9. role change mid-cycle is picked up as an orphan --------------------

inst = make_ace()
inst._auto_dry_cfg = {'1': {'enabled': True}}
push(inst, ACE='1', RH='70', TTL='300')
inst._auto_dry_tick(inst.reactor.now)
inst._external_rh.clear()                  # reading store lost
inst._external_rh_cycle.clear()            # ... and no persisted note
inst.sent = []
inst._auto_dry_tick(inst.reactor.now)
check("role change mid-cycle: no reading and no note -> stop, not dry on",
      'drying_stop' in methods(inst, 1)
      and 1 not in inst._auto_dry_started)

# --- 10. get_status payload: external flag, age, source, old keys intact ---

def status_ready(inst):
    """Everything get_status touches that __init__ would have set."""
    inst._ace_models = {0: ('ACE 2', 'V1.1.60'),
                        1: ('Color Engine Pro', 'CV1.3.863')}
    inst._gate_status_per_ace = {}
    inst._feed_assist_per_ace = {}
    inst._fw_update_hold = set()
    inst._spools = {}
    inst._spool_binding = {}
    inst._swap_phase = ''
    inst._last_swap_result = ''
    inst._event_seq = 0
    inst._head_source = {}
    inst._swap_in_progress = False
    inst._unload_all_active = False
    inst._calibration = {}
    inst._calibration_unload = {}
    inst._head_tag_seen = {}
    inst.head_ace = {h: h for h in range(4)}
    inst.head_manual = {h: False for h in range(4)}
    inst.head_feeder = {h: False for h in range(4)}
    inst._ace_mode = 'multi'
    inst._ace_head = 3
    inst._active_device_index = 0
    inst._info = {'status': 'ready', 'temp': 30, 'dryer_status': {}}
    inst.gate_status = [0, 0, 0, 0]
    inst._pickup_cleaning = False
    inst.preflight_max_copies = 1
    inst.preflight_copies_strict = False
    inst._confirm_commands = False
    inst.spoolman_url = ''
    inst.spoolman_auto = False
    inst.resistance_pause = False
    inst.quad_replenish = False
    inst.purge_matrix = True
    inst.pa_sync = True
    inst.tag_write_format = 'openspool'
    inst.tag_write_uid_sku = True
    inst.spool_mode = 'local'
    inst._any_open_fw = lambda: False
    inst._nozzle_keys_status = lambda: []
    inst._spoollink_active = lambda: False
    inst._spoollink_agent_present = lambda: False
    inst._ptc_spool_id_for = lambda h: 0
    inst.head_uses_ace = lambda h: True
    return inst


inst = status_ready(make_ace())
push(inst, ACE='0', RH='55', TEMP='23')
push(inst, ACE='1', RH='70')
st = inst.get_status()
a0, a1 = st['aces'][0], st['aces'][1]
check("get_status: existing keys intact + external flag/age/source",
      a0['idx'] == 0 and a0['humidity'] == 30.0 and 'auto_dry' in a0
      and a0['external_humidity'] == 55.0
      and a0['external_humidity_temp'] == 23.0
      and a0['external_humidity_age'] == 0.0
      and a0['external_humidity_fresh'] is True
      and a0['humidity_source'] == 'external'
      and a0['humidity_effective'] == 55.0)
check("get_status: a Pro shows the external reading as effective",
      a1['humidity'] is None and a1['humidity_effective'] == 70.0
      and a1['humidity_source'] == 'external')
check("get_status: units with a reading are offered as masters",
      st['auto_dry_masters'] == [0, 1])
inst.reactor.now += 901
a0b = inst.get_status()['aces'][0]
check("get_status: stale reading keeps value, reports age and internal source",
      a0b['external_humidity'] == 55.0
      and a0b['external_humidity_age'] >= 901.0
      and a0b['external_humidity_fresh'] is False
      and a0b['humidity_source'] == 'internal'
      and a0b['humidity_effective'] == 30.0)

# --- summary ----------------------------------------------------------------

if FAILED:
    print("\n%d check(s) FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("\nAll ACE_SET_HUMIDITY / external-humidity self-checks passed (%s)"
      % EXTRAS)
