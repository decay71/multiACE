# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted in-process, read-only observations. No motion or automatic clearance.

The reader must be the Klipper adapter, not a request-supplied status dictionary.
Operator declarations cover unsensed tubing and expire with time, epoch, route,
connection generation, sensor disagreement, operation start or process restart.
This is an in-process trust boundary, not a security boundary against Python code.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import hashlib
import math
import time
import uuid
import threading
from functools import wraps

def serialized(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self._lock:return method(self,*args,**kwargs)
    return call

class EvidenceError(ValueError):
    pass

class LiveEvidenceProvider:
    def __init__(self, routes, reader, clock=time.monotonic, max_age=5., clearance_age=30.):
        if not callable(reader) or not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in (max_age,clearance_age)):
            raise EvidenceError('Invalid evidence provider settings')
        self._lock=threading.RLock()
        self.routes=routes;self.reader=reader;self.clock=clock;self.max_age=max_age;self.clearance_age=clearance_age
        self.session=uuid.uuid4().hex;self.fingerprint=hashlib.sha256(routes.context_encode().encode()).hexdigest()
        self._clear={};self._owners={};self._epoch=None;self._identities={};self._sources={}

    def _fresh(self,stamp,limit=None):
        return type(stamp) in (int,float) and math.isfinite(stamp) and 0<=self.clock()-stamp<=(limit or self.max_age)

    @serialized
    def read(self):
        try:s=copy.deepcopy(self.reader())
        except Exception as exc:
            self._clear.clear();self._owners.clear()
            raise EvidenceError('Live reader unavailable: '+str(exc))
        if (not isinstance(s,dict) or not self._fresh(s.get('observed'))
                or not isinstance(s.get('epoch'),str) or not s['epoch']
                or s.get('routes_sha256')!=self.fingerprint or s.get('mode')!='flexi'):
            self._clear.clear();self._owners.clear();raise EvidenceError('Live session, route or timestamp mismatch')
        if (not isinstance(s.get('devices'),dict) or not isinstance(s.get('heads'),dict)
                or any(type(a) is not int or not isinstance(d,dict) for a,d in s['devices'].items())
                or any(type(h) is not int or not isinstance(d,dict) for h,d in s['heads'].items())):
            self._clear.clear();self._owners.clear();raise EvidenceError('Malformed device or sensor snapshot')
        epoch=s['epoch']
        identities={a:d.get('identity') for a,d in s.get('devices',{}).items() if d.get('connected') is True}
        if self._epoch is not None and self._epoch!=epoch:
            self._clear.clear();self._owners.clear()
        for ace,identity in self._identities.items():
            if identities.get(ace)!=identity:
                for h in self.routes.device_heads(ace):self.invalidate(h)
        sources={(a,slot):signature for a,d in s['devices'].items() for slot,signature in (d.get('source_identities') or {}).items()}
        for source,signature in self._sources.items():
            if sources.get(source)!=signature:
                try:self.invalidate(self.routes.destination(*source))
                except ValueError:pass
        self._sources=sources
        self._epoch=epoch;self._identities=identities
        for ace in sorted({int(k.split(':')[0]) for k in self.routes.as_dict()}):
            try:self.device(s,ace)
            except EvidenceError:
                for h in self.routes.device_heads(ace):self.invalidate(h)
        if s.get('idle') is not True:self._clear.clear()
        # A single contradictory or missing sample consumes clearance.
        for h in range(4):
            p=s.get('heads',{}).get(h,{})
            fresh=self._fresh(p.get('observed'))
            if not fresh or p.get('enabled') is not True or p.get('detected') is not False:self._clear.pop(h,None)
            if not fresh or p.get('enabled') is not True or p.get('detected') is not True:self._owners.pop(h,None)
        return s

    def device(self,s,ace,ready=True):
        d=s.get('devices',{}).get(ace)
        if (not d or d.get('connected') is not True or not isinstance(d.get('identity'),str)
                or not d['identity'] or not self._fresh(d.get('observed'))
                or (ready and d.get('ready') is not True)):
            raise EvidenceError('Device disconnected, stale, unidentified or not ready')
        return d

    def sensor(self,s,head,detected):
        h=s.get('heads',{}).get(head)
        if (not h or h.get('enabled') is not True or h.get('detected') is not detected
                or not self._fresh(h.get('observed'))):
            raise EvidenceError('Required physical toolhead sensor is unknown, stale or contradictory')
        return h

    def _binding(self,s,head,ready=True):
        return tuple((a,self.device(s,a,ready)['identity']) for a in sorted({a for a,_ in self.routes.sources(head)}))

    def source_binding(self,s,head):
        result=[]
        for ace,slot in self.routes.sources(head):
            signature=self.device(s,ace,ready=False).get('source_identities',{}).get(slot)
            if not isinstance(signature,str) or not signature:raise EvidenceError('Shared-path bay identity/generation unavailable')
            result.append((ace,slot,signature))
        return tuple(result)

    @serialized
    def attest_clear(self,head,confirmed_sources,confirmation):
        # Must be invoked by a separately approved operator workflow. Merely
        # providing empty sensors can NEVER manufacture clear-path evidence.
        sources=self.routes.sources(head)
        if (not isinstance(confirmation,str) or not confirmation.strip()
                or tuple(sorted(confirmed_sources))!=sources or not sources):
            raise EvidenceError('Explicit confirmation must cover every source sharing the head')
        s=self.read()
        if s.get('idle') is not True:raise EvidenceError('Printer must be idle')
        self.sensor(s,head,False);binding=self._binding(s,head)
        self._clear[head]={'epoch':s['epoch'],'binding':binding,'sources':self.source_binding(s,head),'at':self.clock(),'confirmation':confirmation}
        self._owners.pop(head,None)

    @serialized
    def confirm_verified_unload(self,head,receipt):
        if receipt.get('recipe_verified') is not True or receipt.get('sensor_clear') is not True or receipt.get('stopped') is not True:
            raise EvidenceError('A verified, stopped withdrawal is required')
        self.attest_clear(head,self.routes.sources(head),'verified unload')
        self._clear[head]['verified_unload']=True

    @serialized
    def path(self,s,head):
        self.routes.sources(head)
        unknown={'state':'unknown','owner':None,'observed':None}
        try:
            h=s.get('heads',{}).get(head,{})
            if h.get('enabled') is not True or not self._fresh(h.get('observed')):return unknown
            if h.get('detected') is False:
                c=self._clear.get(head)
                if (c and c['epoch']==s['epoch'] and (c.get('verified_unload') or self._fresh(c['at'],self.clearance_age))
                        and c['binding']==self._binding(s,head) and c['sources']==self.source_binding(s,head)):
                    return {'state':'clear','owner':None,'observed':h['observed'] if c.get('verified_unload') else min(c['at'],h['observed'])}
            elif h.get('detected') is True:
                o=self._owners.get(head)
                if (o and o['epoch']==s['epoch'] and o['binding']==self._binding(s,head)
                        and o['session']==self.session and o['sources']==self.source_binding(s,head)):
                    return {'state':'occupied','owner':list(o['source']),'observed':h['observed']}
        except EvidenceError:pass
        return unknown

    @serialized
    def attestation_valid(self,head,record):
        """Validate an operator clearance record against the live session.

        The coordinator's path observation window is intentionally short, but
        an operator inspection remains evidence for ``clearance_age``. This
        check binds the record to the same sensor generation, route/topology
        fingerprint, source identities and live clear sample. It never extends
        the record's own lifetime and never creates clearance by itself.
        """
        if (type(head) is not int or head not in range(4) or not isinstance(record,dict)
                or record.get('head')!=head):
            return False
        try:
            s=self.read()
            if s.get('idle') is not True:return False
            h=s.get('heads',{}).get(head,{})
            if h.get('enabled') is not True or h.get('detected') is not False or not self._fresh(h.get('observed')):
                return False
            clear=self._clear.get(head)
            if (not clear or clear.get('epoch')!=s['epoch']
                    or not self._fresh(clear.get('at'),self.clearance_age)
                    or clear.get('binding')!=self._binding(s,head)
                    or clear.get('sources')!=self.source_binding(s,head)):
                return False
            if (record.get('routes_sha256')!=self.fingerprint
                    or record.get('session')!=self.session
                    or record.get('epoch')!=s['epoch']
                    or not self._fresh(record.get('at'),self.clearance_age)):
                return False
            sources=tuple(tuple(source) for source in (record.get('sources') or ()))
            return sources==tuple(tuple(source) for source in self.routes.sources(head))
        except Exception:
            return False

    @serialized
    def invalidate(self,head):
        self._clear.pop(head,None);self._owners.pop(head,None)

    @serialized
    def complete_load(self,head,source,operation_receipt):
        # Called only by the operation owner after bounded transport completion,
        # fresh physical sensor confirmation and reservation-token validation.
        self.routes.require_destination(*source,head)
        if not isinstance(operation_receipt,str) or not operation_receipt:raise EvidenceError('Missing load receipt')
        s=self.read();self.sensor(s,head,True);binding=self._binding(s,head)
        self._owners[head]={'source':tuple(source),'epoch':s['epoch'],'binding':binding,'session':self.session,'sources':self.source_binding(s,head),'receipt':operation_receipt}
        self._clear.pop(head,None)

    @serialized
    def observations(self):
        s=self.read();devices={}
        for ace in sorted({int(k.split(':')[0]) for k in self.routes.as_dict()}):
            try:
                d=self.device(s,ace);devices[str(ace)]={'identity':d['identity'],'ready':True,'observed':d['observed']}
            except EvidenceError:pass
        return {'session':self.session,'epoch':s['epoch'],'routes_sha256':self.fingerprint,
                'devices':devices,'paths':{str(h):self.path(s,h) for h in range(4)}}

    @serialized
    def status(self):
        try:
            o=self.observations();return {'available':True,'session':self.session,'devices':o['devices'],'paths':o['paths'],
                                        'execution_enabled':False,'unsensed_tubing_requires_confirmation':True}
        except Exception as exc:
            return {'available':False,'reason':str(exc),'execution_enabled':False}
