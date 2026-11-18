# SPDX-License-Identifier: GPL-3.0-or-later
"""Flexi PathChek: bounded, simulation-only physical-routing workflow foundation.

Shares RC1 coordinator reservations. No G-code, serial, heating or file I/O.
There is intentionally no native transport grant in RC2 Session 1.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import math
import threading
import time
from functools import wraps
from .ace_flexi_production import ProductionError, digest


class PathChekError(ProductionError):
    pass


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


def profile(value):
    fields = {'max_feed_mm', 'step_mm', 'speed', 'timeout_s', 'parking_mm',
              'retention_min_mm', 'shared_clear_max_mm', 'parking_qualified'}
    if not isinstance(value, dict) or set(value) != fields:
        raise PathChekError('Measured route bounds and parking qualification are required')
    for key in fields - {'parking_qualified'}:
        n = value[key]
        if type(n) not in (int, float) or not math.isfinite(n) or n <= 0:
            raise PathChekError('Invalid PathChek parameter: ' + key)
    if (value['step_mm'] > 5 or value['speed'] > 20 or value['timeout_s'] > 600
            or not value['retention_min_mm'] <= value['parking_mm'] <= value['shared_clear_max_mm'] < value['max_feed_mm']
            or value['parking_qualified'] is not True):
        raise PathChekError('Parking must retain the spool AND clear every shared branch; no percentage rule')
    return copy.deepcopy(value)


class PathChek:
    def __init__(self, routes, coordinator, adapter, profiles, sources, authorize=None,
                 clearance=None, context=None, clock=time.monotonic, records=None, reconcile_next=None):
        self.routes, self.c, self.adapter, self.sources = routes, coordinator, adapter, sources
        self.profiles = copy.deepcopy(profiles)
        self.authorize, self.clearance = authorize, clearance
        self.reconcile_next = reconcile_next
        self.context_reader = context or (lambda source: {})
        self.clock, self.lock = clock, threading.RLock()
        self.job = None
        self.queue = []
        self.records = copy.deepcopy(records or {})
        self.last = None

    def context(self, source):
        key = '%d:%d' % tuple(source)
        return digest({'routes': self.routes.as_dict(), 'profile': self.profiles.get(key),
                       'sources': self.sources.context(), 'environment': self.context_reader(tuple(source))})

    def _snapshot(self, source=None):
        sample = self.adapter.snapshot()
        now = self.clock()
        if sample.get('idle') is not True:
            raise PathChekError('PathChek requires idle printer and feeder state')
        if not sample.get('epoch'):
            raise PathChekError('Missing host generation')
        for h in range(4):
            sensor = sample.get('sensors', {}).get(h, {})
            stamp = sensor.get('observed')
            if (sensor.get('enabled') is not True or sensor.get('kind') != 'presence_idle'
                    or sensor.get('arrival_qualified') is not True or type(sensor.get('present')) is not bool
                    or type(stamp) not in (int, float) or not 0 <= now - stamp <= 1):
                raise PathChekError('All four fresh, independently qualified presence signals are required')
        if source is not None:
            device = sample.get('devices', {}).get(source[0], {})
            if (device.get('connected') is not True or not device.get('identity') or device.get('jam') is not False
                    or device.get('slots',{}).get(source[1]) is not True):
                raise PathChekError('Device disconnected, jammed or identity unknown')
        return sample

    def _authorized(self):
        if getattr(self.adapter, 'simulation_only', False) is not True:
            raise PathChekError('Native PathChek transport is not physically qualified; dispatch disabled')
        if self.authorize is None or self.authorize() is not True:
            raise PathChekError('PathChek execution is not authorized')

    @locked
    def start(self, source=None, all_routes=False):
        if self.job or self.queue:
            raise PathChekError('PathChek already owns a sequence')
        self._authorized()
        queue = [tuple(map(int, key.split(':'))) for key in self.routes.as_dict()] if all_routes is True else [tuple(source or ())]
        if not queue or (all_routes is not True and len(queue[0]) != 2):
            raise PathChekError('Choose one configured route or all routes')
        # Validate every requested route before acquiring or feeding any resource.
        for s in queue:
            self.sources.require_ace(s)
            profile(self.profiles.get('%d:%d' % s))
        self.queue = queue
        try:
            self._next()
        except Exception:
            self.queue = []
            raise
        return self.status()

    def _next(self):
        source = self.queue[0]
        self._authorized()
        self.sources.require_ace(source)
        sample = self._snapshot(source)
        if any(s['present'] for s in sample['sensors'].values()):
            raise PathChekError('All downstream sensors must start clear; inspect all shared branches')
        p = profile(self.profiles['%d:%d' % source])
        lease = self.c.begin(*source, 'feed', exclusive=True)
        self.job = {'source': source, 'head': self.routes.destination(*source), 'token': lease['token'],
                    'profile': p, 'context': self.context(source), 'epoch': sample['epoch'],
                    'device_identity': sample['devices'][source[0]]['identity'], 'phase': 'feeding',
                    'deadline': self.clock() + p['timeout_s'], 'fed': 0., 'retracted': 0.,
                    'pending': None, 'fault': None}

    def _stop(self):
        # No command may ever address a reconnected/replacement device.
        sample = self.adapter.snapshot()
        device = sample.get('devices', {}).get(self.job['source'][0], {})
        if device.get('connected') is not True or sample.get('epoch') != self.job['epoch'] or device.get('identity') != self.job['device_identity']:
            return False
        return self.adapter.stop(self.job['token']) is True

    def _fault(self, message):
        self.queue = []
        self.job['fault'] = str(message)
        self.job['phase'] = 'recovery_required'
        try:
            self.job['stopped'] = self._stop()
        except Exception:
            self.job['stopped'] = False
        # Reservation is deliberately retained, even when the stop is confirmed.
        self.last = {'outcome': 'recovery_required', 'reason': str(message)}
        self.records['%d:%d' % self.job['source']] = dict(self.last, context=self.job['context'], physical_verified=False)

    def _move(self, direction, length):
        self._authorized()
        operation = self.c.snapshot()['operations'].get(self.job['token'])
        if not operation or operation.get('exclusive') is not True:
            raise PathChekError('PathChek lost its exclusive filament reservation')
        if self.context(self.job['source']) != self.job['context']:
            raise PathChekError('Route, material, source or adapter context changed')
        self.sources.require_ace(self.job['source'])
        sample = self._snapshot(self.job['source'])
        if sample['devices'][self.job['source'][0]].get('ready') is not True:
            raise PathChekError('ACE is not freshly ready for another bounded segment')
        if self.clock() + length / self.job['profile']['speed'] + 1 >= self.job['deadline']:
            raise PathChekError('Insufficient deadline for a bounded segment and stop margin')
        self.job['pending'] = self.adapter.move(self.job['source'], direction, length,
                                               self.job['profile']['speed'], self.job['token'])
        if self.job['pending'] is None:
            raise PathChekError('Missing transport acknowledgement handle')
        self.job['pending_length'] = length

    @locked
    def tick(self):
        j = self.job
        if not j or j['phase'] == 'recovery_required':
            return self.status()
        try:
            self._authorized()
            s = self._snapshot(j['source'])
            if (s['epoch'] != j['epoch'] or s['devices'][j['source'][0]]['identity'] != j['device_identity']
                    or self.context(j['source']) != j['context']):
                raise PathChekError('Connection or configuration generation changed')
            if self.clock() >= j['deadline']:
                raise PathChekError('PathChek operation timed out')
            wrong = [h for h, sensor in s['sensors'].items() if h != j['head'] and sensor['present']]
            if wrong:
                raise PathChekError('Wrong toolhead detection: ' + ','.join('T%d' % h for h in wrong))
            if j['phase'] == 'feeding' and s['sensors'][j['head']]['present']:
                j['phase'] = 'stopping_arrival'
            if j['phase'] == 'stopping_arrival':
                if not self._stop():
                    return self.status()
                # Include the entire outstanding bound: commanded travel is not measurement.
                j['fed'] += j.get('pending_length', 0.) if j['pending'] else 0.
                j['pending'] = None
                origin = self.adapter.parking_origin(j['token'], j['source'])
                low, high = origin.get('min_mm'), origin.get('max_mm')
                if (origin.get('token') != j['token'] or origin.get('source') != list(j['source'])
                        or origin.get('identity') != j['device_identity'] or origin.get('epoch') != j['epoch']
                        or origin.get('qualified') is not True
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in (low, high))
                        or not 0 < low <= high <= j['profile']['max_feed_mm']
                        or type(origin.get('observed')) not in (int, float)
                        or not 0 <= self.clock() - origin['observed'] <= 1):
                    raise PathChekError('Qualified stopped-tip position interval is required before retracting')
                # Compute from a qualified position interval, never from the
                # commanded feed upper bound or a percentage of it.
                if low <= j['profile']['parking_mm'] or high-low+j['profile']['parking_mm'] > j['profile']['shared_clear_max_mm']:
                    raise PathChekError('No parking move can guarantee both retention and shared-junction clearance')
                j['arrival_position_bounds'] = [low, high]
                j['parking_retract_mm'] = low-j['profile']['parking_mm']
                j['phase'] = 'parking'
            if j['pending'] is not None:
                answer = self.adapter.poll(j['pending'])
                if answer.get('error'):
                    raise PathChekError(answer['error'])
                if answer.get('complete') is not True:
                    return self.status()
                j['fed' if j['phase'] == 'feeding' else 'retracted'] += j['pending_length']
                j['pending'] = None
            p = j['profile']
            if j['phase'] == 'feeding':
                remaining = p['max_feed_mm'] - j['fed']
                if remaining <= 0:
                    raise PathChekError('Arrival was not observed within the configured feed bound')
                self._move('feed', min(p['step_mm'], remaining))
            elif j['phase'] == 'parking':
                remaining = j['parking_retract_mm'] - j['retracted']
                if remaining > 1e-8:
                    self._move('retract', min(p['step_mm'], remaining))
                else:
                    j['phase'] = 'await_clearance'
            if j['phase'] == 'await_clearance':
                witness = self.clearance(copy.deepcopy(j), s) if self.clearance else None
                if not witness:
                    return self.status()
                position = witness.get('position_mm')
                if (witness.get('token') != j['token'] or witness.get('context') != j['context']
                        or witness.get('source') != list(j['source']) or witness.get('retained') is not True
                        or witness.get('all_branches_clear') is not True or type(position) not in (int, float)
                        or not math.isfinite(position) or not p['retention_min_mm'] <= position <= p['shared_clear_max_mm']
                        or type(witness.get('observed')) not in (int, float)
                        or not 0 <= self.clock() - witness['observed'] <= 1
                        or any(v['present'] for v in s['sensors'].values())):
                    raise PathChekError('Parking/clearance witness is missing, stale or contradictory')
                key = '%d:%d' % j['source']
                self.records[key] = {'context': j['context'], 'outcome': 'simulated_route_verified',
                                     'head': j['head'], 'physical_verified': False, 'parking_witness': copy.deepcopy(witness)}
                self.c.finish(j['token'], 'reported_success')
                self.last = copy.deepcopy(self.records[key])
                self.queue.pop(0)
                self.job = None
                if self.queue and self.reconcile_next is not None:
                    self.reconcile_next(copy.deepcopy(j), copy.deepcopy(witness))
                    self._next()
                # Never fabricate coordinator clear-path evidence from sensor=False.
                # The next route requires a fresh external clearance reconciliation.
        except Exception as exc:
            if self.job:
                self._fault(exc)
            else:
                self.queue = []
                raise
        return self.status()

    @locked
    def continue_sequence(self):
        if self.job or not self.queue:
            raise PathChekError('Complete recovery/clearance before continuing the sequence')
        self._next()
        return self.status()

    @locked
    def cancel(self):
        if self.job:
            self._fault('Operator cancelled PathChek; inspect and recover before further feeding')
        else:
            self.queue = []
        return self.status()

    @locked
    def recover(self, confirmation):
        if not self.job or self.job['phase'] != 'recovery_required' or confirmation != 'INSPECTED_ALL_PATHS_CLEAR_AND_DEVICE_STOPPED':
            raise PathChekError('Explicit stopped-device and all-path inspection required')
        sample = self._snapshot()
        if sample.get('operator_stopped') is not True or any(s['present'] for s in sample['sensors'].values()):
            raise PathChekError('Recovery requires stopped device and clear sensors plus physical inspection')
        self.c.finish(self.job['token'], 'cancelled')
        self.job = None
        return self.status()

    @locked
    def status(self):
        results = {key: dict(copy.deepcopy(row), valid=row.get('outcome') == 'simulated_route_verified' and row.get('context') == self.context(tuple(map(int, key.split(':')))))
                   for key, row in self.records.items()}
        return {'name': 'Flexi PathChek', 'native_dispatch_enabled': False, 'physical_verified': False,
                'job': copy.deepcopy(self.job), 'remaining': [list(s) for s in self.queue],
                'results': results, 'last': copy.deepcopy(self.last)}
