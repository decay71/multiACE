# SPDX-License-Identifier: GPL-3.0-or-later
"""Guided calibration state and one-shot step budgets. No hardware or file I/O."""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy,math,time,uuid
try:
 from .ace_flexi_production import ProductionError,digest
except ImportError:
 from ace_flexi_production import ProductionError,digest
OPERATIONS=('load','unload','switch','failover','recover','assist')
STEPS=('Choose route','Measure path','Set recipe','Review safeguards','Controlled test','Observe results','Saved')
FIELDS={
 'load_limit_mm':('Maximum feed to toolhead sensor','mm','Path'),
 'retract_mm':('Complete withdrawal distance','mm','Path'),
 'seat_mm':('Toolhead sensor to nozzle seating','mm','Nozzle'),
 'prime_mm':('Extrusion verification amount','mm','Nozzle'),
 'purge_mm':('Color-change purge amount','mm','Nozzle'),
 'feed_speed':('ACE loading speed','mm/s','Speed'),
 'retract_speed':('ACE withdrawal speed','mm/s','Speed'),
 'extrude_speed':('Nozzle and tip movement speed','mm/s','Speed'),
 'temperature_min':('Manufacturer minimum nozzle temperature','°C','Temperature'),
 'temperature_max':('Manufacturer maximum nozzle temperature','°C','Temperature'),
 'target_temp':('Loading / printing nozzle temperature','°C','Temperature'),
 'unload_temp':('Tip-forming nozzle temperature','°C','Temperature'),
 'tip_temperature_min':('Verified tip-forming minimum','°C','Temperature'),
 'tip_temperature_max':('Verified tip-forming maximum','°C','Temperature'),
 'timeout_s':('Whole operation time limit','seconds','Timing'),
 'flow_min_delta':('Qualified coil engagement threshold','sensor units','Evidence'),
 'flow_samples':('Coil sample count','samples','Evidence'),
 'flow_interval_s':('Coil sampling interval','seconds','Evidence'),
 'feed_assist_timeout_s':('Feed-assist acknowledgment time limit','seconds','Feed assist'),
 'feed_assist_test_s':('Bounded feed-assist observation window','seconds','Feed assist')}
INSTRUCTIONS={
 'measure':[
 'Keep devices stopped. Trace the selected ACE slot through every PTFE tube and combiner to the stated physical toolhead. Inspect every source sharing that head.',
 'Measure along the actual PTFE centerline with a tape or a removed, marked tube; include the adapter path. Record the endpoints and measurement uncertainty. Do not command filament movement to measure it.',
 'The PTFE measurement is not automatically a safe motor travel limit. Sensor-to-nozzle seating and complete withdrawal must be qualified separately.',
 'Read the filament manufacturer’s nozzle range from the spool label or its exact datasheet. Leave unknown values blank. Do not copy temperatures from a different spool.'],
 'test':[
 'Movement stays locked until a separately approved, scoped calibration session is armed. Preparing the journal, entering measurements and reviewing the route do not arm it.',
 'After explicit physical-test approval, verify clear surroundings, safe nozzle/discard position, homing, tool identity, sensors and shared-path clearance. Selection and heating occur when an approved test starts.',
 'Each single-step permission permits one 5 mm ACE frame (at most 1 mm for a coupled nozzle/tip move). A progressive feed segment is a separate reviewed budget of at most 200 mm and is executed as individual 5 mm ACE frames; it never becomes one long command. Both expire if unused. Stop if filament buckles, slips, jams, or reaches an unexpected toolhead.',
 'A sensor transition is not proof of nozzle extrusion or clearance of unsensed tubing. Inspect visible extrusion and the entire withdrawn tip; never push a backup into an uncertain shared path.'],
 'observe':[
 'For loading, confirm visible, continuous extrusion from the intended nozzle. Coil readings are an engagement proxy, not a direct flow meter.',
 'For unloading, inspect the complete withdrawal, including the deformed tip passing every adapter. Empty toolhead sensors alone do not prove the shared tubing is clear.',
 'For feed assist, observe the selected slot only during the bounded test window. Confirm feeding, tension and positive stop. Other slots must remain inactive.',
 'Cancellation, missing acknowledgments, timeouts or sensor loss require explicit recovery. Restart never restores execution permission.']}
def finite(value,lo,hi,name):
 if type(value) not in (int,float) or not math.isfinite(value) or not lo<value<=hi:raise ProductionError('Invalid '+name)
 return value
def validate_payload(p,routes):
 if not isinstance(p,dict) or not isinstance(p.get('action'),str):raise ProductionError('Expected a wizard action')
 schema={'select':{'source','head','material','operation','transport_only'},'measure':{'path_length_mm','uncertainty_mm','endpoints','physical_route_confirmed','temperature_label_confirmed'},'review':set(),'start':{'confirmation'},'increment':{'max_mm','confirmation'},'segment':{'max_mm','confirmation'},'observe':{'observations','notes'},'record':{'confirmation'},'cancel':set(),'reset':set()}
 action=p['action']
 if action not in schema or set(p)-schema[action]-{'action'}:raise ProductionError('Unsupported wizard fields')
 if action=='select':
  source=p.get('source');head=p.get('head')
  if not isinstance(source,list) or len(source)!=2 or any(type(n) is not int for n in source) or type(head) is not int:raise ProductionError('Choose an explicit ACE, slot and head')
  routes.require_destination(*source,head)
  if p.get('operation') not in OPERATIONS:raise ProductionError('Unknown calibration operation')
  if not isinstance(p.get('material'),str) or not 1<=len(p['material'].strip())<=64:raise ProductionError('Choose the actual material')
  if p.get('transport_only') not in (None,True,False):raise ProductionError('transport_only must be boolean')
  if p.get('transport_only') and p.get('operation')!='load':raise ProductionError('Transport-only qualification requires the load operation')
 elif action=='measure':
  finite(p.get('path_length_mm'),0,3000,'measured path length')
  u=p.get('uncertainty_mm')
  if type(u) not in (int,float) or not math.isfinite(u) or not 0<=u<p['path_length_mm']:raise ProductionError('Record measurement uncertainty')
  if not isinstance(p.get('endpoints'),str) or not 3<=len(p['endpoints'].strip())<=300:raise ProductionError('Describe the measurement endpoints')
  if p.get('physical_route_confirmed') is not True or p.get('temperature_label_confirmed') is not True:raise ProductionError('Physical route and manufacturer temperature range must be confirmed')
 elif action=='increment':
  finite(p.get('max_mm'),0,5,'single-step maximum')
  if p.get('confirmation')!='APPROVED_SINGLE_CALIBRATION_STEP':raise ProductionError('Explicit single-step confirmation required')
 elif action=='segment':
  finite(p.get('max_mm'),0,200,'progressive segment maximum')
  if p.get('confirmation')!='APPROVED_CALIBRATION_SEGMENT':raise ProductionError('Explicit progressive-segment confirmation required')
 elif action=='start' and p.get('confirmation')!='START_APPROVED_CALIBRATION':raise ProductionError('Separate approved calibration start confirmation required')
 elif action=='observe':
  obs=p.get('observations')
  if not isinstance(obs,dict) or set(obs)-{'correct_head','nozzle_flow','full_path_clear','feed_assist','stopped','sensor_arrival'} or any(type(v) is not bool for v in obs.values()):raise ProductionError('Observations must be explicit booleans')
  if not isinstance(p.get('notes',''),str) or len(p.get('notes',''))>1500:raise ProductionError('Observation notes too long')
 elif action=='record' and p.get('confirmation')!='RECORD_PHYSICAL_CALIBRATION':raise ProductionError('Explicit observation recording required')
 return p
class CalibrationWizard:
 def __init__(self,routes,clock=time.monotonic):self.routes=routes;self.clock=clock;self.session=None;self.permit=None;self.last_reason=None
 @staticmethod
 def binding(routes,source,material,raw_material,adapter,recipe):
  return digest({'routes':routes.context(),'source':list(source),'material':material,'raw_material':raw_material,'adapter':adapter,'recipe':recipe})
 def select(self,p,context):
  validate_payload(p,self.routes);self.permit=None
  self.session={'id':uuid.uuid4().hex,'source':p['source'],'head':p['head'],'material':p['material'].strip(),'operation':p['operation'],'transport_only':p.get('transport_only') is True,'step':1,'context':context,'started':self.clock(),'stale':False,'measurements':{},'observations':{},'notes':'','run_token':None,'completed_run':False}
  self.last_reason=None;return self.status()
 def check(self,context):
  s=self.session
  if s and s['context']!=context:
   s['stale']=True;self.permit=None;self.last_reason='Route, material, recipe or adapter changed. Restart calibration.'
  return bool(s and not s['stale'])
 def require(self,*steps):
  if not self.session or self.session['stale']:raise ProductionError('Select a current calibration route')
  if steps and self.session['step'] not in steps:raise ProductionError('Complete the preceding wizard step first')
  return self.session
 def measure(self,p):
  validate_payload(p,self.routes);s=self.require(1,2);s['measurements']={k:v for k,v in p.items() if k!='action'};s['step']=2
 def review(self,complete,missing):
  s=self.require(2,3)
  if not complete:raise ProductionError('Complete the verified recipe: '+', '.join(missing))
  s['step']=3
 def began(self,token):
  s=self.require(3);s['run_token']=token;s['step']=4;self.permit=None
 def grant_step(self,p,token,phase):
  validate_payload(p,self.routes);s=self.require(4)
  if not token or token!=s['run_token']:raise ProductionError('No matching active calibration run')
  self.status()
  if phase not in ('feed_path','seat','prime','purge','tipform','retract_path'):raise ProductionError('Wait for a filament movement phase')
  if self.permit:raise ProductionError('A step permission is already pending')
  if p['action']=='segment':
   if phase!='feed_path':raise ProductionError('Progressive segments are only valid during feed transport')
   self.permit={'kind':'segment','token':token,'phase':phase,'max_mm':p['max_mm'],'remaining_mm':p['max_mm'],'issued':self.clock(),'expires':self.clock()+120}
  else:self.permit={'kind':'increment','token':token,'phase':phase,'max_mm':p['max_mm'],'issued':self.clock(),'expires':self.clock()+15}
 def take_step(self,token,phase,requested):
  s=self.require(4);permit=self.permit
  if not permit:return None
  if not permit['issued']<=self.clock()<permit['expires'] or permit['token']!=token or permit['phase']!=phase:
   self.permit=None;return None
  if permit.get('kind')=='segment':
   if type(requested) not in (int,float) or not math.isfinite(requested) or requested<=0:raise ProductionError('Invalid remaining segment request')
   allowed=min(requested,permit['remaining_mm'],5.)
   if type(allowed) not in (int,float) or not math.isfinite(allowed) or allowed<=0:raise ProductionError('Invalid remaining segment budget')
   permit['remaining_mm']-=allowed
   if permit['remaining_mm']<=1e-9:self.permit=None
   return allowed
  self.permit=None;return min(requested,permit['max_mm'])
 def finished(self,token,success):
  s=self.require(4)
  if token!=s['run_token']:raise ProductionError('Calibration run identity mismatch')
  self.permit=None;s['completed_run']=success
  if success:s['step']=5
  else:self.last_reason='Test failed or stopped. Inspect and use explicit recovery; no result was certified.'
 def observe(self,p):
  validate_payload(p,self.routes);s=self.require(5)
  if not s['completed_run']:raise ProductionError('No successful test run to observe')
  obs=p['observations'];required={'correct_head','stopped'}
  if s.get('transport_only'):required.add('sensor_arrival')
  elif s['operation'] in ('load','switch','failover','recover'):required.add('nozzle_flow')
  if s['operation'] in ('unload','switch','failover'):required.add('full_path_clear')
  if s['operation']=='assist':required.add('feed_assist')
  if any(obs.get(k) is not True for k in required):raise ProductionError('Confirm the required physical observations: '+', '.join(sorted(required)))
  s['observations']=copy.deepcopy(obs);s['notes']=p.get('notes','')
 def record(self):
  s=self.require(5)
  if not s['observations']:raise ProductionError('Record physical observations before saving')
  return copy.deepcopy({k:s[k] for k in ('id','source','head','material','operation','transport_only','context','measurements','observations','notes','run_token')})
 def saved(self):self.require(5)['step']=6;self.permit=None
 def cancel(self,reason='Cancelled; no execution permission retained'):
  self.permit=None;self.last_reason=reason
  if self.session:self.session['completed_run']=False
 def status(self):
  if self.permit and not self.permit['issued']<=self.clock()<self.permit['expires']:self.permit=None
  return {'schema':1,'steps':list(STEPS),'session':copy.deepcopy(self.session),'pending_step':copy.deepcopy(self.permit),'reason':self.last_reason,'instructions':INSTRUCTIONS,'fields':{k:{'label':v[0],'unit':v[1],'group':v[2]} for k,v in FIELDS.items()},'movement_authorized':False,'requires_separate_authorization':True}
