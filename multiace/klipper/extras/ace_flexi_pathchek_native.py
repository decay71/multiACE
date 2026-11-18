# SPDX-License-Identifier: GPL-3.0-or-later
"""Real-interface PathChek transport. Default unbound dispatch is disabled.

Uses existing V1 request vocabulary and coordinator journal/ownership contracts.
Only the durable native executor can bind exact per-request dispatch permission.
The legacy preview/move interface never grants native movement.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import math
import time
from .ace_flexi_pathchek import PathChekError, profile
from .ace_flexi_diagnostics import MISSING_QUALIFICATIONS

NATIVE_DISPATCH_ENABLED = False

class NativePathChekAdapter:
    simulation_only = False
    def __init__(self, routes, coordinator, sources, monitor, live_reader, protocol_reader, clock=time.monotonic, sender=None, reject=None):
        self.routes, self.c, self.sources, self.monitor = routes, coordinator, sources, monitor
        self.live_reader, self.protocol_reader, self.clock = live_reader, protocol_reader, clock
        self._native_sender, self._native_reject, self._native_permit = sender, reject, None

    def bind_native(self, permit):
        # Only the durable native executor supplies the per-request permission.
        self._native_permit = permit

    def dispatch_owned(self, request, token, reply):
        import copy
        method=request.get('method')
        if method not in ('feed_filament','unwind_filament','stop_feed_filament','stop_unwind_filament'):
            raise PathChekError('Unsupported native PathChek method')
        op=self.c.snapshot()['operations'].get(token)
        if self.c.journal is None or not op or op.get('exclusive') is not True or self._native_sender is None or self._native_reject is None:
            raise PathChekError('Native sender and exact exclusive journal ownership required')
        idx=op['ace']
        if self._native_permit is None or self._native_permit(idx,request,token) is not True:
            raise PathChekError('Native PathChek movement is not authorized')
        def callback(self,response):
            try:
                if not isinstance(response,dict) or type(response.get('code')) is not int or reject(response):
                    reply['error']='ACE request rejected: '+str(response)
                    if isinstance(response,dict) and type(response.get('code')) in (int,str):
                        # This is the wire response code. It has no established
                        # equivalence to Anycubic printer-side support codes.
                        reply['reported_error']={'source':'ACE V1 transport','code':response['code'],
                            'message':reply['error'],'operation':method,
                            'timestamp':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
                else:reply['accepted']=True
            except Exception as exc:reply['error']='ACE callback error: '+str(exc)
        reject=self._native_reject
        packet=copy.deepcopy(request);packet['_flexi_token']=token
        self._native_sender(idx,packet,callback)

    def snapshot(self):
        diagnostics = self.monitor.snapshot(self.routes)
        live = self.live_reader()  # Existing cache / Klipper get_status only.
        return {'idle':live.get('idle') is True, 'epoch':live.get('epoch'),
                'devices':live.get('devices', {}),
                'sensors':{r['head']:{'present':r['raw_present'], 'enabled':r['enabled'],
                    'observed':r['observed_at'], 'kind':'unqualified_paxx12_cached',
                    'arrival_qualified':False, 'poll_fresh':r['poll_fresh'],
                    'communication':r['communication']} for r in diagnostics['sensors']}}

    def readiness(self):
        diagnostics = self.monitor.snapshot(self.routes)
        state = self.c.snapshot()
        return {'native_dispatch_enabled':False, 'hardware_qualified':False,
                'transport':'MultiACE V1 send_request_to; not dispatched',
                'journal_present':self.c.journal is not None,
                'resources_busy':bool(state['operations'] or state['printing'] or state.get('recovery_required')),
                'missing_qualifications':list(MISSING_QUALIFICATIONS),
                'sensor_poll_ready':all(r['communication']=='ok' and r['poll_fresh'] and r['enabled'] is True for r in diagnostics['sensors']),
                'reason':'Native execution remains locked. Request previews are not commands, movement receipts or arrival evidence.'}

    def preview_segment(self, source, direction, length, speed, token, deadline, measured_profile):
        """Validate a future segment against REAL ownership interfaces, no I/O.

        Even a fully valid preview cannot be dispatched by this adapter.
        Recovery/stop-unwind semantics must be physically qualified separately.
        """
        source = tuple(source)
        self.sources.require_ace(source)
        p = profile(measured_profile)
        if direction not in ('feed','retract'):
            raise PathChekError('Unknown PathChek direction')
        if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in (length,speed,deadline)) or length > min(5,p['step_mm']) or speed > min(20,p['speed']):
            raise PathChekError('Segment exceeds explicit route bounds')
        if self.clock() + length / speed + 1 >= deadline:
            raise PathChekError('Insufficient segment/stop deadline')
        if self.c.journal is None:
            raise PathChekError('Durable recovery journal required')
        operation = self.c.snapshot()['operations'].get(token)
        head = self.routes.destination(*source)
        if not operation or operation.get('exclusive') is not True or operation.get('head') != head or (operation.get('ace'),operation.get('slot')) != source:
            raise PathChekError('Exact exclusive source/toolhead ownership required')
        sample = self.snapshot()
        device = sample['devices'].get(source[0], {})
        if not sample['idle'] or not sample.get('epoch') or device.get('connected') is not True or device.get('ready') is not True or not device.get('identity') or device.get('identity') != operation.get('identity') or device.get('slots',{}).get(source[1]) is not True:
            raise PathChekError('Fresh idle source/device identity and slot readiness required')
        stamp=device.get('observed')
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0 <= self.clock()-stamp <= 1:
            raise PathChekError('ACE cached telemetry is stale or unknown')
        if self.protocol_reader(source[0]) != 'v1':
            raise PathChekError('Only inspected original ACE Pro V1 vocabulary is supported')
        return {'request':{'method':'feed_filament' if direction=='feed' else 'unwind_filament',
                           'params':{'index':source[1],'length':length,'speed':speed}},
                'ace':source[0], 'head':head, 'token':token, 'dispatch_enabled':False,
                'distance_kind':'commanded_bound_not_measurement',
                'blocked_by':list(MISSING_QUALIFICATIONS)}

    def move(self, *args, **kwargs):
        raise PathChekError('Native PathChek movement disabled pending physical qualification and separate authorization')

    def poll(self, handle):
        raise PathChekError('No native operation dispatched; acknowledgments cannot prove movement')

    def stop(self, token):
        return False  # Never invent a stop receipt or send to a replacement ACE.

    def parking_origin(self, token, source):
        return {'qualified':False, 'reason':'No measured stopped-tip interval or clearance witness'}
