# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded ACE transport using MultiACE send_request_to and rejection handling.

Construction, status and imports never send commands. Dispatch requires an
injected operation authorizer; the default is permanently deny. This adapter
only handles ACE-to-toolhead-sensor transport, NOT nozzle seating/extrusion.
All distance feedback is labeled a commanded upper bound, never measurement.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import logging
import math
import time
from .ace_flexi_executor import ExecutionError

class ACETransport:
    simulation_only=False
    @staticmethod
    def validate_profile(step_mm=10.,speed=10.,validated_speed=False):
        """Reject an unusable bounded profile without constructing or dispatching."""
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (step_mm,speed)) or not 0<step_mm<=20 or not 0<speed<=(100 if validated_speed is True else 20):
            raise ExecutionError('Invalid bounded transport profile')
    def __init__(self,ace,coordinator,authorize=None,clock=time.monotonic,step_mm=10.,speed=10.,material_allowed=None,validated_speed=False,step_limit=None,continuous_load=False,motion_ready=None):
        self.validate_profile(step_mm,speed,validated_speed)
        self.motion_ready=motion_ready
        self.continuous_load=continuous_load is True
        self.dispatching=None
        self.ace=ace;self.c=coordinator;self.provider=coordinator.provider;self.authorize=authorize
        self.clock=clock;self.step_mm=step_mm;self.speed=speed;self.material_allowed=material_allowed;self.step_limit=step_limit;self.job=None;self.commanded=0.;self.step=None;self.error=None;self.cancelled=False;self.stop_reply=None;self.stop_at=None

    def _bound(self,job,require_ready=False):
        op=self.c.snapshot()['operations'].get(job['token'])
        if (not op or op['head']!=job['head'] or [job['ace'],job['slot']] not in op.get('sources',[[op['ace'],op['slot']]])
                or op.get('identities',{}).get(str(job['ace']),op['identity'])!=job['identity']):raise ExecutionError('Transport reservation is stale or does not own this source')
        self.c.routes.require_destination(job['ace'],job['slot'],job['head'])
        s=self.provider.read();d=self.provider.device(s,job['ace'],ready=require_ready)
        if d['identity']!=job['identity'] or s['epoch']!=job['epoch']:raise ExecutionError('Transport connection generation or host changed')
        if not job.get('source_identity') or d.get('source_identities',{}).get(job['slot'])!=job['source_identity']:raise ExecutionError('Selected spool/bay identity is missing or changed')
        if self.provider._binding(s,job['head'],ready=False)!=job['binding']:raise ExecutionError('Shared path identity changed')
        if self.provider.source_binding(s,job['head'])!=job['source_binding']:raise ExecutionError('Shared-path source identity changed')
        return s,d

    def _send(self,request,reply):
        # U1's callback uses keyword arguments literally named self/response.
        owner=self
        ace,token=self.job['ace'],self.job['token']
        params=request.get('params') or {}
        try:
            slots=(getattr(self.ace,'_info_per_ace',{}).get(ace) or {}).get('slots') or []
            slot_state=(slots[self.job['slot']] or {}).get('status') if self.job['slot']<len(slots) else None
        except Exception:
            slot_state=None
        logging.info('[multiACE][flexi][step] ACE%d slot=%s state=%s method=%s length=%s speed=%s cont=%s token=%s',
                     ace, params.get('index'), slot_state, request.get('method'), params.get('length'),
                     params.get('speed'), self.continuous_load, token)
        def callback(self,response):
            try:
                logging.info('[multiACE][flexi][step] ACE%d method=%s code=%r msg=%r token=%s',
                             ace, request.get('method'),
                             response.get('code') if isinstance(response,dict) else response,
                             response.get('msg') if isinstance(response,dict) else '', token)
            except Exception:
                pass
            if owner.continuous_load:
                logging.info('[multiACE][Flexi load] ACE%d token=%s method=%s response=%r',
                             ace, token, request['method'], response)
            try:
                if (not isinstance(response,dict) or type(response.get('code')) is not int
                        or owner.ace._tipform_rejected(response)):
                    reply['error']='ACE rejected request: '+str(response)
                else:reply['accepted']=True
            except Exception as exc:reply['error']='ACE callback failure: '+str(exc)
        request=copy.deepcopy(request)
        if getattr(self.ace,'_flexi_tagged_dispatch',False):request['_flexi_token']=self.job['token']
        self.dispatching=request
        try:self.ace.send_request_to(self.job['ace'],request,callback)
        finally:self.dispatching=None

    def allows_continuous_request(self,idx,request,token):
        if not self.continuous_load or self.job is None or self.dispatching is None:
            return False
        # The gate sees the request after _send attached the internal dispatch
        # token, so both sides must be compared without it; otherwise the
        # continuous feed is always denied and the 5 mm segment limit applies.
        job=self.job
        # Compare the motor parameters only: _flexi_token/_flexi_no_replay are
        # dispatch metadata added by Flexi's own boundary layer, not command
        # content, so they must not make the gate reject its own dispatch.
        expected={k:v for k,v in self.dispatching.items() if not k.startswith('_flexi_')}
        actual={k:v for k,v in request.items() if not k.startswith('_flexi_')}
        params=request.get('params') or {}
        same=actual==expected
        allowed=bool(idx==job.get('ace') and token==job.get('token') and same
                     and request.get('method')=='feed_filament'
                     and params.get('length')==job.get('max_distance_mm'))
        logging.info('[multiACE][cont] allowed=%s cont=%s dispatching=%s idx_ok=%s tok_ok=%s same=%s max=%s len=%s keys_expected=%s keys_actual=%s',
                     allowed,self.continuous_load,self.dispatching is not None,idx==job.get('ace'),
                     token==job.get('token'),same,job.get('max_distance_mm'),params.get('length'),
                     sorted(expected),sorted(actual))
        return allowed

    def _dispatch_step(self):
        if self.job['operation']=='load':self.c.routes.require_available(self.job['ace'],self.job['slot'])
        s,d=self._bound(self.job,require_ready=True)
        # Every load frame starts from a freshly observed clear toolhead sensor.
        # A stale clear bit or a late arrival must stop the next dispatch before
        # another motor request can be queued.
        if self.job['operation']=='load':self.provider.sensor(s,self.job['head'],False)
        if (self.motion_ready(self.job['head']) if self.motion_ready else s.get('motion_ready_heads',{}).get(self.job['head'],s.get('motion_ready'))) is not True:
            raise ExecutionError('Selected toolhead thermal/motion prerequisites are not satisfied')
        if self.authorize is None or self.authorize(copy.deepcopy(self.job)) is not True:raise ExecutionError('Real hardware dispatch is locked')
        material=d.get('materials',{}).get(self.job['slot'])
        if not (self.material_allowed(material) if self.material_allowed is not None else material=='PLA'):raise ExecutionError('Selected source material is not authorized')
        if self.job['operation']=='load' and d.get('slots',{}).get(self.job['slot']) is not True:raise ExecutionError('Selected source is no longer available')
        remaining=self.job['max_distance_mm']-self.commanded
        if remaining<=0:raise ExecutionError('No distance budget remains')
        if self.continuous_load and self.commanded:
            raise ExecutionError('Continuous loading cannot issue a second feed')
        length=remaining if self.continuous_load else min(self.step_mm,remaining)
        if self.step_limit is not None:
            allowed=self.step_limit(length,self.speed)
            if allowed is None:return False
            if type(allowed) not in (int,float) or not math.isfinite(allowed) or not 0<allowed<=length:raise ExecutionError('Invalid calibration step budget')
            if self.continuous_load and allowed!=length:
                raise ExecutionError('Incremental approval cannot authorize a continuous feed')
            length=allowed
        now=self.clock()
        # Never queue a segment whose conservative drain time exceeds deadline.
        if now+length/self.speed+1.>=self.job['deadline']:raise ExecutionError('Insufficient deadline for another bounded segment')
        self.step={'length':length,'sent':now,'earliest_ready':now+length/self.speed+.5,'reply':{}}
        self.commanded+=length
        method='feed_filament' if self.job['operation']=='load' else 'unwind_filament'
        self._send({'method':method,'params':{'index':self.job['slot'],'length':length,'speed':self.speed}},self.step['reply'])

    def start(self,job,max_distance_mm,timeout_s):
        if self.job is not None and self.job['token'] in self.c.snapshot()['operations']:
            raise ExecutionError('Transport already owns an operation')
        if self.authorize is None:raise ExecutionError('Real hardware dispatch is locked')
        if self.continuous_load and (job['operation']!='load' or not 0<max_distance_mm<=2100):
            raise ExecutionError('Continuous load requires the finite direct-load budget')
        if job['max_distance_mm']!=max_distance_mm or not math.isclose(job['deadline']-job['started'],timeout_s,rel_tol=0,abs_tol=1e-8):raise ExecutionError('Transport bounds contradict reservation')
        sample,_=self._bound(job,require_ready=True)
        self.other_loaded={head:state.get('detected') is True for head,state in sample.get('heads',{}).items() if head!=job['head']}
        self.job=copy.deepcopy(job);self.commanded=0.;self.step=None;self.error=None;self.cancelled=False;self.stop_reply=None;self.stop_at=None
        self._dispatch_step()

    def _stopped(self,s,d):
        if self.step is None:return True  # No segment was ever dispatched.
        if d.get('ready') is not True:return False
        if self.job['operation']=='load' and self.stop_reply is not None:
            if self.stop_reply.get('error'):return False
            return self.stop_reply.get('accepted') is True and d['observed']>self.stop_at
        # No verified stop-unwind command exists in the inspected V1 API.
        # Cancel unwind by forbidding further segments; the current bounded
        # segment must drain and report fresh ready before stop is acknowledged.
        return (self.step['reply'].get('accepted') is True
                and self.clock()>=self.step['earliest_ready']
                and d['observed']>=self.step['earliest_ready'])

    def stop(self,token):
        if self.job is None:return False
        if token!=self.job['token']:return False
        try:
            op=self.c.snapshot()['operations'].get(token)
            if (not op or op['head']!=self.job['head'] or [self.job['ace'],self.job['slot']] not in op.get('sources',[[op['ace'],op['slot']]]) or op.get('identities',{}).get(str(self.job['ace']),op['identity'])!=self.job['identity']):return False
            s=self.provider.read();d=self.provider.device(s,self.job['ace'],ready=False)
            if d['identity']!=self.job['identity']:return False
        except Exception:return False  # NEVER send a stop to a replacement ACE.
        # A peer-bay change may forbid feeding but must not forbid braking the
        # still-identified device/slot owned by this durable reservation.
        self.cancelled=True
        if self.step is None:return True
        if self.job['operation']=='load' and self.stop_reply is None:
            self.stop_reply={};self.stop_at=self.clock()
            self._send({'method':'stop_feed_filament','params':{'index':self.job['slot']}},self.stop_reply)
        return self._stopped(s,d)

    def poll(self,token):
        if self.job is None or token!=self.job['token']:raise ExecutionError('Unknown transport operation')
        s,d=self._bound(self.job)
        if any(head!=self.job['head'] and state.get('detected') is True
               and not self.other_loaded.get(head,False)
               for head,state in s.get('heads',{}).items()):
            raise ExecutionError('Unexpected non-target toolhead sensor transition')
        reply=self.step['reply'] if self.step else {}
        if reply.get('error'):self.error=reply['error']
        result={'token':token,'distance_mm':self.commanded,'distance_kind':'commanded_upper_bound','done':False,'stopped':False,'completion_stage':'toolhead_sensor','ready_to_print':False}
        if self.error:return dict(result,error=self.error)
        if self.step and reply.get('accepted') is not True and self.clock()-self.step['sent']>=5.:
            return dict(result,error='Bounded feed acknowledgment timed out')
        if self.stop_at is not None and not self._stopped(s,d) and self.clock()-self.stop_at>=5.:
            return dict(result,error='Feed stop was not acknowledged by fresh Ready status')
        reached=s['heads'][self.job['head']]['detected'] is (self.job['operation']=='load')
        if reached and self.job['operation']=='load':
            self.stop(token)
            if self._stopped(s,d):return dict(result,done=True,stopped=True)
            return result
        if self.cancelled:return dict(result,done=self._stopped(s,d),stopped=self._stopped(s,d))
        if self._stopped(s,d):
            if reached or self.commanded>=self.job['max_distance_mm']:return dict(result,done=True,stopped=True)
            self._dispatch_step();result['distance_mm']=self.commanded
        return result
