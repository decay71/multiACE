"""M2: the internal contract between ace.py and a unit codec, as a checker.

This module is the readable form of UNIT_DRIVER_DESIGN.md section 5 and the
executable form at the same time: the harness installs `ContractChecker`
around `MultiAce.send_request_to` and judges every request ace.py sends and
every response a unit delivers. A violation is recorded, never raised, so a
scenario runs to its end and the test reports the whole list.

The contract, in the order the checker applies it.

REQUESTS (ace.py -> codec)
  R1  A request is a dict `{'method': str, 'params': dict}`; `params` may be
      missing, then it reads as `{}`.
  R2  The method name is one of KNOWN_METHODS (today's V1 vocabulary). A
      method a unit does not support must be answered `unsupported`, it
      must never reach the wire silently as something else.
  R3  `send_request_to` assigns `id`, an int in 0..0xFFFF (the V2 frame
      carries 16 bits; the V1 id wraps at the same bound, section 61).
  R4  Telemetry never stamps the unit busy: for a method in
      TELEMETRY_METHODS the host-side `status` is unchanged by the send.
      Every other method stamps `status = 'busy'` until the next status
      report (held/bootloader units excepted, they drop the request).

DELIVERY (codec -> ace.py)
  D1  A callback runs on the reactor thread, never on a reader thread.
  D2  It is called by keyword, exactly `cb(self=<MultiAce>, response=<dict>)`.
  D3  `response` is never None.
  D4  At most one delivery per request id; a request without a reply gets
      no delivery at all (the callers run their own deadlines, the 8 s
      watchdog covers silence).
  D5  Never inline from within `send_request_to`.

RESPONSES
  A1  `{'id': int, 'code': int, 'msg': str}`, `result` a dict when present;
      `id` equals the request id; other keys (V2 adds `cmd`) are allowed.
  A2  Success is `code == 0` and `msg == 'success'`, nothing else.
  A3  Busy forms per protocol: V1 says `code=0 msg=FORBIDDEN` (and
      `msg=empty` for an assist on an empty gate); V2 says
      `code=n msg=error_n`. A new codec says busy as `code=1 msg=FORBIDDEN`.
      Any other code-0 message is a violation.

STATUS DICT (`get_status.result`)
  S1  Only str keys, at every depth (the webhooks encoder shuts the printer
      down on an int key, section 36).
  S2  `status` is 'ready' or 'busy'; busy only while a motor runs.
  S3  `slots` is a list of four dicts, `index` 0..3 in order; `status` is a
      presence word (prefix 'empty' = empty; 'unknown' = present but not
      identified, V2 only; a unit without a presence sensor says
      'nosensor'); `rfid` int; `sku`, `type`, `brand` str; `color` three
      ints.
  S4  `temp`, when present, is numeric and never a bool (a codec without
      a probe omits the key; it never sends 0 as a stand-in).
  S5  `dryer_status`, when present, is a dict (a codec without a dryer
      omits the key).
  S6  `humidity` only from a unit with a humidity sensor (V2); V1 omits it.
  S7  `slot_status` only from a unit with slot-status truth (V2), values
      from SLOT_STATES; V1 slots carry none.
  S8  No host keys: `subtype`, `uid`, `tag_format`, `tag`, `colors` are set
      by the merge chain in ace.py, never by a codec.
  S9  Every poll delivers a new dict object (the core keeps the old one as
      `prev_info`).
  The freshness rule of section 5.3 (a status answer must not predate the
  last motion command) is a codec obligation that the host cannot observe;
  the fakes model it, a new codec has to prove it on its own.
"""
from __future__ import annotations

import re
import threading

REQUIRED_METHODS = ('get_info', 'get_status', 'feed_filament',
                    'unwind_filament', 'stop_feed_filament',
                    'start_feed_assist', 'stop_feed_assist')
OPTIONAL_METHODS = ('update_feeding_speed', 'drying', 'drying_stop',
                    'set_valve', 'set_feed_check', 'feed_or_rollback_raw',
                    'get_feed_info', 'get_filament_info', 'filament_identify',
                    'filament_recognition', 'discover_device', 'set_fan_speed',
                    'get_temp', 'set_rfid_enable', 'assign_device_id',
                    'iap_version', 'set_dry_temp', 'set_dry_power',
                    'get_key_state', 'rfid_test', 'stop_unwind_filament')
KNOWN_METHODS = frozenset(REQUIRED_METHODS + OPTIONAL_METHODS)
TELEMETRY_METHODS = frozenset(('get_status', 'get_feed_info',
                               'get_filament_info', 'filament_identify',
                               'filament_recognition'))
HOST_KEYS = frozenset(('subtype', 'uid', 'tag_format', 'tag', 'colors'))
PRESENCE_WORDS = frozenset(('ready', 'unknown', 'identified', 'identifying',
                            'nosensor'))
V1_CODE0_MSGS = frozenset(('success', 'FORBIDDEN', 'empty'))
_V2_MSG = re.compile(r'^(success|error_\d+)$')
MAX_ID = 0xFFFF


def non_str_keys(obj, path=''):
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                bad.append('%s[%r]' % (path, k))
            bad.extend(non_str_keys(v, '%s.%s' % (path, k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            bad.extend(non_str_keys(v, '%s[%d]' % (path, i)))
    return bad


class ContractChecker:
    """Judges requests, deliveries, responses and status dicts. Installed by
    the harness; `violations` is a list of (rule, text) tuples."""

    def __init__(self, harness):
        self.h = harness
        self.violations = []
        self.requests = 0
        self.responses = 0
        self.status_dicts = 0
        self._pending = {}        # (idx, id) -> (method, kind)
        self.orphaned = 0         # a pending id reused before any reply
        self._last_status = {}    # idx -> id() of the last status result
        self.slot_states = None   # filled from the V2 codec at install

    # -- bookkeeping ----------------------------------------------------

    def fail(self, rule, text):
        self.violations.append((rule, text))
        self.h.trace('contract', rule, self.h.norm(text))

    def kind_of(self, idx):
        inst = self.h.inst
        if inst is None:
            return '?'
        proto = inst._protocols.get(idx)
        return getattr(proto, 'NAME', '?') if proto is not None else '?'

    # -- install --------------------------------------------------------

    def install(self):
        A = self.h.A
        self.slot_states = set(self.h.P2.SLOT_STATES.values())
        checker = self
        orig = A.MultiAce.send_request_to

        def send_request_to(ace, idx, request, callback):
            return checker._send(orig, ace, idx, request, callback)

        self.h._patch(A.MultiAce, 'send_request_to', send_request_to)

    # -- requests -------------------------------------------------------

    def _send(self, orig, ace, idx, request, callback):
        kind = self.kind_of(idx)
        self.requests += 1
        method = None
        if not isinstance(request, dict):
            self.fail('R1', 'ACE %d: request is %s, not a dict'
                      % (idx, type(request).__name__))
        else:
            method = request.get('method')
            if not isinstance(method, str) or not method:
                self.fail('R1', 'ACE %d: request without a method name: %r'
                          % (idx, request))
            params = request.get('params', {})
            if params is not None and not isinstance(params, dict):
                self.fail('R1', 'ACE %d: %s params is %s, not a dict'
                          % (idx, method, type(params).__name__))
            if isinstance(method, str) and method not in KNOWN_METHODS:
                self.fail('R2', 'ACE %d: unknown method %r' % (idx, method))
        held = idx in getattr(ace, '_held_units', {})
        info = ace._info_per_ace.get(idx) if hasattr(ace, '_info_per_ace') else None
        status_before = info.get('status') if isinstance(info, dict) else None
        # The token marks this very send; its 'sent' flag flips once the
        # call returned, so a delivery before that is inline (D5). The V1
        # write path may pause the reactor during a reconnect, and other
        # units' deliveries landing in that window are not inline.
        token = {'sent': False}
        wrapped = self._wrap_callback(idx, kind, method, callback, token) \
            if callback is not None else None
        requests_before = self.requests
        try:
            ret = orig(ace, idx, request, wrapped)
        finally:
            token['sent'] = True
        nested = self.requests != requests_before + 1
        if isinstance(request, dict) and not held:
            rid = request.get('id')
            if not isinstance(rid, int) or rid < 0 or rid > MAX_ID:
                self.fail('R3', 'ACE %d: %s got id %r (want int 0..%d)'
                          % (idx, method, rid, MAX_ID))
            else:
                if (idx, rid) in self._pending:
                    # id counter reset by a reconnect (or the 16-bit wrap)
                    # while the old request never got its reply: the old
                    # one is orphaned, nothing may be delivered for it.
                    self.orphaned += 1
                self._pending[(idx, rid)] = (method, kind)
            info_after = ace._info_per_ace.get(idx)
            status_after = info_after.get('status') \
                if isinstance(info_after, dict) else None
            if nested or info_after is not info:
                # a reconnect ran inside this send: it replaces the status
                # dict and its handshake stamps busy; judge only flat sends
                pass
            elif method in TELEMETRY_METHODS:
                if status_after != status_before:
                    self.fail('R4', 'ACE %d: telemetry %s changed status %r -> %r'
                              % (idx, method, status_before, status_after))
            elif isinstance(method, str) and status_after != 'busy':
                self.fail('R4', 'ACE %d: %s did not stamp busy (status %r)'
                          % (idx, method, status_after))
        return ret

    # -- delivery + responses -------------------------------------------

    def _wrap_callback(self, idx, kind, method, callback, token):
        checker = self

        def delivered(*args, **kwargs):
            checker._on_delivery(idx, kind, method, args, kwargs, token)
            return callback(*args, **kwargs)

        try:
            delivered.__name__ = getattr(callback, '__name__', 'cb')
            delivered.__qualname__ = getattr(callback, '__qualname__',
                                             delivered.__name__)
        except (AttributeError, TypeError):
            pass
        return delivered

    def _on_delivery(self, idx, kind, method, args, kwargs, token):
        self.responses += 1
        tag = 'ACE %d %s' % (idx, method)
        if threading.current_thread() is not threading.main_thread():
            self.fail('D1', '%s: delivered on thread %s'
                      % (tag, threading.current_thread().name))
        if args or set(kwargs) != {'self', 'response'}:
            self.fail('D2', '%s: delivered with args=%d kwargs=%s'
                      % (tag, len(args), sorted(kwargs)))
        response = kwargs.get('response', args[1] if len(args) > 1 else None)
        if response is None:
            self.fail('D3', '%s: response is None' % tag)
            return
        if not token['sent']:
            self.fail('D5', '%s: delivered inline from send_request_to' % tag)
        if not isinstance(response, dict):
            self.fail('A1', '%s: response is %s, not a dict'
                      % (tag, type(response).__name__))
            return
        rid = response.get('id')
        if not isinstance(rid, int):
            self.fail('A1', '%s: response id %r is not an int' % (tag, rid))
        else:
            pend = self._pending.pop((idx, rid), None)
            if pend is None:
                self.fail('D4', '%s: id %d delivered without a pending '
                          'request (duplicate, or never sent)' % (tag, rid))
        code, msg = response.get('code'), response.get('msg')
        if not isinstance(code, int) or isinstance(code, bool):
            self.fail('A1', '%s: code %r is not an int' % (tag, code))
        if not isinstance(msg, str):
            self.fail('A1', '%s: msg %r is not a str' % (tag, msg))
        result = response.get('result')
        if result is not None and not isinstance(result, dict):
            self.fail('A1', '%s: result is %s, not a dict'
                      % (tag, type(result).__name__))
        if isinstance(code, int) and isinstance(msg, str):
            if kind == 'v1':
                if code == 0 and msg not in V1_CODE0_MSGS:
                    self.fail('A3', '%s: V1 code 0 with msg %r' % (tag, msg))
            elif kind == 'v2':
                if not _V2_MSG.match(msg):
                    self.fail('A3', '%s: V2 msg %r' % (tag, msg))
                if (code == 0) != (msg == 'success'):
                    self.fail('A2', '%s: code %d with msg %r' % (tag, code, msg))
            elif code == 0 and msg != 'success':
                self.fail('A2', '%s: code 0 with msg %r' % (tag, msg))
        if method == 'get_status' and code == 0 and isinstance(result, dict):
            self._check_status(idx, kind, result)

    # -- status dict ------------------------------------------------------

    def _check_status(self, idx, kind, result):
        self.status_dicts += 1
        tag = 'ACE %d status' % idx
        if id(result) == self._last_status.get(idx):
            self.fail('S9', '%s: same dict object delivered twice' % tag)
        self._last_status[idx] = id(result)
        bad = non_str_keys(result)
        if bad:
            self.fail('S1', '%s: non-str keys %s' % (tag, bad))
        st = result.get('status')
        if st not in ('ready', 'busy'):
            self.fail('S2', '%s: status %r' % (tag, st))
        slots = result.get('slots')
        if not isinstance(slots, list) or len(slots) != 4:
            self.fail('S3', '%s: slots is %r' % (tag, type(slots).__name__))
        else:
            for i, s in enumerate(slots):
                stag = '%s slot %d' % (tag, i)
                if not isinstance(s, dict):
                    self.fail('S3', '%s: not a dict' % stag)
                    continue
                if s.get('index') != i:
                    self.fail('S3', '%s: index %r' % (stag, s.get('index')))
                word = s.get('status')
                if not isinstance(word, str) or not word:
                    self.fail('S3', '%s: status %r' % (stag, word))
                elif not word.startswith('empty') and word not in PRESENCE_WORDS:
                    self.fail('S3', '%s: presence word %r' % (stag, word))
                elif word == 'unknown' and kind != 'v2':
                    self.fail('S3', '%s: %s unit says unknown' % (stag, kind))
                if not isinstance(s.get('rfid'), int):
                    self.fail('S3', '%s: rfid %r' % (stag, s.get('rfid')))
                for f in ('sku', 'type', 'brand'):
                    if not isinstance(s.get(f, ''), str):
                        self.fail('S3', '%s: %s %r' % (stag, f, s.get(f)))
                col = s.get('color', [0, 0, 0])
                if (not isinstance(col, (list, tuple)) or len(col) != 3
                        or not all(isinstance(c, int) for c in col)):
                    self.fail('S3', '%s: color %r' % (stag, col))
                if kind == 'v1' and 'slot_status' in s:
                    self.fail('S7', '%s: V1 slot carries slot_status' % stag)
                if kind == 'v2':
                    if s.get('slot_status') not in self.slot_states:
                        self.fail('S7', '%s: slot_status %r'
                                  % (stag, s.get('slot_status')))
                hk = HOST_KEYS & set(s)
                if hk:
                    self.fail('S8', '%s: host keys %s' % (stag, sorted(hk)))
        if 'temp' in result:
            temp = result.get('temp')
            if not isinstance(temp, (int, float)) or isinstance(temp, bool):
                self.fail('S4', '%s: temp %r' % (tag, temp))
        if 'dryer_status' in result:
            if not isinstance(result.get('dryer_status'), dict):
                self.fail('S5', '%s: dryer_status %r'
                          % (tag, result.get('dryer_status')))
        if kind == 'v1' and 'humidity' in result:
            self.fail('S6', '%s: V1 reports humidity' % tag)
        if kind == 'v2' and 'humidity' not in result:
            self.fail('S6', '%s: V2 without humidity' % tag)
        hk = HOST_KEYS & set(result)
        if hk:
            self.fail('S8', '%s: host keys %s' % (tag, sorted(hk)))

    def summary(self):
        return ('requests=%d responses=%d status_dicts=%d orphaned=%d '
                'violations=%d' % (self.requests, self.responses,
                                   self.status_dicts, self.orphaned,
                                   len(self.violations)))
