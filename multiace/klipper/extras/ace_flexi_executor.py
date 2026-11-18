# SPDX-License-Identifier: GPL-3.0-or-later
"""Route-bound bounded operation state machine. Production dispatch stays locked.

No import or method in this module connects to hardware. A future reviewed
transport adapter is required; the only dispatch path presently permitted is
an explicitly simulation-only transport. No automatic switch or failover.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import math
import threading
from .ace_flexi_interlocks import Coordinator, InterlockError
from .ace_flexi_evidence import LiveEvidenceProvider, EvidenceError

# Only these legacy reasons prove the failure happened in selection/heating,
# before the workflow could dispatch filament into a shared path. Records from
# the current build carry physical_started/transport_started instead.
_LEGACY_PRE_TRANSPORT_REASONS=(
    'Physical tool selection and XYZ homing required before heating',
    'Native physical tool selection is required before heating',
    'Native tool selection did not establish X/Y homing',
    'Physical head/thermal prerequisites failed before feeding',
)

class ExecutionError(ValueError):
    pass

class LiveCoordinator(Coordinator):
    def __init__(self,routes,provider,journal=None,**kwargs):
        if type(provider) is not LiveEvidenceProvider or provider.routes.context()!=routes.context():
            raise InterlockError('Coordinator requires its bound in-process evidence provider')
        self.provider=provider
        def merge(state,old):
            old=old or {}
            if old and old.get('routes_sha256') != state['routes_sha256']:
                raise InterlockError('Journal routing context changed; preserve the journal for recovery')
            for key in ('operations', 'printing', 'confirmed_sources', 'nozzle_owners',
                        'nozzle_recovery', 'pathchek_discoveries', 'pathchek_qualifications'):
                records = old.get(key, {})
                if not isinstance(records, dict) or any(not isinstance(record, dict) for record in records.values()):
                    raise InterlockError('Invalid journal records: ' + key)
            for key in ('recovery_required', 'recovery_resolutions', 'recipe_calibrations',
                        'wizard_results', 'pathchek_results'):
                records = old.get(key, [])
                if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                    raise InterlockError('Invalid journal records: ' + key)
            recovery=copy.deepcopy(old.get('recovery_required',[]))
            # An operation that was durably marked as pre-transport was not
            # moving filament when the process ended. Do not turn that into a
            # physical recovery record. Legacy operations without the marker
            # remain conservative.
            # A restart of a still-unresolved operation must not append a second
            # recovery row for a token that already has one: the original record
            # carries the real reason and one inspection clears one path.
            known={r.get('token') for r in recovery if isinstance(r,dict)}
            recovery += [dict(op,token=token,reason='process interrupted during operation')
                         for token,op in old.get('operations',{}).items()
                         if op.get('transport_started') is not False and token not in known]
            for ace, hold in old.get('printing', {}).items():
                slot, head = hold.get('slot'), hold.get('head')
                if (ace not in tuple(map(str, range(4))) or type(slot) is not int or slot not in range(4)
                        or type(head) is not int or head not in range(4)):
                    raise InterlockError('Invalid journal feed-assist reservation')
                recovery.append(dict(ace=int(ace), slot=slot, head=head, aces=[int(ace)],
                                     sources=[[int(ace), slot]], operation='print',
                                     token='print:'+old['session']+':'+ace,
                                     reason='process interrupted with feed-assist reservation'))
            state.update(pathchek_discoveries=copy.deepcopy(old.get('pathchek_discoveries',{})),pathchek_qualifications=copy.deepcopy(old.get('pathchek_qualifications',{})),pathchek_results=copy.deepcopy(old.get('pathchek_results',[])),pathchek_job=copy.deepcopy(old.get('pathchek_job')),engineering_feed=copy.deepcopy(old.get('engineering_feed')),recovery_required=recovery,confirmed_sources=copy.deepcopy(old.get('confirmed_sources',{})),recipe_calibrations=copy.deepcopy(old.get('recipe_calibrations',[])),wizard_results=copy.deepcopy(old.get('wizard_results',[])),nozzle_recovery=copy.deepcopy({**old.get('nozzle_recovery',{}),**old.get('nozzle_owners',{})}),clear_attestations={})
            state['recovery_resolutions']=copy.deepcopy(old.get('recovery_resolutions',[]))
        # Read predecessor and preserve its recovery evidence within ONE writer
        # transaction, not two separately locked startup snapshots.
        super().__init__(routes,journal=journal,simulation=False,recovery_merge=merge,**kwargs)

    def _clear_attestation_valid(self,state,head):
        record=(state.get('clear_attestations') or {}).get(str(head))
        if not isinstance(record,dict):return False
        try:
            if record.get('head')!=head:return False
            recorded=tuple(tuple(source) for source in (record.get('sources') or ()))
            if recorded!=tuple(tuple(source) for source in self.routes.sources(head)):return False
            return self.provider.attestation_valid(head,record)
        except (EvidenceError,TypeError,ValueError,KeyError):return False

    def _feed_path_allowed(self,state,head):
        # A normal load needs a usable route, not a memorialized inspection.
        # Clear and unknown are both executable; only a known occupied or
        # in-progress path is a technical conflict. Operators can record a
        # clearance inspection when they want extra confidence, but consuming
        # that optional record must never be a precondition for loading.
        path=state['paths'][str(head)]
        return path.get('state') in ('clear','unknown')

    def _legacy_pre_transport(self,record):
        if record.get('transport_started') not in (None,False) or record.get('physical_started') not in (None,False):
            return False
        if record.get('arrival_checkpoint') or record.get('operation') not in ('feed','retract'):
            return False
        if len(record.get('sources') or ())!=1:
            return False
        reason=str(record.get('reason') or '')
        return any(reason.startswith(prefix) for prefix in _LEGACY_PRE_TRANSPORT_REASONS)

    def _retire_legacy_pre_transport(self,state,head):
        """Archive only provably pre-transport legacy software failures.

        The old workflow quarantined every failure, including a failed tool
        selection or heat phase before feed_path. Those records cannot be
        resumed because they have no arrival checkpoint, and they should not
        require a physical inspection of a path that was never fed. New records
        carry explicit boundaries and are never retired here.
        """
        if state.get('operations') or state.get('printing'):
            return
        path=state.get('paths',{}).get(str(head),{})
        if path.get('state') in ('occupied','in_progress'):
            return
        retired=[]
        for record in list(state.get('recovery_required') or ()):
            if record.get('head')!=head or not self._legacy_pre_transport(record):
                continue
            state['recovery_required'].remove(record)
            event={'event':'recovery_resolved','head':head,'token':record.get('token'),
                   'disposition':'legacy_pre_transport_software_failure'}
            state.setdefault('recovery_resolutions',[]).append(dict(event,failure=copy.deepcopy(record)))
            state['history']=(state.get('history',[])+[event])[-40:]
            retired.append(record.get('token'))
        return retired

    def begin_pathchek(self, source, sample, normalized_sensors, job):
        """Native executor's explicit physical-clear start, no motion grant.

        One atomic coordinator transaction reserves ALL filament operations and
        persists the job before any command. Source IDs still come from the
        bound provider, not browser telemetry or a saved Boolean.
        """
        if self.journal is None:raise InterlockError('Native PathChek requires durable journal ownership')
        source=tuple(source);head=self.routes.require_available(*source)
        live=self.provider.read();device=self.provider.device(live,source[0])
        if live.get('idle') is not True or sample.get('epoch')!=live['epoch'] or device['identity']!=job['identity']:
            raise InterlockError('Native PathChek live identity or idle state changed')
        if set(normalized_sensors)!=set(range(4)) or any(v is not False for v in normalized_sensors.values()):
            raise InterlockError('All four qualified sensors must start clear')
        token=job['token']
        def change(state):
            if (state['operations'] or state['printing'] or state.get('recovery_required')
                or state.get('nozzle_owners') or state.get('nozzle_recovery') or state.get('confirmed_sources')):
                raise InterlockError('Existing operation, owner or recovery blocks exclusive PathChek')
            state['operations'][token]={'ace':source[0],'slot':source[1],'head':head,
                'identity':device['identity'],'exclusive':True,'background':False,'operation':'pathchek'}
            state['paths'][str(head)]={'state':'in_progress','owner':list(source),'observed':None}
            state.setdefault('clear_attestations',{}).pop(str(head),None)
            state['pathchek_job']=copy.deepcopy(job)
        self._change(change)

    def reserve_engineering_feed(self, job):
        if self.journal is None:
            raise InterlockError('Engineering feed requires a durable journal')
        live = self.provider.read()
        device = self.provider.device(live, 0)
        if (live.get('idle') is not True or live['epoch'] != job['epoch']
                or device['identity'] != job['identity']):
            raise InterlockError('Engineering source or printer readiness changed')
        token = job['token']
        def change(state):
            if any(state.get(key) for key in ('operations', 'printing', 'recovery_required',
                                              'nozzle_owners', 'nozzle_recovery', 'confirmed_sources')):
                raise InterlockError('Engineering feed requires exclusive resources')
            state['operations'][token] = dict(ace=0, slot=0, head=0, identity=device['identity'],
                                              exclusive=True, background=False, operation='engineering_feed')
            state['paths']['0'] = dict(state='in_progress', owner=[0, 0], observed=None)
            state.setdefault('clear_attestations', {}).pop('0', None)
            state['engineering_feed'] = copy.deepcopy(job)
        self._change(change)

    def begin_compound(self,sources,head,operation,recovery_token=None):
        sources=list(dict.fromkeys(tuple(s) for s in sources))
        if not sources or operation not in ('feed','retract'):raise InterlockError('Invalid compound operation')
        for source in sources:
            self.routes.require_destination(*source,head)
            if operation=='feed' or source!=sources[0]:self.routes.require_available(*source)
        import uuid
        token=uuid.uuid4().hex;aces=sorted({a for a,_ in sources})
        if operation=='feed' and recovery_token is None:self._require_feed_sensor(head)
        def change(state):
            if recovery_token is not None:
                records=self._recovery_records(state,head,recovery_token)
                if (operation!='feed' or len(sources)!=1 or len(self.routes.sources(head))!=1
                        or len(records)!=1 or not records[0].get('arrival_checkpoint')):
                    raise InterlockError('No exclusive stopped-arrival recovery checkpoint')
            if any(op.get('exclusive') for op in state['operations'].values()):raise InterlockError('PathChek owns exclusive filament resources')
            if operation=='feed' and recovery_token is None:
                self._retire_legacy_pre_transport(state,head)
            blocking=[r for r in state.get('recovery_required',[]) if r['token']!=recovery_token and
                      (r['head']==head or set(r.get('aces',[r['ace']]))&set(aces))]
            if blocking:
                record=blocking[0];source=record.get('source') or [record.get('ace'),record.get('slot')]
                raise InterlockError('Shared workflow requires explicit recovery for ACE%s slot%s -> T%s (%s)' %
                                     (source[0],source[1],record.get('head'),record.get('reason') or 'interrupted operation'))
            for ace in aces:self._fresh_device(state,ace)
            if any(op['head']==head or set(op.get('aces',[op['ace']]))&set(aces) for op in state['operations'].values()):raise InterlockError('Compound route resource is reserved')
            if any(h['head']==head or int(a) in aces for a,h in state['printing'].items()):raise InterlockError('Printing resources must be paused/released first')
            path=state['paths'][str(head)]
            if operation=='feed' and recovery_token is None:
                if not self._feed_path_allowed(state,head):
                    raise InterlockError('Filament path is known occupied; unload or recover it before loading another source')
            if operation=='retract' and (path['state']!='occupied' or path['owner']!=list(sources[0])):
                witness=state.get('runout_witness',{}).get(str(head));owner=state.get('nozzle_owners',{}).get(head) or state.get('nozzle_owners',{}).get(str(head))
                if (not witness or not owner or witness['source']!=list(sources[0]) or witness['receipt']!=owner['receipt'] or not 0<=self.clock()-witness['observed']<=self.max_age):
                    raise InterlockError('Unknown source cannot be retracted automatically')
            ace,slot=sources[0]
            state['operations'][token]={'ace':ace,'slot':slot,'head':head,'aces':aces,'sources':[list(s) for s in sources],
                                      'identity':state['devices'][str(ace)]['identity'],'identities':{str(a):state['devices'][str(a)]['identity'] for a in aces},'operation':operation,'background':False,
                                      'physical_started':False,'transport_started':False}
            if recovery_token is not None:state['operations'][token]['resumes_recovery']=recovery_token
            state['paths'][str(head)]={'state':'in_progress','owner':list(sources[0]),'observed':None}
            state.setdefault('clear_attestations',{}).pop(str(head),None)
            return {'token':token,'head':head,'execution_enabled':False}
        return self._change(change)

    def record_runout_witness(self,head,source,receipt):
        s=self.provider.read();self.provider.sensor(s,head,False)
        self.provider.device(s,source[0])
        def change(state):
            owner=state.get('nozzle_owners',{}).get(head) or state.get('nozzle_owners',{}).get(str(head))
            if not owner or owner['source']!=list(source) or owner['receipt']!=receipt:raise InterlockError('Runout lacks prior nozzle-owner receipt')
            state.setdefault('runout_witness',{})[str(head)]={'source':list(source),'receipt':receipt,'observed':self.clock()}
        self._change(change)

    def reconcile(self,head=None):
        o=self.provider.observations()
        if o['routes_sha256']!=self.fingerprint:raise InterlockError('Evidence route fingerprint changed')
        def change(state):
            if head is None and (state['operations'] or state['printing']):
                raise InterlockError('Cannot reconcile reserved paths')
            if head is not None:
                aces={a for a,_ in self.routes.sources(head)}
                if any(op.get('exclusive') or op['head']==head or aces.intersection(op.get('aces',[op['ace']])) for op in state['operations'].values()) or any(
                        hold['head']==head or int(a) in aces for a,hold in state['printing'].items()):
                    raise InterlockError('Cannot reconcile an affected reserved path')
            state['devices']=copy.deepcopy(o['devices'])
            for h,p in o['paths'].items():
                if head is not None and h!=str(head):continue
                blocked=any(r['head']==int(h) for r in state.get('recovery_required',[]))
                state['paths'][h]=({'state':'unknown','owner':None,'observed':None} if blocked else copy.deepcopy(p))
                if state['paths'][h].get('state')!='clear':state.setdefault('clear_attestations',{}).pop(str(h),None)
        self._change(change)

    def _require_feed_sensor(self,head):
        try:sensor=self.provider.read().get('heads',{}).get(head,{})
        except Exception as exc:raise InterlockError('Live toolhead sensor evidence unavailable: %s'%exc)
        observed=sensor.get('observed')
        fresh=(type(observed) in (int,float) and 0<=self.clock()-observed<=self.max_age)
        if sensor.get('enabled') is not True or type(sensor.get('detected')) is not bool or not fresh:
            raise InterlockError('Toolhead sensor evidence is missing or stale; restore sensor communication before loading')
        if sensor.get('detected') is True:
            raise InterlockError('Toolhead sensor already detects filament; unload or recover this path before loading another source')

    def begin(self,ace,slot,operation,background=False,exclusive=False):
        head=self.routes.destination(ace,slot)
        with self._lock:
            if any(ace in r.get('aces',[r['ace']]) or r['head']==head for r in self.snapshot().get('recovery_required',[])):
                raise InterlockError('Interrupted or unconfirmed stop requires explicit recovery')
            if operation=='feed':self._require_feed_sensor(head)
            return super().begin(ace,slot,operation,background,exclusive=exclusive)

    def quarantine(self,token,reason):
        def change(state):
            op=state['operations'].get(token)
            if op is None:raise InterlockError('Unknown operation for quarantine')
            record=dict(op,reason=reason)
            if not any(r.get('token')==token for r in state.get('recovery_required',[])):
                state.setdefault('recovery_required',[]).append(dict(record,token=token))
            state['paths'][str(op['head'])]={'state':'unknown','owner':None,'observed':None}
            state.setdefault('clear_attestations',{}).pop(str(op['head']),None)
        self._change(change)

    def complete_full_workflow(self,token,owners,completion):
        def change(state):
            if token not in state['operations']:raise InterlockError('Full workflow token is stale')
            op=state['operations'].pop(token)
            state['paths'][str(op['head'])]={'state':'unknown','owner':None,'observed':None}
            original=op.get('resumes_recovery')
            if original:
                resolved=[r for r in state.get('recovery_required',[]) if r['head']==op['head']]
                if len(resolved)!=1 or resolved[0]['token']!=original:raise InterlockError('Original recovery checkpoint changed')
                self._resolve_recovery(state,op['head'],resolved,'resumed_load',
                                       {'devices':state['devices'],'paths':state['paths']})
            state['nozzle_owners']=copy.deepcopy(owners);state['full_workflow']=None
            state.setdefault('clear_attestations',{}).pop(str(op['head']),None)
            state.setdefault('runout_witness',{}).pop(str(op['head']),None)
            state['history']=(state['history']+[{'event':'full_'+completion,'token':token,'head':op['head']}])[-40:]
        self._change(change)

    def complete_sensor_operation(self,token,operation,source_identity):
        def change(state):
            if token not in state['operations']:raise InterlockError('Completion token is stale')
            op=state['operations'].pop(token);head=op['head']
            state['paths'][str(head)]={'state':'unknown','owner':None,'observed':None}
            if operation=='load':
                state.setdefault('confirmed_sources',{})[str(head)]={
                    'ace':op['ace'],'slot':op['slot'],'receipt':token,
                    'source_identity':source_identity,'routes_sha256':self.fingerprint,'stage':'toolhead_sensor'}
            else:
                state.setdefault('confirmed_sources',{}).pop(str(head),None)
                state.setdefault('recovery_required',[]).append(dict(op,token=token,reason='sensor-only unload; unsensed tubing must be reconciled'))
            state.setdefault('clear_attestations',{}).pop(str(head),None)
            state['history']=(state['history']+[{'event':'sensor_'+operation+'_complete','token':token,'head':head}])[-40:]
        self._change(change)

    def _recovery_records(self,state,head,expected_token):
        if state['operations'] or state['printing']:
            raise InterlockError('Stop active filament operations before recovery')
        records=[r for r in state.get('recovery_required',[]) if r['head']==head]
        owner=state.get('nozzle_recovery',{}).get(str(head),{})
        current_token=records[-1]['token'] if records else owner.get('receipt')
        if expected_token is not None and current_token!=expected_token:
            raise InterlockError('The recovery record changed. Refresh before confirming the inspection.')
        return records

    def _resolve_recovery(self,state,head,records,disposition,observations):
        state['recovery_required']=[r for r in state.get('recovery_required',[]) if r['head']!=head]
        for record in records:
            event={'event':'recovery_resolved','head':head,'token':record['token'],'disposition':disposition}
            state.setdefault('recovery_resolutions',[]).append(dict(event,failure=copy.deepcopy(record)))
            state['history']=(state['history']+[event])[-40:]
        state['devices']=copy.deepcopy(observations['devices'])
        state['paths'][str(head)]=copy.deepcopy(observations['paths'][str(head)])

    def acknowledge_loaded_recovery(self,head,source,confirmation,expected_token=None,verify_nozzle=None,rebind=False):
        with self._lock:
            try:
                if confirmation!='SOURCE_TRACED_AND_STOPPED':raise InterlockError('Explicit physically traced source and stopped-device confirmation required')
                state=self.snapshot()
                records=self._recovery_records(state,head,expected_token)
                old=state.get('nozzle_recovery',{}).get(str(head)) or state.get('nozzle_recovery',{}).get(head)
                record=state.get('confirmed_sources',{}).get(str(head))
                if old and (verify_nozzle is not None or not record):
                    record={'ace':old['source'][0],'slot':old['source'][1],'receipt':old['receipt'],'routes_sha256':old['routes_sha256']}
                if not record or not record.get('receipt') or (record['ace'],record['slot'])!=tuple(source) or record['routes_sha256']!=self.fingerprint:
                    raise InterlockError('No matching durable sensor-load receipt exists')
                sample=self.provider.read()
                if sample.get('idle') is not True:raise InterlockError('Printer must be idle')
                self.provider.sensor(sample,head,True)
                device=self.provider.device(sample,source[0])
                owner=None
                if verify_nozzle is not None:
                    if (not old or old.get('nozzle_verified') is not True or (old.get('device_identity')!=device['identity'] and not rebind)
                        or any(r['token']!=old['receipt'] for r in records)):
                        raise InterlockError('No matching verified nozzle load for this ACE connection')
                    owner=copy.deepcopy(old)
                    updated_identity=verify_nozzle(old)
                    if rebind:
                        if not isinstance(updated_identity,str) or not updated_identity:
                            raise InterlockError('Current spool identity is required for inspected restoration')
                        owner.update(device_identity=device['identity'],identity=updated_identity)
                self.provider.complete_load(head,source,'operator-traced:'+record['receipt'])
                observations=self.provider.observations()
                path=observations['paths'][str(head)]
                if path['state']!='occupied' or path['owner']!=list(source):
                    raise InterlockError('Filament presence or source changed during recovery')
                def change(s):
                    self._recovery_records(s,head,expected_token)
                    self._resolve_recovery(s,head,records,'loaded',observations)
                    if owner is not None:
                        s.setdefault('nozzle_owners',{})[str(head)]=owner
                        if not records:
                            event={'event':'recovery_resolved','head':head,'token':owner['receipt'],'disposition':'loaded'}
                            s.setdefault('recovery_resolutions',[]).append(dict(event,prior_nozzle=copy.deepcopy(old),operator_rebind=rebind))
                            s['history']=(s['history']+[event])[-40:]
                        s.get('nozzle_recovery',{}).pop(str(head),None)
                        s.get('nozzle_recovery',{}).pop(head,None)
                    if (s.get('full_workflow') or {}).get('head')==head:s['full_workflow']=None
                    s.setdefault('clear_attestations',{}).pop(str(head),None)
                self._change(change)
                return owner
            except Exception:
                self.provider.invalidate(head)
                raise

    def retire_isolated_stop(self,head,source,token,kind):
        # The operator attests physical isolation, not firmware stop or clearance.
        # Do not produce positive path evidence when the transport was lost.
        sample=self.provider.read()
        if sample.get('idle') is not True:raise InterlockError('Cancel the print and wait for idle before isolating a failed stop')
        self.provider.sensor(sample,head,False)
        self.routes.require_destination(*source,head)
        self.provider.invalidate(head)
        def change(state):
            if kind=='operation':
                owned=state['operations'].get(token)
                if not owned or owned['head']!=head or [owned['ace'],owned['slot']]!=list(source):
                    raise InterlockError('The failed operation no longer owns this route')
                if owned.get('exclusive'):raise InterlockError('Use the recovery procedure for the exclusive test')
                state['operations'].pop(token)
            else:
                hold=state['printing'].get(str(source[0]))
                if not hold or hold['head']!=head or hold['slot']!=source[1]:
                    raise InterlockError('The failed print hold no longer owns this route')
                owned=state['printing'].pop(str(source[0]))
            records=[r for r in state.get('recovery_required',[]) if r['head']==head]
            if not any(r['token']==token for r in records):
                records.append(dict(owned,head=head,ace=source[0],slot=source[1],token=token,reason='Stop was not confirmed by the transport'))
            for record in records:
                event=dict(event='recovery_resolved',head=head,token=record['token'],disposition='operator_isolated',sensor_clear=True)
                state.setdefault('recovery_resolutions',[]).append(dict(event,failure=copy.deepcopy(record)))
                state['history']=(state['history']+[event])[-40:]
            state['recovery_required']=[r for r in state.get('recovery_required',[]) if r['head']!=head]
            state['paths'][str(head)]=dict(state='unknown',owner=None,observed=None)
            state.get('confirmed_sources',{}).pop(str(head),None)
            for name in ('nozzle_owners','nozzle_recovery'):
                state.get(name,{}).pop(str(head),None);state.get(name,{}).pop(head,None)
            if (state.get('full_workflow') or {}).get('head')==head:state['full_workflow']=None
        self._change(change)

    def acknowledge_clear_recovery(self,head,confirmation,expected_token=None):
        with self._lock:
            try:
                records=self._recovery_records(self.snapshot(),head,expected_token)
                self.provider.attest_clear(head,self.routes.sources(head),confirmation)
                observations=self.provider.observations()
                if observations['paths'][str(head)]['state']!='clear':
                    raise InterlockError('Clearance evidence changed during recovery')
                def change(state):
                    self._recovery_records(state,head,expected_token)
                    self._resolve_recovery(state,head,records,'operator_attested_clear',observations)
                    state.setdefault('clear_attestations',{})[str(head)]={
                        'head':head,'sources':[list(s) for s in self.routes.sources(head)],
                        'confirmation':confirmation,'routes_sha256':observations['routes_sha256'],
                        'session':observations['session'],'epoch':observations['epoch'],
                        'observed':observations['paths'][str(head)]['observed'],'at':self.clock(),
                        'topology':self.routes.topology.as_dict()}
                    state.setdefault('confirmed_sources',{}).pop(str(head),None)
                    for key in ('nozzle_owners','nozzle_recovery'):
                        state.setdefault(key,{}).pop(str(head),None);state[key].pop(head,None)
                    if (state.get('full_workflow') or {}).get('head')==head:state['full_workflow']=None
                self._change(change)
            except Exception:
                self.provider.invalidate(head)
                raise

class DisabledTransport:
    simulation_only=False
    def start(self,*args,**kwargs):raise ExecutionError('Production filament dispatch is disabled')
    def poll(self,*args,**kwargs):raise ExecutionError('Production filament dispatch is disabled')
    def stop(self,*args,**kwargs):raise ExecutionError('Production filament dispatch is disabled')

class Executor:
    """One bounded operation per executor; coordinator arbitrates other users.

    Transport contract: start receives exact route/token and HARD distance/time
    limits; poll is nonblocking; stop returns True only on positive stopped
    acknowledgment. A successful load requires a fresh physical sensor TRUE;
    unload requires FALSE but NEVER establishes clearance of unsensed tubing.
    """
    def __init__(self,coordinator,transport=None,simulation=False,authorization=None):
        if type(simulation) is not bool:raise ExecutionError('Invalid simulation flag')
        self.coordinator=coordinator;self.provider=coordinator.provider
        self.transport=transport or DisabledTransport();self.simulation=simulation;self.authorization=authorization
        self._lock=threading.RLock();self.job=None;self.last_result=None
        if simulation and getattr(self.transport,'simulation_only',False) is not True:
            raise ExecutionError('Simulation requires a simulation-only transport')

    def _limits(self,max_distance_mm,timeout_s):
        for v,limit in [(max_distance_mm,3000),(timeout_s,1200)]:
            if type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=limit:
                raise ExecutionError('Explicit finite operation limits are required')

    def start(self,operation,ace,slot,head,material,max_distance_mm,timeout_s):
        with self._lock:
            if not self.simulation:
                if (self.authorization is None or self.coordinator.journal is None
                        or not self.authorization.check_request(operation,ace,slot,head,material,max_distance_mm,timeout_s)):
                    raise ExecutionError('Production filament dispatch remains disabled without explicit single-use test authorization')
            if self.job is not None:raise ExecutionError('An operation is already active')
            if operation not in ('load','unload'):raise ExecutionError('Only explicit load/unload is supported; no switching or failover')
            self.coordinator.routes.require_destination(ace,slot,head)
            self._limits(max_distance_mm,timeout_s)
            # First-print policy: ordinary PLA only, no material-override bypass.
            if not isinstance(material,str) or material.strip().upper()!='PLA':raise ExecutionError('Controlled executor accepts ordinary PLA only')
            s=self.provider.read()
            if s.get('idle') is not True:raise ExecutionError('Printer must be idle')
            d=self.provider.device(s,ace)
            if operation=='load' and d.get('slots',{}).get(slot) is not True:raise ExecutionError('Explicitly selected source is unavailable')
            if d.get('materials',{}).get(slot)!='PLA':raise ExecutionError('Trusted source metadata must independently identify PLA')
            # Occupied source metadata does not prove safe temperature/homing.
            if s.get('motion_ready_heads',{}).get(head,s.get('motion_ready')) is not True:raise ExecutionError('Homing, thermal and motion prerequisites are not satisfied')
            self.coordinator.reconcile()
            op=self.coordinator.begin(ace,slot,'feed' if operation=='load' else 'retract')
            token=op['token'];now=self.coordinator.clock()
            self.job={'token':token,'operation':operation,'ace':ace,'slot':slot,'head':head,
                      'identity':d['identity'],'source_identity':d.get('source_identities',{}).get(slot),'binding':self.provider._binding(s,head),'source_binding':self.provider.source_binding(s,head),'epoch':s['epoch'],'started':now,'deadline':now+timeout_s,
                      'max_distance_mm':max_distance_mm,'distance_mm':0.0,'phase':'starting','dispatch_started':False}
            # Consume manual clearance BEFORE issuing any transport request.
            self.provider.invalidate(head)
            try:
                # The durable reservation is written before a dispatch that
                # could partially succeed or lose its acknowledgment.
                # Re-sample after durable reservation and before dispatch.
                # A sensor/device change in that interval must block the request.
                latest=self.provider.read()
                if (latest['epoch']!=self.job['epoch'] or latest.get('idle') is not True
                        or latest.get('motion_ready_heads',{}).get(head,latest.get('motion_ready')) is not True
                        or self.provider._binding(latest,head)!=self.job['binding']):
                    raise ExecutionError('Pre-dispatch evidence changed')
                self.provider.sensor(latest,head,operation=='unload')
                if operation=='load' and self.provider.device(latest,ace).get('slots',{}).get(slot) is not True:
                    raise ExecutionError('Selected source became unavailable')
                if not self.simulation:self.authorization.bind(self.job)
                self.job['dispatch_started']=True
                self.transport.start(copy.deepcopy(self.job),max_distance_mm,timeout_s)
                self.job['phase']='running'
            except Exception as exc:
                self._abort('start failed: '+str(exc),'failed')
                raise ExecutionError('Transport start failed; recovery state preserved') from exc
            return copy.deepcopy(self.job)

    def _abort(self,reason,outcome):
        j=self.job;j['_abort_outcome']=outcome;self.provider.invalidate(j['head'])
        try:stopped=not j['dispatch_started'] or self.transport.stop(j['token']) is True
        except Exception:stopped=False
        if j['dispatch_started'] or not stopped:
            try:
                if not any(r.get('token')==j['token'] for r in self.coordinator.snapshot().get('recovery_required',[])):
                    self.coordinator.quarantine(j['token'],reason+'; physical path reconciliation required')
            except Exception as exc:
                if stopped:
                    j['phase']='persistence_fault';j['error']=str(exc)
                    self.last_result={'outcome':'persistence_fault','reason':str(exc),'head':j['head']}
                    return copy.deepcopy(self.last_result)
                reason+='; recovery journal unavailable: '+str(exc)
        if not stopped:
            j['phase']='stop_unconfirmed';j['error']=reason
            self.last_result={'outcome':'stop_unconfirmed','reason':reason,'head':j['head']}
            return copy.deepcopy(self.last_result)
        try:self.coordinator.finish(j['token'],outcome)
        except Exception as exc:
            j['phase']='persistence_fault';j['error']=str(exc)
            self.last_result={'outcome':'persistence_fault','reason':reason+'; '+str(exc),'head':j['head']}
            return copy.deepcopy(self.last_result)
        # If this operation was previously quarantined, a positive stop alone
        # does not remove its recovery-required record or infer path clearance.
        self.last_result={'outcome':outcome,'reason':reason,'head':j['head'],'path_state':'unknown'}
        self.job=None
        return copy.deepcopy(self.last_result)

    def cancel(self):
        with self._lock:
            if self.job is None:raise ExecutionError('No active operation')
            return self._abort('operator cancellation','cancelled')

    def tick(self):
        with self._lock:
            if self.job is None:raise ExecutionError('No active operation')
            j=self.job
            if j['phase']=='stop_unconfirmed':return self._abort(j['error'],j.get('_abort_outcome','failed'))
            if j['phase']=='persistence_fault':return copy.deepcopy(self.last_result)
            try:
                now=self.coordinator.clock()
                if not math.isfinite(now) or now<j['started'] or now>=j['deadline']:raise ExecutionError('Operation timed out or clock regressed')
                s=self.provider.read();d=self.provider.device(s,j['ace'],ready=False)
                if d['identity']!=j['identity'] or s['epoch']!=j['epoch']:raise ExecutionError('Device identity or host epoch changed')
                if self.provider._binding(s,j['head'],ready=False)!=j['binding']:raise ExecutionError('Shared-path device identity changed')
                if self.provider.source_binding(s,j['head'])!=j['source_binding']:raise ExecutionError('Shared-path bay identity/generation changed')
                if j.get('source_identity') is not None and d.get('source_identities',{}).get(j['slot'])!=j['source_identity']:raise ExecutionError('Selected spool/bay identity changed')
                for peer,_ in j['binding']:
                    if peer!=j['ace']:self.provider.device(s,peer)
                h=s.get('heads',{}).get(j['head'],{})
                if h.get('enabled') is not True or type(h.get('detected')) is not bool or not self.provider._fresh(h.get('observed')):
                    raise ExecutionError('Physical sensor evidence lost')
                if s.get('idle') is not True or s.get('motion_ready_heads',{}).get(j['head'],s.get('motion_ready')) is not True:raise ExecutionError('Motion prerequisite lost')
                r=self.transport.poll(j['token'])
                if r.get('token')!=j['token'] or r.get('error'):raise ExecutionError('Transport token mismatch or operation error')
                distance=r.get('distance_mm')
                if (type(distance) not in (int,float) or not math.isfinite(distance)
                        or not j['distance_mm']<=distance<=j['max_distance_mm']):raise ExecutionError('Distance limit exceeded or invalid feedback')
                j['distance_mm']=distance
                if r.get('done') is True:
                    if r.get('stopped') is not True or d.get('ready') is not True:raise ExecutionError('Completion lacks positive stopped/ready acknowledgment')
                    self.provider.sensor(s,j['head'],j['operation']=='load')
                    if j['operation']=='load':self.provider.complete_load(j['head'],(j['ace'],j['slot']),j['token'])
                    self.coordinator.complete_sensor_operation(j['token'],j['operation'],j.get('source_identity'))
                    self.last_result={'outcome':'reported_success','operation':j['operation'],'head':j['head'],
                                      'source':[j['ace'],j['slot']],'distance_mm':distance,
                                      'path_state':'occupied' if j['operation']=='load' else 'unknown',
                                      'completion_stage':r.get('completion_stage','transport'),'ready_to_print':False}
                    self.job=None;return copy.deepcopy(self.last_result)
                return {'outcome':'running','distance_mm':distance}
            except Exception as exc:
                return self._abort(str(exc),'failed')

    def status(self):
        active=bool(not self.simulation and self.authorization is not None and self.job is not None and self.authorization.allows(self.job))
        return {'execution_enabled':False,'production_dispatch_enabled':active,
                'simulation':self.simulation,'job':copy.deepcopy(self.job),'last_result':copy.deepcopy(self.last_result),
                'automatic_failover':False,'color_switching':False}
