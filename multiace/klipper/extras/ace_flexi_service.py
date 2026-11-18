# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Flexi production integration. All sessions default locked.

Construction never arms motion or contacts hardware. Status/inventory may revoke
volatile safety state but never persist changes. Explicit telemetry observation
and mutation boundaries commit queued invalidations. These are not pure RAM
reads. Only separately approved operator commands may arm or run a workflow.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy,json,math,re,time
from .ace_flexi_production import FullProfiles,RiskConsent,CalibrationBook,Workflow,ProductionError,digest,flexible,movement_settings
from .ace_flexi_adapters import NativeU1Adapter,ACE2ProAdapter,AdapterRegistry
from .ace_flexi_interlocks import AtomicJournal
from .ace_flexi_executor import LiveCoordinator
from .flexi_web_policy import validate as validate_payload,configuration,ATTEST_CONFIRMATION
from .ace_flexi_calibration import CalibrationWizard,validate_payload as validate_wizard
from .ace_flexi_sources import SourceSelect

class ProductionService:
 def __init__(self,ace,routes,raw=None,clock=time.monotonic):
  self.ace=ace;self.routes=routes;self.clock=clock
  self.config=self.validate_config(routes,raw);self.profiles=FullProfiles(routes,self.config['profiles'],defaults=self.standard_profile,mode=self.config['settings_mode'])
  self.source_select=SourceSelect(routes,self.config.get('source_select',{}))
  self.risk=RiskConsent(clock);self.book=CalibrationBook();self.workflow=None;self.registry=AdapterRegistry();self.lease=None;self.runtime_selection={};self.tool_replacements={};self._pending_invalidations=set();self.invalidation_error=None;self.last_plan=None;self._wizard_dispatch=False;self.pending_start=None;self.wizard=CalibrationWizard(routes,clock)
  self.registry.register('original_ace_pro',lambda s:self._adapter(s,NativeU1Adapter),False)
  self.registry.register('ace2_pro',lambda s:self._adapter(s,ACE2ProAdapter),False)
 @staticmethod
 def validate_config(routes,raw):
  if raw is None:raw={}
  if isinstance(raw,str):
   if len(raw)>49152:raise ProductionError('Production configuration is oversized')
   try:raw=json.loads(raw)
   except ValueError as exc:raise ProductionError('Invalid production configuration JSON') from exc
  if not isinstance(raw,dict) or set(raw)-{'profiles','backups','selection','device_adapters','source_select','settings_mode'}:raise ProductionError('Invalid production configuration keys')
  settings_mode=raw.get('settings_mode','standard')
  if settings_mode not in ('standard','custom'):raise ProductionError('Invalid Flexi settings mode')
  configuration(raw,routes)
  profiles=FullProfiles(routes,raw.get('profiles',{}));backups=raw.get('backups',[]);selection=raw.get('selection',{});adapters=raw.get('device_adapters',{})
  if not isinstance(backups,list) or len(backups)>32 or len(set(backups))!=len(backups):raise ProductionError('Backups must be unique source keys')
  for k in backups:
   if not isinstance(k,str) or k not in routes.as_dict():raise ProductionError('Backup does not have a configured route')
  if not isinstance(selection,dict) or set(selection)-set('0123'):raise ProductionError('Invalid physical-head selection')
  for h,k in selection.items():
   if not isinstance(k,str) or routes.as_dict().get(k)!=int(h):raise ProductionError('Selected spool contradicts physical routing')
  if not isinstance(adapters,dict) or any(not isinstance(k,str) or k not in '01234567' or v not in ('original_ace_pro','ace2_pro') for k,v in adapters.items()):raise ProductionError('Invalid device adapter selection')
  result={'profiles':profiles.raw,'backups':list(backups),'selection':dict(selection),'device_adapters':dict(adapters),'settings_mode':settings_mode}
  if 'source_select' in raw:result['source_select']=SourceSelect.validate(raw['source_select'])
  return result
 def standard_profile(self,source,head,action):
  """Values inherited by a route that has no explicit per-route recipe.

  These are the existing MultiACE General/per-ACE/per-slot values, not
  measured Flexi path data. Route and toolhead tables still override them.
  Optional phases are omitted unless a real setting is positive; native
  per-material load/unload temperatures remain authoritative.
  """
  ace,slot=tuple(source)
  def setting(name,getter_args,default=0):
   try:
    fn=getattr(self.ace,name,None)
    value=fn(*getter_args) if callable(fn) else getattr(self.ace,name,default)
    return value
   except Exception:return default
  def positive(value):
   try:return float(value) if math.isfinite(float(value)) and float(value)>0 else None
   except (TypeError,ValueError):return None
  load=positive(setting('get_load_length',(ace,slot),getattr(self.ace,'load_length',0)))
  retract=positive(setting('get_retract_length',(ace,slot),getattr(self.ace,'retract_length',0)))
  feed=positive(setting('get_feed_speed',(ace,),getattr(self.ace,'feed_speed',0)))
  retract_speed=positive(setting('get_retract_speed',(ace,),getattr(self.ace,'retract_speed',0)))
  seat=positive(getattr(self.ace,'seat_overshoot_length',0))
  values={}
  for key,value in (('load_limit_mm',load),('retract_mm',retract),
                    ('feed_speed',feed),('retract_speed',retract_speed),
                    ('seat_mm',seat)):
   if value is not None:values[key]=value
  # Stock load/seat motion speed and a bounded operation deadline. These are
  # operating limits, not measurements of the PTFE path.
  values.update(extrude_speed=3.0,timeout_s=900.0)
  # Presence-only flow check: the toolhead sensor must still report filament.
  values.update(flow_min_delta=0.0,flow_samples=2,flow_interval_s=0.2)
  if action in ('switch','failover'):
   # Use the original MultiACE flush resolver when available so the General
   # purge scale/floor/cap policy applies. A zero still means the stock 80 mm.
   try:
    purge_fn=getattr(self.ace,'get_purge_length',None)
    purge=purge_fn() if callable(purge_fn) else getattr(self.ace,'swap_purge_length',0)
   except Exception:
    purge=getattr(self.ace,'swap_purge_length',0)
   values['purge_mm']=positive(purge) or 80.0
  return values
 def _effective_profiles(self,inventory):
  # What a load/unload will actually use, and where each value came from
  # ('standard' = original MultiACE resolver, 'custom_route'/'custom_head').
  out={}
  for s,d in inventory.items():
   try:
    load=self.profiles.resolve(s,d['head'],'load');unload=self.profiles.resolve(s,d['head'],'unload')
    values=dict(unload['values']);values.update(load['values']);origin=dict(unload['origin']);origin.update(load['origin'])
    out['%d:%d'%s]={'mode':load['mode'],'values':values,'origin':origin}
   except Exception as exc:out['%d:%d'%s]={'mode':self.profiles.mode,'error':str(exc)}
  return out
 @staticmethod
 def slot_presence(protocol,state,fresh):
  if not fresh or protocol not in ('v1','v2'):return None
  if state==('empty1' if protocol=='v1' else 'empty'):return False
  if state in ('ready','loaded','feeding','retracting','assisting',
               'shifting','preload','preloading','rollback','rollback_assisting'):return True
  return None
 @staticmethod
 def route_readiness(source,data,profile,supported,connected,head_clearance,recovery):
  """Describe whether this route can be requested, without hiding it.

  Readiness is intentionally conservative: a complete recipe is not physical
  validation, and live clearance blockers still make the route not ready.
  Configured routes remain present even when setup is incomplete.
  """
  missing=list(profile.get('missing') or [])
  availability=data.get('availability','assigned')
  head=data.get('head')
  if not connected:
   return {'state':'disconnected','reason':'ACE %d is not connected. Reconnect the unit, then refresh before loading.'%source[0],'action':'open_devices','missing':missing}
  if recovery:
   detail='; '.join(str(r.get('reason') or 'interrupted operation') for r in recovery)
   return {'state':'recovery_required','reason':'Resolve the interrupted operation for T%s first: %s. Open recovery, inspect the complete tube, and record what you observed.'%(head,detail),'action':'open_recovery','missing':missing}
  if availability!='assigned':
   return {'state':'unavailable','reason':'This slot is %s. Set its availability to Assigned in Paths before loading.'%availability,'action':'review_source','missing':missing}
  if not supported:
   return {'state':'adapter_unsupported','reason':'This ACE generation is not qualified for automatic loading. Review the device adapter in Config > Devices.','action':'open_devices','missing':missing}
  if missing:
   return {'state':'needs_recipe','reason':'The route uses custom settings with missing values: %s. The route is saved; review or customize its settings, or use the standard MultiACE settings.'%', '.join(missing),'action':'set_up_recipe','missing':missing}
  blockers=list((head_clearance or {}).get('load_blockers') or [])
  if blockers:
   action='open_recovery' if any(('recovery' in b.lower() or 'occupied' in b.lower()) for b in blockers) else 'review_path'
   return {'state':'blocked','reason':' '.join(blockers),'action':action,'missing':missing}
  return {'state':'ready','reason':'Route settings are complete. Starting a load still uses the normal live sensor, path and authorization checks.','action':'open_operations','missing':missing}
 def inventory(self):
  # A single status composition used to rebuild the inventory nine times:
  # once itself and once per adapter_for() call, and each build reads the
  # evidence snapshot, deep-copies the coordinator journal and re-derives a
  # risk context for every source (~27 ms each). The result is a pure read
  # of the state at this instant, so one build can be shared for the whole
  # composition. It is published only inside status(), whose rows are copies,
  # so no caller can mutate the shared mapping.
  memo=getattr(self,'_inventory_memo',None)
  if memo is not None:return memo
  try:s=self.ace._flexi_evidence.read()
  except Exception:s={'devices':{}}
  out={}
  for key,head in self.routes.as_dict().items():
   a,slot=map(int,key.split(':'));info=(getattr(self.ace,'_info_per_ace',{}).get(a) or {});slots=info.get('slots') or [];cell=slots[slot] if slot<len(slots) else {}
   override=self.ace._override_for(a,slot) or {};raw=cell.get('type') or '';material=override.get('material') or raw
   color=override.get('color')
   if not color:
    c=cell.get('color');color=('#'+''.join('%02x'%int(v) for v in c[:3])) if isinstance(c,(list,tuple)) and len(c)>=3 else None
   d=s['devices'].get(a) or {};identity=(d.get('source_identities') or {}).get(slot)
   state=cell.get('status')
   protocol=getattr(getattr(self.ace,'_protocols',{}).get(a),'NAME',None)
   fresh=d.get('connected') is True and self.ace._flexi_evidence._fresh(d.get('observed'))
   present=self.slot_presence(protocol,state,fresh)
   if protocol=='v1' and present is True and identity and d.get('connected') is True and not raw:
    try:
     raw=json.loads(identity)[3] or ''
     material=override.get('material') or raw
    except (ValueError,TypeError,IndexError):pass
   # Expected runout may clear bay RFID metadata. Preserve only the already
   # verified in-process nozzle owner's identity/material on the SAME live
   # connection; never transfer this cache to a newly present spool or reboot.
   owner=self.workflow.owners.get(head) if self.workflow else None
   if (present is False and not raw and not material and owner and tuple(owner['source'])==(a,slot)
       and d.get('connected') is True and owner.get('device_identity')==d.get('identity')):
    material=owner['material'];raw=owner.get('raw_material',material);identity=owner['identity'];color=owner.get('color')
   detected={'v1':'original_ace_pro','v2':'ace2_pro'}.get(protocol,'unvalidated_hardware')
   kind=self.config['device_adapters'].get(str(a),detected)
   if kind!=detected:kind='unvalidated_hardware'
   out[(a,slot)]={'head':head,'present':present,'identity':identity,'material':material or '', 'raw_material':raw,'color':color,'availability':self.routes.availability.get(a,slot),'eligible':self.routes.availability.get(a,slot)=='assigned','approved_backup':key in self.config['backups'] and self.routes.availability.get(a,slot)=='assigned','adapter':kind,'connected':d.get('connected') is True,'generation':protocol or '','generation_label':('Gen2' if protocol=='v2' else 'Gen1' if protocol=='v1' else 'Unknown')}
  stored=self.ace._flexi_coordinator.snapshot()
  persisted={tuple(r["source"]):r for r in stored.get("recipe_calibrations",[])}
  for source,d in out.items():
   self.risk.bind_context(source,self.risk_context(source,d))
   record=self.book.records.get(source) or persisted.get(source)
   partial=[r for r in stored.get('wizard_results',[]) if tuple(r['source'])==source and not r.get('invalidated')]
   partial_changed=bool(partial and any(r.get('context')!=self.calibration_context(source,d['head'],out) for r in partial))
   if partial_changed or (record and (record.get('material')!=[d.get('material'),d.get('raw_material')] or record.get('routes_sha256')!=digest(self.routes.context()) or record.get('adapter')!=d.get('adapter') or record.get('profile') not in {self.profiles.resolve(source,d['head'],'load')[key] for key in ('digest','legacy_digest')})):
    self.book.records.pop(source,None)
    self._pending_invalidations.add(source)
    if self.lease and source in self.lease['sources']:self.lease=None
  return out
 def synchronize_inventory(self):
  """Explicit mutation boundary: durable invalidation, never hardware motion."""
  inventory=self.inventory();pending=set(self._pending_invalidations)
  if not pending:return inventory
  coordinator=self.ace._flexi_coordinator
  try:
   if coordinator.journal:
    def invalidate(state):
     state['recipe_calibrations']=[r for r in state.get('recipe_calibrations',[]) if tuple(r['source']) not in pending]
     for result in state.get('wizard_results',[]):
      if tuple(result['source']) in pending:result['invalidated']='Material or route changed'
    coordinator._change(invalidate)
  except Exception as exc:
   self.lease=None;self.invalidation_error=str(exc)
   raise ProductionError('Calibration invalidation could not be saved: '+str(exc)) from exc
  self._pending_invalidations.difference_update(pending);self.invalidation_error=None
  return inventory
 def risk_context(self,source,data):
  head=self.routes.destination(*source)
  shared={('%d:%d'%s):movement_settings(self.profiles.resolve(s,head,'load')['values']) for s in self.routes.sources(head)}
  return digest({'routes':self.routes.context(),'source':list(source),'material':data.get('material'),'raw_material':data.get('raw_material'),'adapter':data.get('adapter'),'recipes':shared})
 def _adapter(self,source,kind):
  return kind(self.ace,self.ace._flexi_coordinator,self.ace._flexi_thermal,authorize=self.native_authorized,
              material_permission=lambda s,m,p:self.risk.allows(s,self.inventory().get(tuple(s),{}).get('identity'),self.workflow.job['action'] if self.workflow and self.workflow.job else (self.workflow.print_session.get('action','print') if self.workflow and self.workflow.print_session else 'print')),
              calibrations=self.book,clock=self.clock,step_limit=self.calibration_step_limit)
 def adapter_for(self,source):
  inventory=getattr(self,'_inventory_memo',None)
  if inventory is None:inventory=self.inventory()
  return self.registry.get(source,inventory.get(tuple(source),{}).get('adapter','unknown'))
 def _workflow(self):
  if self.workflow is None or self.workflow.c is not self.ace._flexi_coordinator:
   if self.workflow and (self.workflow.job or self.workflow.print_session):raise ProductionError('Cannot replace active production resources')
   self.registry.instances.clear()
   self.workflow=Workflow(self.ace._flexi_coordinator,self.profiles,self.inventory,self.adapter_for,
                          authorize=self.authorized,risk=self.risk,calibrations=self.book,pause=self.pause,print_context=self.print_context)
   for r in self.ace._flexi_coordinator.snapshot().get('recipe_calibrations',[]):
    if isinstance(r,dict) and set(r)>={'source','profile','adapter','actions','material'}:
     self.book.records[tuple(r['source'])]={'profile':r['profile'],'adapter':r['adapter'],'actions':set(r['actions']),'material':r['material'],'routes_sha256':r.get('routes_sha256')}
  return self.workflow
 def print_context(self):
  printer=getattr(self.ace,'printer',None)
  if printer is None:return None,None
  stats=printer.lookup_object('print_stats',None)
  sd=printer.lookup_object('virtual_sdcard',None)
  status=stats.get_status(self.clock()) if stats is not None else {}
  file=getattr(sd,'current_file',None)
  return (file,status.get('filename')) if file is not None and status.get('filename') else None,status.get('state')
 def pause(self,reason):
  if self.print_context()[1]=='printing':self.ace.gcode.run_script_from_command('PAUSE')
 def execution_available(self):
  return getattr(self.ace,'_ace_mode',None)=='flexi' and not getattr(self.ace,'_flexi_init_error',None)
 def recipe_revision(self):
  return digest({'routing': self.routes.context(), 'production': self.config})
 def authorized(self,stage,request):
  if not self.execution_available():return False
  requested=request.get('sources') or [request.get('source')]
  for selected in requested:
   if selected is not None:
    try:
     owner=self.workflow.owners.get(request.get('head')) if self.workflow else None
     owned=bool(owner and tuple(owner['source'])==tuple(selected))
     active=bool(self.workflow and self.workflow.job is request)
     cleanup=request.get('action') in ('unload','switch','failover') and (owned or active) and (tuple(selected)==tuple(request.get('old_source') or ()) or (request.get('action')=='unload' and owned))
     self.source_select.require_ace(tuple(selected),allow_unavailable=cleanup)
    except (ValueError,TypeError):return False
  controlled=(stage=='calibration_test' or request.get('calibration_test') is True
              or request.get('action')=='assist')
  if not controlled:
   if not requested or any(s is None for s in requested):return False
   for s in requested:
    s=tuple(s);action=request.get('action');profile=self.profiles.resolve(s,request['head'],action)
    if not profile['complete'] or not getattr(self.adapter_for(s),'is_supported',lambda:True)():return False
   return True
  lease=self.lease
  if not lease or not lease['issued']<=self.clock()<lease['expires']:return False
  if lease.get('wizard_id'):
   session=self.wizard.session
   if not session or session['stale'] or session['id']!=lease['wizard_id'] or not (self._wizard_dispatch or (session.get('run_token') is not None and request.get('token')==session['run_token'])):return False
  action=request.get('action')
  if action not in lease['actions']:return False
  if lease.get('test') is not True:return False
  sources=request.get('sources') or [request.get('source')]
  return all(s is not None and tuple(s) in lease['sources'] for s in sources)
 def native_authorized(self,token,phase,source,head):
  w=self.workflow
  if w is None:return False
  if phase=='print':
   session=w.print_session
   return bool(session and session['token']==token and session['head']==head and tuple(session['source'])==tuple(source) and self.authorized('print',dict(session,action=session.get('action','print'))) and (not session.get('calibration_test') or self.clock()<session['deadline']))
  j=w.job
  return bool(j and j['token']==token and j['head']==head and j['phases'][j['index']]==(tuple(source),phase) and self.authorized('phase',j))
 def plan(self,action,source,head):
  p=self._workflow().plan(action,tuple(source),head)
  report={'action':action,'source':list(source),'head':head,'phases':[{'source':list(s),'phase':name} for s,name in p['phases']],
          'execution_authorized':self.authorized('start',p),'physical_validation_required':[], 'physical_validation_unrecorded':[list(s) for s in p['sources'] if not self.book.valid(s,p['profiles'][s]['digest'],self.adapter_for(s).kind,action)]}
  self.last_plan=report;return report
 def arm(self,sources,actions,seconds,confirmation,test=False,wizard_id=None):
  if self.workflow and (self.workflow.job or self.workflow.print_session):raise ProductionError('Stop the active operation before replacing authorization')
  if not test:raise ProductionError('Ordinary operations use saved configuration; arming is only for controlled tests')
  payload={'sources':sources,'actions':actions,'seconds':seconds,'confirmation':confirmation,'test':test}
  if wizard_id is not None:payload['wizard_id']=wizard_id
  validate_payload('arm',payload,self.routes)
  if wizard_id is not None:
   session=self.wizard.require(3)
   expected={tuple(session['source'])}
   if session['operation'] in ('switch','failover'):
    owner=self._workflow().owners.get(session['head'])
    if not owner:raise ProductionError('Switch calibration needs a verified loaded source first')
    expected.add(tuple(owner['source']))
   if wizard_id!=session['id'] or {tuple(s) for s in sources}!=expected or len(sources)!=len(expected) or actions!=[session['operation']]:raise ProductionError('Authorization must match every source affected by the wizard operation')
  if confirmation!=('ENABLE_CONTROLLED_FLEXI_TEST' if test else 'ENABLE_VALIDATED_FLEXI_SESSION'):raise ProductionError('Explicit production-session confirmation required')
  if (not sources or not actions or set(actions)-{'load','unload','switch','failover','print','recover','assist'} or type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<seconds<=43200):raise ProductionError('Invalid session scope/lifetime')
  if self.ace._flexi_coordinator.journal is None:raise ProductionError('Explicit persistent runtime preparation is required')
  self._workflow();inventory=self.synchronize_inventory()
  for source in sources:
   source=tuple(source);head=self.routes.destination(*source);a=self.adapter_for(source)
   owner=self.workflow.owners.get(head) if self.workflow else None
   cleanup=bool(owner and tuple(owner['source'])==source and set(actions)<= {'unload','switch','failover'})
   self.source_select.require_ace(source,allow_unavailable=cleanup)
   if not getattr(a,'is_supported',lambda:True)():raise ProductionError('Selected adapter requires hardware validation')
   for action in actions:
    p=self.profiles.resolve(source,head,action)
    if action=='assist' and not test:raise ProductionError('Feed-assist calibration requires a controlled-test session')
    if action=='assist' and not p['values'].get('feed_assist_test_s'):raise ProductionError('An explicit feed-assist observation window is required')
    if not p['complete'] or (not test and not self.book.valid(source,p['digest'],a.kind,action)):raise ProductionError('Each route/adapter/action must be physically validated')
    if test and action=='print':raise ProductionError('Printing requires validated load/unload/feed-assist, not a calibration-test bypass')
  self.lease={'sources':set(tuple(s) for s in sources),'actions':set(actions),'issued':self.clock(),'expires':self.clock()+seconds,'test':test,'wizard_id':wizard_id}
 def prepare_runtime(self,source,action):
  head=self.routes.destination(*source);p=self.profiles.resolve(source,head,action)
  if not p['complete']:raise ProductionError('Unknown route parameters: '+', '.join(p['missing']))
  w=self._workflow()
  if w.job or w.print_session:raise ProductionError('Production runtime is busy')
  if self.ace._flexi_coordinator.journal is None:
   self.ace._flexi_coordinator=LiveCoordinator(self.routes,self.ace._flexi_evidence,journal=AtomicJournal(self.ace._flexi_journal_path),clock=self.clock,max_age=5.)
  self._workflow()
 def validate_recipe(self,source,actions,observations,confirmation):
  if self.workflow and (self.workflow.job or self.workflow.print_session):raise ProductionError('Stop active resources before recording calibration')
  self.synchronize_inventory()
  validate_payload('validate',{'source':list(source),'actions':actions,'observations':observations,'confirmation':confirmation},self.routes)
  if self.ace._flexi_coordinator.journal is None:raise ProductionError('Durable runtime must be prepared before recording validation')
  source=tuple(source);head=self.routes.destination(*source);adapter=self.adapter_for(source)
  for action in actions:
   p=self.profiles.resolve(source,head,action)
   if not p['complete']:raise ProductionError('Cannot validate a recipe with unknown parameters')
  d=self.inventory()[source];previous=copy.deepcopy(self.book.records)
  self.book.approve(source,p['digest'],adapter.kind,actions,observations,confirmation)
  self.book.records[source]['material']=[d.get('material'),d.get('raw_material')];self.book.records[source]['routes_sha256']=digest(self.routes.context())
  # Store calibration data, never restored grants or path clearance.
  if self.ace._flexi_coordinator.journal is None:raise ProductionError('Durable runtime must be prepared before recording validation')
  records=[{'source':list(s),'profile':r['profile'],'adapter':r['adapter'],'actions':sorted(r['actions']),'material':r.get('material'),'routes_sha256':r.get('routes_sha256')} for s,r in self.book.records.items()]
  try:self.ace._flexi_coordinator._change(lambda state:state.update(recipe_calibrations=records))
  except Exception:self.book.records=previous;raise
 def _invalidate_source_head(self,head):
  affected=set(self.routes.sources(head));self.lease=None
  invalidate_evidence=getattr(self.ace._flexi_evidence,'invalidate',None)
  if not callable(invalidate_evidence):raise ProductionError('SourceSelect requires trusted path-evidence invalidation')
  def invalidate(state):
   if state['operations'] or state['printing']:raise ProductionError('Filament resources became busy during SourceSelect update')
   state['paths'][str(head)]={'state':'unknown','owner':None,'observed':None}
   state['recipe_calibrations']=[r for r in state.get('recipe_calibrations',[]) if tuple(r['source']) not in affected]
   for record in state.get('wizard_results',[]):
    if tuple(record['source']) in affected:record['invalidated']='SourceSelect changed'
  self.ace._flexi_coordinator._change(invalidate)
  invalidate_evidence(head)
  for source in affected:self.book.records.pop(source,None)
  self.risk=RiskConsent(self.clock)
  if self.wizard.session:self.wizard.cancel('SourceSelect changed')
 def _source_change_idle(self):
  sample=self.ace._flexi_evidence.read();state=self.ace._flexi_coordinator.snapshot()
  if (sample.get('mode')!='flexi' or sample.get('idle') is not True or state['operations'] or state['printing'] or state.get('recovery_required')
      or (self.workflow and (self.workflow.job or self.workflow.print_session))
      or getattr(self.ace,'_swap_in_progress',False) or getattr(self.ace,'_loading_heads',set())
      or getattr(self.ace,'_unload_all_active',False) or getattr(self.ace,'_bg_staged',{})
      or getattr(self.ace,'_calibration',{'state':'idle'}).get('state')!='idle'):
   raise ProductionError('SourceSelect requires idle printer and no filament reservations')
  # A source change must never abandon an established downstream owner.
  if self.workflow and self.workflow.owners:raise ProductionError('Unload and verify downstream clearance before changing sources')
  if any(h.get('detected') is not False or h.get('enabled') is not True for h in sample.get('heads',{}).values()) or len(sample.get('heads',{}))!=4:
   raise ProductionError('Verify all toolhead sensors clear before changing sources')
 def request(self,payload):
  validate_payload('request',payload,self.routes)
  action=payload.get('action')
  if action in ('source_select','source_restore'):
   with self.ace._flexi_coordinator._lock:
    self._source_change_idle()
    self._invalidate_source_head(payload['head'])
    if action=='source_select':self.source_select.override_manual(payload['head'],{k:v for k,v in payload['record'].items() if k!='type'},idle=True)
    else:self.source_select.restore(payload['head'],idle=True)
    self.lease=None;self.runtime_selection.pop(str(payload['head']),None)
   return self.status()
  w=self._workflow()
  if action not in ('plan','cancel','end_print','isolate_failed_stop'):self.synchronize_inventory()
  if action=='cancel':self.lease=None;return w.cancel()
  if action=='attest_clear':
   if w.job or w.print_session:raise ProductionError('Stop the active production operation before confirming path clearance')
   coordinator=self.ace._flexi_coordinator;state=coordinator.snapshot()
   head=payload['head'];head_aces={a for a,_ in self.routes.sources(head)}
   affected=[r for r in state.get('recovery_required',[]) if r.get('head')==head]
   if affected:raise ProductionError('Outstanding recovery for this toolhead or its ACEs must be resolved before a new clearance confirmation')
   if state.get('operations') or state.get('printing'):raise ProductionError('Filament resources are reserved; stop and reconcile them before confirming path clearance')
   if coordinator.journal is None:raise ProductionError('Prepare the durable recovery journal before recording path clearance')
   topology=self.routes.topology.as_dict()
   for name,joiner in topology.get('joiners',{}).items():
    if joiner.get('head')==head and joiner.get('confirmed') is not True:
     raise ProductionError('Shared PTFE topology for T%d must be physically confirmed first: %s'%(head,name))
   coordinator.acknowledge_clear_recovery(head,payload['confirmation'])
   paths=getattr(self.ace,'_flexi_paths',None)
   if paths is not None:paths.operator_clear(head,'recorded inspected clearance of every path')
   return self.status()
  if action=='end_print':return w.end_print()
  if action=='confirm_clearance':return w.confirm_clearance(payload.get('confirmation'))
  if action=='isolate_failed_stop':
   with w.lock:
    token=payload['recovery_token'];source=tuple(payload['source']);head=payload['head']
    job=w.job;session=w.print_session
    if job and job.get('failed') and not job.get('dispatching') and not w.ticking:
     record=job;kind='operation'
    elif not job and session and session.get('stop_unconfirmed') and not w.print_transition:
     record=session;kind='print'
    else:raise ProductionError('Retry Stop first. Only an unresolved stopped workflow can be isolated.')
    if record['token']!=token or record['head']!=head or tuple(record.get('old_source') or record['source'])!=source:
     raise ProductionError('The failed stop changed. Refresh before confirming isolation.')
    w.c.retire_isolated_stop(head,source,token,kind)
    w.job=None;w.print_session=None;w.resume_pending=False;w.owners.pop(head,None);self.lease=None
   return self.status()
  if action=='same_source_in_tube':
   # Honest outcome of a short or interrupted load: the operator saw this
   # source's filament still in the tube. Nothing is marked empty; the same
   # source may continue loading, other sources stay blocked on a shared path.
   with w.lock:
    if w.job or w.print_session:raise ProductionError('Stop active resources before physical recovery')
    if payload.get('confirmation')!='FILAMENT_OF_SOURCE_IN_TUBE':raise ProductionError('Confirm that you saw the filament of this source in the tube')
    head=payload['head'];source=tuple(payload['source']);token=payload.get('recovery_token')
    self.routes.require_destination(*source,head)
    coordinator=self.ace._flexi_coordinator
    def change(state):
     if state.get('operations') or state.get('printing'):raise ProductionError('Stop active filament operations before recovery')
     records=[r for r in state.get('recovery_required',[]) if r.get('head')==head]
     if token is not None and (not records or records[-1]['token']!=token):raise ProductionError('The recovery record changed. Refresh before confirming.')
     state['recovery_required']=[r for r in state.get('recovery_required',[]) if r.get('head')!=head]
     for record in records:
      event={'event':'recovery_resolved','head':head,'token':record['token'],'disposition':'operator_same_source_in_tube','source':list(source)}
      state.setdefault('recovery_resolutions',[]).append(dict(event,failure=copy.deepcopy(record)))
      state['history']=(state['history']+[event])[-40:]
     state['paths'][str(head)]=dict(state='unknown',owner=list(source),observed=None)
     if (state.get('full_workflow') or {}).get('head')==head:state['full_workflow']=None
    coordinator._change(change)
    w.owners.pop(head,None)
    paths=getattr(self.ace,'_flexi_paths',None)
    if paths is not None:paths.operator_same_source(head,source,'saw the filament of this source in the tube')
   return self.status()
  if action in ('inspect_clear','trace_loaded'):
   with w.lock:
    if w.job or w.print_session:raise ProductionError('Stop active resources before physical recovery')
    coordinator=self.ace._flexi_coordinator
    head=payload['head'];source=tuple(payload['source']);token=payload.get('recovery_token')
    if action=='inspect_clear':
     coordinator.acknowledge_clear_recovery(head,payload['confirmation'],expected_token=token)
     w.owners.pop(head,None)
     paths=getattr(self.ace,'_flexi_paths',None)
     if paths is not None:paths.operator_clear(head,'inspected the complete path clear')
    else:
     self.source_select.require_ace(source,allow_unavailable=True)
     rebind=payload.get('rebind') is True
     def verify_nozzle(record):
      current=w._source(source,allow_unavailable=True)
      if rebind:
       if (not token or current.get('material')!=record.get('material') or current.get('color')!=record.get('color')):
        raise ProductionError('The inspected spool does not match the material and color of the recorded load')
       if not current.get('color'):raise ProductionError('Record the observed spool color before restoring this load')
       def tags(identity):
        try:
         fields=json.loads(identity)
         return fields[1:3] if isinstance(fields,list) and len(fields)>=3 else (None,None)
        except (ValueError,TypeError):return (None,None)
       if any(old and new and old!=new for old,new in zip(tags(record.get('identity')),tags(current['identity']))):
        raise ProductionError('The spool tag contradicts the recorded load')
      elif current['identity']!=record.get('identity'):
       raise ProductionError('The recorded spool no longer matches the selected source')
      if record.get('profile_digest') not in {self.profiles.resolve(source,head,'load')[key] for key in ('digest','legacy_digest')}:
       raise ProductionError('The load recipe changed since the recorded load')
      return current['identity']
     owner=coordinator.acknowledge_loaded_recovery(head,source,payload['confirmation'],expected_token=token,verify_nozzle=verify_nozzle,rebind=rebind)
     w.owners[head]=owner
     paths=getattr(self.ace,'_flexi_paths',None)
     if paths is not None:paths.operator_loaded(head,source,'traced the loaded source to the nozzle')
    if token is not None or action=='trace_loaded':self.lease=None
   return self.status()
  if action=='resume_loading':
   if self.print_context()[1]=='printing':raise ProductionError('Pause the active print before recovery')
   if payload.get('test') is True and self.lease and self.lease.get('wizard_id'):raise ProductionError('Calibration wizard authorization cannot resume a load')
   return w.start('load',tuple(payload['source']),payload['head'],test=payload.get('test') is True,recovery_token=payload['recovery_token'])
  if action=='prepare':self.prepare_runtime(tuple(payload['source']),payload.get('operation','load'));return self.status()
  if action=='risk':
   source=tuple(payload['source']);d=self.inventory().get(source,{})
   if not d.get('identity'):raise ProductionError('Spool identity unavailable')
   if payload.get('scope')=='path':self.risk.grant_path(source,d['identity'],payload.get('confirmation'),payload.get('seconds',3600),self.risk_context(source,d))
   else:self.risk.grant(source,d['identity'],payload['actions'],payload.get('confirmation'),payload.get('seconds',3600))
   return self.status()
  if action=='plan':return self.plan(payload['operation'],payload['source'],payload['head'])
  if action not in ('load','unload','switch','failover','recover'):raise ProductionError('Unsupported production request')
  source=tuple(payload['source']);head=payload['head'];self.routes.require_destination(*source,head)
  test=payload.get('test') is True
  if action=='switch' and w.print_session:return w.request_switch(head,source)
  if self.print_context()[1]=='printing':raise ProductionError('Pause the active print before running a manual filament operation')
  if not test and w.c.journal is None:
   plan=w.plan(action,source,head)
   if not self.authorized('start',plan):raise ProductionError('Complete the missing route settings before first use. No filament was moved.')
   self.prepare_runtime(source,action);w=self._workflow()
  return w.start(action,source,head,test=test)
 def tick(self):
  w=self._workflow();self.synchronize_inventory()
  if w.job:w.tick()
  elif w.print_session:w.monitor_print()
  resume=w.take_resume()
  if resume:
   head=w.last['head']
   if w.last.get('action')=='failover' and w.last.get('old_source'):self.tool_replacements[tuple(w.last['old_source'])]=tuple(w.owners[head]['source'])
   self.runtime_selection[str(head)]='%d:%d'%tuple(w.owners[head]['source']);w.begin_print(head)
   if self.print_context()==(resume,'paused'):self.ace.gcode.run_script_from_command('RESUME')
   else:w.end_print()
  self.calibration_poll()
  return self.status()
 def calibration_context(self,source,head,inventory=None):
  inventory=self.inventory() if inventory is None else inventory
  d=inventory.get(tuple(source),{});p=self.profiles.resolve(tuple(source),head,'load')
  shared={('%d:%d'%s):{'recipe':self.profiles.resolve(s,head,'load')['values'],'adapter':inventory.get(s,{}).get('adapter')} for s in self.routes.sources(head)}
  return CalibrationWizard.binding(self.routes,source,d.get('material'),d.get('raw_material'),d.get('adapter'),{'selected':p['values'],'shared_head':shared,'source_select':self.source_select.context()})
 def calibration_step_limit(self,token,phase,length,speed):
  session=self.wizard.session
  if session and session.get('run_token')==token:
   if not self.wizard.check(self.calibration_context(session['source'],session['head'])):raise ProductionError('Calibration context changed')
   return self.wizard.take_step(token,phase,length)
  return length
 def calibration_status(self,inventory=None):
  inventory=self.inventory() if inventory is None else inventory
  session=self.wizard.session
  if session:self.wizard.check(self.calibration_context(session['source'],session['head'],inventory))
  status=self.wizard.status();status['step_ready']=False
  w=self.workflow
  if session and w and w.job and session.get('run_token')==w.job['token'] and not w.job.get('failed'):
   source,phase=w.job['phases'][w.job['index']]
   adapter=self.adapter_for(source)
   status['step_ready']=phase in ('feed_path','seat','prime','purge','tipform','retract_path') and getattr(adapter,'calibration_step_ready',lambda:True)()
  c=self.ace._flexi_coordinator
  results=c.snapshot().get('wizard_results',[])
  status['results']=[dict(r,valid=not r.get('invalidated') and r.get('context')==self.calibration_context(r['source'],r['head'],inventory)) for r in results]
  return status
 def calibration_poll(self):
  session=self.wizard.session;w=self.workflow
  if not session or session['step']!=4 or not w:return
  if w.job and self.wizard.permit:
   source,phase=w.job['phases'][w.job['index']]
   if self.wizard.permit['token']!=w.job['token'] or self.wizard.permit['phase']!=phase:self.wizard.permit=None
  last=w.last
  if last and last.get('token')==session['run_token'] and not w.job and not w.print_session:
   self.wizard.finished(session['run_token'],last.get('outcome')=='complete')
  elif w.job and w.job.get('failed') and w.job['token']==session['run_token']:self.wizard.cancel('Stop is unconfirmed; inspect and retry cancellation before recovery')
 def calibration_request(self,payload):
  validate_wizard(payload,self.routes);action=payload['action'];w=self._workflow()
  if action in ('start','increment','record'):self.synchronize_inventory()
  if action in ('select','reset') and (w.job or w.print_session):raise ProductionError('Cancel and positively stop the current operation first')
  if action=='reset':self.wizard=CalibrationWizard(self.routes,self.clock);return self.calibration_status()
  if action=='select':
   d=self.inventory().get(tuple(payload['source']),{})
   if d.get('material') and d['material'].strip().upper()!=payload['material'].strip().upper():raise ProductionError('Wizard material contradicts the selected spool metadata')
   return self.wizard.select(payload,self.calibration_context(payload['source'],payload['head']))
  session=self.wizard.require();source=tuple(session['source']);head=session['head']
  if not self.wizard.check(self.calibration_context(source,head)):raise ProductionError('Calibration context changed; restart the wizard')
  if action=='measure':self.wizard.measure(payload)
  elif action=='review':
   p=self.profiles.resolve(source,head,session['operation']);self.wizard.review(p['complete'],p['missing'])
  elif action=='start':
   self.wizard.require(3)
   if not self.lease or self.lease.get('test') is not True or not (self.lease['issued']<=self.clock()<self.lease['expires'] and session['operation'] in self.lease['actions'] and source in self.lease['sources'] and (not self.lease.get('wizard_id') or self.lease['wizard_id']==session['id'])):raise ProductionError('A separately authorized controlled-test session is required')
   d=self.inventory().get(source,{})
   if d.get('material','').strip().upper()!=session['material'].strip().upper():raise ProductionError('Actual material is not confirmed')
   self._wizard_dispatch=True
   try:
    if session['operation']=='assist':
     w.begin_print(head,calibration_test=True);token=w.print_session['token']
    else:
     w.start(session['operation'],source,head,test=True,
             transport_only=session['transport_only']);token=w.job['token']
    self.wizard.began(token)
   finally:self._wizard_dispatch=False
  elif action=='increment':
   j=w.job
   if not j or j.get('failed'):raise ProductionError('No active incrementable calibration operation')
   if not self.authorized('phase',j):raise ProductionError('Current calibration authorization expired or was revoked')
   active_source,phase=j['phases'][j['index']];adapter=self.adapter_for(active_source)
   if not getattr(adapter,'calibration_step_ready',lambda:True)():raise ProductionError('Wait for the current bounded step to stop and be acknowledged')
   self.wizard.grant_step(payload,j['token'],phase)
  elif action=='observe':self.wizard.observe(payload)
  elif action=='record':
   record=self.wizard.record()
   selected=self.inventory()[source]
   record.update(recipe=copy.deepcopy(self.profiles.resolve(source,head,session['operation'])['values']),adapter=selected.get('adapter'),raw_material=selected.get('raw_material'),routes=self.routes.as_dict(),recorded_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
   if self.ace._flexi_coordinator.journal is None:raise ProductionError('Durable calibration storage required')
   current=self.ace._flexi_coordinator.snapshot().get('wizard_results',[])
   results=(current+[record])[-128:];valid=[r for r in results if tuple(r['source'])==source and r['context']==session['context'] and not r.get('invalidated')]
   observed={k:any(r['observations'].get(k) is True for r in valid) for k in ('nozzle_flow','full_path_clear','correct_head','feed_assist')}
   operations={r['operation'] for r in valid};candidate=copy.deepcopy(self.book.records)
   if observed['nozzle_flow'] and observed['full_path_clear'] and observed['correct_head']:
    scopes=operations-{'assist'}
    if observed['feed_assist'] and {'load','unload','assist'}<=operations:scopes|={'assist','print'}
    if scopes:
     temp=CalibrationBook();temp.approve(source,self.profiles.resolve(source,head,'load')['digest'],self.adapter_for(source).kind,scopes,observed,'PHYSICALLY_VERIFIED_RECIPE')
     d=self.inventory()[source];temp.records[source].update(material=[d.get('material'),d.get('raw_material')],routes_sha256=digest(self.routes.context()))
     candidate[source]=temp.records[source]
   certificates=[dict(r,source=list(s),actions=sorted(r['actions'])) for s,r in candidate.items()]
   def save(state):state.update(wizard_results=results,recipe_calibrations=certificates)
   self.ace._flexi_coordinator._change(save);self.book.records=candidate;self.wizard.saved()
  elif action=='cancel':
   self.lease=None;self.wizard.cancel();w.cancel()
   if w.print_session:w.end_print()
  return self.calibration_status()
 def invalidate_for_config(self,config):
  """Invalidate changed recipes durably before accepting new configuration."""
  if not self.ace._flexi_coordinator.journal:return
  # Compare against the same inherited standard settings the live service
  # uses. Without defaults, any config save would look like every route
  # changed and would discard unrelated calibration evidence.
  new=FullProfiles(self.routes,config['profiles'],defaults=self.standard_profile,mode=config.get('settings_mode','standard'));invalid=[]
  for key,head in self.routes.as_dict().items():
   source=tuple(map(int,key.split(':')))
   if (new.resolve(source,head,'load')['digest']!=self.profiles.resolve(source,head,'load')['digest'] or config['device_adapters'].get(str(source[0]))!=self.config['device_adapters'].get(str(source[0])) or config.get('source_select',{}).get(str(head))!=self.config.get('source_select',{}).get(str(head))):invalid.append(source)
  affected_heads={self.routes.destination(*source) for source in invalid}
  invalid=[tuple(map(int,key.split(':'))) for key,head in self.routes.as_dict().items() if head in affected_heads]
  if not invalid:return
  def change(state):
   state['recipe_calibrations']=[r for r in state.get('recipe_calibrations',[]) if tuple(r['source']) not in invalid]
   for record in state.get('wizard_results',[]):
    if tuple(record['source']) in invalid:record['invalidated']='Recipe or adapter changed'
  self.ace._flexi_coordinator._change(change)
 def selected(self,head):
  if self.source_select.effective(head)['type']!='ace':raise ProductionError('External source selected; native manual/stock print bridge is not yet qualified')
  key=self.config['selection'].get(str(head))
  if key is None:raise ProductionError('Explicit spool selection is required for T%d'%head)
  return self.effective_source(tuple(map(int,key.split(':'))))
 def effective_source(self,source):
  self.source_select.require_ace(source)
  seen=set();head=self.routes.destination(*source)
  while source in self.tool_replacements:
   if source in seen:raise ProductionError('Cyclic spool replacement mapping')
   seen.add(source);source=self.tool_replacements[source];self.routes.require_destination(*source,head);self.source_select.require_ace(source)
  return source
 def active_head(self):
  name=self.ace.toolhead.get_extruder().get_name()
  return 0 if name=='extruder' else int(name.removeprefix('extruder'))
 def initial_tool(self,print_id):
  file=print_id[0] if print_id else None
  path=getattr(file,'name',None)
  if not isinstance(path,str):return None
  try:
   with open(path,'rb') as stream:
    for raw in stream.read(512*1024).splitlines():
     code=raw.partition(b';')[0].strip().upper()
     m=re.fullmatch(rb'T([0-9]+)(?:\s+A0)?',code)
     if m:return int(m.group(1))
     if re.match(rb'(?:G0?[0123])\s*(?:[^;]*?)E[+-]?(?:[0-9]|\.)',code):return None
  except OSError:return None
  return None
 def manual_tool(self,number,select):
  w=self._workflow()
  if w.job:raise ProductionError('Stop the active filament operation before changing tools')
  if self.pending_start and self.pending_start!=(self.print_context()[0],number):
   raise ProductionError('Native print selected a different tool than its inspected startup sequence')
  self.check_manual_source(number,require_present=self.print_context()[1]=='printing')
  if w.print_session:w.end_print()
  return select()
 def check_manual_source(self,head,require_present=True):
  coordinator=self.ace._flexi_coordinator
  state=coordinator.snapshot()
  if any(op.get('exclusive') or op['head']==head for op in state['operations'].values()) or any(
      record['head']==head for record in state.get('recovery_required',[])):
   raise ProductionError('Recover or stop the affected filament path before printing')
  if require_present:coordinator.provider.sensor(coordinator.provider.read(),head,True)
 def check_native_print_helper(self,head):
  if head!=self.active_head() or self.print_context()[1]!='printing' or self.pending_start:
   raise ProductionError('Select and verify the requested tool before the native print helper')
  if self.source_select.effective(head)['type']!='ace':return self.check_manual_source(head)
  w=self._workflow();session=w.print_session
  if (not session or session['head']!=head or not w.adapter_for(session['source']).verify_follow(session['token'],session['source'],head)):
   raise ProductionError('Verified ACE feed assist is required for this native print helper')
 def tool(self,number):
  if number<4:head=number;source=self.selected(head)
  else:
   t=self.ace._virtual_tool_target(number);head=t['head'];source=self.effective_source((t['ace'],t['slot']))
  w=self._workflow();owner=w.owners.get(head)
  print_id,print_state=self.print_context()
  already_selected=bool(owner and owner.get('nozzle_verified') and tuple(owner['source'])==source and self.active_head()==head)
  if already_selected and w.print_session and w.print_session['head']==head:
   if not w.adapter_for(source).verify_follow(w.print_session['token'],source,head):raise ProductionError('Active tool feed assist is not confirmed')
   return {'head':head,'source':source,'ready':True}
  if already_selected and print_state!='printing' and not w.job and not self.pending_start:
   sample=w.c.provider.read()
   if w.c.provider.path(sample,head).get('owner')==list(source):
    return {'head':head,'source':source,'ready':True}
  if self.pending_start and (self.pending_start not in ((print_id,number),(print_id,None)) or not owner or tuple(owner['source'])!=source):
   raise ProductionError('Load the intended first tool before starting this print')
  attach_print=bool(print_state=='printing' and not w.print_session)
  if owner and tuple(owner['source'])==source:
   changing_print=bool(w.print_session)
   if changing_print:self.pause('Physical tool selection');w.end_print()
   w.activate_loaded(source,head)
   if changing_print:w.bind_resume(print_id)
  elif owner:
   if w.print_session and w.print_session['head']!=head:
    self.pause('Selecting another loaded toolhead');w.end_print()
    w.start('switch',source,head);w.bind_resume(print_id)
   else:w.request_switch(head,source)
  else:
   if w.print_session:self.pause('Loading another physical toolhead');w.end_print()
   elif print_state=='printing':raise ProductionError('Load the first print tool before printing')
   w.start('load',source,head)
   if print_state=='printing':w.bind_resume(print_id)
  self.runtime_selection[str(head)]='%d:%d'%source
  deadline=self.clock()+self.profiles.resolve(source,head,'load')['values']['timeout_s']
  while w.job and not w.job.get('failed') and self.clock()<deadline:
   self.tick();self.ace.reactor.pause(self.ace.reactor.monotonic()+.1)
  owner=w.owners.get(head)
  if w.job or not owner or tuple(owner['source'])!=source or not owner.get('nozzle_verified'):raise ProductionError('Tool request did not complete verified nozzle loading')
  if self.pending_start or attach_print:
   if self.print_context()!=(print_id,'printing'):raise ProductionError('Native print changed during tool selection')
   self.pending_start=(print_id,None)
   self.finish_native_warmup()
  return {'head':head,'source':source,'ready':True}
 def finish_native_warmup(self):
  if not self.pending_start or self.print_context()!=(self.pending_start[0],'printing'):return
  w=self._workflow();head=self.active_head();owner=w.owners.get(head)
  if self.pending_start[1] is not None:return
  if not owner or not owner.get('nozzle_verified'):raise ProductionError('Load the first print tool before extrusion')
  adapter=w.adapter_for(tuple(owner['source']))
  if hasattr(adapter,'_klipper_extrusion_ready') and not adapter._klipper_extrusion_ready(head):return
  w.begin_print(head);self.pending_start=None
 def begin_print(self):
  if getattr(self.ace,'_flexi_init_error',None):raise ProductionError('Flexi recovery journal is unavailable')
  w=self._workflow();head=self.active_head()
  if w.job:raise ProductionError('Finish or stop the active filament operation before printing')
  if w.print_session:
   if w.print_session.get('calibration_test'):raise ProductionError('End feed-assist calibration before printing')
   return w.monitor_print()
  print_id,_=self.print_context()
  sd=self.ace.printer.lookup_object('virtual_sdcard',None)
  first=self.initial_tool(print_id) if not getattr(sd,'file_position',0) else None
  if first is not None:
   self.pending_start=(print_id,first)
   return None
  self.pending_start=None
  if self.source_select.effective(head)['type']!='ace':return self.check_manual_source(head)
  boundary=getattr(self.ace,'_flexi_spool_boundary',None)
  if boundary:boundary(head)
  selected=tuple(map(int,self.runtime_selection[str(head)].split(':'))) if str(head) in self.runtime_selection else self.selected(head)
  if selected!=tuple(w.owners.get(head,{}).get('source',())):raise ProductionError('Print spool selection and verified owner differ')
  adapter=w.adapter_for(selected)
  if print_id and hasattr(adapter,'_klipper_extrusion_ready') and not adapter._klipper_extrusion_ready(head):
   self.pending_start=(print_id,None)
   return None
  return w.begin_print(head)
 def replacement_status(self):
  print_id,state=self.print_context()
  devices=[]
  for index,protocol in sorted(getattr(self.ace,'_protocols',{}).items()):
   caps=getattr(protocol,'CAPS_STATIC',{})
   name=getattr(protocol,'NAME','unknown')
   model,firmware=(getattr(self.ace,'_ace_models',{}).get(index)
                   or ('',''))
   unit_key=''
   unit_key_fn=getattr(self.ace,'_unit_key',None)
   if callable(unit_key_fn):
    try:unit_key=unit_key_fn(index)
    except Exception:unit_key=''
   devices.append({'ace':index,'generation':name,
                   'generation_label':('Gen2' if name=='v2'
                                       else 'Gen1' if name=='v1'
                                       else 'Unknown'),
                   'model':model,'firmware':firmware,
                   'unit_key':unit_key,
                   'slot_count':4,
                   'connected':getattr(self.ace,'_connected_per_ace',{}).get(index) is True,
                   'stable_id_available':caps.get('has_stable_id') is True,
                   'slot_state_reporting':caps.get('has_slot_status_truth') is True,
                   'decoder':caps.get('has_decoder') is True,
                   'humidity':caps.get('has_humidity') is True,
                   'dryer':caps.get('has_dryer') is True,
                   'feed_assist':caps.get('fa_monitor') is True or caps.get('v1_fa_health_log') is True,
                   'flash_engine':caps.get('flash_engine',''),
                   'protocol':name})
  return {'paused':state=='paused','file':print_id[1] if print_id else None,'devices':devices,
          'automatic_reassignment':False,'automatic_resume':False}
 def clearance_status(self):
  coordinator=self.ace._flexi_coordinator;state=coordinator.snapshot()
  try:sample=self.ace._flexi_evidence.read();sample_error=None
  except Exception as exc:sample=None;sample_error=str(exc)
  topology=self.routes.topology.as_dict();recovery=copy.deepcopy(state.get('recovery_required') or [])
  active=bool(self.workflow and (self.workflow.job or self.workflow.print_session))
  heads=[]
  for head in range(4):
   sources=[list(s) for s in self.routes.sources(head)]
   if not sources:continue
   head_aces={a for a,_ in self.routes.sources(head)}
   head_recovery=[r for r in recovery if r.get('head')==head]
   joiners=[dict(id=name,**joiner) for name,joiner in topology.get('joiners',{}).items() if joiner.get('head')==head]
   path=copy.deepcopy(state.get('paths',{}).get(str(head)) or {'state':'unknown','owner':None,'observed':None})
   provider_path=copy.deepcopy(self.ace._flexi_evidence.path(sample,head)) if sample is not None else {'state':'unknown','owner':None,'observed':None}
   record=copy.deepcopy(state.get('clear_attestations',{}).get(str(head)))
   sensor=None
   if sample is not None:
    raw=sample.get('heads',{}).get(head,{});stamp=raw.get('observed')
    age=None if type(stamp) not in (int,float) else max(0.,self.clock()-stamp)
    sensor={'detected':raw.get('detected'),'enabled':raw.get('enabled'),'observed':stamp,'age_s':age,
            'fresh':bool(age is not None and age<=5.),'state':'clear' if raw.get('detected') is False and raw.get('enabled') is True and age is not None and age<=5. else 'occupied' if raw.get('detected') is True and raw.get('enabled') is True and age is not None and age<=5. else 'unknown'}
   blockers=[]
   if coordinator.journal is None:blockers.append('Durable recovery journal is not prepared.')
   if sample is None:blockers.append('Live sensor snapshot unavailable'+((': '+sample_error) if sample_error else '.'))
   else:
    if sample.get('idle') is not True:blockers.append('Printer and feeders must be idle.')
    if sensor is None or sensor['state']!='clear':blockers.append('T%d toolhead sensor must be fresh, enabled and clear.'%head)
   if active:blockers.append('A production operation or print session is active.')
   if state.get('operations') or state.get('printing'):blockers.append('Filament resources are reserved.')
   if head_recovery:blockers.append('Outstanding recovery for this toolhead must be resolved with the recovery controls before new clearance confirmation.')
   if path.get('state') in ('occupied','in_progress'):blockers.append('T%d path is %s.'%(head,path['state']))
   if len(sources)>1 and not joiners:blockers.append('Shared T%d routes need a confirmed physical joiner definition.'%head)
   if any(j.get('confirmed') is not True for j in joiners):blockers.append('Shared T%d PTFE joiner is not physically confirmed.'%head)
   attestation_age=None;attestation_remaining=None
   if isinstance(record,dict) and type(record.get('at')) in (int,float):
    attestation_age=max(0.,self.clock()-record['at'])
    attestation_remaining=max(0.,float(getattr(self.ace._flexi_evidence,'clearance_age',30.))-attestation_age)
   try:
    valid=bool(record and provider_path.get('state')=='clear' and sample is not None
               and self.ace._flexi_evidence.attestation_valid(head,record))
   except Exception:
    valid=False
   path_state=path.get('state')
   load_blockers=[]
   if coordinator.journal is None:load_blockers.append('Durable recovery journal is not prepared.')
   if sample is None:load_blockers.append('Live sensor snapshot unavailable'+((': '+sample_error) if sample_error else '.'))
   if sample is not None and (sensor is None or sensor['state']!='clear'):
    load_blockers.append('T%d toolhead sensor must be fresh, enabled and clear before feeding.'%head)
   if active:load_blockers.append('A production operation or print session is active; wait for it to finish or stop it.')
   if state.get('operations') or state.get('printing'):load_blockers.append('Filament resources are reserved by another operation.')
   if head_recovery:load_blockers.append('Interrupted-operation recovery for this toolhead must be resolved before a new load. Open recovery and record what you find in the tube.')
   if path_state in ('occupied','in_progress'):load_blockers.append('T%d path is known %s; unload or recover it before loading another source.'%(head,path_state))
   path_warning=None
   if path_state=='unknown':path_warning='T%d path state is unknown. A normal load is allowed; inspect the route only if you want extra confidence.'%head
   ledger=getattr(self.ace,'_flexi_paths',None);ledger_record=ledger.record(head) if ledger is not None else None
   if ledger_record and len(sources)>1:
    if ledger_record['state']=='uncertain' and not ledger_record.get('owner'):
     load_blockers.append('T%d is a shared PTFE path with no recorded state (%s). Inspect the joiner and record inspected clearance once; a verified unload also records it.'%(head,ledger_record['basis']))
    elif ledger_record.get('owner') and ledger_record['state']!='clear':
     path_warning='T%d shared path holds ACE%d slot %d filament (%s). Only that source can load until it is unloaded.'%(head,ledger_record['owner'][0],ledger_record['owner'][1],ledger_record['state'])
   elif path_state in ('occupied','in_progress'):path_warning='T%d path is known %s; a technical conflict blocks a new load.'%(head,path_state)
   heads.append({'head':head,'sources':sources,'shared':len(sources)>1,'joiners':joiners,
                 'sensor':sensor,'path':path,'provider_path':provider_path,'path_warning':path_warning,'ledger':ledger_record,
                 'attestation':record,'attestation_valid':valid,
                  'attestation_age_s':attestation_age,'attestation_remaining_s':attestation_remaining,
                 'can_attest':not blockers,'blockers':blockers,
                 'load_allowed':not load_blockers,'load_blockers':load_blockers})
  return {'schema':1,'required_confirmation':ATTEST_CONFIRMATION,'heads':heads,'topology':topology,
          'recovery_required':[{'head':r.get('head'),'source':[r.get('ace'),r.get('slot')],'token':r.get('token'),'reason':r.get('reason')} for r in recovery],
          'shared_heads':[h['head'] for h in heads if h['shared']],'execution_enabled':False,
          'instruction':'Path inspection is optional for normal load, unload and switching. Use it when a path is unknown, after an interrupted operation, or when you want extra verification. A clear toolhead sensor alone does not prove a shared PTFE branch is empty.'}
 def status(self):
  inventory=self.inventory();w=self.workflow
  previous=getattr(self,'_inventory_memo',None)
  self._inventory_memo=inventory
  try:return self._compose_status(inventory,w)
  finally:self._inventory_memo=previous
 def _compose_status(self,inventory,w):
  clearance=self.clearance_status()
  recovery=clearance.get('recovery_required') or []
  rows=[]
  for s,d in inventory.items():
   p=self.profiles.resolve(s,d['head'],'switch')
   load_profile=self.profiles.resolve(s,d['head'],'load')
   try:adapter=self.adapter_for(s)
   except ProductionError:adapter=None
   # Loads run the original MultiACE loader, which drives ACE Pro (v1)
   # and ACE Pro 2 (v2) alike; the old Flexi adapter qualification only
   # matters for the Advanced Flexi transport.
   supported=bool(d.get('generation') in ('v1','v2') or (adapter is not None and getattr(adapter,'is_supported',lambda:True)() is True))
   actions=[a for a in ('load','unload','switch','failover','print','recover') if self.profiles.resolve(s,d['head'],a)['complete'] and supported]
   hardware_validated=bool(getattr(adapter,'hardware_validated',False))
   connected=d.get('connected')
   if connected is None:connected=getattr(self.ace,'_connected_per_ace',{}).get(s[0]) is True
   head_clearance=next((item for item in clearance.get('heads',[]) if item.get('head')==d['head']),None)
   route_recovery=[r for r in recovery if r.get('head')==d['head'] or list(r.get('source') or [])==[s[0],s[1]]]
   readiness=self.route_readiness(s,d,load_profile,supported,connected,head_clearance,route_recovery)
   rows.append(dict(d,source=list(s),key='%d:%d'%s,connected=connected,missing=p['missing'],recipe_complete=p['complete'],load_missing=load_profile['missing'],load_recipe_complete=load_profile['complete'],route_state=readiness['state'],route_reason=readiness['reason'],route_action=readiness['action'],route_missing=readiness['missing'],recovery_token=(route_recovery[0].get('token') if route_recovery else None),recovery_reason=(route_recovery[0].get('reason') if route_recovery else None),validated_actions=actions,profile_complete_actions=actions,adapter_supported=supported,hardware_validated=hardware_validated,path_risk_acknowledged=self.risk.allows(s,d.get('identity'),'failover') and self.risk.allows(s,d.get('identity'),'switch'),flexible_risk=flexible(d.get('material')) or flexible(d.get('raw_material'))))
  route_counts={}
  for row in rows:route_counts[row['route_state']]=route_counts.get(row['route_state'],0)+1
  targets={}
  for tool in range(32):
   try:
    if tool<4:source=self.selected(tool);head=tool
    else:target=self.ace._virtual_tool_target(tool);source=(target['ace'],target['slot']);head=target['head'];source=self.effective_source(source)
    targets[str(tool)]={'source':list(source),'head':head}
   except Exception:pass
  return {'replacement':self.replacement_status(),'availability_dependencies':[{'source':list(s),'availability':d['availability'],'selection_heads':[h for h,k in self.config['selection'].items() if k=='%d:%d'%s],'backup':'%d:%d'%s in self.config['backups'],'recipe_retained':('%d:%d'%s) in self.config['profiles'].get('routes',{})} for s,d in inventory.items() if d['availability']!='assigned'],'source_select':self.source_select.status(),'pathchek':getattr(self.ace,'_flexi_pathchek').status() if getattr(self.ace,'_flexi_pathchek',None) is not None else {'name':'Flexi PathChek','native_dispatch_enabled':False,'physical_verified':False,'routes':self.routes.as_dict(),'reason':'Native execution requires a current physical qualification and separately approved session'},'clearance':clearance,'route_readiness':{'counts':route_counts,'total':len(rows)},'calibration':self.calibration_status(inventory),'last_plan':copy.deepcopy(self.last_plan),'tool_targets':targets,'authorized_actions':sorted(self.lease['actions']) if self.lease else [],'authorized_sources':[list(s) for s in self.lease['sources']] if self.lease else [],'profiles':copy.deepcopy(self.config['profiles']),'backups':list(self.config['backups']),'selection':dict(self.config['selection']),
          'device_adapters':dict(self.config['device_adapters']),'adapters':self.registry.status(),'inventory':rows,
          'settings_mode':self.config.get('settings_mode','standard'),
          'standard_profiles':{'%d:%d'%s:self.standard_profile(s,d['head'],'load') for s,d in inventory.items()},'effective_profiles':self._effective_profiles(inventory),'path_ledger':(self.ace._flexi_paths.status() if getattr(self.ace,'_flexi_paths',None) is not None else None),
          'recipe_revision':self.recipe_revision(),
          'execution_authorized':bool(self.execution_available() and self.lease and self.lease['issued']<=self.clock()<self.lease['expires']),
          'workflow':w.status() if w is not None else {'job':None,'owners':{},'execution_enabled':False},
          'print_ready':bool(w and w.print_session and self.authorized('print',dict(w.print_session,action='print'))),
          'requires_physical_validation':True}
