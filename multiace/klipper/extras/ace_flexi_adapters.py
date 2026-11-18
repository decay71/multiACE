# SPDX-License-Identifier: GPL-3.0-or-later
"""Interchangeable ACE transports plus the U1's native hotend primitives.

All effects require a scoped phase authorization. Both the original ACE Pro
(Gen1, JSON-RPC) and the ACE 2 Pro (Gen2, protobuf) use the existing MultiACE
protocol interface; the protocol object remains the single source of
generation-specific capabilities. No instances are armed or send commands at
construction/import.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy,math,time
from .ace_flexi_production import ProductionError,flexible,digest,movement_digest
from .ace_flexi_transport import ACETransport
from .ace_flexi_executor import ExecutionError

class AdapterRegistry:
 def __init__(self):self.factories={};self.instances={}
 def register(self,kind,factory,validated=False):
  if kind in self.factories:raise ProductionError('Adapter already registered')
  self.factories[kind]=(factory,bool(validated))
 def get(self,source,kind):
  if kind not in self.factories:raise ProductionError('Unsupported hardware adapter: '+str(kind))
  key=(tuple(source),kind)
  if key not in self.instances:
   instance=self.factories[kind][0](tuple(source))
   # The registry is what binds a source-less adapter to its logical ACE.
   # Keep this out of the constructors so existing adapters stay compatible.
   if getattr(instance,'source',None) is None:instance.source=tuple(source)
   self.instances[key]=instance
  return self.instances[key]
 def status(self):return {k:{'registered':True,'hardware_validated':v[1]} for k,v in self.factories.items()}

class NativeU1Adapter:
 kind='original_ace_pro'
 def is_supported(self):return True
 def __init__(self,ace,coordinator,thermal,authorize=None,material_permission=None,calibrations=None,clock=time.monotonic,step_limit=None):
  self.ace=ace;self.c=coordinator;self.provider=coordinator.provider;self.thermal=thermal;self.authorize=authorize
  self.step_limit=step_limit;self.material_permission=material_permission;self.calibrations=calibrations;self.clock=clock
  self.phase=None;self.moves=[];self.path=None;self.pending=None;self.samples=[];self.retracted=0.;self.saw_present=False;self.saw_clear=False;self.token=None;self.saved=False;self.source=None
  self.idle_timeout_scope=None;self.heat_target=None;self.heater_scope=None
 def _extend_load_idle_timeout(self,token,deadline):
  idle=self.ace.printer.lookup_object('idle_timeout',None)
  previous=getattr(idle,'idle_timeout',None)
  if type(previous) not in (int,float) or not math.isfinite(previous) or previous<=0:
   raise ProductionError('Current Klipper idle timeout unavailable')
  extended=max(previous,math.ceil(deadline-self.clock())+30)
  if extended==previous:return
  self.idle_timeout_scope=(token,idle,previous)
  self.ace.gcode.run_script_from_command('SET_IDLE_TIMEOUT TIMEOUT=%s'%extended)
  if idle.idle_timeout!=extended:raise ProductionError('Klipper idle timeout extension was not applied')
 def restore_load_idle_timeout(self,token):
  heater=self.heater_scope
  if heater is not None and heater[0]==token:
   heaters=self.ace.printer.lookup_object('heaters',None)
   if heaters is not None:heaters.set_temperature(heater[1],0.,False)
   self.heater_scope=None
  scope=self.idle_timeout_scope
  if scope is None or scope[0]!=token:return
  _,idle,previous=scope
  self.ace.gcode.run_script_from_command('SET_IDLE_TIMEOUT TIMEOUT=%s'%previous)
  if idle.idle_timeout!=previous:raise ProductionError('Klipper idle timeout restoration was not confirmed')
  self.idle_timeout_scope=None
 def _check(self,braking=False):
  if not braking and (self.authorize is None or self.authorize(self.token,self.phase,self.source,self.head) is not True):raise ProductionError('Native production effects are locked')
  state=self.c.snapshot();op=state['operations'].get(self.token)
  if not op or op['head']!=self.head or list(self.source) not in op.get('sources',[[op['ace'],op['slot']]]):raise ProductionError('Native adapter does not own this route')
  s=self.provider.read();d=self.provider.device(s,self.source[0],ready=False)
  if d['identity']!=op.get('identities',{}).get(str(self.source[0]),op['identity']):raise ProductionError('Native adapter connection identity changed')
  self.c.routes.require_destination(*self.source,self.head)
  if not braking and self.clock()>=self.deadline:raise ProductionError('Native phase deadline exceeded')
  material=d.get('materials',{}).get(self.source[1])
  if not braking and flexible(material) and (self.material_permission is None or self.material_permission(self.source,material,self.phase) is not True):raise ProductionError('Flexible-filament acknowledgment unavailable')
  sensor=s['heads'].get(self.head,{})
  if not braking and (sensor.get('enabled') is not True or type(sensor.get('detected')) is not bool or not self.provider._fresh(sensor.get('observed'))):raise ProductionError('Native phase sensor evidence lost')
  if not braking and op.get('resumes_recovery') and sensor.get('detected') is not True:
   raise ProductionError('Toolhead presence was lost during resumed load')
  self.saw_present|=sensor.get('detected') is True
  self.saw_clear|=self.saw_present and sensor.get('detected') is False
  return s,d
 def _selected(self):
  ext=self.ace.toolhead.get_extruder();expected='extruder' if self.head==0 else 'extruder%d'%self.head
  if ext.get_name()!=expected:return False
  exts=self.ace.printer.lookup_object('extruder_list',None)
  return bool(isinstance(exts,(list,tuple)) and len(exts)==4 and all(getattr(e,'park_detector',None) is not None and e.get_park_detector_status().get('state')==('ACTIVATE' if i==self.head else 'PARKED') for i,e in enumerate(exts)))
 def _coil(self):
  if not self._selected():raise ProductionError('Physical head changed during flow verification')
  sensor=getattr(getattr(self.ace.toolhead.get_extruder(),'binding_probe',None),'sensor',None)
  if sensor is None:raise ProductionError('Native nozzle-flow/coil sensor unavailable')
  value=sensor.get_coil_freq()
  if type(value) not in (int,float) or not math.isfinite(value) or value<=0:raise ProductionError('Invalid native coil sample')
  return value
 def _send(self,method,length=None,speed=None):
  reply={};owner=self
  def callback(self,response):
   try:
    if self._tipform_rejected(response):reply['error']='Native ACE rejected '+method
    else:reply['accepted']=True
   except Exception as exc:reply['error']=str(exc)
  params={'index':self.source[1]}
  if length is not None:params.update(length=length,speed=speed)
  request={'method':method,'params':params}
  if getattr(self.ace,'_flexi_tagged_dispatch',False):request['_flexi_token']=self.token
  self.ace.send_request_to(self.source[0],request,callback)
  return reply
 def _feed_speed_validated(self,source,head,profile,action):
  records=getattr(self,'calibrations',None)
  return bool(records and any(records.valid(tuple(source),fp,self.kind,action) for fp in (movement_digest(profile),digest(profile))))
 def _legacy_direct_load_speed_allowed(self,action,calibration_test,source,head,profile,material):
  # The first controlled direct T0 load may reuse this printer's own
  # legacy V1 request speed. It still uses finite Flexi segments.
  if (action!='load' or calibration_test is not True or tuple(source)!=(0,0)
      or head!=0 or set(self.c.routes.sources(0))!={(0,0)}
      or any('0:0' in j['branches'] for j in self.c.routes.topology.as_dict()['joiners'].values())
      or str(material).strip().upper() not in ('PLA','PLA+')):
   return False
  protocol=getattr(self.ace,'_protocols',{}).get(0)
  speed=getattr(self.ace,'get_feed_speed',None)
  length=getattr(self.ace,'get_load_length',None)
  return (getattr(protocol,'NAME',None)=='v1' and callable(speed) and callable(length)
          and profile['feed_speed']==speed(0)
          and profile['load_limit_mm']<=length(0,0))
 def _allow_legacy_direct_load_speed(self,workflow,material):
  if workflow.get('token')!=self.token:return False
  return self._legacy_direct_load_speed_allowed(workflow.get('action'),workflow.get('calibration_test') is True,self.source,self.head,self.profile,material)
 def _allow_continuous_load(self,workflow,material):
  # An ordinary PLA-family load uses the printer's own single V1 feed request,
  # exactly as the stock MultiACE load does: the ACE runs once and Flexi still
  # stops on toolhead arrival, the deadline or the stop acknowledgement. The
  # stock load length/speed ceiling bounds the request; no wizard or recipe is
  # required for routine operation.
  if workflow.get('token')!=self.token or workflow.get('action')!='load':return False
  if str(material).strip().upper().replace(' ','') not in ('PLA','PLA+','PLAPLUS','PLABASIC'):return False
  protocol=getattr(self.ace,'_protocols',{}).get(self.source[0])
  if getattr(protocol,'NAME',None)!='v1':return False
  if not 0<self.profile['load_limit_mm']<=2100 or not 0<self.profile['feed_speed']<=80:return False
  length=getattr(self.ace,'get_load_length',None)
  if callable(length):
   try:
    if self.profile['load_limit_mm']>length(self.source[0],self.source[1]):return False
   except Exception:return False
  return True
 def validate_feed_profile(self,action,calibration_test,source,head,profile,material=None):
  # Planning calls this before any reservation, tool selection or heating.
  source=tuple(source)
  if not material:
   inventory=getattr(getattr(self.ace,'_flexi_production',None),'inventory',None)
   if callable(inventory):material=inventory().get(source,{}).get('material')
  calibrated=self._feed_speed_validated(source,head,profile,action)
  legacy=self._legacy_direct_load_speed_allowed(action,calibration_test is True,source,head,profile,material)
  try:ACETransport.validate_profile(step_mm=5,speed=profile['feed_speed'],validated_speed=calibrated or legacy)
  except ExecutionError as exc:raise ProductionError(str(exc)) from exc

 def _homed_axes(self):
  return set(self.ace.toolhead.get_status(0).get('homed_axes',''))
 def _xy_homed(self):
  return {'x','y'}<=self._homed_axes()
 def _ensure_xy_homed(self):
  # Native FEED_AUTO requires X/Y only. With [homing_xyz_override] this is
  # already done by the stock T command; the fallback below is the same
  # native requirement, and never homes Z.
  if self._xy_homed():
   return
  self.ace.gcode.run_script_from_command('G28 X Y')
  self.ace.toolhead.wait_moves()
 def _klipper_extrusion_ready(self,head):
  if not self._selected() or not self._xy_homed():
   return False
  ext=self.ace.printer.lookup_object('extruder' if head==0 else 'extruder%d'%head)
  state=ext.get_status(0)
  return state.get('can_extrude') is True and state.get('activating_move') is False
 def _native_temperature(self,head,phase):
  # Reuse the native feed's material/DB temperature resolution. A configured
  # Flexi range may narrow the native value, but it never requires the
  # operator to preheat the nozzle.
  try:
   module,channel=self.ace.EXTRUDER_MAP[head]
   feed=self.ace.printer.lookup_object('filament_feed %s'%module,None)
   name='_get_filament_unload_temp' if phase=='heat_unload' else '_get_filament_temp'
   getter=getattr(feed,name,None)
   value=getter(channel) if callable(getter) else 250
  except Exception:
   value=250
  try:value=float(value)
  except (TypeError,ValueError):value=250
  if not math.isfinite(value) or value<=0:raise ProductionError('Native load temperature is unavailable for T%d'%head)
  return value
 def _effective_temperature(self,head,phase):
  key='unload_temp' if phase=='heat_unload' else 'target_temp'
  limits=(self.profile.get('tip_temperature_min'),self.profile.get('tip_temperature_max')) if phase=='heat_unload' else (self.profile.get('temperature_min'),self.profile.get('temperature_max'))
  target=self.profile.get(key)
  if target is None:
   target=self._native_temperature(head,phase)
   if all(type(v) in (int,float) and math.isfinite(v) for v in limits):
    target=min(max(target,limits[0]),limits[1])
  if type(target) not in (int,float) or not math.isfinite(target) or not 0<target<=400:
   raise ProductionError('No usable nozzle temperature for T%d'%head)
  return float(target),limits
 def _target_ready(self,head,target):
  if not self._klipper_extrusion_ready(head):return False
  state=self.ace.printer.lookup_object('extruder' if head==0 else 'extruder%d'%head).get_status(0)
  try:
   return (math.isclose(float(state.get('target')),target,abs_tol=1e-6)
           and float(state.get('temperature'))>=target-3)
  except (TypeError,ValueError):return False
 def _set_heater(self,head,target):
  ext=self.ace.printer.lookup_object('extruder' if head==0 else 'extruder%d'%head)
  heaters=self.ace.printer.lookup_object('heaters',None)
  if heaters is None:raise ProductionError('Klipper heater control is unavailable')
  heaters.set_temperature(ext.get_heater(),target,False)
  self.heater_scope=(self.token,ext.get_heater())
 def _movement_ready(self,head):
  return self.thermal.assess(head)['ready'] if self.controlled_heat else self._klipper_extrusion_ready(head)
 def begin_phase(self,phase,token,source,head,profile,deadline):
  boundary=getattr(self.ace,'_flexi_spool_boundary',None)
  if boundary:boundary(head)
  if self.token!=token:
   self.retracted=0.;self.saw_present=False;self.saw_clear=False;self.saved=False;self.heat_target=None;self.heater_scope=None
  self.phase=phase;self.token=token;self.source=tuple(source);self.head=head;self.profile=copy.deepcopy(profile);self.deadline=deadline
  witness=self.c.snapshot().get('runout_witness',{}).get(str(head))
  if witness and witness['source']==list(source):self.saw_present=True
  self.pending=None;self.path=None;self.moves=[];self.phase_error=None;self.started=self.clock();self.last_sample=self.started
  self._check()
  workflow=self.c.snapshot().get('full_workflow') or {}
  stats=self.ace.printer.lookup_object('print_stats',None)
  print_state=stats.get_status(0).get('state') if stats is not None else None
  self.controlled_heat=workflow.get('calibration_test') is True and print_state not in ('printing','paused')
  if phase=='select':
   if not self._selected():
    # Q is a numeric traditional-G-code parameter; the engine accepts it
    # only for this exact authorized workflow token/head/selection phase.
    self.ace.gcode.run_script_from_command('T%d A0 Q%d'%(head,int(token,16)))
  elif phase in ('heat_load','heat_unload'):
   # The native loading workflow owns physical tool selection and X/Y homing.
   # With [homing_xyz_override], stock T has already homed X/Y. If a different
   # configuration is installed, use only the same G28 X Y that FEED_AUTO uses.
   if not self._selected():
    raise ProductionError('Native physical tool selection is required before heating')
   self._ensure_xy_homed()
   if not self._selected() or not self._xy_homed():
    raise ProductionError('Native tool selection did not establish X/Y homing')
   workflow=self.c.snapshot().get('full_workflow') or {}
   if phase=='heat_load' and workflow.get('token')==token and workflow.get('action')=='load':
    self._extend_load_idle_timeout(token,deadline)
   target,limits=self._effective_temperature(head,phase)
   self.heat_target=target
   if self.controlled_heat:self.thermal.set_profile(head,target,verified_range=limits)
   # Ordinary operations own the heater for this bounded phase, exactly as
   # the stock FEED_AUTO M104/M109 sequence does. No manual preheat is needed.
   self._set_heater(head,target)
  elif phase=='feed_path':
   if not self._movement_ready(head):
    raise ProductionError('Physical head/thermal prerequisites failed before feeding')
   s,d=self._check();op=self.c.snapshot()['operations'][token]
   job={'token':token,'ace':source[0],'slot':source[1],'head':head,'operation':'load','identity':d['identity'],
        'source_identity':d['source_identities'][source[1]],'binding':self.provider._binding(s,head,ready=False),
        'source_binding':self.provider.source_binding(s,head),'epoch':s['epoch'],'started':self.clock(),
        'deadline':deadline,'max_distance_mm':profile['load_limit_mm']}
   workflow=self.c.snapshot().get('full_workflow') or {}
   material=d.get('effective_materials',{}).get(source[1]) or d.get('materials',{}).get(source[1])
   # The live transport may lack RFID material; use the workflow inventory
   # only through the scoped production service when deriving this allowance.
   if not material:
    inventory=getattr(getattr(self.ace,'_flexi_production',None),'inventory',None)
    if callable(inventory):material=inventory().get(tuple(source),{}).get('material')
   calibrated=self._feed_speed_validated(source,head,profile,workflow.get('action','load'))
   legacy_speed=self._allow_legacy_direct_load_speed(workflow,material)
   ordinary=self._allow_continuous_load(workflow,material)
   lease=getattr(getattr(self.ace,'_flexi_production',None),'lease',None) or {}
   continuous=(legacy_speed or ordinary) and not lease.get('wizard_id') and profile['load_limit_mm']<=2100
   self.path=ACETransport(self.ace,self.c,step_limit=(lambda length,speed:self.step_limit(token,phase,length,speed)) if self.step_limit else None,authorize=lambda j:self.authorize(token,phase,tuple(source),head),clock=self.clock,step_mm=5,speed=profile['feed_speed'],validated_speed=calibrated or legacy_speed or ordinary,continuous_load=continuous,motion_ready=self._movement_ready,material_allowed=lambda m:not flexible(m) or self.material_permission(tuple(source),m,phase))
   self.path.start(job,profile['load_limit_mm'],deadline-job['started'])
  elif phase in ('seat','prime','purge','tipform','retract_path'):
   if not self._movement_ready(head):raise ProductionError('Klipper does not permit extrusion on the selected tool')
   if phase in ('seat','prime','purge'):
    if phase in ('seat','prime'):self.ace.gcode.run_script_from_command('MOVE_TO_DISCARD_FILAMENT_POSITION')
    self.samples=[self._coil()]
    self.moves=[[profile[{'seat':'seat_mm','prime':'prime_mm','purge':'purge_mm'}[phase]],profile['extrude_speed']]]
   elif phase=='tipform':self.moves=copy.deepcopy(profile['tip_moves'])
   else:self.moves=[[-profile['retract_mm'],profile['retract_speed']]]
   if phase!='retract_path':
    self.ace.gcode.run_script_from_command('SAVE_GCODE_STATE NAME=FLEXI_'+token)
    self.ace.gcode.run_script_from_command('M83');self.saved=True
  elif phase=='verify_flow':pass
  elif phase=='verify_clear':pass
  else:raise ProductionError('Unsupported native phase')
 def _receipt(self,**extra):return dict(token=self.token,source=list(self.source),head=self.head,phase=self.phase,done=True,stopped=True,**extra)
 def _restore(self):
  if self.saved:self.ace.gcode.run_script_from_command('RESTORE_GCODE_STATE NAME=FLEXI_'+self.token);self.saved=False
 def poll_phase(self,token):
  if token!=self.token:raise ProductionError('Stale native phase token')
  s,d=self._check();base={'token':token,'phase':self.phase,'head':self.head,'source':list(self.source),'done':False}
  if self.phase=='select':
   if not self._selected():raise ProductionError('Native physical tool selection failed')
   return self._receipt()
  if self.phase in ('heat_load','heat_unload'):
   if self.controlled_heat:ready=self._movement_ready(self.head)
   else:ready=self.heat_target is not None and self._target_ready(self.head,self.heat_target)
   return self._receipt() if ready else base
  if self.phase=='feed_path':
   if not self._movement_ready(self.head):
    raise ProductionError('Physical head/thermal prerequisites lost during feeding')
   r=self.path.poll(token)
   if r.get('error'):raise ProductionError(r['error'])
   if not r['done']:return base
   if s['heads'][self.head]['detected'] is not True:
    raise ProductionError('Bounded feed ended without toolhead filament arrival')
   return self._receipt(arrival_confirmed=True)
  if self.phase=='verify_flow':
   if self.clock()-self.last_sample>=self.profile['flow_interval_s']:
    self.samples.append(self._coil());self.last_sample=self.clock()
   if len(self.samples)<self.profile['flow_samples']:return base
   return self._receipt(flow_delta=max(self.samples)-min(self.samples),sensor_present=s['heads'][self.head]['detected'])
  if self.phase=='verify_clear':
   action=(self.c.snapshot().get('full_workflow') or {}).get('action','unload')
   valid=bool(self.calibrations and any(self.calibrations.valid(self.source,fp,self.kind,action) for fp in (movement_digest(self.profile),digest(self.profile))))
   if not self.saw_clear or self.retracted+1e-8<self.profile['retract_mm']:raise ProductionError('Retraction lacks sensor transition or full calibrated travel')
   return self._receipt(sensor_clear=s['heads'][self.head]['detected'] is False,recipe_verified=valid)
  if not self._movement_ready(self.head):raise ProductionError('Klipper extrusion permission was lost')
  if self.pending:
   reply=self.pending['reply']
   if reply.get('error'):raise ProductionError(reply['error'])
   if reply.get('accepted') is not True:return base
   if self.pending.get('extruder_step') is not None and not self.pending.get('extruder_queued'):
    self._check()
    if not self._movement_ready(self.head):raise ProductionError('Klipper extrusion permission changed after ACE acknowledgment')
    self.ace.gcode.run_script_from_command('G1 E%.6f F%.6f'%(self.pending['extruder_step'],self.pending['speed']*60))
    self.pending['extruder_queued']=True;self.pending['ready_after']=max(self.pending['ready_after'],self.clock()+abs(self.pending['extruder_step'])/self.pending['speed']+.1)
   if d.get('ready') is not True or d['observed']<self.pending['ready_after']:return base
   self.ace.toolhead.wait_moves()
   if self.phase in ('seat','prime','purge'):self.samples.append(self._coil())
   self.pending=None
  if not self.moves:
   self._restore();return self._receipt()
  mm,speed=self.moves[0];limit=5. if self.phase=='retract_path' else 1.;step=math.copysign(min(abs(mm),limit),mm)
  if self.clock()+abs(step)/speed+1.>=self.deadline:raise ProductionError('No deadline budget remains for a bounded native step')
  if self.step_limit is not None:
   allowed=self.step_limit(token,self.phase,abs(step),speed)
   if allowed is None:return base
   if type(allowed) not in (int,float) or not math.isfinite(allowed) or not 0<allowed<=abs(step):raise ProductionError('Invalid calibration step budget')
   step=math.copysign(allowed,step)
  method='feed_filament' if step>0 else 'unwind_filament';reply=self._send(method,abs(step),speed)
  self.pending={'reply':reply,'ready_after':self.clock()+abs(step)/speed+.5}
  if self.phase!='retract_path':self.pending.update(extruder_step=step,speed=speed,extruder_queued=False)
  else:self.retracted+=abs(step)
  remaining=mm-step
  if abs(remaining)<1e-8:self.moves.pop(0)
  else:self.moves[0][0]=remaining
  return base
 def stop(self,token):
  if token!=self.token:return False
  if self.path is not None:return self.path.stop(token)
  # Never dispatch a new E/unwind step on cancellation. Pending short native
  # E/motor steps must drain and provide fresh ready acknowledgment.
  self.moves=[]
  try:
   s,d=self._check(braking=True)
   if self.pending and (d.get('ready') is not True or d['observed']<self.pending['ready_after'] or self.pending['reply'].get('accepted') is not True):return False
   self.ace.toolhead.wait_moves();self._restore();return True
  except Exception:return False

 def calibration_step_ready(self):
  if self.pending is not None:return False
  if self.path is not None and self.path.step is not None:
   try:
    sample,device=self.path._bound(self.path.job)
    return self.path._stopped(sample,device) and not self.path.cancelled and sample['heads'][self.head]['detected'] is False
   except Exception:return False
  return True

 def _print_check(self,token,source,head,braking=False):
  if not braking and (self.authorize is None or self.authorize(token,'print',tuple(source),head) is not True):raise ProductionError('Print adapter authorization missing')
  hold=self.c.snapshot()['printing'].get(str(source[0]))
  if not hold or hold['head']!=head or hold['slot']!=source[1]:raise ProductionError('Print adapter reservation mismatch')
  s=self.provider.read();device=self.provider.device(s,source[0],ready=False)
  expected=getattr(self,'_print_identity',None)
  if expected is not None and device['identity']!=expected:raise ProductionError('Printing ACE connection changed')
  if not braking:
   self.provider.sensor(s,head,True)
   binding=getattr(self,'_follow_binding',None)
   if binding is not None and self.provider.source_binding(s,head)!=binding:raise ProductionError('Shared printing route changed')
  if not braking and not self._klipper_extrusion_ready(head):raise ProductionError('Klipper does not permit extrusion on the selected print tool')
  return s
 def start_follow(self,token,source,head):
  self.source=tuple(source);self.head=head;self._print_identity=None;self._follow_binding=None
  sample=self._print_check(token,source,head);self._print_identity=self.provider.device(sample,source[0],ready=False)['identity'];self._follow_binding=self.provider.source_binding(sample,head)
  sent=self.clock()
  response=self.ace._tipform_send(source[0],dict({'method':'start_feed_assist','params':{'index':source[1]}},**({'_flexi_token':token} if getattr(self.ace,'_flexi_tagged_dispatch',False) else {})),timeout=2.)
  if self.ace._tipform_rejected(response):return False
  profile=self.c.routes.destination(*source)
  window=getattr(self,'profile',{}).get('feed_assist_timeout_s') or 5.
  deadline=self.clock()+window
  while self.clock()<deadline:
   self._print_check(token,source,head)
   info=self.ace._info_per_ace.get(source[0]) or {}
   if self.kind=='original_ace_pro':
    sample=self._print_check(token,source,head);d=self.provider.device(sample,source[0],ready=False)
    if d['ready'] is True and d['observed']>sent:
     self._follow_ack={'token':token,'source':tuple(source),'head':head,'basis':'accepted_command+fresh_ready+physically_validated_recipe'};return True
   elif any(slot.get('index')==source[1] and slot.get('slot_status')=='assisting' for slot in info.get('slots',[])):return True
   self.ace.reactor.pause(self.ace.reactor.monotonic()+.1)
  return False
 def stop_follow(self,token,source,head):
  self._print_check(token,source,head,braking=True)
  sent=self.clock()
  response=self.ace._tipform_send(source[0],dict({'method':'stop_feed_assist','params':{'index':source[1]}},**({'_flexi_token':token} if getattr(self.ace,'_flexi_tagged_dispatch',False) else {})),timeout=2.)
  if self.ace._tipform_rejected(response):return False
  profile=self.c.routes.destination(*source)
  window=getattr(self,'profile',{}).get('feed_assist_timeout_s') or 5.
  deadline=self.clock()+window
  while self.clock()<deadline:
   s=self._print_check(token,source,head,braking=True)
   d=self.provider.device(s,source[0],ready=False)
   info=self.ace._info_per_ace.get(source[0]) or {}
   if d['ready'] is True and d['observed']>sent and (self.kind=='original_ace_pro' or any(slot.get('index')==source[1] and slot.get('slot_status')!='assisting' for slot in info.get('slots',[]))):self._follow_ack=None;return True
   self.ace.reactor.pause(self.ace.reactor.monotonic()+.1)
  return False
 def verify_follow(self,token,source,head):
  self._print_check(token,source,head)
  if self.kind=='original_ace_pro':
   ack=getattr(self,'_follow_ack',None)
   return bool(ack and ack['token']==token and ack['source']==tuple(source) and ack['head']==head)
  return any(slot.get('index')==source[1] and slot.get('slot_status')=='assisting' for slot in (self.ace._info_per_ace.get(source[0]) or {}).get('slots',[]))

class ACE2ProAdapter(NativeU1Adapter):
 """Gen2 adapter backed by the already-tested V2 command translator.

 The V2 protocol supplies the command set, UID identity, slot-status truth,
 decoder and feed-assist semantics. Support is admitted from those
 capabilities, not from an optimistic constant: a path without a detected V2
 protocol is refused before any phase can dispatch. Hardware validation
 remains a separate, explicit release gate; it must never be inferred from
 the existence of this class or from simulated protocol tests.
 """
 kind='ace2_pro'
 hardware_validation_required=True

 def _caps(self):
  source=getattr(self,'source',None)
  if not source:return {}
  protocol=getattr(self.ace,'_protocols',{}).get(source[0])
  return getattr(protocol,'CAPS_STATIC',{}) or {}

 def is_supported(self):
  caps=self._caps()
  return (caps.get('v2_command_set') is True
          and caps.get('has_stable_id') is True
          and caps.get('has_slot_status_truth') is True)

 def _require_supported(self):
  if not self.is_supported():
   raise ProductionError('ACE 2 Pro protocol/capabilities are not available for this source; hardware validation has not been completed')

 # Braking remains available even if capability discovery is incomplete: a
 # stop must never be refused by the adapter gate. The transport itself still
 # bounds and verifies every stop.
 def stop(self,*args,**kwargs):
  if not self.is_supported():return False
  return super().stop(*args,**kwargs)

 def stop_follow(self,*args,**kwargs):
  if not self.is_supported():return False
  return super().stop_follow(*args,**kwargs)

 def begin_phase(self,*args,**kwargs):
  self._require_supported()
  return super().begin_phase(*args,**kwargs)

 def start_follow(self,*args,**kwargs):
  self._require_supported()
  return super().start_follow(*args,**kwargs)

 def verify_follow(self,*args,**kwargs):
  return self.is_supported() and super().verify_follow(*args,**kwargs)
