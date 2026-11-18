# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline resource interlocks. Reservations never authorize hardware motion.

The observation API is simulation-only until a trusted live evidence provider
is implemented. Restarted journals retain history but never restore clearance.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid


class InterlockError(ValueError):
    pass


class AtomicJournal:
    """One writer session, serialized threads/processes, atomic JSON snapshots."""
    def __init__(self, filename):
        self.path = Path(filename)
        self.lock_path = self.path.with_name(self.path.name + '.lock')
        self._thread_lock = threading.RLock()

    def _locked(self, fn):
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink() or self.lock_path.is_symlink():
                raise InterlockError('Unsafe journal or lock symlink')
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(str(self.lock_path), flags, 0o600)
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                return fn()
            finally:
                # Closing the descriptor releases either platform's lock.
                os.close(fd)

    def _read(self):
        if not self.path.exists():
            return None
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 131072:
            raise InterlockError('Journal is unsafe or oversized')
        # Session 2 (reactor stalls): the status build read, re-hashed and
        # deep-copied this file eight times per build. The verified state is
        # cached against the file identity; every write replaces the file
        # (new inode/mtime/size), so a stale cache is impossible. Callers get
        # a fresh object parsed from the verified canonical JSON.
        try:
            st = self.path.stat()
            key = (st.st_mtime_ns, st.st_size, getattr(st, 'st_ino', 0))
        except OSError:
            key = None
        cached = getattr(self, '_read_cache', None)
        if key is not None and cached is not None and cached[0] == key:
            return json.loads(cached[1])
        try:
            with self.path.open('rb') as stream:payload=stream.read(131073)
            if len(payload)>131072:raise ValueError('Journal is oversized')
            envelope = json.loads(payload)
            if set(envelope) != {'sha256', 'state'}:
                raise ValueError('Invalid journal envelope')
            encoded = json.dumps(envelope['state'], sort_keys=True, separators=(',', ':')).encode()
            if hashlib.sha256(encoded).hexdigest() != envelope['sha256']:
                raise ValueError('Journal fingerprint mismatch')
            state = envelope['state']
            if (not isinstance(state, dict) or state.get('schema') != 1
                    or type(state.get('revision')) is not int or state['revision'] < 0
                    or not isinstance(state.get('session'), str)
                    or not isinstance(state.get('history'), list)):
                raise ValueError('Invalid journal state')
            if key is not None:
                self._read_cache = (key, encoded)
            return state
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise InterlockError('Journal unavailable: ' + str(exc))

    def _write(self, state):
        encoded = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
        envelope = json.dumps({'sha256': hashlib.sha256(encoded).hexdigest(), 'state': state}, sort_keys=True).encode()
        if len(envelope)>131072:raise InterlockError('Journal would exceed the safe size limit')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.flexi-', dir=str(self.path.parent))
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(envelope)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            if os.name != 'nt':
                directory = os.open(str(self.path.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def recover(self, fresh, merge=None):
        def action():
            old = self._read()  # Refuse corruption; never manufacture clearance.
            state = copy.deepcopy(fresh)
            if old:
                state['revision'] = old['revision'] + 1
                state['history'] = (old['history'] + [{'event': 'restart', 'previous_session': old['session']}])[-40:]
            if merge is not None:merge(state,old)
            self._write(state)
            return state
        return self._locked(action)

    def update(self, session, change):
        def action():
            state = self._read()
            if state is None or state['session'] != session:
                raise InterlockError('Writer session changed; reconciliation is required')
            result = change(state)
            state['revision'] += 1
            self._write(state)
            return copy.deepcopy(result)
        return self._locked(action)

    def read(self, session):
        # Writers replace complete envelopes atomically. Reading needs no
        # lock-file creation or directory mutation; a prior/current valid
        # snapshot is checked against the expected writer session.
        with self._thread_lock:
            if self.lock_path.is_symlink():raise InterlockError('Unsafe lock symlink')
            state=self._read()
            if state is None or state['session']!=session:raise InterlockError('Writer session changed')
            return state  # _read returns a fresh object on every call


class Coordinator:
    """Exclusive ACE and toolhead resources with expiring simulated evidence."""
    def __init__(self, routes, journal=None, simulation=False, clock=time.monotonic, max_age=30.0, recovery_merge=None):
        if (type(simulation) is not bool or type(max_age) not in (int, float)
                or not math.isfinite(max_age) or max_age <= 0):
            raise InterlockError('Invalid coordinator settings')
        self.routes = routes
        self.journal = journal
        self.simulation = simulation
        self.clock = clock
        self.max_age = max_age
        self._lock = threading.RLock()
        self.session = uuid.uuid4().hex
        self.fingerprint = hashlib.sha256(routes.context_encode().encode()).hexdigest()
        fresh = {'schema': 1, 'revision': 0, 'session': self.session,
                 'routes_sha256': self.fingerprint, 'simulation': simulation,
                 'paths': {str(h): {'state': 'unknown', 'owner': None, 'observed': None} for h in range(4)},
                 'devices': {}, 'operations': {}, 'printing': {}, 'history': []}
        self._state = journal.recover(fresh,recovery_merge) if journal and recovery_merge is not None else journal.recover(fresh) if journal else fresh
        if journal is None and recovery_merge is not None:recovery_merge(self._state,None)

    def _change(self, fn):
        with self._lock:
            if self.journal:
                return self.journal.update(self.session, fn)
            state = copy.deepcopy(self._state)
            result = fn(state)
            state['revision'] += 1
            self._state = state
            return copy.deepcopy(result)

    def snapshot(self):
        with self._lock:
            state = self.journal.read(self.session) if self.journal else copy.deepcopy(self._state)
            state.update(execution_enabled=False, physical_path_verified=False)
            return state

    def _observation_allowed(self):
        if not self.simulation:
            raise InterlockError('Live evidence provider is not implemented; no path can be reconciled')

    def observe_device(self, ace, identity, ready=True):
        self._observation_allowed()
        if type(ace) is not int or ace not in range(4) or not isinstance(identity, str) or not identity or type(ready) is not bool:
            raise InterlockError('Invalid device evidence')
        def change(state):
            old = state['devices'].get(str(ace))
            if old and old['identity'] != identity:
                self._invalidate(state, set(self.routes.device_heads(ace)), 'device identity changed')
            state['devices'][str(ace)] = {'identity': identity, 'ready': ready, 'observed': self.clock()}
            if not ready:
                self._invalidate(state, set(self.routes.device_heads(ace)), 'device unavailable')
        self._change(change)

    def observe_path(self, head, state_name, owner=None):
        self._observation_allowed()
        if type(head) is not int or head not in range(4) or state_name not in ('clear', 'occupied'):
            raise InterlockError('Invalid path observation')
        if state_name == 'clear' and owner is not None:
            raise InterlockError('Clear path cannot have an owner')
        if state_name == 'occupied':
            try:
                owner = tuple(owner)
                self.routes.require_destination(*owner, head)
            except (ValueError, TypeError):
                raise InterlockError('Occupied path requires its configured source')
        def change(state):
            if any(op['head'] == head for op in state['operations'].values()) or any(v['head'] == head for v in state['printing'].values()):
                raise InterlockError('Path is reserved; observation cannot replace operation state')
            state['paths'][str(head)] = {'state': state_name, 'owner': list(owner) if owner else None, 'observed': self.clock()}
            if state_name != 'clear': state.setdefault('clear_attestations', {}).pop(str(head), None)
        self._change(change)

    def _fresh_device(self, state, ace):
        device = state['devices'].get(str(ace))
        if (not device or device['ready'] is not True or device['observed'] is None
                or not 0 <= self.clock() - device['observed'] <= self.max_age):
            raise InterlockError('Device identity/readiness evidence missing or stale')
        return device

    def _clear_attestation_valid(self, state, head):
        """Subclasses may accept a current, route-bound operator attestation."""
        return False

    def _clear_path_ready(self, state, head):
        """True only for fresh live clearance or a valid operator attestation.

        The live observation window (max_age) is deliberately shorter than
        the operator clearance window. A recorded inspection is not a sensor
        reading, so it must be checked against its own route/session-bound
        evidence instead of being treated as a stale path observation.
        """
        path = state['paths'][str(head)]
        if path['state'] != 'clear':
            return False
        if (path['observed'] is not None
                and 0 <= self.clock() - path['observed'] <= self.max_age):
            return True
        return self._clear_attestation_valid(state, head)

    def _feed_path_allowed(self, state, head):
        """Whether an ordinary feed may start for this observed path.

        The base coordinator is used by simulations and controlled tests, so
        it keeps the conservative fresh-clear requirement. The live coordinator
        overrides this to permit an unknown path with an operator warning: an
        unknown path is not evidence of occupancy, and inspection evidence is
        optional rather than a prerequisite for a normal load.
        """
        return self._clear_path_ready(state, head)

    def begin(self, ace, slot, operation, background=False, exclusive=False):
        if operation not in ('feed', 'retract', 'calibrate') or type(background) is not bool or type(exclusive) is not bool:
            raise InterlockError('Unsupported operation')
        head = self.routes.destination(ace, slot)
        if operation in ('feed', 'calibrate'):self.routes.require_available(ace, slot)
        token = uuid.uuid4().hex
        def change(state):
            if any(op.get('exclusive') for op in state['operations'].values()) or (exclusive and (state['operations'] or state['printing'])):
                raise InterlockError('Exclusive filament verification reserves all operations')
            device = self._fresh_device(state, ace)
            if str(ace) in state['printing']:
                raise InterlockError('ACE is supplying a printing toolhead')
            for op in state['operations'].values():
                if ace in op.get('aces', [op['ace']]) or op['head'] == head or (background and op['background']):
                    raise InterlockError('ACE, toolhead or background queue is reserved')
            path = state['paths'][str(head)]
            if operation in ('feed', 'calibrate') and not self._feed_path_allowed(state, head):
                if path['observed'] is None or not 0 <= self.clock() - path['observed'] <= self.max_age:
                    raise InterlockError('Path evidence missing or stale')
                raise InterlockError('Feed requires a reconciled clear path')
            if operation == 'retract' and (path['observed'] is None or not 0 <= self.clock() - path['observed'] <= self.max_age):
                raise InterlockError('Path evidence missing or stale')
            if operation == 'retract' and (path['state'] != 'occupied' or path['owner'] != [ace, slot]):
                raise InterlockError('Retract requires the matching source owner')
            state['operations'][token] = {'ace': ace, 'slot': slot, 'head': head, 'operation': operation,
                                         'identity': device['identity'], 'background': background}
            if exclusive:state['operations'][token]['exclusive'] = True
            state['paths'][str(head)] = {'state': 'in_progress', 'owner': [ace, slot], 'observed': None}
            state.setdefault('clear_attestations', {}).pop(str(head), None)
            return {'token': token, 'head': head, 'execution_enabled': False}
        return self._change(change)

    def finish(self, token, outcome):
        if not isinstance(token, str) or outcome not in ('reported_success', 'failed', 'cancelled'):
            raise InterlockError('Invalid outcome')
        def change(state):
            if token not in state['operations']:
                raise InterlockError('Unknown or stale operation token')
            op = state['operations'].pop(token)
            state['paths'][str(op['head'])] = {'state': 'unknown', 'owner': None, 'observed': None}
            state.setdefault('clear_attestations', {}).pop(str(op['head']), None)
            state['history'] = (state['history'] + [{'event': outcome, 'token': token, 'head': op['head']}])[-40:]
        self._change(change)

    def hold_printing(self, ace, slot):
        head = self.routes.require_available(ace, slot)
        def change(state):
            self._fresh_device(state, ace)
            if any(op.get('exclusive') or ace in op.get('aces', [op['ace']]) or op['head'] == head for op in state['operations'].values()):
                raise InterlockError('A conflicting operation is in progress')
            if str(ace) in state['printing'] or any(v['head'] == head for v in state['printing'].values()):
                raise InterlockError('ACE or head is already held for printing')
            path = state['paths'][str(head)]
            if (path['state'] != 'occupied' or path['owner'] != [ace, slot] or path['observed'] is None
                    or not 0 <= self.clock() - path['observed'] <= self.max_age):
                raise InterlockError('Printing source is not freshly reconciled')
            state['printing'][str(ace)] = {'head': head, 'slot': slot}
        self._change(change)

    def release_printing(self, ace):
        def change(state):
            hold = state['printing'].pop(str(ace), None)
            if hold is None:
                raise InterlockError('No printing reservation exists')
            state['paths'][str(hold['head'])] = {'state': 'unknown', 'owner': None, 'observed': None}
        self._change(change)

    def _invalidate(self, state, heads, reason):
        for head in heads:
            state['paths'][str(head)] = {'state': 'unknown', 'owner': None, 'observed': None}
        for token, op in list(state['operations'].items()):
            if op['head'] in heads:
                del state['operations'][token]
        for ace, hold in list(state['printing'].items()):
            if hold['head'] in heads:
                del state['printing'][ace]
        state['history'] = (state['history'] + [{'event': 'invalidated', 'reason': reason, 'heads': sorted(heads)}])[-40:]

    def disconnect(self, ace):
        if type(ace) is not int or ace not in range(4):
            raise InterlockError('Invalid ACE')
        def change(state):
            state['devices'].pop(str(ace), None)
            self._invalidate(state, set(self.routes.device_heads(ace)), 'device disconnected')
        self._change(change)
