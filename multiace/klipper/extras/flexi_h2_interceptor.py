# SPDX-License-Identifier: GPL-3.0-or-later
"""Version-pinned H2 interception for one MultiACE movement boundary.

The selected boundary is ``MultiAce._run_tipform``. That method is genuinely
movement-capable (discard-position moves, ``G1 E`` extrusion, ``M400`` and an
OEM unload macro), and every existing unload caller invokes it before the
retract loop. The wrapper authorizes the method invocation before any of those
commands reach Klipper's dispatcher.

This is intentionally not global G-code interception. It does not see slicer
movement, arbitrary macros, or other MultiACE paths.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import ast
import hashlib
import inspect
import logging
import sys
import textwrap
import time

from .flexi_h2 import H2Decision, H2Denied, H2Request


PINNED_BUILD = 'flexi-rc3-20261110'
SITE = {
    'file': 'klipper/extras/ace.py',
    'function': '_run_tipform',
    'line': 18093,
}
PINNED_SOURCE_SHA256 = (
    'a245ea2f0dc8cc9794179d2df5fb50abb5292e6bda7f63e25fb9000e39a47de0')
_ABSENT = object()


class SourceUnavailable(Exception):
    pass


def normalize_source(source):
    text = source.replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.rstrip() for line in textwrap.dedent(text).split('\n')]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines) + '\n'


def _loader_source(method):
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


def verify_backend(backend, expected_source_sha256=None):
    expected = (PINNED_SOURCE_SHA256 if expected_source_sha256 is None
                else expected_source_sha256)
    entry = getattr(backend, '_run_tipform', None)
    if not callable(entry):
        return 'incompatible', 'backend has no callable _run_tipform', None
    try:
        actual = source_sha256(entry)
    except SourceUnavailable as exc:
        return 'source_drift', 'source unavailable: ' + str(exc), None
    if actual != expected:
        return ('source_drift',
                '_run_tipform sha256 %s does not match pinned %s'
                % (actual, expected), actual)
    return 'ready', None, actual


class H2Interceptor:
    """Removable instance-level authorization at the selected movement method."""

    def __init__(self, backend, provider, expected_source_sha256=None,
                 clock=time.monotonic):
        self.backend = backend
        self.provider = provider
        self.expected_source_sha256 = (PINNED_SOURCE_SHA256
                                       if expected_source_sha256 is None
                                       else expected_source_sha256)
        self.clock = clock
        self.installed = False
        self.source_sha256 = None
        self.verify_state = 'not-verified'
        self.verify_reason = None
        self._original = None
        self._previous = _ABSENT
        self._last = None
        self._counters = {'allowed': 0, 'denied': 0, 'provider_exception': 0}

    def install(self):
        state, reason, actual = verify_backend(
            self.backend, self.expected_source_sha256)
        self.verify_state = state
        self.verify_reason = reason
        self.source_sha256 = actual
        if state != 'ready':
            return False, state, reason
        if self.installed:
            return True, state, None
        self._original = self.backend._run_tipform
        self._previous = self.backend.__dict__.get('_run_tipform', _ABSENT)
        self.backend._run_tipform = self._run_tipform
        self.installed = True
        return True, state, None

    def uninstall(self):
        if not self.installed:
            return False
        current = getattr(self.backend, '_run_tipform', None)
        if current != self._run_tipform:
            return False
        if self._previous is _ABSENT:
            try:
                del self.backend._run_tipform
            except AttributeError:
                pass
        else:
            self.backend._run_tipform = self._previous
        self.installed = False
        return True

    def _run_tipform(self, head, temp, soft, nozzle_diameter):
        if getattr(self.backend, '_ace_mode', None) != 'flexi':
            return self._original(head, temp, soft, nozzle_diameter)
        request = self._request(head, temp, soft, nozzle_diameter)
        try:
            decision = self.provider.authorize(request)
        except Exception as exc:
            decision = H2Decision(False, 'provider-exception',
                                  request.classification)
            self._counters['provider_exception'] += 1
            logging.warning('[flexi][h2] provider exception for %s: %s',
                            request.operation, type(exc).__name__)
        self._last = {
            'request': request.summary(),
            'decision': decision.as_dict(),
            'at': self.clock(),
        }
        self._counters['allowed' if decision.allowed else 'denied'] += 1
        if not decision.allowed:
            logging.warning(
                '[flexi][h2] denied %s head=%s classification=%s: %s',
                request.operation, head, request.classification,
                decision.reason)
            raise H2Denied(
                'H2 denied %s: %s' % (request.operation, decision.reason),
                decision)
        return self._original(head, temp, soft, nozzle_diameter)

    def _request(self, head, temp, soft, nozzle_diameter):
        job = self._job_for_head(head)
        token = job.get('token') if job else None
        active = bool(job and token and not job.get('failed'))
        locked, lock_reason = self._lock_state(head)
        if (job is not None and job is getattr(self.backend, '_flexi_native_unload', None)
                and lock_reason in ('execution-locked', 'execution-state-missing')):
            # Flexi's own dispatch lock governs Flexi-dispatched motion. A
            # native routed ACE_UNLOAD_HEAD is authorized by its route and
            # path ledger; only a recovery record / init fault blocks it.
            locked, lock_reason = False, None
        recovery_required = lock_reason == 'recovery-required'
        phase = None
        source = None
        if job:
            phase = job.get('phase')
            if phase is None:
                try:
                    source, phase = job['phases'][job['index']]
                except (KeyError, IndexError, TypeError):
                    phase = None
            if source is None:
                source = job.get('source') or job.get('old_source')
        # The active-job flag is not a lock. The authoritative lock is the
        # read-only coordinator state plus the initialization fault; a missing
        # or inconsistent lock state fails closed. A valid token cannot
        # override an active movement/execution or recovery lock.
        locked = (not active) or locked
        stepper = ('extruder%d' % head) if type(head) is int else None
        return H2Request(
            origin='multiace',
            operation='filament_tip_conditioning',
            classification='tip_conditioning',
            site=SITE,
            head=head,
            stepper=stepper,
            parameters={'temp': temp, 'soft': bool(soft),
                        'nozzle_diameter': nozzle_diameter},
            job_token=token,
            job_active=active,
            movement_locked=locked,
            recovery_required=recovery_required,
            phase=phase,
            source=source,
            mode=getattr(self.backend, '_ace_mode', None),
            is_movement=True,
            context={'job_action': job.get('action') if job else None,
                     'job_operation': job.get('operation') if job else None,
                     'lock_reason': lock_reason},
        )

    def _job_for_head(self, head):
        service = getattr(self.backend, '_flexi_production', None)
        workflow = getattr(service, 'workflow', None)
        controller = getattr(self.backend, '_flexi_test_controller', None)
        executor = getattr(controller, 'executor', None)
        for owner in (workflow, executor):
            job = getattr(owner, 'job', None)
            if (isinstance(job, dict) and job.get('token')
                    and job.get('head') == head):
                return job
        # Original MultiACE ACE_UNLOAD_HEAD of a routed source (Flexi mode):
        # the backend publishes a scope for exactly that head while it runs.
        native = getattr(self.backend, '_flexi_native_unload', None)
        if (isinstance(native, dict) and native.get('token')
                and native.get('head') == head):
            return native
        return None

    def _lock_state(self, head=None):
        """Read the authoritative lock state without changing it.

        The coordinator exposes execution_enabled=False and a recovery list;
        the engine also records initialization failure. Missing or malformed
        state is treated as locked. No lock is cleared or inferred from an
        active job token.
        """
        if getattr(self.backend, '_flexi_init_error', None):
            return True, 'initialization-error'
        coordinator = getattr(self.backend, '_flexi_coordinator', None)
        if coordinator is None:
            return True, 'lock-state-unavailable'
        try:
            snapshot = coordinator.snapshot()
        except Exception:
            return True, 'lock-state-error'
        if not isinstance(snapshot, dict):
            return True, 'lock-state-invalid'
        records = snapshot.get('recovery_required') or []
        if head is not None and isinstance(records, list):
            # A recovery record locks only its own toolhead's tube.
            records = [r for r in records if not isinstance(r, dict) or r.get('head') == head]
        if records:
            return True, 'recovery-required'
        if snapshot.get('movement_locked') is not None:
            if bool(snapshot.get('movement_locked')):
                return True, 'movement-locked'
        elif snapshot.get('movement_lock') is not None:
            if bool(snapshot.get('movement_lock')):
                return True, 'movement-locked'
        execution = snapshot.get('execution_enabled')
        if execution is not True:
            return True, ('execution-locked' if execution is False
                          else 'execution-state-missing')
        return False, None

    def status(self):
        return {
            'installed': self.installed,
            'pinned_build': PINNED_BUILD,
            'site': dict(SITE),
            'source_sha256': self.source_sha256,
            'pinned_source_sha256': self.expected_source_sha256,
            'source_verified': self.verify_state == 'ready',
            'verify_state': self.verify_state,
            'verify_reason': self.verify_reason,
            'provider': self.provider.status(),
            'counters': dict(self._counters),
            'last': dict(self._last) if self._last is not None else None,
            'physical_confirmation': 'not-observed',
            'filament_unloaded': False,
            'toolhead_sensor_clear': 'not-observed',
        }


def install(backend, provider, expected_source_sha256=None):
    interceptor = H2Interceptor(backend, provider, expected_source_sha256)
    ok, state, reason = interceptor.install()
    return (interceptor if ok else None), state, reason
