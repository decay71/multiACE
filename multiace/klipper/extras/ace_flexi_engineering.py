# SPDX-License-Identifier: GPL-3.0-or-later
"""Single cold engineering feed, independent of PathChek qualification.

The guard is a trusted, independently enforcing transport, not the ACE V1
request/ACK adapter. No production guard or command is registered here.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import math
import threading
import uuid

from .ace_flexi_executor import ExecutionError


class EngineeringFeed:
    SOURCE = (0, 0)
    HEAD = 0
    APPROVAL = 'START TEST'

    def __init__(self, coordinator, sources, reader, clock, guard=None):
        self.coordinator = coordinator
        self.sources = sources
        self.reader = reader
        self.clock = clock
        self.guard = guard
        self.job = None
        self.lock = threading.RLock()

    def _snapshot(self):
        sample = self.reader()
        if (sample.get('mode') != 'flexi' or sample.get('idle') is not True or sample.get('cold') is not True
                or sample.get('pathchek_safe') is not True or not sample.get('epoch')):
            raise ExecutionError('Cold idle printer and inactive production required')
        self.sources.require_ace(self.SOURCE)
        device = sample['devices'][0]
        if (sample['protocols'].get(0) != 'v1' or device.get('connected') is not True
                or not device.get('identity') or not self._fresh(device.get('observed'), 1)):
            raise ExecutionError('Fresh original ACE V1 connection required')
        sensors = sample['diagnostics']['sensors']
        if len(sensors) != 4 or {row['head'] for row in sensors} != set(range(4)):
            raise ExecutionError('All four sensor channels are required')
        raw = {}
        for row in sensors:
            if (row.get('enabled') is not True or row.get('communication') != 'ok'
                    or not self._fresh(row.get('observed_at'), .25)
                    or type(row.get('raw_present')) is not bool):
                raise ExecutionError('Sensor state unknown or stale')
            raw[row['head']] = row['raw_present']
        return sample, device, raw

    def _fresh(self, stamp, age):
        return type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= self.clock() - stamp <= age

    def _save(self, **changes):
        updated = dict(self.job, **changes)
        def change(state):
            state['engineering_feed'] = updated
            if updated['phase'] == 'inspection_required':
                entry = dict(ace=0, slot=0, head=0, token=updated['token'],
                             operation='engineering_feed', reason=updated['reason'])
                recovery = state.setdefault('recovery_required', [])
                if not any(row.get('token') == entry['token'] for row in recovery):
                    recovery.append(entry)
        self.coordinator._change(change)
        self.job = updated

    def start(self, distance, speed, timeout, confirmation, route_inspected):
        with self.lock:
            self._start(distance, speed, timeout, confirmation, route_inspected)

    def _start(self, distance, speed, timeout, confirmation, route_inspected):
        if confirmation != self.APPROVAL or route_inspected is not True:
            raise ExecutionError('Explicit START TEST and direct-route inspection required')
        if self.job is not None or self.coordinator.journal is None:
            raise ExecutionError('Single-use session and durable journal required')
        if self.guard is None:
            raise ExecutionError('Independent hardware feed limit and fail-safe stop unavailable')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in (distance, speed, timeout)) or distance > 5 or speed > 20 or timeout > 10 or distance / speed >= timeout:
            raise ExecutionError('Invalid finite engineering request or software ceiling exceeded')
        routes = self.coordinator.routes
        if (routes.require_available(*self.SOURCE) != self.HEAD
                or set(routes.sources(self.HEAD)) != {self.SOURCE}
                or any('0:0' in joiner['branches'] for joiner in routes.topology.as_dict()['joiners'].values())):
            raise ExecutionError('Engineering feed requires an unshared direct T0 route')
        sample, device, raw = self._snapshot()
        material = device.get('effective_materials', device.get('materials', {})).get(0)
        if (device.get('ready') is not True or device.get('slots', {}).get(0) is not True
                or not device.get('source_identities', {}).get(0) or material not in ('PLA', 'PLA Basic')):
            raise ExecutionError('Ready slot with ordinary PLA metadata required')
        if any(raw.values()):
            raise ExecutionError('Engineering baseline requires all four raw readings false')
        token = uuid.uuid4().hex
        request = dict(token=token, source=self.SOURCE, head=self.HEAD, identity=device['identity'],
                       distance=distance, speed=speed, timeout=timeout)
        job = dict(request, phase='reserved', epoch=sample['epoch'], raw_baseline=raw,
                   source_identity=device['source_identities'][0], deadline=self.clock() + timeout,
                   sensor_configuration=sample.get('sensor_configuration'), route_context=routes.context_encode())
        self.coordinator.reserve_engineering_feed(job)
        self.job = job
        try:
            # Arm is non-motion. It must bind a local, fail-closed physical
            # limit and watchdog to this exact command before accepting it.
            if self.guard.arm(request.copy()) != request:
                raise ExecutionError('Independent guard did not bind the exact finite request')
            latest, current, readings = self._snapshot()
            if (self.clock() >= job['deadline'] or latest['epoch'] != job['epoch']
                    or current['identity'] != job['identity'] or current.get('ready') is not True
                    or current.get('slots', {}).get(0) is not True
                    or current.get('source_identities', {}).get(0) != job['source_identity']
                    or latest.get('sensor_configuration') != job['sensor_configuration']
                    or routes.context_encode() != job['route_context'] or readings != raw
                    or token not in self.coordinator.snapshot()['operations']):
                raise ExecutionError('Engineering preflight changed before dispatch')
            self._save(phase='moving')
            self.guard.feed(request.copy())
        except Exception as exc:
            self.stop('Dispatch or guard state uncertain: ' + str(exc))

    def stop(self, reason):
        with self.lock:
            self._stop(reason)

    def _stop(self, reason, **evidence):
        if not self.job or self.job['phase'] == 'inspection_required':
            return
        try:
            self.guard.stop(self.job['token'])
        except Exception as exc:
            reason += '; stop request uncertain: ' + str(exc)
        # Neither ACK, a raw edge nor a power cut reconciles filament position.
        try:
            self._save(phase='inspection_required', reason=reason, qualified=False, **evidence)
        finally:
            # A disk failure cannot re-enable dispatch. The earlier durable
            # reservation remains and startup converts it to recovery.
            self.job.update(phase='inspection_required', reason=reason, qualified=False)

    def tick(self):
        with self.lock:
            self._tick()

    def _tick(self):
        if not self.job or self.job['phase'] != 'moving':
            return
        try:
            j = self.job
            sample, device, raw = self._snapshot()
            if (sample['epoch'] != j['epoch'] or device['identity'] != j['identity']
                    or device.get('source_identities', {}).get(0) != j['source_identity']
                    or sample.get('sensor_configuration') != j['sensor_configuration']
                    or self.coordinator.routes.context_encode() != j['route_context']
                    or j['token'] not in self.coordinator.snapshot()['operations']):
                raise ExecutionError('Live identity, sensor configuration or ownership changed')
            if self.clock() >= j['deadline']:
                raise ExecutionError('Host deadline expired')
            result = self.guard.status(j['token'])
            if result not in ('running', 'stopped'):
                raise ExecutionError('Independent guard reported a fault or out-of-envelope motion')
            changed = [head for head in raw if raw[head] != j['raw_baseline'][head]]
            if changed:
                self._stop('Raw T0 edge observed' if changed == [0] else 'Unexpected or ambiguous raw sensor change',
                           raw_observation=raw)
            elif result == 'stopped':
                self.stop('Guard stopped without an observed arrival')
        except Exception as exc:
            self.stop(str(exc))
