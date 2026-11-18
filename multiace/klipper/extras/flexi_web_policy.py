# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict Flexi web payloads. No I/O, hardware imports, or default grants."""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import json,math,re
try:
 from .ace_flexi import Routes
 from .ace_flexi_production import FullProfiles,ProductionError
except ImportError:
 from ace_flexi import Routes
 from ace_flexi_production import FullProfiles,ProductionError
RC_RELEASE='multiace-1.30b-flexi-s2.36-20261118'
ATTEST_CONFIRMATION='ALL PATHS CLEAR'
ACTIONS={'load','unload','switch','failover','print','recover','assist'}
def source(value,routes):
 if not isinstance(value,list) or len(value)!=2 or any(type(n) is not int for n in value):raise ProductionError('Source requires two zero-based integers')
 routes.destination(*value);return value
def actions(value):
 if not isinstance(value,list) or not value or any(type(a) is not str or a not in ACTIONS for a in value) or len(value)!=len(set(value)):raise ProductionError('Invalid explicit action scope')
 return value
def configuration(value,routes):
 if not isinstance(value,dict) or set(value)-{'profiles','backups','selection','device_adapters','source_select','settings_mode'}:raise ProductionError('Unknown configuration field')
 if value.get('settings_mode','standard') not in ('standard','custom'):raise ProductionError('Invalid Flexi settings mode')
 if 'source_select' in value:
  try:from .ace_flexi_sources import SourceSelect
  except ImportError:from ace_flexi_sources import SourceSelect
  SourceSelect.validate(value['source_select'])
 FullProfiles(routes,value.get('profiles',{}))
 backups=value.get('backups',[]);selection=value.get('selection',{});adapters=value.get('device_adapters',{})
 if not isinstance(backups,list) or any(type(k) is not str or k not in routes.as_dict() for k in backups) or len(set(backups))!=len(backups):raise ProductionError('Invalid backup source keys')
 if not isinstance(selection,dict) or any(k not in ('0','1','2','3') or type(v) is not str or routes.as_dict().get(v)!=int(k) for k,v in selection.items()):raise ProductionError('Selection must match physical routes')
 if not isinstance(adapters,dict) or any(k not in tuple(str(i) for i in range(4)) or v not in ('original_ace_pro','ace2_pro') for k,v in adapters.items()):raise ProductionError('Invalid device adapter')
 return value
def recipe_save(value, routes):
 if not isinstance(value, dict) or set(value) != {'base_revision', 'config'}:
  raise ProductionError('Recipe saves require the draft revision and configuration')
 revision = value['base_revision']
 if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
  raise ProductionError('Invalid recipe revision. Reload saved recipes before editing.')
 configuration(value['config'], routes)
 return value

def validate(kind,value,routes):
 if not isinstance(value,dict):raise ProductionError('Expected an object')
 if kind=='wizard':
  try:from .ace_flexi_calibration import validate_payload
  except ImportError:from ace_flexi_calibration import validate_payload
  return validate_payload(value,routes)
 if kind=='config':return recipe_save(value,routes)
 if kind=='request':
  action=value.get('action')
  if action in ('source_select','source_restore'):
   try:from .ace_flexi_sources import SourceSelect
   except ImportError:from ace_flexi_sources import SourceSelect
   required={'action','head','record','confirmation'} if action=='source_select' else {'action','head','confirmation'}
   if set(value)!=required or value.get('confirmation')!='CHANGE_SOURCE_WITHOUT_MOVEMENT':raise ProductionError('Explicit source-only confirmation required')
   SourceSelect.head(value.get('head'))
   if action=='source_select' and SourceSelect.record(value['record'])['type']!='manual':raise ProductionError('Temporary overrides support manual/external spools only')
   return value
  fields={'attest_clear':{'head','confirmation'},'isolate_failed_stop':{'source','head','confirmation','recovery_token'},'resume_loading':{'source','head','confirmation','recovery_token','test'},'cancel':set(),'end_print':set(),'inspect_clear':{'source','head','confirmation','recovery_token'},'same_source_in_tube':{'source','head','confirmation','recovery_token'},'trace_loaded':{'source','head','confirmation','recovery_token','rebind'},'confirm_clearance':{'confirmation'},'prepare':{'source','operation'},'risk':{'source','actions','confirmation','seconds','scope'},'plan':{'source','head','operation'}}
  allowed=fields.get(action,{'source','head','test'} if action in ACTIONS-{'print'} else None)
  if allowed is None or set(value)-allowed-{'action'}:raise ProductionError('Unsupported action fields')
  if 'source' in allowed:source(value.get('source'),routes)
  if 'head' in allowed:
   if type(value.get('head')) is not int:raise ProductionError('Head must be an integer')
   if action not in ('attest_clear',):routes.require_destination(*value['source'],value['head'])
   elif value['head'] not in range(4) or not routes.sources(value['head']):raise ProductionError('No configured path for this head')
  if action=='attest_clear' and value.get('confirmation')!=ATTEST_CONFIRMATION:raise ProductionError('Explicit inspection of every required path is required')
  if 'operation' in allowed and value.get('operation') not in ACTIONS-{'print'}:raise ProductionError('Invalid planned operation')
  if 'test' in value and type(value['test']) is not bool:raise ProductionError('Test flag must be boolean')
  if action=='risk':
   if value.get('scope')=='path':
    if 'actions' in value:raise ProductionError('Path consent covers all operations; action subset is not accepted')
   elif 'scope' in value:raise ProductionError('Unknown risk consent scope')
   else:actions(value.get('actions'))
   if value.get('confirmation')!='TPU_TPE_AT_OWN_RISK':raise ProductionError('Explicit flexible-filament risk confirmation required')
  if action in ('inspect_clear','trace_loaded') and value.get('confirmation')!=('ALL_SHARED_PATHS_INSPECTED_CLEAR' if action=='inspect_clear' else 'SOURCE_TRACED_AND_STOPPED'):raise ProductionError('Explicit physical recovery inspection required')
  if action=='same_source_in_tube' and value.get('confirmation')!='FILAMENT_OF_SOURCE_IN_TUBE':raise ProductionError('Confirm that the filament of this source is still in the tube')
  if action=='isolate_failed_stop' and (not value.get('recovery_token') or value.get('confirmation')!='AFFECTED_FILAMENT_PATH_PHYSICALLY_ISOLATED'):raise ProductionError('Explicit physical isolation is required')
  if action=='resume_loading' and (not value.get('recovery_token') or value.get('confirmation')!='RESUME_STOPPED_SENSOR_ARRIVAL'):raise ProductionError('Explicit stopped-arrival recovery confirmation required')
  if 'rebind' in value and (value['rebind'] is not True or not value.get('recovery_token')):raise ProductionError('Explicit inspected restoration requires a current recovery record')
  if 'recovery_token' in value and (not isinstance(value['recovery_token'],str) or not re.fullmatch(r'[a-zA-Z0-9:_-]{1,128}',value['recovery_token'])):raise ProductionError('Invalid recovery record reference')
  if action=='confirm_clearance' and value.get('confirmation')!='INSPECTED_FULL_SHARED_PATH_CLEAR':raise ProductionError('Explicit path inspection confirmation required')
 elif kind=='arm':
  if set(value)-{'sources','actions','seconds','confirmation','test','wizard_id'}:raise ProductionError('Unknown authorization field')
  if 'wizard_id' in value and (value.get('test') is not True or not isinstance(value['wizard_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',value['wizard_id'])):raise ProductionError('Wizard authorization requires a valid controlled-test session ID')
  if not isinstance(value.get('sources'),list) or not 1<=len(value['sources'])<=32:raise ProductionError('Explicit sources required')
  for s in value['sources']:source(s,routes)
  actions(value.get('actions'))
  if type(value.get('test',False)) is not bool:raise ProductionError('Test flag must be boolean')
  if value.get('test') and 'print' in value['actions']:raise ProductionError('Calibration authorization cannot permit printing')
  expected='ENABLE_CONTROLLED_FLEXI_TEST' if value.get('test') else 'ENABLE_VALIDATED_FLEXI_SESSION'
  if value.get('confirmation')!=expected:raise ProductionError('Explicit session confirmation required')
 elif kind=='validate':
  if set(value)!={'source','actions','observations','confirmation'}:raise ProductionError('Invalid validation fields')
  source(value['source'],routes);actions(value['actions'])
  if value['confirmation']!='PHYSICALLY_VERIFIED_RECIPE' or not isinstance(value['observations'],dict):raise ProductionError('Explicit physical observations required')
  if set(value['observations'])-{'nozzle_flow','full_path_clear','correct_head','feed_assist'} or any(type(v) is not bool for v in value['observations'].values()):raise ProductionError('Observations must be boolean')
 else:raise ProductionError('Unsupported web operation')
 if 'seconds' in value and (type(value['seconds']) not in (int,float) or not math.isfinite(value['seconds']) or not 0<value['seconds']<=43200):raise ProductionError('Invalid lifetime')
 return value
def command(kind,value,routes):
 validate(kind,value,routes)
 data=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode().hex()
 if len(data)>98304:raise ProductionError('Payload is oversized')
 return 'ACE_FLEXI_PRODUCTION_'+{'config':'CONFIG','request':'REQUEST','arm':'ARM','validate':'VALIDATE','wizard':'CALIBRATION'}[kind]+' DATA='+data

# Existing spool table operations reviewed as metadata-only for ACE-slot scope.
METADATA_COMMANDS=frozenset(('ACE_SPOOL_ADD','ACE_SPOOL_SET','ACE_SPOOL_ASSIGN','ACE_SPOOL_LIST','MULTIACE_REFRESH_OVERRIDES'))
def slot_metadata_command(payload,routes):
 if not isinstance(payload,dict) or set(payload)!={'source','record'}:raise ProductionError('Expected source and record only')
 source(payload['source'],routes);record=payload['record']
 if record is not None:
  if not isinstance(record,dict) or set(record)-{'material','brand','subtype','color'}:raise ProductionError('Invalid slot identity fields')
  if any(not isinstance(v,str) or len(v)>128 or any(ord(c)<32 for c in v) for v in record.values()):raise ProductionError('Invalid slot identity text')
  if record.get('color') and not re.fullmatch(r'#?[0-9a-fA-F]{6}',record['color']):raise ProductionError('Color requires six hexadecimal digits')
 return 'ACE_FLEXI_SLOT_METADATA DATA='+json.dumps(payload,separators=(',',':')).encode().hex()

# Maintenance permission is checked again by the native command wrapper.
FLEXI_MAINTENANCE_COMMANDS = frozenset(('ACE_DRY', 'ACE_START_DRYING', 'ACE_STOP_DRYING', 'ACE_TAG_READ'))
# Session 2: Flexi runs the original MultiACE filament operations. Klipper
# enforces routes, the shared-path ledger, recovery and TPU consent for them.
FLEXI_NATIVE_COMMANDS = frozenset((
 'ACE_LOAD_HEAD', 'ACE_UNLOAD_HEAD', 'ACE_UNLOAD_ALL_HEADS', 'ACE_UNLOAD_ALL_CANCEL',
 'ACE_SWAP_HEAD', 'ACE_FAILOVER_PRIORITY', 'ACE_FAILOVER_ALLOW_COLOR', 'ACE_SPOOL_PA',
 'ACE_SPOOL_DELETE', 'ACE_PA_CALIBRATE', 'MULTIACE_SET_LANGUAGE'))
FLEXI_BLOCKED_SETTERS = frozenset(('ACE_SET_HEAD_MANUAL', 'ACE_SET_HEAD_FEEDER', 'ACE_SET_HEAD_ACE', 'ACE_SET_FLEXI_ROUTES'))
FLEXI_WEB_COMMANDS = METADATA_COMMANDS | FLEXI_MAINTENANCE_COMMANDS | FLEXI_NATIVE_COMMANDS | frozenset((
 'SET_ACE_MODE', 'ACE_RUN_MODE_SWITCH', 'ACE_FLEXI_STATUS', 'ACE_HEAD_STATUS', 'ACE_LIST'))

def validate_web_script(script):
 for line in script.splitlines():
  line = line.split(';', 1)[0].strip()
  if not line:
   continue
  name=line.split()[0].upper()
  if name.startswith('ACE_SET_') and name not in FLEXI_BLOCKED_SETTERS:
   continue  # no-motion MultiACE settings
  if name not in FLEXI_WEB_COMMANDS:
   raise ValueError('Command unavailable in Flexi; use the routed operation controls')


def discovery_command(payload):
    """G-code for one route-discovery web action. Strictly typed: start needs
    the explicit movement confirmation; numbers are range-checked again in
    Klipper. Returns the script string."""
    if not isinstance(payload, dict):
        raise ProductionError('Expected a discovery action')
    action = payload.get('action')
    allowed = {'start': {'action', 'confirmation', 'ace', 'slot', 'speed', 'max_mm', 'margin_mm'},
               'stop': {'action'}, 'acknowledge': {'action'}, 'clear': {'action'}}
    if action not in allowed or set(payload) - allowed[action]:
        raise ProductionError('Unknown discovery action or field')
    if action != 'start':
        return 'ACE_FLEXI_DISCOVER ACTION=%s' % action.upper()
    if payload.get('confirmation') != 'DISCOVER_ROUTES':
        raise ProductionError('Route discovery moves filament; confirm it explicitly')
    parts = ['ACE_FLEXI_DISCOVER ACTION=START CONFIRM=DISCOVER_ROUTES']
    for key, name, low, high in (('ace', 'ACE', 0, 15), ('slot', 'SLOT', 0, 3)):
        if payload.get(key) is not None:
            v = payload[key]
            if type(v) is not int or not low <= v <= high:
                raise ProductionError('Invalid %s' % key)
            parts.append('%s=%d' % (name, v))
    if payload.get('slot') is not None and payload.get('ace') is None:
        raise ProductionError('A slot needs its ACE')
    for key, name, low, high in (('speed', 'SPEED', 5, 50), ('max_mm', 'MAX', 300, 4000), ('margin_mm', 'MARGIN', 10, 200)):
        if payload.get(key) is not None:
            v = payload[key]
            if type(v) not in (int, float) or not low <= v <= high:
                raise ProductionError('%s must be between %d and %d' % (key, low, high))
            parts.append('%s=%g' % (name, v))
    return ' '.join(parts)

