# SPDX-License-Identifier: GPL-3.0-or-later
"""Full Flexi workflow: nozzle load, tip/retract, switch and runout recovery.

No hardware or network calls at import/construction. Dispatch is default-deny;
all native effects belong to an injected adapter and scoped session authorization.
Unknown route recipes never inherit from old MultiACE geometry.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import ast,copy,hashlib,json,math,re,threading,time,uuid

class ProductionError(ValueError):pass
NUMERIC={'load_limit_mm':(0,3000),'retract_mm':(0,3000),'seat_mm':(0,100),
 'prime_mm':(0,300),'purge_mm':(0,1000),'feed_speed':(0,100),'retract_speed':(0,100),
 'extrude_speed':(0,20),'temperature_min':(0,400),'temperature_max':(0,400),
 'target_temp':(0,400),'unload_temp':(0,400),'timeout_s':(0,1800),
 'feed_assist_timeout_s':(0,30),'feed_assist_test_s':(0,30),'tip_temperature_min':(0,400),'tip_temperature_max':(0,400),'flow_min_delta':(0,1000000),'flow_samples':(1,100),'flow_interval_s':(0,10)}
# A failure before one of these phases cannot leave filament unaccounted for in
# a shared path. Selection and heating are physical, but not filament transport.
_TRANSPORT_PHASES=frozenset(('feed_path','seat','prime','purge','tipform','retract_path','verify_clear'))
_LIFECYCLE_COMMANDS=frozenset({
 'PRINT_START','PRINT_END','DEFECT_DETECTION_START','DEFECT_DETECTION_END',
 'TIMELAPSE_START','TIMELAPSE_STOP','DEFECT_DETECT_NOODLE_FIRST',
 'DEFECT_DETECTION_DETECT_BED','MOVE_TO_DISCARD_FILAMENT_POSITION',
 'MOVE_TO_XY_IDLE_POSITION_EXTRUDER','ROUGHLY_CLEAN_NOZZLE',
 'ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD','DETECT_BED_PLATE',
 'FINELY_CLEAN_NOZZLE_STAGE_1','FINELY_CLEAN_NOZZLE_STAGE_2'})


_OBJECT_NAME_RE=re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_MACRO_KEY_RE=re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
# Validator hot path (session 2): precompiled per-line patterns and an exact
# fast path for plain G0/G1 moves, which are >90% of a sliced file. Anything
# that does not match the fast pattern goes through the full parser.
_FAST_G01_RE=re.compile(r'G[01](?:[ \t]+[XYZEF][-+]?(?:\d+(?:\.\d*)?|\.\d+))+')
_FAST_KEYS_RE=re.compile(r'[ \t]([XYZEF])')
_LAYER_CHANGE_RE=re.compile(r'^;\s*LAYER_CHANGE\b')
_PROCESSED_RE=re.compile(r'^;\s*MULTIACE\s+(?:PROCESSED:|AUTO-LOAD:)')
_M220_BR_RE=re.compile(r'M220\s+[BR]',re.I)
_CMD_RE=re.compile(r'^([GMTgmt])(\d+)')
_WORD_RE=re.compile(r'^([A-Za-z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))?')

def _finite(value):
 """Return a finite float for a macro value, or None when it is invalid."""
 try:number=float(value)
 except (TypeError,ValueError):return None
 return number if math.isfinite(number) else None

def _macro_fields(text):
 """Parse a bounded KEY=value macro tail; None means ambiguous/invalid."""
 fields={}
 for token in str(text or '').split():
  key,sep,value=token.partition('=')
  if not sep or not _MACRO_KEY_RE.fullmatch(key) or not value:
   return None
  key=key.upper()
  if key in fields:return None
  fields[key]=value
 return fields
import os
import hashlib

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
TEMPERATURE_FIELDS={'target_temp','unload_temp','temperature_min','temperature_max','tip_temperature_min','tip_temperature_max'}
def movement_settings(values):
 return {key:value for key,value in values.items() if key not in TEMPERATURE_FIELDS}
def movement_digest(values):
 return digest(movement_settings(values))
def flexible(material):
 # Risk classifier, not a universal polymer taxonomy. Nonempty unclassified
 # labels require the same path review; missing metadata is rejected separately.
 label=str(material or '').strip().upper()
 if not label:return False
 if re.search(r'\b(?:TPU|TPE|TPC|TPV|TPO|TPA|PEBA|FLEX|ELAST|RUBBER|SOFT|SEBS|SEPS|COPE)',label):return True
 return not bool(re.match(r'^(?:PLA|PETG|PCTG|ABS|ASA|PC|PA[0-9]*|NYLON|PVA|HIPS|PET)(?:\b|_)',label))

def _plain_copy(value):
  # copy.deepcopy cost ~80 ms per Flexi status build (7k calls) for plain
  # JSON-like data. Same result for dict/list/tuple/scalars; anything else
  # falls back to deepcopy.
  t=type(value)
  if t is dict:return {k:_plain_copy(v) for k,v in value.items()}
  if t is list:return [_plain_copy(v) for v in value]
  if t is tuple:return tuple(_plain_copy(v) for v in value)
  if t in (str,int,float,bool) or value is None:return value
  return copy.deepcopy(value)


class FullProfiles:
 def __init__(self,routes,raw=None,defaults=None,mode='custom'):
  # The runtime service always passes the saved settings_mode (absent ->
  # 'standard'). The constructor default 'custom' only serves raw recipe tables
  # (validation/engineering) that have no MultiACE resolver to inherit from.
  # mode='standard': only the original MultiACE resolvers (defaults) apply;
  # saved Flexi head/route recipes are kept but NOT applied. mode='custom':
  # saved recipes override field by field, blank fields inherit defaults.
  if mode not in ('standard','custom'):raise ProductionError('Invalid Flexi settings mode')
  self.mode=mode
  self.routes=routes;self.defaults=defaults;raw={} if raw is None else raw
  if isinstance(raw,str):
   if len(raw)>32768:raise ProductionError('Production profiles too large')
   def pairs(items):
    d={}
    for k,v in items:
     if k in d:raise ProductionError('Duplicate profile key')
     d[k]=v
    return d
   raw=json.loads(raw,object_pairs_hook=pairs)
  if not isinstance(raw,dict) or set(raw)-{'heads','routes'}:raise ProductionError('Profiles require heads/routes')
  self.raw={'heads':{},'routes':{}}
  for group,pattern in [('heads',r'[0-3]'),('routes',r'[0-3]:[0-3]')]:
   values=raw.get(group,{})
   if not isinstance(values,dict) or len(values)>32:raise ProductionError('Invalid profile table')
   for k,v in values.items():
    if not isinstance(k,str) or not re.fullmatch(pattern,k):raise ProductionError('Invalid profile key')
    self.raw[group][k]=self.validate(v)
 def validate(self,v):
  if not isinstance(v,dict) or set(v)-set(NUMERIC)-{'tip_moves'}:raise ProductionError('Unknown production profile field')
  v=copy.deepcopy(v)
  for key,value in v.items():
   if value is None:continue
   if key=='tip_moves':
    if not isinstance(value,list) or not 1<=len(value)<=40:raise ProductionError('Tip recipe must be explicit and bounded')
    for move in value:
     if (not isinstance(move,list) or len(move)!=2 or any(type(n) not in (int,float) or not math.isfinite(n) for n in move)
         or not 0<abs(move[0])<=100 or not 0<move[1]<=20):raise ProductionError('Invalid tip-form move')
   else:
    # Zero is a real "off" value for optional phases and the standard
    # presence-only flow check. It must never mean an unknown measurement.
    if type(value) not in (int,float) or not math.isfinite(value) or not NUMERIC[key][0]<=value<=NUMERIC[key][1] or (value==0 and key not in {'seat_mm','prime_mm','purge_mm','flow_min_delta'}):raise ProductionError('Invalid '+key)
    if key=='flow_samples' and type(value) is not int:raise ProductionError('Flow sample count must be an integer')
  lo,hi=v.get('temperature_min'),v.get('temperature_max')
  if lo is not None and hi is not None:
   if lo>hi:raise ProductionError('Temperature range reversed')
   for key in ['target_temp']:
    if v.get(key) is not None and not lo<=v[key]<=hi:raise ProductionError('Temperature contradicts verified material range')
  if v.get('unload_temp') is not None and v.get('tip_temperature_min') is not None and v.get('tip_temperature_max') is not None and not v['tip_temperature_min']<=v['unload_temp']<=v['tip_temperature_max']:raise ProductionError('Unload target contradicts verified tip-form range')
  return v
 def resolve(self,source,head,action):
  # Session 2 (stalls): a status build resolved ~100 profiles, each
  # re-validating and hashing identical inputs. Inputs are fixed for the life
  # of this object (config saves build a new service), so memoize; callers
  # get their own copy.
  source=tuple(source)
  base=None
  if self.defaults is not None:
   base=self.defaults(source,head,action) if callable(self.defaults) else dict(self.defaults)
  key=(source,head,action,self.mode,repr(base),repr(self.raw['heads'].get(str(head))),repr(self.raw['routes'].get('%d:%d'%source)))
  memo=self.__dict__.setdefault('_resolve_memo',{})
  if key in memo:return _plain_copy(memo[key])
  if len(memo)>512:memo.clear()
  result=self._resolve(source,head,action,base)
  memo[key]=copy.deepcopy(result)
  return result
 def _resolve(self,source,head,action,base):
  self.routes.require_destination(*source,head)
  base=base or {}
  v=dict(base or {});origin={k:'standard' for k in v}
  if self.mode=='custom':
   # A blank (None) custom value never invents data: it falls back to the
   # MultiACE standard value when one exists, otherwise it masks the less
   # specific custom value and the field is reported missing.
   for scope,overrides in (('head',self.raw['heads'].get(str(head),{})),('route',self.raw['routes'].get('%d:%d'%tuple(source),{}))):
    for k,val in overrides.items():
     if val is not None:v[k]=val;origin[k]='custom_'+scope
     elif k in (base or {}):v[k]=base[k];origin[k]='standard'
     else:v.pop(k,None);origin.pop(k,None)
  v=self.validate(v)
  # Standard MultiACE settings supply every field a normal operation needs.
  # Route/head tables above remain explicit overrides and may opt into the
  # optional phases (seat, prime, purge, tip forming, flow delta). Unknown
  # measurements are never invented and are never reported as verified.
  fields=set()
  if action in ('load','switch','failover'):fields|={'feed_speed','load_limit_mm','flow_min_delta','flow_samples','flow_interval_s','timeout_s'}
  if action in ('load','switch','failover','recover'):fields.add('extrude_speed')
  if action in ('unload','switch','failover'):fields|={'retract_speed','retract_mm','timeout_s'}
  if action=='assist':fields|={'feed_assist_timeout_s','feed_assist_test_s'}
  missing=sorted(k for k in fields if v.get(k) is None)
  return {'values':v,'missing':missing,'complete':not missing,'digest':movement_digest(v),'legacy_digest':digest(v),'mode':self.mode,'origin':origin}

class RiskConsent:
 """Explicit flexible-filament risk acknowledgment; never a hardware override."""
 def __init__(self,clock=time.monotonic):self.clock=clock;self.grants={}
 def grant(self,source,identity,actions,confirmation,seconds=3600):
  if confirmation!='TPU_TPE_AT_OWN_RISK':raise ProductionError('Explicit TPU/TPE at-own-risk acknowledgment required')
  if not actions or set(actions)-{'load','unload','switch','failover','print','recover','assist'}:raise ProductionError('Invalid flexible-filament action scope')
  if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<seconds<=43200:raise ProductionError('Invalid risk acknowledgment lifetime')
  self.grants[tuple(source)]={'identity':identity,'actions':set(actions),'issued':self.clock(),'expires':self.clock()+seconds}
 def grant_path(self,source,identity,confirmation,seconds,context):
  """One path acknowledgment covers automatic operations, never execution."""
  if not isinstance(context,str) or not context or not isinstance(identity,str) or not identity:raise ProductionError('Current path and source identity are required')
  self.grant(source,identity,['load','unload','switch','failover','print','recover','assist'],confirmation,seconds)
  self.grants[tuple(source)].update(scope='path',context=context)
 def bind_context(self,source,context):
  grant=self.grants.get(tuple(source))
  if grant and grant.get('scope')=='path' and grant.get('context')!=context:self.revoke(source)
 def allows(self,source,identity,action):
  g=self.grants.get(tuple(source));return bool(g and g['identity']==identity and action in g['actions'] and g['issued']<=self.clock()<g['expires'])
 def revoke(self,source):self.grants.pop(tuple(source),None)

class CalibrationBook:
 def __init__(self):self.records={}
 def approve(self,source,profile_digest,adapter_kind,actions,observations,confirmation):
  if confirmation!='PHYSICALLY_VERIFIED_RECIPE':raise ProductionError('Physical recipe validation required')
  if set(actions)-{'load','unload','switch','failover','print','recover','assist'} or not actions:raise ProductionError('Invalid calibration scope')
  if not observations.get('nozzle_flow') or not observations.get('full_path_clear') or not observations.get('correct_head'):
   raise ProductionError('Calibration needs observed flow, full clearance and physical head identity')
  if ('print' in actions or 'assist' in actions) and observations.get('feed_assist') is not True:raise ProductionError('Print validation requires observed feed-assist behavior')
  self.records[tuple(source)]={'profile':profile_digest,'adapter':adapter_kind,'actions':set(actions)}
 def valid(self,source,profile_digest,adapter_kind,action):
  r=self.records.get(tuple(source));return bool(r and r['profile']==profile_digest and r['adapter']==adapter_kind and action in r['actions'])

class Workflow:
 """Full phase graph over one shared durable coordinator and adapter registry.

 Each phase must return an exact, fresh source/head/token receipt. Source and
 destination ACEs remain reserved THROUGH unload->clear->load during a switch.
 Failures never auto-load, resume printing, choose another spool, or roll back.
 """
 def __init__(self,coordinator,profiles,inventory,adapter_for,authorize=None,risk=None,calibrations=None,pause=None,print_context=None):
  self.c=coordinator;self.profiles=profiles;self.inventory=inventory;self.adapter_for=adapter_for
  self.authorize=authorize;self.risk=risk or RiskConsent(clock=coordinator.clock);self.calibrations=calibrations or CalibrationBook()
  self.pause=pause or (lambda reason:None);self.job=None;self.owners={};self.lock=threading.RLock();self.ticking=False;self.last=None;self.print_session=None;self.resume_pending=False;self.print_transition=None;self.print_context=print_context or (lambda: (None,None))
 def _source(self,source,allow_empty=False,allow_unavailable=False):
  self.c.routes.destination(*source)
  if not allow_unavailable:self.c.routes.require_available(*source)
  d=copy.deepcopy(self.inventory().get(tuple(source)))
  if allow_empty and isinstance(d,dict) and not d.get('material'):
   previous=next((o for o in self.owners.values() if tuple(o['source'])==tuple(source)),None)
   if previous is None and self.job is not None:previous=self.job.get('metadata',{}).get(tuple(source))
   if previous:d.update(material=previous['material'],color=previous.get('color'))
  if not isinstance(d,dict) or not d or (d.get('present') is not True and not allow_empty) or not isinstance(d.get('identity'),str) or not d['identity'] or not isinstance(d.get('material'),str):
   raise ProductionError('Selected spool metadata/presence is unknown')
  return d
 def _job_source(self,job,source):
  cleanup=source==job['old_source'] and job['action'] in ('unload','switch','failover')
  return self._source(source,allow_empty=cleanup and job['action']=='failover',allow_unavailable=cleanup)
 def bind_resume(self,print_id):
  if print_id is not None and self.job and not self.job.get('failed'):
   self.resume_pending=(print_id,self.job['token'])
 def take_resume(self):
  if self.job or not self.resume_pending:return False
  pending=self.resume_pending;self.resume_pending=False
  print_id,state=self.print_context()
  if (state=='paused' and self.last and self.last.get('outcome')=='complete'
      and pending==(print_id,self.last.get('token'))):return print_id
  return None
 def eligible_backups(self,head,failed_source,require_calibration=True):
  # Legacy keyword retained for callers; eligibility uses operation profiles and live guards.
  wanted=self.owners.get(head)
  if not wanted:return []
  candidates=[]
  for source in self.c.routes.sources(head):
   if source==tuple(failed_source):continue
   try:
    d=self._source(source);p=self.profiles.resolve(source,head,'failover');a=self.adapter_for(source)
    if (d.get('approved_backup') is not True or d['material'].strip().upper()!=wanted['material'].strip().upper()
        or not d.get('color') or not wanted.get('color') or d['color'].lstrip('#').upper()!=wanted['color'].lstrip('#').upper()
        or not p['complete']):continue
    if (flexible(d['material']) or flexible(d.get('raw_material'))) and not self.risk.allows(source,d['identity'],'failover'):continue
    candidates.append(source)
   except (ValueError,KeyError):continue
  return candidates
 def plan(self,action,source,head,calibration_test=False,transport_only=False):
  if action not in ('load','unload','switch','failover','recover'):raise ProductionError('Unsupported production action')
  if transport_only and action!='load':raise ProductionError('Transport-only qualification requires a load operation')
  source=tuple(source);self.c.routes.require_destination(*source,head)
  owner=self.owners.get(head)
  target=self._source(source,allow_unavailable=bool(action=='unload' and owner and tuple(owner['source'])==source))
  if action in ('unload','switch','failover') and not owner:raise ProductionError('No verified nozzle owner for T%d; %s cannot start. Use Dashboard > Filament operations > Recover a filament path to reconcile the interrupted operation.' % (head,action))
  if action=='unload' and tuple(owner['source'])!=source:raise ProductionError('Unload source contradicts nozzle owner')
  if action=='load' and owner:raise ProductionError('Loaded shared head must be explicitly unloaded or switched')
  if action=='recover':
   sensor=self.c.provider.read()['heads'].get(head,{})
   if sensor.get('detected') is not True:raise ProductionError('Nozzle recovery requires positive physical filament presence')
   old_record=(self.c.snapshot().get('nozzle_recovery') or {}).get(str(head)) or (self.c.snapshot().get('nozzle_recovery') or {}).get(head)
   if not old_record or tuple(old_record['source'])!=source:raise ProductionError('No matching durable nozzle-recovery record')
  old=tuple(owner['source']) if owner else None
  sources=list(dict.fromkeys(([old] if old else [])+[source]));recipes={};phases=[]
  if action in ('switch','failover') and old==source:raise ProductionError('Switch target is already loaded')
  for s in sources:
   p=self.profiles.resolve(s,head,action);d=self._source(s,allow_empty=action=='failover' and s==old,allow_unavailable=action in ('unload','switch','failover') and s==old);a=self.adapter_for(s)
   if not getattr(a,'is_supported',lambda:True)():raise ProductionError('Selected adapter requires hardware validation. Review the ACE generation in Config > Devices; no filament was moved.')
   if not p['complete']:raise ProductionError('Missing route profile fields: '+', '.join(p['missing'])+'. The route is saved; review or customize its settings, or use the standard MultiACE settings. No filament was moved.')
   if (flexible(d['material']) or flexible(d.get('raw_material'))) and not self.risk.allows(s,d['identity'],action):raise ProductionError('TPU/TPE action requires scoped at-own-risk authorization')
   recipes[s]=p
  if action=='failover' and source not in self.eligible_backups(head,old,require_calibration=not calibration_test):raise ProductionError('Requested backup is not eligible')
  if action in ('unload','switch','failover'):
   phases += [(old,n) for n in ('select','heat_unload')]
   # Tip forming is quality work, not a transport prerequisite. Standard
   # settings omit it; an explicitly saved tip recipe still runs unchanged.
   if recipes[old]['values'].get('tip_moves') is not None:phases.append((old,'tipform'))
   phases += [(old,n) for n in ('retract_path','verify_clear')]
  if action in ('load','switch','failover'):
   phases += [(source,n) for n in ('select','heat_load','feed_path')]
   if not transport_only:
    if recipes[source]['values'].get('seat_mm') is not None:phases.append((source,'seat'))
    if recipes[source]['values'].get('prime_mm') is not None:phases.append((source,'prime'))
    phases.append((source,'verify_flow'))
  if not transport_only and (action in ('switch','failover') or (action=='load' and recipes[source]['values'].get('purge_mm') is not None)):
   phases += [(source,'purge'),(source,'verify_flow')]
  if action=='recover':
   phases += [(source,n) for n in ('select','heat_load')]
   if recipes[source]['values'].get('prime_mm') is not None:phases.append((source,'prime'))
   phases.append((source,'verify_flow'))
  # Validate bounded feed profiles while planning, before any reservation or
  # physical select/heat phase can run. Runtime construction still validates
  # defensively, but an unusable recipe must never reach the printer.
  if action in ('load','switch','failover'):
   for s in sources:
    if not any(src==s and phase=='feed_path' for src,phase in phases):continue
    validate=getattr(self.adapter_for(s),'validate_feed_profile',None)
    if callable(validate):
     d=self._source(s)
     validate(action,calibration_test,s,head,recipes[s]['values'],d.get('material'))
  return {'action':action,'source':source,'head':head,'old_source':old,'sources':sources,'profiles':recipes,'phases':phases,'transport_only':transport_only is True,'identities':{s:self._source(s,allow_empty=action=='failover' and s==old,allow_unavailable=action in ('unload','switch','failover') and s==old)['identity'] for s in sources},'metadata':{s:self._source(s,allow_empty=action=='failover' and s==old,allow_unavailable=action in ('unload','switch','failover') and s==old) for s in sources}}
 def resume_evidence(self,source,head,profile,record):
  source=tuple(source);self.c.routes.require_destination(*source,head)
  if len(self.c.routes.sources(head))!=1:raise ProductionError('Shared paths need a verified branch-clearance procedure')
  sample=self.c.provider.read();self.c.provider.sensor(sample,head,True)
  device=self.c.provider.device(sample,source[0]);metadata=self._source(source)
  if (not record or record.get('source')!=list(source) or record.get('head')!=head
      or record.get('routes')!=self.c.fingerprint or record.get('device')!=device['identity']
      or record.get('spool')!=metadata['identity'] or record.get('profile')!=profile['digest']
      or record.get('binding')!=[list(v) for v in self.c.provider.source_binding(sample,head)]):
   raise ProductionError('The stopped-arrival record no longer matches the route, feeder, spool or recipe')
  if sample.get('idle') is not True:raise ProductionError('Printer must be idle before recovery')
 def start(self,action,source,head,test=False,recovery_token=None,transport_only=False):
  with self.lock:
   if self.job is not None or self.print_session is not None:raise ProductionError('Stop the active workflow or print assist before starting another operation')
   if transport_only and not test:raise ProductionError('Transport-only qualification requires a controlled test')
   if test and (self.authorize is None or self.authorize('calibration_test',{'action':action,'source':tuple(source),'head':head}) is not True):raise ProductionError('Controlled full-test scope is not authorized')
   p=self.plan(action,source,head,calibration_test=test,transport_only=transport_only);p['calibration_test']=test
   if self.c.journal is None:raise ProductionError('Durable production journal required')
   if self.authorize is None or self.authorize('start',p) is not True:raise ProductionError('Production execution is locked')
   if recovery_token is not None:
    if action!='load' or self.print_session:raise ProductionError('Only an idle stopped load can be resumed')
    pending=[r for r in self.c.snapshot().get('recovery_required',[]) if r['head']==head]
    if len(pending)!=1 or pending[0]['token']!=recovery_token:
     raise ProductionError('The recovery record changed. Refresh before resuming.')
    record=pending[0].get('arrival_checkpoint')
    self.resume_evidence(p['source'],head,p['profiles'][p['source']],record)
    p['arrival_checkpoint']=copy.deepcopy(record)
    p['phases']=[(s,phase) for s,phase in p['phases'] if phase!='feed_path']
   self.c.reconcile(head=head)
   reservation=self.c.begin_compound(p['sources'],head,'feed' if action=='load' else 'retract',recovery_token=recovery_token)
   token=reservation['token'];now=self.c.clock();p.update(token=token,index=0,started=now,deadline=now+max(v['values']['timeout_s'] for v in p['profiles'].values()),phase_started=False,calibration_test=test,transport_only=transport_only is True)
   self.job=p
   self.c._change(lambda state:state.update(full_workflow={'token':token,'head':head,'action':action,'phase':p['phases'][0][1],'calibration_test':test}))
   try:self._launch()
   except Exception as exc:self._fail(str(exc));raise ProductionError('Full workflow start failed') from exc
   return self.status()
 def activate_loaded(self,source,head):
  """Select an already proven nozzle without pretending to load it again."""
  with self.lock:
   if self.job or self.print_session:raise ProductionError('Release print/filament resources before selecting a loaded head')
   source=tuple(source);owner=self.owners.get(head);d=self._source(source);profile=self.profiles.resolve(source,head,'print');a=self.adapter_for(source)
   if not owner or tuple(owner['source'])!=source or owner['identity']!=d['identity'] or not owner.get('nozzle_verified'):raise ProductionError('Loaded-head selection lacks current nozzle owner')
   if not getattr(a,'is_supported',lambda:True)():raise ProductionError('Selected adapter requires hardware validation. Review the ACE generation in Config > Devices; no filament was moved.')
   if self.authorize is None or self.authorize('print',{'action':'print','source':source,'head':head}) is not True:raise ProductionError('Loaded-head selection is not authorized')
   if (flexible(d['material']) or flexible(d.get('raw_material'))) and not self.risk.allows(source,d['identity'],'print'):raise ProductionError('Flexible print scope missing')
   self.c.provider.sensor(self.c.provider.read(),head,True);self.c.reconcile(head=head);reservation=self.c.begin_compound([source],head,'retract');now=self.c.clock()
   self.job={'action':'print','source':source,'head':head,'old_source':source,'sources':[source],'profiles':{source:profile},'phases':[(source,'select')],'identities':{source:d['identity']},'metadata':{source:d},'token':reservation['token'],'index':0,'started':now,'deadline':now+profile['values'].get('timeout_s',120),'phase_started':False,'calibration_test':False}
   try:self._launch()
   except Exception as exc:self._fail(str(exc));raise
   return self.status()
 def _launch(self):
  j=self.job;s,phase=j['phases'][j['index']]
  if self.authorize('phase',j) is not True:raise ProductionError('Production authorization revoked')
  if self._job_source(j,s)['identity']!=j['identities'][s]:raise ProductionError('Spool identity changed')
  def mark_phase(state):
   op=state['operations'].get(j['token'])
   if op is None:raise ProductionError('Workflow token is stale')
   op['phase']=phase
   state.update(full_workflow={'token':j['token'],'head':j['head'],'action':j['action'],'phase':phase,'calibration_test':j.get('calibration_test',False)})
  self.c._change(mark_phase)
  if phase=='feed_path':
   # Only the verified clear receipt of THIS serialized unload may replace
   # the old owner on a shared head; never infer clearance from distance.
   if j['old_source'] and not j.get('clear_receipt'):raise ProductionError('Shared path was not positively reconciled')
  if phase=='seat':
   if j.get('arrival_checkpoint'):
    self.resume_evidence(s,j['head'],j['profiles'][s],j['arrival_checkpoint'])
   # Replaying seating after a crash is unsafe even if dispatch had not yet begun.
   def consume(state):state['operations'][j['token']].pop('arrival_checkpoint',None)
   self.c._change(consume)
  transport=phase in _TRANSPORT_PHASES
  if transport or not j.get('physical_started'):
   # Persist the physical boundary BEFORE the adapter can dispatch anything.
   # A crash or stop-ack failure after this point remains conservative.
   def mark_started(state):
    op=state['operations'].get(j['token'])
    if op is None:raise ProductionError('Workflow token is stale')
    op['physical_started']=True
    if transport:op['transport_started']=True
   self.c._change(mark_started)
   j['physical_started']=True
   if transport:j['transport_started']=True
  j['dispatching']=True
  try:self.adapter_for(s).begin_phase(phase,j['token'],s,j['head'],j['profiles'][s]['values'],j['deadline'])
  finally:j['dispatching']=False
  j['phase_started']=True
 def tick(self):
  with self.lock:
   j=self.job
   if not j:raise ProductionError('No active workflow')
   if j.get('failed') or j.get('dispatching') or self.ticking:return self.status()
   self.ticking=True
   try:
    if not j['started']<=self.c.clock()<j['deadline']:raise ProductionError('Workflow timed out or clock regressed')
    if self.authorize('phase',j) is not True:raise ProductionError('Production authorization revoked')
    s,phase=j['phases'][j['index']]
    for source in j['sources']:
     d=self._job_source(j,source)
     if d['identity']!=j['identities'][source]:raise ProductionError('Shared workflow source identity changed')
     if (flexible(d['material']) or flexible(d.get('raw_material'))) and not self.risk.allows(source,d['identity'],j['action']):raise ProductionError('Flexible-filament authorization expired/revoked')
    receipt=self.adapter_for(s).poll_phase(j['token'])
    if receipt.get('error'):raise ProductionError(receipt['error'])
    if not receipt.get('done'):return self.status()
    if (receipt.get('token')!=j['token'] or receipt.get('phase')!=phase or tuple(receipt.get('source',()))!=s or receipt.get('head')!=j['head'] or receipt.get('stopped') is not True):
     raise ProductionError('Unverified or mismatched phase receipt')
    if phase=='feed_path' and j.get('transport_only'):
     if receipt.get('arrival_confirmed') is not True:raise ProductionError('Transport-only qualification did not reach the toolhead sensor')
     owners=copy.deepcopy(self.owners)
     restore=getattr(self.adapter_for(s),'restore_load_idle_timeout',None)
     if restore is not None:restore(j['token'])
     self.c.complete_full_workflow(j['token'],owners,'transport')
     self.owners=owners
     self.last={'outcome':'complete','action':'transport','head':j['head'],'source':list(j['source']),
                'token':j['token'],'transport_only':True,'arrival_confirmed':True,
                'commanded_mm':receipt.get('commanded_mm'),'calibration_test':j.get('calibration_test',False)}
     self.job=None;return self.status()
    if (phase=='feed_path' and j['action']=='load' and receipt.get('arrival_confirmed') is True
        and len(self.c.routes.sources(j['head']))==1):
     sample=self.c.provider.read();self.c.provider.sensor(sample,j['head'],True)
     device=self.c.provider.device(sample,s[0])
     checkpoint={'source':list(s),'head':j['head'],'routes':self.c.fingerprint,'device':device['identity'],
                 'spool':j['identities'][s],'profile':j['profiles'][s]['digest'],
                 'binding':[list(v) for v in self.c.provider.source_binding(sample,j['head'])]}
     self.c._change(lambda state:state['operations'][j['token']].update(arrival_checkpoint=checkpoint))
    if phase=='verify_clear':
     if receipt.get('sensor_clear') is not True:raise ProductionError('Toolhead sensor still occupied')
     if receipt.get('recipe_verified') is not True and not j.get('manual_clearance'):
      if j.get('calibration_test'):
       j['awaiting_operator_clearance']=True;return self.status()
      # A normal unload/switch has already completed a bounded withdrawal and
      # observed the toolhead sensor go clear. Missing calibration evidence is
      # a recoverable uncertainty about the shared branch, not a reason to
      # trap the operator in a completed operation. Continue, keep the path
      # unknown, and surface the warning instead of requiring an inspection.
      j['clearance_warning']='Shared path clearance was not calibrated; completed withdrawal and sensor transition were verified, but the path remains unknown.'
     j.pop('awaiting_operator_clearance',None)
     j['clear_receipt']=receipt;self.owners.pop(j['head'],None)

    if phase=='verify_flow':
     delta=receipt.get('flow_delta');threshold=j['profiles'][s]['values']['flow_min_delta']
     if type(delta) not in (int,float) or not math.isfinite(delta) or delta<threshold or receipt.get('sensor_present') is not True:raise ProductionError('Nozzle flow not verified')
     j['flow_receipt']=receipt
    j['index']+=1;j['phase_started']=False
    if j['index']<len(j['phases']):self._launch();return self.status()
    owners=copy.deepcopy(self.owners)
    if j['action']=='print':
     self.c.provider.sensor(self.c.provider.read(),j['head'],True)
    elif j['action']!='unload':
     if not j.get('flow_receipt'):raise ProductionError('Cannot complete load without nozzle-flow evidence')
     d=self._source(j['source']);owners[j['head']]={'source':list(j['source']),'identity':d['identity'],'material':d['material'],'raw_material':d.get('raw_material',d['material']),'device_identity':self.c.provider.read()['devices'][j['source'][0]]['identity'],'color':d.get('color'),'nozzle_verified':True,'receipt':j['token'],'routes_sha256':self.c.fingerprint,'profile_digest':j['profiles'][j['source']]['digest']}
    restore=getattr(self.adapter_for(j['source']),'restore_load_idle_timeout',None)
    if restore is not None:restore(j['token'])
    if j['action']!='unload':self.c.provider.complete_load(j['head'],j['source'],j['token'])
    elif j.get('clear_receipt',{}).get('recipe_verified') is True:
     self.c.provider.confirm_verified_unload(j['head'],j['clear_receipt'])
    self.c.complete_full_workflow(j['token'],owners,j['action']);self.owners=owners
    self.last={'outcome':'complete','action':j['action'],'head':j['head'],'source':list(j['source']),'nozzle_verified':j['action']!='unload','token':j['token'],'calibration_test':j.get('calibration_test',False),'old_source':list(j['old_source']) if j.get('old_source') else None,'clearance_warning':j.get('clearance_warning')};self.job=None;return self.status()
   except Exception as exc:self._fail(str(exc));return self.status()
   finally:self.ticking=False
 def _fail(self,reason):
  j=self.job;stopped=True;self.resume_pending=False
  for s in j['sources']:
   try:stopped=self.adapter_for(s).stop(j['token']) is True and stopped
   except Exception:stopped=False
   try:
    restore=getattr(self.adapter_for(s),'restore_load_idle_timeout',None)
    if restore is not None:restore(j['token'])
   except Exception as exc:
    stopped=False;reason+='; idle timeout restoration failed: '+str(exc)
  try:self.pause(reason)
  except Exception as exc:reason+='; pause could not be confirmed: '+str(exc)
  self.c.provider.invalidate(j['head']);self.owners.pop(j['head'],None)
  # A software/pre-transport failure has no shared-path uncertainty. Keep the
  # path unknown and release the reservation; do not manufacture a recovery
  # that demands a physical inspection. A started physical phase whose stop is
  # unconfirmed, or any filament transport failure, remains quarantined.
  physical=bool(j.get('physical_started'));transport=bool(j.get('transport_started'))
  uncertain=transport or (physical and not stopped)
  try:
   if uncertain:
    self.c.quarantine(j['token'],reason)
    if stopped:self.c.finish(j['token'],'failed')
   elif stopped:self.c.finish(j['token'],'failed')
  except Exception as exc:reason+='; journal failure: '+str(exc)
  self.last={'token':j['token'],'outcome':'recovery_required' if uncertain else 'failed',
             'reason':reason,'stopped':stopped,'head':j['head'],
             'physical_started':physical,'transport_started':transport}
  j['failed']=True
  if stopped:self.job=None
 def confirm_clearance(self,confirmation):
  j=self.job
  if not j or not j.get('awaiting_operator_clearance') or confirmation!='INSPECTED_FULL_SHARED_PATH_CLEAR':raise ProductionError('No matching manual-clearance checkpoint')
  s=self.c.provider.read();self.c.provider.sensor(s,j['head'],False)
  for source in j['sources']:
   self.c.provider.device(s,source[0])
   if self._source(source,allow_empty=j['action']=='failover' and source==j['old_source'])['identity']!=j['identities'][source]:raise ProductionError('Source changed during clearance confirmation')
  j['manual_clearance']=True
  return self.status()
 def cancel(self):
  with self.lock:
   if self.job:self._fail('Operator cancelled full workflow')
   self.resume_pending=False
   if self.print_session:self.end_print()
   return self.status()
 def begin_print(self,head,calibration_test=False):
  if self.job is not None or self.print_session is not None:raise ProductionError('Print/filament resources are busy')
  owner=self.owners.get(head)
  if not owner or not owner.get('nozzle_verified'):raise ProductionError('Printing requires verified nozzle loading')
  source=tuple(owner['source']);d=self._source(source);p=self.profiles.resolve(source,head,'print');a=self.adapter_for(source)
  if not getattr(a,'is_supported',lambda:True)():raise ProductionError('Selected adapter requires hardware validation')
  action='assist' if calibration_test else 'print'
  request={'head':head,'source':source,'action':action,'calibration_test':calibration_test}
  if calibration_test and (self.authorize is None or self.authorize('calibration_test',request) is not True):raise ProductionError('Feed-assist calibration scope is not authorized')
  if calibration_test:
   finite_window=p['values'].get('feed_assist_test_s')
   if type(finite_window) not in (int,float) or not 0<finite_window<=30:raise ProductionError('Explicit bounded feed-assist test window required')
  if self.authorize is None or self.authorize('print',request) is not True:raise ProductionError('Printing is not authorized')
  if d['identity']!=owner['identity']:raise ProductionError('Print source changed')
  if (flexible(d['material']) or flexible(d.get('raw_material'))) and not self.risk.allows(source,d['identity'],action):raise ProductionError('Flexible print risk scope is missing')
  self.c.reconcile(head=head);self.c.hold_printing(*source)
  token=uuid.uuid4().hex;self.print_session={'token':token,'head':head,'source':source,'identity':d['identity'],'action':action,'calibration_test':calibration_test,'deadline':self.c.clock()+finite_window if calibration_test else None}
  self.print_transition='starting'
  try:
   if a.start_follow(token,source,head) is not True:raise ProductionError('Feed assist was not positively verified')
   if not self.print_session or self.print_session['token']!=token:raise ProductionError('Print start was cancelled during feed-assist startup')
  except Exception:
   try:self.pause('Feed assist verification failed; printing is blocked')
   except Exception:pass
   try:self.end_print()
   except Exception:pass
   raise
  finally:self.print_transition=None
  return self.status()
 def end_print(self):
  session=self.print_session
  if not session:return self.status()
  previous_transition=self.print_transition;self.print_transition='stopping'
  try:
   source=session['source'];a=self.adapter_for(source)
   session['stop_unconfirmed']=True
   if a.stop_follow(session['token'],source,session['head']) is not True:
    self.pause('Feed-assist stop is not confirmed; resource remains reserved')
    raise ProductionError('Print stop acknowledgment missing')
   s=self.c.provider.read()
   if s['heads'][session['head']].get('detected') is True:self.c.provider.complete_load(session['head'],source,self.owners[session['head']]['receipt'])
   self.c.release_printing(source[0]);self.print_session=None
   return self.status()
  finally:self.print_transition=previous_transition
 def request_switch(self,head,source):
  if self.print_transition:raise ProductionError('Wait for the current feed-assist transition')
  self.resume_pending=False;print_id=None
  if self.print_session:
   if self.print_session['head']!=head:raise ProductionError('Pause and explicitly select the target physical head first')
   try:self.pause('Color/spool switch')
   except Exception as exc:
    try:self.end_print()
    except Exception:pass
    raise ProductionError('Print pause failed; switch blocked: '+str(exc)) from exc
   print_id,_=self.print_context();self.end_print()
  result=self.start('switch',source,head);self.bind_resume(print_id);return result
 def monitor_print(self):
  if self.print_transition:return self.status()
  session=self.print_session
  if not session:return self.status()
  try:
   source=session['source'];head=session['head'];s=self.c.provider.read()
   if self.authorize('print',dict(session,action=session.get('action','print'))) is not True:raise ProductionError('Print authorization expired/revoked')
   if session.get('calibration_test') and self.c.clock()>=session['deadline']:
    self.end_print();self.last={'outcome':'complete','action':'assist','head':head,'source':list(source),'token':session['token']};return self.status()
   self.c.provider.device(s,source[0],ready=False)
   d=self.inventory().get(source,{})
   if (flexible(d.get('material')) or flexible(d.get('raw_material'))) and not self.risk.allows(source,d.get('identity'),session.get('action','print')):raise ProductionError('Flexible print authorization expired/revoked')
   if s['heads'][head].get('enabled') is not True or not self.c.provider._fresh(s['heads'][head].get('observed')):raise ProductionError('Print sensor unavailable')
   if not session.get('calibration_test') and d.get('present') is False and type(s['heads'][head].get('detected')) is bool:
    self.resume_pending=False;print_id,_=self.print_context()
    self.pause('Verified source runout');self.end_print()
    result=self.runout(head,already_paused=True);self.bind_resume(print_id);return result
   if self._source(source)['identity']!=session['identity']:raise ProductionError('Printing spool identity changed')
   if self.adapter_for(source).verify_follow(session['token'],source,head) is not True:raise ProductionError('Physical tool/temperature/feed-assist safety check failed')
  except Exception as exc:
   self.resume_pending=False;reason=str(exc)
   try:self.pause(reason)
   except Exception as pause_error:reason+='; pause failed: '+str(pause_error)
   try:self.end_print()
   except Exception as stop_error:reason+='; stop unconfirmed: '+str(stop_error)
   self.last={'outcome':'paused','reason':reason,'stopped':self.print_session is None}
  return self.status()

 def runout(self,head,already_paused=False):
  owner=self.owners.get(head)
  if not owner:raise ProductionError('Runout without known nozzle owner')
  failed=tuple(owner['source'])
  if self.inventory().get(failed,{}).get('present') is not False:raise ProductionError('Runout source is not confirmed empty')
  sample=self.c.provider.read()
  if sample['heads'][head].get('detected') is False:self.c.record_runout_witness(head,failed,owner['receipt'])
  else:self.c.provider.sensor(sample,head,True)
  if not already_paused:self.pause('Runout: selecting an approved same-head backup')
  candidates=self.eligible_backups(head,owner['source'])
  if not candidates:self.last={'outcome':'paused','reason':'No eligible backup with complete operation settings','head':head};return self.status()
  return self.start('failover',candidates[0],head)
 def status(self):
  j=self.job
  return {'execution_enabled':False,'production_dispatch_authorized':bool(self.authorize and ((self.job and self.authorize('phase',self.job)) or (self.print_session and self.authorize('print',dict(self.print_session,action=self.print_session.get('action','print')))))),
          'job':None if j is None else {'token':j['token'],'head':j['head'],'action':j['action'],'source':list(j['source']),'phase':j['phases'][min(j['index'],len(j['phases'])-1)][1],'awaiting_operator_clearance':j.get('awaiting_operator_clearance',False),'clearance_warning':j.get('clearance_warning'),'stop_unconfirmed':bool(j.get('failed')),'isolation_source':list(j['old_source'] or j['source']),'calibration_test':j.get('calibration_test',False)},
          'print_session':copy.deepcopy(self.print_session),'resume_pending':bool(self.resume_pending),'owners':copy.deepcopy(self.owners),'last_result':copy.deepcopy(self.last),'requires_hardware_validation':True}

def preflight_gcode_lines(lines,status,routes,*,strict_live=True,sha256=None,allow_print_prefs=False,allow_flow_cal=False):
 """Streaming Flexi admission parser shared by upload checks and the
 preparation validator.

 `strict_live` keeps the print-start requirements (a verified initial nozzle
 owner). Publication validation passes False because no motion may happen
 there, while the native print path still performs the full live check.
 """
 errors=[];tools=[];tool_counts={};line_count=0;layer_changes=0;heater_count=0;processed=False;active=None;targets=status.get('tool_targets',{});inventory={r['key']:r for r in status.get('inventory',[])}
 defined_objects=set();active_objects=set();feeder_heads=set();head_sources={}
 allowed={'G0':'XYZEF','G1':'XYZEF','G2':'XYZEFIJKR','G3':'XYZEFIJKRP','G4':'PS','G17':'','G18':'','G19':'','G21':'','G28':'XYZIJ','G90':'','G91':'','G92':'XYZE','M82':'','M83':'','M104':'STA','M109':'STRA','M106':'SP','M107':'P','M140':'S','M190':'SR','M204':'SPRT','M205':'XYZEJST','M220':'S','M221':'S','M400':'','M73':'PR','M117':None}
 standard=status.get('standard_profiles') or {}
 profiles=FullProfiles(routes,status.get('profiles',{}),defaults=lambda src,hd,act: standard.get('%d:%d'%tuple(src),{}),mode=status.get('settings_mode','standard'))
 owners={int(h):tuple(o['source']) for h,o in (status.get('workflow',{}).get('owners') or {}).items() if o and o.get('nozzle_verified') is True}
 for target in targets.values():
  try:head=int(target['head']);source=tuple(target['source'])
  except (KeyError,TypeError,ValueError):continue
  if 0<=head<=3 and source not in head_sources.setdefault(head,[]):head_sources[head].append(source)
 def active_source(head):
  return active[0] if active is not None and active[1]==head else None
 def qualify(source,needed,line_no):
  row=inventory.get('%d:%d'%source,{})
  if not needed.issubset(set(row.get('validated_actions',[]))):errors.append('Line %d: required filament movement settings are incomplete'%line_no)
  if row.get('present') is not True:errors.append('Line %d: selected spool is not confirmed present'%line_no)
 route_cache={}
 def route_head(head,line_no,action='print',source=None):
  if source is None:
   source=active_source(head)
  key=(source,head,action) if source is not None else None
  if key is not None and key in route_cache:return route_cache[key]
  if source is None:
   candidates=head_sources.get(head) or []
   chosen=None
   for candidate in candidates:
    try:routes.require_destination(*candidate,head)
    except Exception:continue
    row=inventory.get('%d:%d'%candidate,{})
    if not {action}.issubset(set(row.get('validated_actions',[]))) or row.get('present') is not True:continue
    if profiles.resolve(candidate,head,action)['complete']:chosen=candidate;break
   if chosen is None:
    errors.append('Line %d: physical tool T%d has no usable selected route'%(line_no,head));return None
   source=chosen
  try:routes.require_destination(*source,head)
  except Exception as exc:errors.append('Line %d: physical tool T%d route is invalid: %s'%(line_no,head,exc));return None
  qualify(source,{action},line_no)
  if not profiles.resolve(source,head,action)['complete']:errors.append('Line %d: route %s recipe is incomplete'% (line_no,action))
  if key is not None:route_cache[key]=source
  return source
 for line_no,line in enumerate(lines,1):
  line_count=line_no
  if len(line)>16*1024*1024:errors.append('Line %d is oversized'%line_no);continue
  code=line.split(';',1)[0].strip()
  if not code:
   upper=line.lstrip().upper()
   if _LAYER_CHANGE_RE.match(upper):layer_changes+=1
   elif _PROCESSED_RE.match(upper):processed=True
   continue
  if _FAST_G01_RE.fullmatch(code):
   keys=_FAST_KEYS_RE.findall(code)
   if len(keys)==len(set(keys)):
    if 'E' in keys and active is None:errors.append('Line %d: extrusion before explicit tool selection'%line_no)
    continue
  if code.upper().startswith('SET_PRINT_PREFERENCES'):
   # Only the exact line emitted by _prepend_print_prefs is accepted, and
   # only in the first few lines of a prepared derivative during validation.
   if allow_print_prefs and line_no<=3 and re.fullmatch(
     r'SET_PRINT_PREFERENCES BED_LEVEL=[01] FLOW_CALIBRATE=[01] '
     r'TIME_LAPSE_CAMERA=[01] FORCE=1',code,re.I):
    continue
   errors.append('Line %d: print-preference override needs review'%line_no);continue
  if code.upper().startswith('SM_PRINT_FLOW_CALIBRATE'):
   # A routed plan may generate this stock command only after selecting the
   # physical head it names. Reject it for raw uploads and for any other
   # parameter form; the command itself stays stock so PA is measured by the
   # firmware's own flow calibrator.
   flow=re.fullmatch(r'SM_PRINT_FLOW_CALIBRATE\s+EXTRUDER=([0-3])',code,re.I)
   if allow_flow_cal and flow:
    # Stock Snapmaker startup emits SM_PRINT_FLOW_CALIBRATE for each
    # extruder before its first explicit T selection. The command names the
    # physical head, so validate that head's saved route instead of requiring
    # an already-active T. Raw uploads never set allow_flow_cal.
    target_head=int(flow.group(1))
    source=active[0] if active is not None and active[1]==target_head else None
    route_head(target_head,line_no,source=source)
    continue
   errors.append('Line %d: flow-calibration command needs a selected routed head'%line_no);continue
  parts=code.split(None,1);macro=parts[0].upper();macro_tail=parts[1] if len(parts)>1 else ''
  if macro.startswith('EXCLUDE_OBJECT_'):
   fields=_macro_fields(macro_tail)
   if macro=='EXCLUDE_OBJECT_DEFINE':
    if fields is None or set(fields)!={'NAME','CENTER','POLYGON'}:
     errors.append('Line %d: object definition parameters are ambiguous'%line_no);continue
    name=fields['NAME']
    if not _OBJECT_NAME_RE.fullmatch(name):
     errors.append('Line %d: object definition name is invalid'%line_no);continue
    center=fields['CENTER'].split(',')
    if len(center)!=2 or any(_finite(v) is None for v in center):
     errors.append('Line %d: object definition center is invalid'%line_no);continue
    try:polygon=ast.literal_eval(fields['POLYGON'])
    except (SyntaxError,ValueError):polygon=None
    if not isinstance(polygon,list) or len(polygon)<3 or any(
      not isinstance(point,(list,tuple)) or len(point)!=2 or
      any(_finite(v) is None for v in point) for point in polygon):
     errors.append('Line %d: object definition polygon is invalid'%line_no);continue
    if name in defined_objects:
     errors.append('Line %d: duplicate object definition'%line_no);continue
    defined_objects.add(name);continue
   if macro in ('EXCLUDE_OBJECT_START','EXCLUDE_OBJECT_END'):
    if fields is None or set(fields)!={'NAME'} or not _OBJECT_NAME_RE.fullmatch(fields['NAME']):
     errors.append('Line %d: object boundary parameters are invalid'%line_no);continue
    name=fields['NAME']
    if name not in defined_objects:
     errors.append('Line %d: object boundary has no definition'%line_no);continue
    if macro=='EXCLUDE_OBJECT_START':
     if name in active_objects:errors.append('Line %d: duplicate object start'%line_no);continue
     active_objects.add(name)
    else:
     if name not in active_objects:errors.append('Line %d: object end without start'%line_no);continue
     active_objects.remove(name)
    continue
   errors.append('Line %d: object command %s needs compatibility review'%(line_no,macro));continue
  if macro=='SET_VELOCITY_LIMIT':
   fields=_macro_fields(macro_tail)
   if fields is None or not fields or not set(fields).issubset({'ACCEL','ACCEL_TO_DECEL','VELOCITY','SQUARE_CORNER_VELOCITY'}):
    errors.append('Line %d: velocity-limit parameters are unsupported'%line_no);continue
   values={key:_finite(value) for key,value in fields.items()}
   if any(value is None or value<0 for value in values.values()):errors.append('Line %d: velocity-limit value is invalid'%line_no)
   continue
  if macro=='SET_PRESSURE_ADVANCE':
   fields=_macro_fields(macro_tail)
   if fields is None or 'ADVANCE' not in fields or not set(fields).issubset({'ADVANCE','SMOOTH_TIME'}):
    errors.append('Line %d: pressure-advance parameters are unsupported'%line_no);continue
   advance=_finite(fields['ADVANCE']);smooth=_finite(fields.get('SMOOTH_TIME','0'))
   if advance is None or not 0<=advance<=5 or smooth is None or not 0<=smooth<=0.2:
    errors.append('Line %d: pressure-advance value is invalid'%line_no);continue
   if active is None:
    errors.append('Line %d: pressure advance before explicit tool selection'%line_no);continue
   route_head(active[1],line_no,source=active[0]);continue
  if macro=='SET_PRINT_STATS_INFO':
   fields=_macro_fields(macro_tail)
   if fields is None or set(fields)!={'TOTAL_LAYER','CURRENT_LAYER'}:
    errors.append('Line %d: print-statistics parameters are unsupported'%line_no);continue
   try:total=int(fields['TOTAL_LAYER']);current=int(fields['CURRENT_LAYER'])
   except ValueError:errors.append('Line %d: print-statistics value is invalid'%line_no);continue
   if total<1 or current<0 or current>total:errors.append('Line %d: print-statistics layer is invalid'%line_no)
   continue
  if macro in ('TIMELAPSE_TAKE_FRAME','DEFECT_DETECTION_DETECT'):
   if macro_tail:errors.append('Line %d: %s parameters are unsupported'%(line_no,macro))
   continue
  if macro=='SM_PRINT_AUTO_FEED':
   fields=_macro_fields(macro_tail)
   if fields is None or set(fields)!={'EXTRUDER'}:
    errors.append('Line %d: stock auto-feed parameters are unsupported'%line_no);continue
   try:head=int(fields['EXTRUDER'])
   except ValueError:head=-1
   if not 0<=head<=3:errors.append('Line %d: stock auto-feed tool is invalid'%line_no);continue
   route_head(head,line_no,source=active_source(head))
   if head in feeder_heads:errors.append('Line %d: duplicate stock auto-feed for physical tool T%d'%(line_no,head))
   feeder_heads.add(head);continue
  if macro=='SM_PRINT_PREEXTRUDE_FILAMENT':
   fields=_macro_fields(macro_tail)
   if fields is None or set(fields)!={'INDEX'}:
    errors.append('Line %d: pre-extrusion parameters are unsupported'%line_no);continue
   try:index=int(fields['INDEX'])
   except ValueError:index=-1
   target=targets.get(str(index))
   if not 0<=index<=31 or target is None:
    errors.append('Line %d: pre-extrusion tool has no authoritative route'%line_no);continue
   head=int(target['head']);source=tuple(target['source'])
   if active is None or active[1]!=head or active[0]!=source:
    errors.append('Line %d: pre-extrusion target differs from selected physical route'%line_no);continue
   route_head(head,line_no,source=source);continue
  if macro=='SM_PRINT_EXTRUDER_PREHEAT':
   fields=_macro_fields(macro_tail)
   if fields is None or set(fields)!={'EXTRUDER','TEMP'}:
    errors.append('Line %d: extruder-preheat parameters are unsupported'%line_no);continue
   try:head=int(fields['EXTRUDER']);temp=_finite(fields['TEMP'])
   except ValueError:head=-1;temp=None
   if not 0<=head<=3 or temp is None or not 0<=temp<=300:
    errors.append('Line %d: extruder-preheat value is invalid'%line_no);continue
   route_head(head,line_no,source=active_source(head));continue
  if macro=='SM_PRINT_CHECK_SWITCH_EXTRUDER':
   if macro_tail:errors.append('Line %d: extruder-check parameters are unsupported'%line_no);continue
   if active is None:errors.append('Line %d: extruder check before explicit tool selection'%line_no);continue
   route_head(active[1],line_no,source=active[0]);continue
  if macro in _LIFECYCLE_COMMANDS:
   if macro_tail:errors.append('Line %d: lifecycle macro %s parameters are unsupported'%(line_no,macro))
   continue
  if macro=='WAIT_CHAMBER_TEMP':
   fields=_macro_fields(macro_tail);timeout=_finite(fields.get('TIMEOUT','')) if fields else None
   if fields is None or set(fields)!={'TIMEOUT'} or timeout is None or timeout<0:
    errors.append('Line %d: chamber-wait parameters are invalid'%line_no)
   continue
  if macro=='BED_MESH_CALIBRATE':
   fields=_macro_fields(macro_tail)
   if fields is None or not set(fields).issubset({'PROBE_COUNT','Z_OFFSET'}) or 'PROBE_COUNT' not in fields:
    errors.append('Line %d: bed-mesh parameters are unsupported'%line_no);continue
   try:counts=[int(v) for v in fields['PROBE_COUNT'].split(',')]
   except ValueError:counts=[]
   offset=_finite(fields.get('Z_OFFSET','0'))
   if len(counts)!=2 or any(v<1 or v>100 for v in counts) or offset is None or not -5<=offset<=5:
    errors.append('Line %d: bed-mesh value is invalid'%line_no)
   continue
  if macro=='SET_PURIFIER_MODE':
   fields=_macro_fields(macro_tail)
   if fields is None or not fields or not set(fields).issubset({'MODE','DESIRE_TEMP','ALARM_TEMP','FAN_SPEED','DELAY_OFF'}):
    errors.append('Line %d: purifier parameters are unsupported'%line_no);continue
   values={key:_finite(value) for key,value in fields.items()}
   mode=values.get('MODE');fan=values.get('FAN_SPEED');delay=values.get('DELAY_OFF')
   if any(value is None for value in values.values()) or (mode is not None and not 0<=mode<=3) or (fan is not None and not 0<=fan<=1) or (delay is not None and not 0<=delay<=3600):
    errors.append('Line %d: purifier value is invalid'%line_no)
   continue
  if _M220_BR_RE.fullmatch(code):continue
  match=_CMD_RE.match(code)
  if not match:errors.append('Line %d: startup/print command needs Flexi compatibility review'%line_no);continue
  cmd=match[1].upper()+str(int(match[2]));tail=code[match.end():].strip()
  if cmd.startswith('T'):
   if tail and not re.fullmatch(r'A0',tail,re.I):errors.append('Line %d: tool selection parameters are not supported'%line_no);continue
   target=targets.get(cmd[1:])
   if not target:errors.append('Line %d: tool has no authoritative route'%line_no);continue
   source=tuple(target['source']);head=target['head'];routes.require_destination(*source,head)
   old=owners.get(head)
   # Repeated same-head selections are the dominant G-code pattern.  The
   # route, inventory qualification and print recipe are invariant for this
   # call, so validate them only when the head owner changes.  This keeps
   # admission linear for multi-million-line 1 GiB files.
   if old==source:
    active=(source,head);tool=int(cmd[1:]);tool_counts[tool]=tool_counts.get(tool,0)+1
    if not any(t['tool']==tool for t in tools):tools.append({'tool':tool,'source':list(source),'head':head})
    continue
   needed={'print'}
   if old is None:needed.add('load')
   elif old!=source:
    needed.add('switch');qualify(old,{'switch'},line_no)
   qualify(source,needed,line_no)
   if not profiles.resolve(source,head,'print')['complete']:errors.append('Line %d: route print recipe is incomplete'%line_no)
   if strict_live and not tools and old!=source:errors.append('Initial tool requires controlled nozzle loading before print start')
   active=(source,head);owners[head]=source;tool=int(cmd[1:]);tool_counts[tool]=tool_counts.get(tool,0)+1
   if not any(t['tool']==tool for t in tools):tools.append({'tool':tool,'source':list(source),'head':head})
   continue
  if cmd not in allowed:errors.append('Line %d: startup/print command %s needs Flexi compatibility review'%(line_no,cmd));continue
  if cmd=='M117':continue
  params={};bad=False
  while tail:
   word=_WORD_RE.match(tail)
   if not word:bad=True;break
   key=word[1].upper();value=word[2]
   if key in params or key not in allowed[cmd] or (value is None and cmd!='G28'):bad=True;break
   params[key]=float(value) if value is not None else 0.;tail=tail[word.end():].lstrip()
   if not math.isfinite(params[key]):bad=True;break
  if bad:errors.append('Line %d: ambiguous, duplicate or unsupported parameters'%line_no);continue
  if cmd in ('G0','G1','G2','G3') and 'E' in params and active is None:errors.append('Line %d: extrusion before explicit tool selection'%line_no)
  if cmd in ('M104','M109'):
   heater_count+=1
   if 'T' in params:
    if params['T']!=int(params['T']) or not 0<=params['T']<=3:
     errors.append('Line %d: heater target is invalid'%line_no);continue
    head=int(params['T'])
    if head not in head_sources:errors.append('Line %d: heater target has no selected route'%line_no);continue
   else:
    if active is None:errors.append('Line %d: heater command before explicit tool selection'%line_no);continue
    head=active[1]
   if 'A' in params and params['A'] not in (0.,1.):
    errors.append('Line %d: heater A parameter is invalid'%line_no);continue
   if any(key in params and not 0<=params[key]<=300 for key in ('S','R')):
    errors.append('Line %d: heater temperature is outside the extruder range'%line_no);continue
   if 'S' in params and 'R' in params:errors.append('Line %d: ambiguous heater temperature'%line_no)
   route_head(head,line_no)
 if not tools:errors.append('Print file has no explicit supported tool selection')
 return {'ok':not errors,'errors':errors[:100],'error_count':len(errors),'tools':tools,'tool_counts':{str(k):v for k,v in sorted(tool_counts.items())},'lines':line_count,'layer_changes':layer_changes,'heater_commands':heater_count,'processed':processed,'sha256':sha256,'rewritten':False,'requires_live_checks':bool(strict_live)}

def preflight_gcode_file(path,status,routes,*,strict_live=True,allow_print_prefs=False,allow_flow_cal=False):
 """Streaming Flexi admission parser; never reads the whole file into one
 string.  SHA-256 covers the exact bytes on disk."""
 digest=hashlib.sha256()
 with open(path,'rb') as raw:
  for block in iter(lambda:raw.read(1024*1024),b''):digest.update(block)
 with open(path,'r',encoding='utf-8',errors='replace') as source:
  result=preflight_gcode_lines(source,status,routes,strict_live=strict_live,sha256=digest.hexdigest(),allow_print_prefs=allow_print_prefs,allow_flow_cal=allow_flow_cal)
 result['size']=os.path.getsize(path)
 return result

def preflight_gcode(text,status,routes):
 """Conservative Flexi parser; no macro expansion or legacy route rewriting.

 Compact parameters are supported. Duplicated/ambiguous parameters are refused
 rather than interpreting a different heater target from Klipper's parser.
 """
 if not isinstance(text,str) or not text.strip() or len(text)>32*1024*1024:raise ProductionError('Invalid or oversized print file')
 errors=[];tools=[];active=None;targets=status.get('tool_targets',{});inventory={r['key']:r for r in status.get('inventory',[])}
 allowed={'G0':'XYZEF','G1':'XYZEF','G2':'XYZEFIJKR','G3':'XYZEFIJKR','G4':'PS','G17':'','G18':'','G19':'','G21':'','G28':'XYZ','G90':'','G91':'','G92':'XYZE','M82':'','M83':'','M104':'ST','M109':'STR','M106':'SP','M107':'P','M140':'S','M190':'SR','M204':'SPRT','M205':'XYZEJST','M220':'S','M221':'S','M400':'','M73':'PR','M117':None}
 standard=status.get('standard_profiles') or {}
 profiles=FullProfiles(routes,status.get('profiles',{}),defaults=lambda src,hd,act: standard.get('%d:%d'%tuple(src),{}),mode=status.get('settings_mode','standard'))
 owners={int(h):tuple(o['source']) for h,o in (status.get('workflow',{}).get('owners') or {}).items() if o and o.get('nozzle_verified') is True}
 def qualify(source,needed,line_no):
  row=inventory.get('%d:%d'%source,{})
  if not needed.issubset(set(row.get('validated_actions',[]))):errors.append('Line %d: required filament movement settings are incomplete'%line_no)
  if row.get('present') is not True:errors.append('Line %d: selected spool is not confirmed present'%line_no)
 for line_no,line in enumerate(text.splitlines(),1):
  if len(line)>2048:errors.append('Line %d is oversized'%line_no);continue
  code=line.split(';',1)[0].strip()
  if not code:continue
  # The original preflight can prepend a bounded print-preference command.
  # It is not a motion or heater command; accept only the exact key=value
  # form emitted by the UI, never an arbitrary macro.
  if re.match(r'^SET_PRINT_PREFERENCES\b',code,re.I):
   if not re.fullmatch(r'SET_PRINT_PREFERENCES(?:\s+[A-Z_]+=\d+)+',code,re.I):
    errors.append('Line %d: malformed print-preference command'%line_no)
   continue
  match=re.match(r'^([GMTgmt])(\d+)',code)
  if not match:errors.append('Line %d: startup/print command needs Flexi compatibility review'%line_no);continue
  cmd=match[1].upper()+str(int(match[2]));tail=code[match.end():].strip()
  if cmd.startswith('T'):
   if tail:errors.append('Line %d: tool selection parameters are not supported'%line_no);continue
   target=targets.get(cmd[1:])
   if not target:errors.append('Line %d: tool has no authoritative route'%line_no);continue
   source=tuple(target['source']);head=target['head'];routes.require_destination(*source,head)
   old=owners.get(head);needed={'print'}
   if old is None:needed.add('load')
   elif old!=source:
    needed.add('switch');qualify(old,{'switch'},line_no)
   qualify(source,needed,line_no)
   if not profiles.resolve(source,head,'print')['complete']:errors.append('Line %d: route print recipe is incomplete'%line_no)
   if not tools and old!=source:errors.append('Initial tool requires controlled nozzle loading before print start')
   active=(source,head);owners[head]=source;tools.append({'tool':int(cmd[1:]),'source':list(source),'head':head});continue
  if cmd not in allowed:errors.append('Line %d: startup/print command %s needs Flexi compatibility review'%(line_no,cmd));continue
  if cmd=='M117':continue
  params={};bad=False
  while tail:
   word=re.match(r'^([A-Za-z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))?',tail)
   if not word:bad=True;break
   key=word[1].upper();value=word[2]
   if key in params or key not in allowed[cmd] or (value is None and cmd!='G28'):bad=True;break
   params[key]=float(value) if value is not None else 0.;tail=tail[word.end():].lstrip()
   if not math.isfinite(params[key]):bad=True;break
  if bad:errors.append('Line %d: ambiguous, duplicate or unsupported parameters'%line_no);continue
  if cmd in ('G0','G1','G2','G3') and 'E' in params and active is None:errors.append('Line %d: extrusion before explicit tool selection'%line_no)
  if cmd in ('M104','M109'):
   if active is None:errors.append('Line %d: heater command before explicit tool selection'%line_no);continue
   if 'T' in params and params['T']!=active[1]:errors.append('Line %d: heater target differs from selected physical tool'%line_no)
   if 'S' in params and 'R' in params:errors.append('Line %d: ambiguous heater temperature'%line_no)
 if not tools:errors.append('Print file has no explicit supported tool selection')
 return {'ok':not errors,'errors':errors[:100],'tools':tools,'sha256':hashlib.sha256(text.encode()).hexdigest(),'rewritten':False,'requires_live_checks':True}
