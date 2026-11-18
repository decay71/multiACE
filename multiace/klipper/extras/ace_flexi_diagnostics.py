# SPDX-License-Identifier: GPL-3.0-or-later
"""Passive PAXX12 sensor diagnostics. RAM only; never sends printer commands.

A successful poll observes a cached value, NOT a physical sample timestamp.
No public Boolean or operator acknowledgment qualifies an arrival detector.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import math
import threading
import time
import uuid

SENSORS = tuple('filament_motion_sensor e%d_filament' % h for h in range(4))
MISSING_QUALIFICATIONS = (
    'Physical sensor polarity and startup state',
    'Idle ACE-only arrival response, latency and missed-edge bounds',
    'Feed stop latency and worst-case overshoot; unwind drain behavior',
    'Route-specific retained parking window and shared-junction clearance',
    'Connection loss, interruption and physical recovery verification',
)

class SensorMonitor:
    def __init__(self, clock=time.monotonic, max_age=2., history_limit=100):
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or max_age <= 0:
            raise ValueError('Positive observation freshness limit required')
        if type(history_limit) is not int or not 1 <= history_limit <= 1000:
            raise ValueError('Bounded transition history required')
        self.clock, self.max_age, self.limit = clock, max_age, history_limit
        self.lock = threading.RLock()
        self.rows, self.events, self.sequence = {}, [], 0
        self.last_time = None
        self.session_id, self.poll_sequence = uuid.uuid4().hex, 0

    def observe(self, lookup, eventtime, printer_state, routes):
        """lookup/get_status only; catches failure separately for every sensor."""
        with self.lock:
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now):
                raise ValueError('Invalid monotonic clock')
            if self.last_time is not None and now < self.last_time:
                self.rows = {}  # No transitions may bridge a clock reset.
            self.last_time = now
            self.poll_sequence += 1
            for head, name in enumerate(SENSORS):
                old = self.rows.get(head, {})
                row = dict(head=head, object=name, raw_present=None, enabled=None,
                           communication='unavailable', communication_scope='Klipper object read only', mcu_link='unqualified', error=None, observed_at=None,
                           physical_sample_at=None, physical_freshness='unknown',
                           arrival_qualified=False, polarity='unverified',
                           semantics='cached idle presence-like / print runout-window',
                           printer_state=printer_state)
                try:
                    sensor = lookup(name, None)
                    if sensor is None:
                        raise ValueError('Sensor object is not registered')
                    status = sensor.get_status(eventtime)
                    if not isinstance(status, dict) or type(status.get('filament_detected')) is not bool or type(status.get('enabled')) is not bool:
                        raise ValueError('Sensor status has unknown or malformed values')
                    row.update(raw_present=status['filament_detected'], enabled=status['enabled'],
                               communication='ok', observed_at=now)
                except Exception as exc:
                    row['error'] = str(exc)[:256]
                if (old.get('communication') == 'ok' and row['communication'] == 'ok'
                        and old.get('enabled') is True and row['enabled'] is True
                        and old.get('printer_state') == printer_state
                        and 0 <= now - old['observed_at'] <= self.max_age
                        and old['raw_present'] != row['raw_present']):
                    self.sequence += 1
                    self.events.append(dict(sequence=self.sequence, head=head, object=name,
                                            previous=old['raw_present'], current=row['raw_present'],
                                            observed_at=now, kind='polled_cached_transition',
                                            physical_edge_time=None, physical_verified=False))
                    self.events = self.events[-self.limit:]
                self.rows[head] = row
            return self.snapshot(routes)

    def snapshot(self, routes, expected_head=None):
        with self.lock:
            if expected_head is not None and (type(expected_head) is not int or expected_head not in range(4)):
                raise ValueError('Expected toolhead must be T0-T3')
            now = self.clock()
            rows = []
            for h, name in enumerate(SENSORS):
                row = copy.deepcopy(self.rows.get(h, dict(head=h, object=name,
                    raw_present=None, enabled=None, communication='unavailable', observed_at=None,
                    physical_sample_at=None, arrival_qualified=False, polarity='unverified',
                    physical_freshness='unknown', error='No diagnostic poll received')))
                stamp = row['observed_at']
                row['poll_age_s'] = now - stamp if stamp is not None and now >= stamp else None
                row['poll_fresh'] = row['poll_age_s'] is not None and row['poll_age_s'] <= self.max_age
                row['usable_for_arrival'] = False
                rows.append(row)
            unexpected = [r['head'] for r in rows if expected_head is not None and r['head'] != expected_head
                          and r['poll_fresh'] and r['communication'] == 'ok' and r['enabled'] is True and r['raw_present'] is True]
            return dict(schema=1, session_id=self.session_id, poll_sequence=self.poll_sequence, sensors=rows, transitions=copy.deepcopy(self.events),
                        transition_sequence=self.sequence, expected_head=expected_head,
                        unexpected_raw_heads=unexpected, wrong_toolhead_confirmed=False,
                        explanation='Unexpected raw assertions require inspection; unverified polarity cannot prove wrong-head arrival. Poll freshness is host/cache freshness only; transitions between polls may be missed.',
                        routes=routes.as_dict(), slot_status=routes.availability.as_dict(),
                        routing_context=routes.context(), native_dispatch_enabled=False,
                        missing_qualifications=list(MISSING_QUALIFICATIONS), physical_verified=False,
                        read_only=True)
