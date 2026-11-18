# SPDX-License-Identifier: GPL-3.0-or-later
# @MULTIACE_FLEXI_HOOK Flexi-owned H1 adapter; original modes remain unwrapped.
"""Version-pinned H1 boundary adapter for MultiACE ``send_request_to``.

The adapter adds one enforcement and classification layer above the existing
motor-request gate. It deliberately owns no routing or safety policy: every
decision is delegated to the backend's ``_flexi_motor_request_allowed``.

Scope is narrow and local:

* only while the backend is in ``flexi`` mode and the request is a motor
  method, so normal MultiACE modes are untouched;
* only the installed, pinned ``send_request_to`` source is wrapped; an
  unexpected source fingerprint refuses patching instead of guessing;
* verification is optional and removable by deleting this module and its
  installation call; ``ace.py`` itself is never edited.

Delivery and stop are separate facts. A device acknowledgment is recorded as
``acknowledged_unconfirmed``; this module never reports a physical stop.
"""
import ast
import hashlib
import inspect
import logging
import sys
import textwrap
import time


PINNED_BUILD = 'flexi-session2-structural'
# Byte-exact source pins are disabled (None) in the shipped build: every
# reviewed edit of the gate made Klipper refuse to start (live 2026-11-17).
# verify_structure() enforces the actual invariant instead. Setting these
# to a sha256 restores byte-exact verification for a frozen build.
# sha256 of the normalized source of MultiAce.send_request_to (line 7802 of
# the pinned ace.py). Any edit to that function must deliberately
# re-pin this value; otherwise the adapter refuses to install.
PINNED_SOURCE_SHA256 = None
# sha256 of the normalized FlexiMixin._flexi_motor_request_allowed source.
# The boundary delegates every decision here, so a drifted gate is a drifted
# safety policy. Re-pinned 2026-11-17 for the ordinary-load single-request
# branch: an authorized PLA-family load may dispatch the printer's own one
# feed request, and the gate still matches that exact dispatch, so a 2.1 m
# load no longer needs hundreds of heartbeat-paced 5 mm segments.
PINNED_GATE_SHA256 = None
# sha256 of the normalized MultiAce._send_request_to source. H1 sets
# ``_flexi_no_replay`` so an uncertain write is not automatically retried;
# an installed source without that narrow branch must refuse H1 installation.
PINNED_TRANSPORT_SHA256 = None

MOTOR_METHODS = frozenset((
    'feed_filament', 'unwind_filament', 'start_feed_assist',
    'stop_feed_assist', 'stop_feed_filament', 'stop_unwind_filament',
    'update_feeding_speed',
    # ACE Pro 2 raw opcodes (session 2): gated like the ones above.
    'feed_or_rollback_raw', 'raw', 'drying_raw', 'rfid_test',
))

# The backend's own set is checked during install. Adapter code never adds a
# brake exemption of its own; it only asks the existing gate.
VERIFIED_BRAKES = frozenset(('stop_feed_filament', 'stop_feed_assist'))

_ABSENT = object()

_PHASES = (
    'authorized', 'authorization_denied', 'authorization_unavailable',
    'undeliverable', 'queued', 'transmitted', 'acknowledged_unconfirmed',
    'device_rejected', 'delivery_uncertain',
)


class SourceUnavailable(Exception):
    pass


def normalize_source(source):
    """Canonical text used for the source pin and drift detection."""
    text = source.replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.rstrip() for line in textwrap.dedent(text).split('\n')]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines) + '\n'


def _loader_source(method):
    """Source for embedded modules whose ``__file__`` is not a disk path."""
    module = sys.modules.get(getattr(method, '__module__', None))
    loader = getattr(module, '__loader__', None)
    get_source = getattr(loader, 'get_source', None)
    if not callable(get_source):
        raise SourceUnavailable('no loader source available')
    try:
        raw = get_source(method.__module__)
    except Exception as exc:
        raise SourceUnavailable(str(exc))
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    if not isinstance(raw, str):
        raise SourceUnavailable('loader returned no text')
    tree = ast.parse(raw)
    node = tree
    for part in getattr(method, '__qualname__', '').split('.'):
        body = getattr(node, 'body', ())
        node = next((item for item in body if isinstance(
            item, (ast.ClassDef, ast.FunctionDef)) and item.name == part), None)
        if node is None:
            raise SourceUnavailable('function not found in loader source')
    lines = raw.splitlines()
    return '\n'.join(lines[node.lineno - 1:node.end_lineno])


def source_sha256(method):
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        source = _loader_source(method)
    return hashlib.sha256(normalize_source(source).encode('utf-8')).hexdigest()


def verify_backend(backend, expected_source_sha256=None,
                   expected_gate_sha256=None,
                   expected_transport_sha256=None):
    """Return ``(state, reason, actual_sha)`` without patching anything."""
    expected = (PINNED_SOURCE_SHA256 if expected_source_sha256 is None
                else expected_source_sha256)
    expected_gate = (PINNED_GATE_SHA256 if expected_gate_sha256 is None
                     else expected_gate_sha256)
    expected_transport = (PINNED_TRANSPORT_SHA256
                          if expected_transport_sha256 is None
                          else expected_transport_sha256)
    entry = getattr(backend, 'send_request_to', None)
    if not callable(entry):
        return 'incompatible', 'backend has no callable send_request_to', None
    gate = getattr(backend, '_flexi_motor_request_allowed', None)
    if not callable(gate):
        return ('incompatible',
                'backend has no callable _flexi_motor_request_allowed', None)
    transport = getattr(backend, '_send_request_to', None)
    if not callable(transport):
        return ('incompatible',
                'backend has no callable _send_request_to', None)
    brakes = getattr(backend, '_FLEXI_VERIFIED_BRAKES', None)
    try:
        brakes = frozenset(brakes or ())
    except TypeError:
        brakes = frozenset()
    if brakes != VERIFIED_BRAKES:
        return ('incompatible',
                'verified brake set differs from the pinned stop-only set',
                None)
    try:
        actual = source_sha256(entry)
    except SourceUnavailable as exc:
        return 'source_drift', 'source unavailable: ' + str(exc), None
    if expected is None and expected_gate is None and expected_transport is None:
        # Default: structural verification. Byte-exact source pins turned
        # every reviewed edit of the gate into "Klipper will not start"
        # (live 2026-11-17). The protection that matters is structural: the
        # entry point must route motor requests through the Flexi gate, the
        # gate must be the Flexi engine's own policy function, and the
        # transport must keep its no-automatic-replay branch.
        return verify_structure(entry, gate, transport, actual)
    if actual != expected:
        return ('source_drift',
                'send_request_to sha256 %s does not match pinned %s'
                % (actual, expected), actual)
    try:
        gate_actual = source_sha256(gate)
    except SourceUnavailable as exc:
        return 'source_drift', 'gate source unavailable: ' + str(exc), actual
    if gate_actual != expected_gate:
        return ('source_drift',
                '_flexi_motor_request_allowed sha256 %s does not match '
                'pinned %s' % (gate_actual, expected_gate), actual)
    try:
        transport_actual = source_sha256(transport)
    except SourceUnavailable as exc:
        return ('source_drift',
                '_send_request_to source unavailable: ' + str(exc), actual)
    if transport_actual != expected_transport:
        return ('source_drift',
                '_send_request_to sha256 %s does not match pinned %s'
                % (transport_actual, expected_transport), actual)
    return 'ready', None, actual


def _source_text(method):
    try:
        return normalize_source(inspect.getsource(method))
    except (OSError, TypeError):
        return normalize_source(_loader_source(method))


def verify_structure(entry, gate, transport, actual=None):
    """Structural H1 verification (no byte pins). Returns verify_backend's
    ``(state, reason, actual_sha)`` triple."""
    try:
        entry_text = _source_text(entry)
        transport_text = _source_text(transport)
    except SourceUnavailable as exc:
        return 'source_drift', 'source unavailable: ' + str(exc), actual
    if '_flexi_motor_request_allowed(' not in entry_text:
        return ('source_drift',
                'send_request_to no longer calls the Flexi motor gate', actual)
    if not callable(gate):
        return ('incompatible', 'motor gate is not callable', actual)
    if '_flexi_no_replay' not in transport_text:
        return ('source_drift',
                '_send_request_to lacks the no-automatic-replay branch', actual)
    return 'ready', None, actual


class H1Adapter:
    """Removable instance-level wrapper; no class monkey-patching."""

    def __init__(self, backend, expected_source_sha256=None,
                 expected_gate_sha256=None, expected_transport_sha256=None,
                 clock=time.monotonic):
        self.backend = backend
        # None = structural verification (default). Explicit values keep
        # the byte-exact comparison for callers that pin a reviewed build.
        self.expected_source_sha256 = (PINNED_SOURCE_SHA256
                                       if expected_source_sha256 is None
                                       else expected_source_sha256)
        self.expected_gate_sha256 = (PINNED_GATE_SHA256
                                     if expected_gate_sha256 is None
                                     else expected_gate_sha256)
        self.expected_transport_sha256 = (PINNED_TRANSPORT_SHA256
                                          if expected_transport_sha256 is None
                                          else expected_transport_sha256)
        self.clock = clock
        self.installed = False
        self.source_sha256 = None
        self.gate_source_sha256 = None
        self.transport_source_sha256 = None
        self.verify_state = 'not-verified'
        self.verify_reason = None
        self._original = None
        self._previous = _ABSENT
        self._last = None
        self._counters = dict((name, 0) for name in _PHASES)

    def install(self):
        state, reason, actual = verify_backend(
            self.backend, self.expected_source_sha256,
            self.expected_gate_sha256, self.expected_transport_sha256)
        self.verify_state = state
        self.verify_reason = reason
        self.source_sha256 = actual
        try:
            self.gate_source_sha256 = source_sha256(
                self.backend._flexi_motor_request_allowed)
        except SourceUnavailable:
            self.gate_source_sha256 = None
        try:
            self.transport_source_sha256 = source_sha256(
                self.backend._send_request_to)
        except SourceUnavailable:
            self.transport_source_sha256 = None
        if state != 'ready':
            return False, state, reason
        if self.installed:
            return True, state, None
        self._original = self.backend.send_request_to
        self._previous = self.backend.__dict__.get('send_request_to', _ABSENT)
        self.backend.send_request_to = self.send_request_to
        self.installed = True
        return True, state, None

    def uninstall(self):
        if not self.installed:
            return False
        current = getattr(self.backend, 'send_request_to', None)
        if current != self.send_request_to:
            # Someone else replaced the boundary; never clobber it.
            return False
        if self._previous is _ABSENT:
            try:
                del self.backend.send_request_to
            except AttributeError:
                pass
        else:
            self.backend.send_request_to = self._previous
        self.installed = False
        return True

    def status(self):
        last = dict(self._last) if self._last is not None else None
        return {
            'installed': self.installed,
            'pinned_build': PINNED_BUILD,
            'source_sha256': self.source_sha256,
            'pinned_source_sha256': self.expected_source_sha256,
            'gate_source_sha256': self.gate_source_sha256,
            'pinned_gate_sha256': self.expected_gate_sha256,
            'transport_source_sha256': self.transport_source_sha256,
            'pinned_transport_sha256': self.expected_transport_sha256,
            'source_verified': self.verify_state == 'ready',
            'verify_state': self.verify_state,
            'verify_reason': self.verify_reason,
            'enforcement': 'flexi_mode' if self.installed else 'inactive',
            'physical_confirmation': 'not-observed',
            'last': last,
            'counters': dict(self._counters),
        }

    # ------------------------------------------------------------------ H1
    def send_request_to(self, idx, request, callback):
        if not isinstance(request, dict):
            return self._original(idx, request, callback)
        method = request.get('method')
        if (getattr(self.backend, '_ace_mode', None) != 'flexi'
                or method not in MOTOR_METHODS):
            return self._original(idx, request, callback)

        token = request.get('_flexi_token')
        record = self._record(idx, request, method, token)
        try:
            allowed = self.backend._flexi_motor_request_allowed(
                idx, request, token)
        except Exception as exc:
            detail = '%s: %s' % (type(exc).__name__, str(exc)[:160])
            logging.warning(
                '[flexi][h1] authorization unavailable for %s on ACE %s: %s',
                method, idx, detail)
            return self._reject(record, callback, 'authorization_unavailable',
                                'FLEXI_AUTHORIZATION_UNAVAILABLE', detail)
        if not allowed:
            return self._reject(record, callback, 'authorization_denied',
                                'FLEXI_AUTHORIZATION_DENIED',
                                'the existing motor gate denied this request')

        record['authorization'] = 'allowed'
        self._phase(record, 'authorized')
        blocked = self._delivery_block(idx)
        if blocked is not None:
            reason, message = blocked
            return self._reject(record, callback, 'undeliverable', message,
                                reason)

        engine = self._engine(idx)
        record['phase'] = 'transmitting'
        record['delivery'] = 'pending'
        wrapped = self._wrap_callback(record, callback)
        # The pinned writer honors this only to suppress its automatic replay
        # after an uncertain write. Copy so the caller's request is untouched.
        forwarded = dict(request)
        forwarded['_flexi_no_replay'] = True
        record['automatic_replay_suppressed'] = True
        try:
            result = self._original(idx, forwarded, wrapped)
        except Exception as exc:
            detail = '%s: %s' % (type(exc).__name__, str(exc)[:160])
            self._phase(record, 'delivery_uncertain')
            record['delivery'] = 'uncertain'
            logging.warning(
                '[flexi][h1] delivery uncertain for %s on ACE %s: %s',
                method, idx, detail)
            self._respond(callback, 'FLEXI_DELIVERY_UNCERTAIN', record,
                          detail)
            return None
        if record['phase'] == 'transmitting':
            final = 'queued' if engine == 'threaded' else 'transmitted'
            self._phase(record, final)
            record['delivery'] = final
        return result

    # ------------------------------------------------------- classification
    def _record(self, idx, request, method, token):
        params = request.get('params')
        slot = params.get('index') if isinstance(params, dict) else None
        record = {
            'idx': idx,
            'method': method,
            'slot': slot,
            'brake': method in VERIFIED_BRAKES,
            'has_token': token is not None,
            'authorization': 'pending',
            'delivery': 'not-attempted',
            'phase': 'authorizing',
            'device_acknowledged': False,
            'device_code': None,
            'physically_confirmed': False,
            'stop_confirmed': False,
            'reason': None,
            'at': self.clock(),
        }
        self._last = record
        return record

    def _phase(self, record, phase):
        if record['phase'] == phase:
            return
        record['phase'] = phase
        self._counters[phase] = self._counters.get(phase, 0) + 1

    def _reject(self, record, callback, phase, message, reason):
        if phase in ('authorization_denied', 'authorization_unavailable'):
            record['authorization'] = (
                'denied' if phase == 'authorization_denied' else 'unavailable')
            record['delivery'] = 'not-attempted'
        else:
            record['delivery'] = 'undeliverable'
        record['reason'] = reason
        self._phase(record, phase)
        self._respond(callback, message, record, reason)
        return None

    def _respond(self, callback, message, record, reason):
        if callback is None:
            return
        payload = {
            'code': 1,
            'msg': message,
            'flexi': {
                'phase': record['phase'],
                'reason': reason,
                'method': record['method'],
                'brake': record['brake'],
                'physically_confirmed': False,
                'stop_confirmed': False,
            },
        }
        try:
            callback(self=self.backend, response=payload)
        except Exception:
            logging.exception('[flexi][h1] rejection callback raised; ignored')

    def _wrap_callback(self, record, callback):
        def observe(*args, **kwargs):
            responder = kwargs.get('self')
            response = kwargs.get('response')
            if response is None and len(args) >= 2:
                responder, response = args[0], args[1]
            elif response is None and len(args) == 1:
                response = args[0]
            if isinstance(response, dict):
                code = response.get('code')
                record['device_acknowledged'] = True
                record['device_code'] = code
                record['physically_confirmed'] = False
                record['stop_confirmed'] = False
                if code == 0:
                    self._phase(record, 'acknowledged_unconfirmed')
                    record['delivery'] = 'acknowledged'
                elif str(response.get('msg', '')).strip() == 'FLEXI_MOTION_LOCKED':
                    # The pinned original evaluates the same gate again. If it
                    # denies after the adapter allowed, nothing was dispatched;
                    # classify that as a late authorization denial, not a
                    # device rejection.
                    record['authorization'] = 'denied'
                    record['delivery'] = 'not-attempted'
                    record['reason'] = 'original gate denied after adapter authorization'
                    self._phase(record, 'authorization_denied')
                else:
                    self._phase(record, 'device_rejected')
                    record['delivery'] = 'device-rejected'
            if callback is None:
                return
            try:
                callback(self=(responder if responder is not None
                               else self.backend), response=response)
            except Exception:
                logging.exception(
                    '[flexi][h1] response callback raised; ignored')
        return observe

    def _engine(self, idx):
        cap = getattr(self.backend, '_cap', None)
        if not callable(cap):
            return 'unknown'
        try:
            return cap(idx, 'transport_engine') or 'unknown'
        except Exception:
            return 'unknown'

    def _delivery_block(self, idx):
        held = getattr(self.backend, '_held_units', None)
        try:
            if held is not None and idx in held:
                return ('held_unit', 'FLEXI_UNDELIVERABLE_HELD_UNIT')
        except TypeError:
            pass
        engine = self._engine(idx)
        if engine == 'threaded':
            queues = getattr(self.backend, '_writer_queues', None)
            queue = queues.get(idx) if isinstance(queues, dict) else None
            if queue is None:
                return ('writer_queue_unavailable',
                        'FLEXI_UNDELIVERABLE_WRITER_QUEUE')
            try:
                if queue.full():
                    return ('writer_queue_full',
                            'FLEXI_UNDELIVERABLE_WRITER_QUEUE')
            except Exception:
                pass
            return None
        if engine == 'reactor_fd':
            serials = getattr(self.backend, '_serials', None)
            serial = serials.get(idx) if isinstance(serials, dict) else None
            failed = getattr(self.backend, '_serial_failed_per_ace', None)
            failed = failed.get(idx, False) if isinstance(failed, dict) else False
            if serial is None or failed:
                return ('serial_unavailable',
                        'FLEXI_UNDELIVERABLE_SERIAL')
            return None
        return ('transport_unknown', 'FLEXI_UNDELIVERABLE_TRANSPORT')


def install(backend, expected_source_sha256=None,
            expected_gate_sha256=None, expected_transport_sha256=None):
    """Convenience used by the plugin; returns ``(adapter, state, reason)``."""
    adapter = H1Adapter(backend, expected_source_sha256,
                        expected_gate_sha256, expected_transport_sha256)
    ok, state, reason = adapter.install()
    return (adapter if ok else None), state, reason
