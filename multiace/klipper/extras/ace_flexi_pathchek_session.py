# SPDX-License-Identifier: GPL-3.0-or-later
"""Native U1 PathChek: durable, operator-stepped ACE V1 execution.

No motion at import, construction, status or qualification recording. Every
segment needs a fresh explicit approval. ACK is never a physical completion.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy, math, threading, time, uuid
from .ace_flexi_production import digest, flexible
from .ace_flexi_pathchek import PathChekError
from .ace_flexi_pathchek_policy import validate

Q_NUMBERS = {'step_mm','speed','max_feed_mm','safe_limit_mm','timeout_s',
 'stop_settle_s','stop_timeout_s','ack_timeout_s','overshoot_mm',
 'retention_min_mm','parking_mm','shared_clear_max_mm','max_sensor_age_s','max_device_age_s'}
Q_FLAGS = {'bounded_motion_qualified','sensors_qualified','stop_qualified','parking_qualified','shared_clearance_qualified'}
Q_FIELDS = Q_NUMBERS | Q_FLAGS | {'context','present_polarity','reference'}
TERMINAL = ('complete','reconciled')

class NativePathChekSession:
    def __init__(self,routes,coordinator,sources,driver,reader,clock=time.monotonic):
        self.routes,self.c,self.sources,self.driver,self.reader=routes,coordinator,sources,driver,reader
        self.clock,self.lock=clock,threading.RLock()
        state=self.c.snapshot();self.qualifications=copy.deepcopy(state.get('pathchek_qualifications',{}))
        self.job=copy.deepcopy(state.get('pathchek_job'))
        self.armed=False;self.client=None;self.lease_until=0.;self.reply={};self.stop_reply={};self.dispatching=None
        self.persistence_error=None
        if self.job and self.job['phase'] not in TERMINAL:
            self.job.update(phase='recovery_required',reason='Interrupted session: physical reconciliation required')
        self.driver.bind_native(self.motor_allowed)

    def _event(self,j,kind,**fields):
        j.setdefault('events',[]).append(dict(kind=kind,at=self.clock(),**fields))
        j['events']=j['events'][-40:]
        j['sequence']=j.get('sequence',0)+1

    def _commit(self,job=None,qualifications=None,release=False):
        if self.c.journal is None:raise PathChekError('A durable journal is required before recording native execution state')
        job=copy.deepcopy(self.job if job is None else job)
        def change(s):
            s['pathchek_job']=job
            if job and job.get('discovery'):
                s.setdefault('pathchek_discoveries',{})['%d:%d'%tuple(job['source'])]=copy.deepcopy(job['discovery'])
            if qualifications is not None:s['pathchek_qualifications']=copy.deepcopy(qualifications)
            if release:
                token=job['token'];s['operations'].pop(token,None)
                s['recovery_required']=[r for r in s.get('recovery_required',[]) if r.get('token')!=token]
                s['paths'][str(job['head'])]={'state':'unknown','owner':None,'observed':None}
                summary=copy.deepcopy(job);summary['events']=summary.get('events',[])[-8:]
                history=s.get('pathchek_results',[])+[summary]
                # Keep each route's newest outcome plus the recent history.
                seen=set()
                kept=[]
                for offset,result in enumerate(reversed(history)):
                    source=tuple(result.get('source',()))
                    if offset<5 or source not in seen:
                        kept.append(result)
                    seen.add(source)
                s['pathchek_results']=list(reversed(kept))
        self.c._change(change)  # Persist BEFORE publishing RAM or any dispatch.
        self._status_memo=None
        self.job=job
        if qualifications is not None:self.qualifications=copy.deepcopy(qualifications)

    def _read(self,source=None,ready=False,sample=None,require_slot=True,require_motion=True):
        s=self.reader() if sample is None else sample;now=self.clock()
        if s.get('idle') is not True or s.get('pathchek_safe',True) is not True or not s.get('epoch'):
            raise PathChekError('Idle Flexi printer and inactive production/calibration required')
        if source is not None:
            d=s.get('devices',{}).get(source[0],{});stamp=d.get('observed')
            if (d.get('connected') is not True or not d.get('identity') or type(stamp)not in (int,float)
                or not math.isfinite(stamp) or not 0<=now-stamp<=getattr(getattr(self.c,'provider',None),'max_age',5.) or (ready and d.get('ready') is not True)
                or (require_slot and ((ready and d.get('slots',{}).get(source[1]) is not True)
                or not d.get('source_identities',{}).get(source[1]))) or s.get('protocols',{}).get(source[0])!='v1'):
                raise PathChekError('Original ACE V1 identity, source, fresh connection and readiness required')
            material=d.get('effective_materials',d.get('materials',{})).get(source[1]);raw=d.get('materials',{}).get(source[1])
            if require_motion:
                if not isinstance(material,str) or not material.strip():raise PathChekError('Known source material metadata required')
                if (flexible(material) or flexible(raw)) and s.get('flexible_permissions',{}).get('%d:%d'%tuple(source))is not True:
                    raise PathChekError('TPU/TPE requires existing explicit scoped load AND unload at-own-risk consent')
        return s

    def context(self,source,s=None):
        s=s or self._read(source);d=s['devices'][source[0]]
        return digest({'routes':self.routes.context(),'source_select':self.sources.context(),'live_source_select':s.get('source_select_context'),
          'source':list(source),'identity':d['identity'],'epoch':s['epoch'],
          'head_source_identities':{'%d:%d'%peer:[s.get('devices',{}).get(peer[0],{}).get('identity'),s.get('devices',{}).get(peer[0],{}).get('source_identities',{}).get(peer[1])] for peer in self.routes.sources(self.routes.destination(*source))},
          'source_identity':d['source_identities'][source[1]],'material':d.get('effective_materials',d.get('materials',{})).get(source[1]),'raw_material':d.get('materials',{}).get(source[1]),
          'head_states':s.get('head_states'),'sensor_configuration':s.get('sensor_configuration'),'ace_config_revision':s.get('ace_config_revision')})

    def _qualification(self,source,s):
        key='%d:%d'%tuple(source);q=self.qualifications.get(key)
        if not q or q.get('context')!=self.context(source,s):
            raise PathChekError('Missing or stale route/device/material/sensor qualification')
        self._fresh_device(s,source,q)
        self._topology_ready(source)
        return q

    def _topology_ready(self,source):
        # A declared joiner must be inspected independently. Sensor arrival
        # cannot prove branches or shared-junction geometry/clearance.
        key='%d:%d'%tuple(source)
        for joiner in self.routes.topology.as_dict()['joiners'].values():
            if key in joiner['branches'] and not joiner['confirmed']:
                raise PathChekError('Declared PTFE joiner requires physical topology confirmation')

    def _fresh_device(self,s,source,q):
        stamp=s['devices'][source[0]].get('observed')
        if type(stamp)not in (int,float) or not math.isfinite(stamp) or not 0<=self.clock()-stamp<=q['max_device_age_s']:
            raise PathChekError('ACE telemetry exceeds the explicitly qualified freshness bound')

    def check_qualification(self,source,record,confirmation):
        if self.job and self.job['phase'] not in TERMINAL:raise PathChekError('Reconcile the existing operation before editing qualifications')
        if confirmation!='RECORD_PHYSICALLY_MEASURED_PATHCHEK_LIMITS':raise PathChekError('Physical measurement approval required')
        self.sources.require_ace(source);s=self._read(source,True);self._topology_ready(source)
        if not isinstance(record,dict) or set(record)!=Q_FIELDS:raise PathChekError('Complete measured PathChek qualification required; no defaults')
        for key in Q_NUMBERS:
            n=record[key]
            if type(n)not in (int,float) or not math.isfinite(n) or n<=0:raise PathChekError('Missing/invalid measured bound: '+key)
        for key in Q_FLAGS:
            if type(record[key])is not bool:raise PathChekError('Qualification flags must be explicit Booleans')
        if (not isinstance(record['present_polarity'],list) or len(record['present_polarity'])!=4 or any(type(v)is not bool for v in record['present_polarity'])
            or not isinstance(record['reference'],str) or not record['reference'].strip() or len(record['reference'])>512
            or record['context']!=self.context(source,s)):
            raise PathChekError('Measured polarity, evidence reference and current context required')
        if (record['step_mm']>5 or record['speed']>20 or record['timeout_s']>600 or record['max_sensor_age_s']>1
            or record['max_device_age_s']>getattr(getattr(self.c,'provider',None),'max_age',5.)
            or record['ack_timeout_s']>10 or record['stop_timeout_s']<=record['stop_settle_s']
            or not record['retention_min_mm']<=record['parking_mm']<=record['shared_clear_max_mm']<record['max_feed_mm']
            or record['max_feed_mm']+record['overshoot_mm']>record['safe_limit_mm']):
            raise PathChekError('Measured motion/stop/retention limits are inconsistent or exceed software ceilings')
        if not all(record[k] for k in ('bounded_motion_qualified','sensors_qualified','stop_qualified','parking_qualified')):
            raise PathChekError('Sensor, stopping and parking qualification required before native movement')
        if len(self.routes.sources(self.routes.destination(*source)))>1 and record['shared_clearance_qualified'] is not True:
            raise PathChekError('Shared-junction clearance remains unqualified')
        self._fresh_device(s,source,record)
        resources=self.c.snapshot()
        if resources['operations'] or resources['printing'] or resources.get('recovery_required'):raise PathChekError('Active resources or recovery block qualification edits')
    def qualify(self,source,record,confirmation):
        self.check_qualification(source,record,confirmation)
        qualifications=copy.deepcopy(self.qualifications);qualifications['%d:%d'%tuple(source)]=copy.deepcopy(record)
        self._commit(qualifications=qualifications)

    def _sensors(self,s,q):
        rows=s.get('diagnostics',{}).get('sensors',[])
        if len(rows)!=4 or {r.get('head') for r in rows}!=set(range(4)):raise PathChekError('All four independent sensors required')
        result={};now=self.clock()
        for row in rows:
            stamp=row.get('observed_at');h=row['head']
            if (row.get('enabled')is not True or row.get('communication')!='ok' or type(row.get('raw_present'))is not bool
                or type(stamp)not in (int,float) or not math.isfinite(stamp) or not 0<=now-stamp<=q['max_sensor_age_s']):
                raise PathChekError('Sensor unknown, stale, disabled or communication lost')
            result[h]=row['raw_present']==q['present_polarity'][h]
        return result

    def _branches(self,head,items):
        expected={tuple(s) for s in self.routes.sources(head)}
        if len(items)!=len(expected) or {tuple(s) for s in items}!=expected:
            raise PathChekError('Physical clearance confirmation must cover EVERY shared branch, including Empty/Unused')

    def start(self,p):
        if self.c.journal is None:raise PathChekError('Durable PathChek journal required before start')
        if self.job and self.job['phase']not in TERMINAL:raise PathChekError('An unreconciled PathChek already owns recovery')
        if p['confirmation']!='START_PATHCHEK_WITH_PHYSICAL_ROUTE_CONFIRMED':raise PathChekError('Route-specific physical tubing confirmation required')
        source=tuple(p['source']);self.sources.require_ace(source);s=self._read(source,True);q=self._qualification(source,s);head=self.routes.destination(*source)
        if p['mode']=='commissioning' and (source!=(0,0) or head!=0 or len(self.routes.sources(head))!=1):
            raise PathChekError('First commissioning route is exclusively ACE0 slot0 to T0')
        material=s['devices'][source[0]].get('effective_materials',s['devices'][source[0]].get('materials',{})).get(source[1],'')
        if p['mode']=='commissioning' and not material.strip().upper().startswith('PLA'):raise PathChekError('First restricted commissioning requires verified ordinary PLA')
        self._branches(head,p['cleared_sources'])
        if any(self._sensors(s,q).values()):raise PathChekError('Every downstream sensor must be physically clear at start')
        low,high=p['initial_min_mm'],p['initial_max_mm']
        if not q['retention_min_mm']<=low<=high<=q['shared_clear_max_mm']:raise PathChekError('Measured initial retained/clear parking interval required')
        token=uuid.uuid4().hex
        j={'token':token,'source':list(source),'head':head,'mode':p['mode'],'phase':'await_feed','sequence':0,
           'context':self.context(source,s),'identity':s['devices'][source[0]]['identity'],'epoch':s['epoch'],
           'sensor_configuration':copy.deepcopy(s.get('sensor_configuration')),'started':self.clock(),'deadline':self.clock()+q['timeout_s'],'position_bounds':[low,high],
           'initial_position_bounds':[low,high],'commanded_feed_bound_mm':0.,'commanded_retract_bound_mm':0.,'arrival_observed':False,
           'qualification':copy.deepcopy(q),'qualification_digest':digest(q),'events':[],'pending':None,'confirmed_by':'operator measurements + qualified sensor/stop envelope'}
        self._event(j,'start',cleared_sources=p['cleared_sources'])
        self.c.begin_pathchek(source,s,self._sensors(s,q),j)  # One durable reservation + job commit.
        self.job=j;self.armed=True;self.client=p['client'];self.lease_until=self.clock()+5

    def _owner(self,p,sequence=True):
        j=self.job
        if not j or p['token']!=j['token']:raise PathChekError('Stale PathChek operation token')
        if p.get('client')!=self.client or not self.armed or self.clock()>=self.lease_until:raise PathChekError('Browser lease expired or client changed; cancel/reconcile required')
        if sequence and p['sequence']!=j['sequence']:raise PathChekError('PathChek state conflict: refresh before retrying')
        return j

    def _observe_destination(self,s,present):
        j=self.job
        if not j.get('pending') or j.get('direction')!='feed' or j['phase'] not in ('moving','stopping','await_stop_witness'):return
        heads=[h for h,value in present.items() if value]
        if not heads:return
        observed=heads[0] if len(heads)==1 else None
        prior=j.get('discovery')
        if prior and prior['observed_heads']==heads:return
        observation={'id':uuid.uuid4().hex,'source':list(j['source']),'configured_head':j['head'],
            'observed_head':observed,'observed_heads':heads,'status':'Conflict' if observed!=j['head'] else 'Unverified',
            'verified':False,'qualified_observation':True,'physically_reconciled':False,
            'observed_at':self.clock(),'observed_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
            'physical_edge_timestamp':None,
            'last_clear_position_bounds':copy.deepcopy(j['position_bounds']),
            'commanded_feed_bound_mm':j['commanded_feed_bound_mm'],
            'source_identity':s['devices'][j['source'][0]]['source_identities'][j['source'][1]],
            'device_identity':j['identity'],'qualification_context':j['context'],'qualification_digest':j['qualification_digest'],
            'route_context':digest(self.routes.context()),'operation_token':j['token'],
            'sensor_evidence':copy.deepcopy(s.get('diagnostics',{})),'qualified_inputs':present,
            'reason':'Ambiguous sensor activations' if observed is None else 'Observed destination differs; stop and reconcile before proposing correction' if observed!=j['head'] else 'Arrival observed; physical stop, position and clearance still required'}
        candidate=copy.deepcopy(j);candidate['discovery']=observation
        self._event(candidate,'destination_observed',heads=heads);self._commit(candidate)

    def _bound(self,ready=False):
        j=self.job;source=tuple(j['source']);self.sources.require_ace(source);s=self._read(source,ready)
        self._fresh_device(s,source,j['qualification'])
        if self.context(source,s)!=j['context']:raise PathChekError('Route/source/device/sensor generation changed')
        if self.clock()>=j['deadline']:raise PathChekError('PathChek operation timed out')
        op=self.c.snapshot()['operations'].get(j['token'])
        if not op or op.get('exclusive')is not True or op.get('identity')!=j['identity'] or (op['ace'],op['slot'])!=source:
            raise PathChekError('Exclusive durable ownership lost')
        present=self._sensors(s,j['qualification'])
        self._observe_destination(s,present)
        wrong=[h for h,v in present.items() if h!=j['head'] and v]
        if wrong:raise PathChekError('Wrong-toolhead sensor activation: expected T%d, asserted %s' % (j['head'],','.join('T%d'%h for h in wrong)))
        return s,present

    def motor_allowed(self,idx,request,token):
        j=self.job
        if self.c.journal is None or type(idx)is not int or type(request.get('params',{}).get('index'))is not int:return False
        if not j or token!=j['token'] or idx!=j['source'][0] or request.get('params',{}).get('index')!=j['source'][1]:return False
        # Only the executor's synchronous dispatch window may use this token.
        if self.dispatching!=request or digest(self.dispatching)!=digest(request):return False
        try:
            braking=request['method'] in ('stop_feed_filament','stop_unwind_filament')
            s=self.driver.live_reader() if braking else self.reader();d=s.get('devices',{}).get(idx,{})
            stamp=d.get('observed')
            if s.get('epoch')!=j['epoch'] or d.get('identity')!=j['identity'] or d.get('connected')is not True or type(stamp)not in (int,float) or not math.isfinite(stamp) or not 0<=self.clock()-stamp<=j['qualification']['max_device_age_s']:return False
            op=self.c.snapshot()['operations'].get(token)
            if not op or op.get('exclusive')is not True or op.get('head')!=j['head'] or op.get('identity')!=j['identity'] or (op.get('ace'),op.get('slot'))!=tuple(j['source']):return False
            if request['method'] in ('stop_feed_filament','stop_unwind_filament'):return True
            self._bound(True)
            return self.armed and self.clock()<self.lease_until and j['phase']=='moving' and j['pending']['request']==request
        except Exception:return False

    def _send(self,request,target):
        self.dispatching=copy.deepcopy(request)
        try:self.driver.dispatch_owned(request,self.job['token'],target)
        finally:self.dispatching=None

    def step(self,p):
        j=self._owner(p);direction=p['direction'];q=j['qualification']
        if p['confirmation']!='APPROVE_ONE_BOUNDED_PATHCHEK_MOVE':raise PathChekError('Approve exactly one bounded movement')
        expected='await_feed' if direction=='feed' else 'await_retract'
        if j['phase']!=expected:raise PathChekError('Movement is not permitted in the current physical state')
        s,present=self._bound(True);low,high=j['position_bounds'];length=p['length']
        if length>q['step_mm']:raise PathChekError('Step exceeds measured route bound')
        if direction=='feed':
            if any(present.values()) or high+length+q['overshoot_mm']>q['max_feed_mm']:raise PathChekError('Feed would exceed safe position bound or sensor is occupied')
        else:
            if not j['arrival_observed'] or low-length-q['overshoot_mm']<q['retention_min_mm']:raise PathChekError('Retract would risk feeder retention')
        if self.clock()+length/q['speed']+q['stop_timeout_s']>=j['deadline']:raise PathChekError('No time for segment and qualified stop')
        request={'method':'feed_filament' if direction=='feed' else 'unwind_filament','params':{'index':j['source'][1],'length':length,'speed':q['speed']}}
        candidate=copy.deepcopy(j);candidate.update(phase='moving',direction=direction,
            pending={'request':request,'sent_at':self.clock(),'segment_deadline':self.clock()+length/q['speed'],'stop_sent_at':None})
        candidate['commanded_'+direction+'_bound_mm']+=length
        self._event(candidate,'approved_segment',direction=direction,length=length)
        self._commit(candidate);self.reply={};self.stop_reply={}
        try:self._send(request,self.reply)
        except Exception as exc:self._fault('Dispatch uncertain: '+str(exc))

    def _stop(self,reason):
        j=self.job
        if not j or not j.get('pending'):return
        pending=j['pending']
        if pending.get('stop_sent_at')is not None:return
        candidate=copy.deepcopy(j);candidate['phase']='stopping';candidate['pending']['stop_sent_at']=self.clock();candidate['stop_reason']=reason
        self._event(candidate,'stop_requested',reason=reason);self._commit(candidate)
        method='stop_feed_filament' if j['direction']=='feed' else 'stop_unwind_filament'
        self.stop_reply={}
        try:self._send({'method':method,'params':{'index':j['source'][1]}},self.stop_reply)
        except Exception as exc:self.stop_reply['error']=str(exc)

    def _fault(self,reason):
        # Stop permission is independent of movement grants. Never auto-retract.
        self.armed=False
        try:self._stop(reason)
        except Exception as exc:
            self.persistence_error='Stop/journal failure: '+str(exc)
            # A failed new journal write must not prevent a still-owned brake.
            if self.job and self.job.get('pending'):
                try:
                    method='stop_feed_filament' if self.job['direction']=='feed' else 'stop_unwind_filament'
                    self._send({'method':method,'params':{'index':self.job['source'][1]}},self.stop_reply)
                except Exception:pass
        candidate=copy.deepcopy(self.job)
        if candidate:
            candidate.update(phase='recovery_required',reason=str(reason))
            reported=[reply['reported_error'] for reply in (self.reply,self.stop_reply) if 'reported_error' in reply]
            if reported:
                candidate['reported_error']=copy.deepcopy(reported[0])
                candidate['transport_errors']=copy.deepcopy(reported)
            observation=candidate.get('discovery') or {}
            heads=observation.get('observed_heads',[])
            code='position_uncertain'
            if len(heads)>1:code='ambiguous_sensor_arrival'
            elif heads and heads[0]!=candidate['head']:code='unexpected_toolhead'
            elif reported:code='transport_rejected'
            elif any(word in str(reason).lower() for word in ('communication','heartbeat','stale','timeout','timed out','unavailable','link')):
                code='observation_or_deadline_lost'
            elif 'progress' in str(reason).lower():code='physical_progress_unverified'
            candidate['feed_diagnostic']={
                'reason':code,'direction':candidate.get('direction'),
                'expected_head':candidate['head'],'observed_heads':heads,
                'commanded_feed_bound_mm':candidate.get('commanded_feed_bound_mm'),
                'recovery_required':True,
            }
            self._event(candidate,'recovery_required',reason=str(reason))
            try:self._commit(candidate)
            except Exception as exc:
                self.job=candidate;self.persistence_error=str(exc)  # Prior durable moving record remains blocking on restart.

    def tick(self):
        with self.lock:
            j=self.job
            if not j or j['phase']in TERMINAL or j['phase']=='recovery_required':return
            try:
                if not self.armed or self.clock()>=self.lease_until:raise PathChekError('Browser heartbeat lost: physical reconciliation required')
                s,present=self._bound();j=self.job;q=j['qualification'];pending=j.get('pending')
                if j['phase']in ('moving','stopping'):
                    if self.reply.get('error'):raise PathChekError(self.reply['error'])
                    if not self.reply.get('accepted') and self.clock()-pending['sent_at']>=q['ack_timeout_s']:raise PathChekError('ACE request acknowledgment timed out')
                    if present[j['head']] and j['direction']=='feed':
                        if not j['arrival_observed']:
                            candidate=copy.deepcopy(j);candidate['arrival_observed']=True;self._event(candidate,'qualified_sensor_arrival',expected_head=j['head'],qualified_inputs=present,poll_sequence=s.get('diagnostics',{}).get('poll_sequence'));self._commit(candidate);j=self.job
                        self._stop('destination arrival')
                    elif j['phase']=='moving' and self.clock()>=pending['segment_deadline']:self._stop('bounded segment elapsed')
                    j=self.job;pending=j['pending']
                    if j['phase']=='stopping':
                        if self.stop_reply.get('error'):raise PathChekError('Stop rejected/unconfirmed: '+self.stop_reply['error'])
                        if self.clock()-pending['stop_sent_at']>=q['stop_timeout_s']:raise PathChekError('Physical stop was not confirmed within measured limit')
                        d=s['devices'][j['source'][0]]
                        if (self.stop_reply.get('accepted') and d.get('ready')is True
                            and d['observed']>=pending['stop_sent_at']+q['stop_settle_s']):
                            candidate=copy.deepcopy(j);candidate['phase']='await_stop_witness';self._event(candidate,'fresh_ready_after_stop_not_physical_proof');self._commit(candidate)
                elif j['phase']=='await_stop_witness' and self.clock()-j['pending']['stop_sent_at']>=q['stop_timeout_s']:
                    raise PathChekError('Physical stop witness timed out')
            except Exception as exc:self._fault(str(exc))

    def confirm_stop(self,p):
        j=self._owner(p)
        if j['phase']!='await_stop_witness' or p['confirmation']!='PHYSICALLY_STOPPED_AND_POSITION_MEASURED':raise PathChekError('Physical stop and measured position witness required')
        s,present=self._bound(True);j=self.job;q=j['qualification'];low,high=p['position_min_mm'],p['position_max_mm']
        if not q['retention_min_mm']<=low<=high<=q['safe_limit_mm']:raise PathChekError('Measured position interval is unsafe/unknown')
        old_low,old_high=j['position_bounds'];length=j['pending']['request']['params']['length']
        if j['direction']=='feed' and (low<=old_high or high>old_high+length+q['overshoot_mm']):raise PathChekError('Measured feed progress absent or outside qualified motion envelope')
        if j['direction']=='retract' and (high>=old_low or low<old_low-length-q['overshoot_mm']):raise PathChekError('Measured retraction progress absent or outside qualified motion envelope')
        if j['arrival_observed'] and j['direction']=='feed' and not present[j['head']]:raise PathChekError('Arrival sensor no longer confirms the expected path')
        if j['arrival_observed'] and high-q['shared_clear_max_mm']>low-q['retention_min_mm']-q['overshoot_mm']:
            raise PathChekError('Measured position uncertainty leaves no safe retained/shared-clear parking move')
        candidate=copy.deepcopy(j);candidate['position_bounds']=[low,high];candidate['pending']=None
        if candidate.get('discovery'):
            witness={'bounds':[low,high],'reference':p['reference'],'observed_at':self.clock()}
            candidate['discovery']['stop_witness']=witness
            candidate['discovery']['arrival_stop_witness' if j['direction']=='feed' else 'parking_stop_witness']=witness
        candidate['parking_retract_window_mm']=[max(0,high-q['shared_clear_max_mm']),max(0,low-q['retention_min_mm']-q['overshoot_mm'])]
        candidate['phase']='await_retract' if j['arrival_observed'] else 'await_feed'
        if j['arrival_observed'] and high<=q['shared_clear_max_mm'] and not any(present.values()):candidate['phase']='await_clearance'
        self._event(candidate,'physical_stop_witness',bounds=[low,high],reference=p['reference']);self._commit(candidate)

    def reconcile(self,p,recovery=False):
        j=self.job
        if not j or p['token']!=j['token']:raise PathChekError('No matching operation to reconcile')
        if not recovery:self._owner(p)
        resources=self.c.snapshot()
        for token,op in resources['operations'].items():
            if token!=j['token'] or op.get('operation')!='pathchek' or op.get('head')!=j['head'] or (op.get('ace'),op.get('slot'))!=tuple(j['source']):
                raise PathChekError('Unrelated or changed ownership blocks physical reconciliation')
        if recovery and j['phase']!='recovery_required':raise PathChekError('Only blocked recovery can be reconciled')
        if not recovery and (j['phase']!='await_clearance' or not j['arrival_observed']):raise PathChekError('Measured parking and arrival must complete first')
        expected='PHYSICALLY_STOPPED_REMOVED_ISOLATED_AND_ALL_BRANCHES_CLEAR' if recovery and p['disposition']=='removed' else 'PHYSICALLY_STOPPED_RETAINED_AND_ALL_BRANCHES_CLEAR'
        if p['confirmation']!=expected:raise PathChekError('Explicit stopped, retained and ALL branch-clear inspection required')
        discovery=j.get('discovery')
        if recovery and discovery and discovery.get('observed_head')!=j['head'] and p['disposition']!='removed':
            raise PathChekError('Conflicting/ambiguous destination requires separately authorized manual removal and isolation')
        observed=discovery.get('observed_head') if discovery else None
        heads={j['head']}|set(discovery.get('observed_heads',[]) if discovery else [])
        required=set().union(*(set(self.routes.sources(h)) for h in heads))
        if len(p['cleared_sources'])!=len(required) or {tuple(x) for x in p['cleared_sources']}!=required:
            raise PathChekError('Recovery clearance must cover configured AND observed destination branches')
        removed=recovery and p['disposition']=='removed'
        s=self._read(tuple(j['source']),True,require_slot=not removed,require_motion=False);q=j['qualification']
        if s.get('sensor_configuration')!=j.get('sensor_configuration'):raise PathChekError('Sensor configuration changed; review physical reconciliation outside this workflow')
        if any(self._sensors(s,q).values()):raise PathChekError('All downstream sensors must be clear; physical branch inspection also required')
        bounds=[p['position_min_mm'],p['position_max_mm']] if recovery else j['position_bounds']
        if removed:
            if bounds!=[0,0]:raise PathChekError('Removed-filament reconciliation requires explicit zero/zero position and physical source isolation')
        elif not q['retention_min_mm']<=bounds[0]<=bounds[1]<=q['shared_clear_max_mm']:raise PathChekError('Physical retained parking bounds must be verified')
        candidate=copy.deepcopy(j);candidate.update(phase='reconciled' if recovery else 'complete',position_bounds=bounds,pending=None,
            physically_reconciled=True,reconciled_current_device=s['devices'][j['source'][0]]['identity'],disposition='removed' if removed else 'parked',operator_reference=p['reference'],production_authorized=False)
        if candidate.get('discovery'):
            discovery=candidate['discovery'];discovery['physically_reconciled']=True;discovery['reconciliation_reference']=p['reference']
            discovery['verified']=not recovery and discovery['observed_head']==j['head'] and bool(discovery.get('arrival_stop_witness')) and bool(discovery.get('parking_stop_witness'))
            discovery['status']='Pass' if discovery['verified'] else discovery['status']
            initial=candidate.get('initial_position_bounds')
            last_clear=discovery.get('last_clear_position_bounds')
            if discovery['verified'] and initial and last_clear:
                arrival=discovery['arrival_stop_witness']['bounds']
                # Position witnesses are independent measurements in the same
                # route reference. Keep the stop overshoot inside the upper
                # bound; the cached sensor does not timestamp the physical edge.
                discovery['travel_calibration']={
                    'kind':'effective_travel_from_retained_start_to_sensor',
                    'interval_mm':[max(0.,last_clear[0]-initial[1]),arrival[1]-initial[0]],
                    'initial_position_bounds_mm':initial,
                    'last_clear_position_bounds_mm':last_clear,
                    'arrival_stop_position_bounds_mm':arrival,
                    'parked_position_bounds_mm':bounds,
                    'commanded_feed_bound_mm':discovery['commanded_feed_bound_mm'],
                    'physical_edge_timestamp':None,
                    'usable_as_motion_limit':False,
                }
            discovery['reason']='Qualified arrival, physical stop/position and all-branch clearance confirmed' if discovery['verified'] else 'Historical observation retained as proposal only; no movement authorization'
        self._event(candidate,'physical_reconciliation',recovery=recovery,cleared_sources=p['cleared_sources']);self._commit(candidate,release=True)
        self.armed=False;self.client=None;self.lease_until=0

    def request(self,p):
        with self.lock:
            validate(p,self.routes);action=p['action']
            if action=='qualify':self.qualify(tuple(p['source']),p['record'],p['confirmation'])
            elif action=='start':self.start(p)
            elif action=='heartbeat':self._owner(p,False);self.lease_until=self.clock()+5
            elif action=='step':self.step(p)
            elif action=='confirm_stop':
                try:self.confirm_stop(p)
                except Exception as exc:
                    if self.job and self.job['phase']=='await_stop_witness' and self.job['token']==p['token'] and self.job['sequence']==p['sequence']:
                        self._fault('Uncertain physical stop/progress: '+str(exc))
                    raise
            elif action=='complete':self.reconcile(p)
            elif action=='recover':self.reconcile(p,True)
            elif action=='cancel':
                if not self.job or p['token']!=self.job['token'] or self.job['phase']in TERMINAL:raise PathChekError('Stale or completed cancel token')
                # Cancellation may revoke a lost client; it can never authorize movement.
                self._fault('Operator cancelled; physical reconciliation required')
            return self.status()

    STATUS_MEMO_S=1.0

    def status(self):
        # One Flexi status build asked for this twice (engine + production
        # service), ~22 ms each. While no job runs, reuse the result for 1 s.
        if self.job is None:
            memo=getattr(self,'_status_memo',None);now=time.monotonic()
            if memo is not None and now-memo[0]<self.STATUS_MEMO_S and memo[1]==self.armed:return memo[2]
            result=self._status()
            if self.job is None:self._status_memo=(now,self.armed,result)
            return result
        self._status_memo=None
        return self._status()

    def _status(self):
        with self.lock:
            try:state=self.c.snapshot()
            except Exception as exc:
                return {'name':'Flexi PathChek','schema':2,'state_unavailable':True,'native_dispatch_enabled':False,
                    'default_execution_enabled':False,'job':copy.deepcopy(self.job),'routes':[],
                    'persistence_error':str(exc),'reason':'Journal unavailable: movement blocked; physical recovery required',
                    'results':[],'qualifications':{},'hardware_qualified':False}
            routes=[]
            try:sample=self._read();sample_error=None
            except Exception as exc:sample=None;sample_error=str(exc)
            for key,head in self.routes.as_dict().items():
                source=tuple(map(int,key.split(':')));row={'key':key,'source':list(source),'head':head,'availability':self.routes.availability.get(*source),'qualified':False,'ready':False,'context':None}
                try:
                    self.sources.require_ace(source)
                    if sample_error:raise PathChekError(sample_error)
                    s=self._read(source,False,sample);row['context']=self.context(source,s);q=self._qualification(source,s);row['qualified']=True
                    present=self._sensors(s,q);d=s['devices'][source[0]];row['ready']=not any(present.values()) and d.get('ready')is True and d.get('slots',{}).get(source[1])is True and not state['operations'] and not state['printing'] and not state.get('recovery_required')
                    row['reason']='Measured record matches; explicit start/step approvals required' if row['ready'] else 'Source, sensors or exclusive resources are not ready; inspect before starting'
                except Exception as exc:row['reason']=str(exc)
                routes.append(row)
            contexts={r['key']:r['context'] for r in routes}
            results=copy.deepcopy(state.get('pathchek_results',[]))
            for result in results:
                key='%d:%d'%tuple(result['source']);q=self.qualifications.get(key)
                result['current_context_matches']=result.get('context')==contexts.get(key) and q is not None and result.get('qualification_digest')==digest(q)
                result['historical_only']=True
            return {'name':'Flexi PathChek','schema':2,'default_execution_enabled':False,'native_dispatch_enabled':self.armed,
                'commissioning_enabled':self.armed,'job':copy.deepcopy(self.job),'routes':routes,
                'qualifications':copy.deepcopy(self.qualifications),'results':results,'discoveries':copy.deepcopy(state.get('pathchek_discoveries',{})),
                'persistence_error':self.persistence_error,'hardware_qualified':False,'production_authorized':False,
                'reason':'Operator-stepped native V1 execution. No movement without current measured qualification, physical routing confirmation, exclusive journal and per-segment approval.'}
