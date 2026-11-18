# SPDX-License-Identifier: GPL-3.0-or-later
"""Experimental native Flexi mode, with filament execution deliberately locked."""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import json
import os
import hashlib
import copy
import logging
import time
import uuid
from pathlib import Path
from contextlib import nullcontext
from .ace_flexi import Routes, RouteError, decode_command, decode_route_update, routing_document
from .ace_flexi_interlocks import Coordinator, InterlockError, AtomicJournal
from .ace_flexi_evidence import LiveEvidenceProvider, EvidenceError
from .ace_flexi_executor import LiveCoordinator
from .ace_flexi_thermal import ThermalGuard
from .ace_flexi_controlled_test import ControlledTest
from .ace_flexi_diagnostics import SensorMonitor
from .ace_flexi_pathchek_native import NativePathChekAdapter
from .ace_flexi_pathchek_session import NativePathChekSession
from .ace_flexi_pathchek_policy import validate as validate_pathchek
from .ace_flexi_profiles import TestProfiles, ProfileError
from .ace_flexi_service import ProductionService
from .ace_flexi_production import ProductionError, flexible, digest
from .ace_flexi_paths import PathLedger, PathLedgerError
from .ace_flexi_discovery import RouteDiscovery, DiscoveryError, CONFIRMATION as DISCOVERY_CONFIRMATION
from .ace_protocol import GATE_AVAILABLE
from .flexi_web_policy import FLEXI_MAINTENANCE_COMMANDS, METADATA_COMMANDS,slot_metadata_command,RC_RELEASE,recipe_save


# Verified stop-only requests. Opcode 9 (STOP_FEED_OR_ROLLBACK) carries a slot
# index and nothing else - no length, speed or mode - so a brake can only reduce
# hazard and cannot be turned into movement. Module-level so it is reachable from
# any host object, including the bare fixtures used by the offline tests.
# unwind_filament is NOT here: opcode 8 mode=ROLLBACK is movement.
VERIFIED_BRAKES = frozenset(('stop_feed_filament','stop_feed_assist'))

# Every ACE request method the Klipper code sends is in exactly one of these
# (tests/test_motor_method_classification.py enforces it, so a new upstream
# method cannot slip past the Flexi motor gate unclassified).
# Motion: gated in Flexi by send_request_to -> _flexi_motor_request_allowed.
FLEXI_MOTION_METHODS = frozenset((
    'feed_filament', 'unwind_filament', 'start_feed_assist', 'stop_feed_assist',
    'stop_feed_filament', 'stop_unwind_filament', 'update_feeding_speed',
    # ACE Pro 2 opcode 8: mode 0/2 feed, mode 1/3 rollback. Stock uses it for
    # the tip-form rollback assist; gated by direction like feed/unwind.
    'feed_or_rollback_raw',
    # ACE Pro 2 raw frame (A_RAW): can encode any opcode, never allowed in Flexi.
    'raw',
    # ACE Pro 2 debug opcodes that can turn spools (A_DRY AUTO_ROLL=1,
    # A_RFID_TEST): treated as motion, so blocked in Flexi.
    'drying_raw', 'rfid_test'))
# No filament motion (status, drying, LEDs, tag reads; the ACE Pro 2 tag read
# that turns the spool is refused at command level in Flexi: ACE_TAG_READ).
FLEXI_NON_MOTION_METHODS = frozenset((
    'get_info', 'get_status', 'get_feed_info', 'get_filament_info',
    'discover_device', 'drying', 'drying_stop', 'flash_led', 'set_feed_check',
    'set_valve', 'set_fan_speed', 'filament_identify', 'filament_recognition',
    'get_temp', 'get_key_state', 'set_rfid_enable', 'set_dry_temp', 'set_fan_raw',
    'assign_device_id'))
# Manual ACE Pro 2 debug commands that drive a lane directly, bypassing routes
# and the shared-tube record. Refused in Flexi (Load/Unload/Find routes instead).
FLEXI_RAW_MOTOR_COMMANDS = frozenset(('A_FEED', 'A_ROLLBACK', 'A_SPEED', 'A_RAW',
                                      'A_DRY', 'A_RFID_TEST'))


class FlexiMixin:
    _FLEXI_SAFE_COMMANDS = frozenset((
        'SET_ACE_MODE', 'ACE_RUN_MODE_SWITCH', 'ACE_SET_FLEXI_ROUTES',
        'ACE_FLEXI_STATUS', 'ACE_FLEXI_PATHCHEK', 'ACE_HEAD_STATUS', 'ACE_LIST', 'ACE_FLEXI_SLOT_METADATA',
        'ACE_FLEXI_TEST_PREPARE', 'ACE_FLEXI_TEST_CONFIRM_CLEAR',
        'ACE_FLEXI_TEST_ARM', 'ACE_FLEXI_TEST_RUN',
        'ACE_FLEXI_TEST_CANCEL', 'ACE_FLEXI_TEST_RECOVER',
        'ACE_FLEXI_PRODUCTION_CONFIG','ACE_FLEXI_PRODUCTION_REQUEST','ACE_FLEXI_PROFILE','ACE_FLEXI_DISCOVER',
        'ACE_FLEXI_PRODUCTION_ARM','ACE_FLEXI_PRODUCTION_VALIDATE','ACE_FLEXI_PRODUCTION_CALIBRATION',
    ))
    _FLEXI_RAW_MOTOR_COMMANDS = FLEXI_RAW_MOTOR_COMMANDS
    _FLEXI_HEAD_CONFIG_COMMANDS = frozenset((
        'ACE_SET_HEAD_MANUAL', 'ACE_SET_HEAD_FEEDER', 'ACE_SET_HEAD_ACE',
    ))

    def _flexi_init(self, config):
        try:
            self._flexi_sensor_monitor = SensorMonitor()
            self._flexi_routes = Routes(config.get('flexi_routes', '{}'), config.get('flexi_slot_status', '{}'),config.get('flexi_topology','{}'))
            self._flexi_routes.validate_topology()
            self._flexi_test_profiles_raw = config.get('flexi_test_profiles','{}')
            self._flexi_production_raw = config.get('flexi_production','{}')
            self._flexi_tagged_dispatch = True
            self._flexi_epoch = uuid.uuid4().hex
            self._flexi_connection_generations = {}
            self._flexi_info_observed = {}
            self._flexi_journal_path = config.get('flexi_journal_path','/oem/printer_data/config/extended/multiace/flexi_execution_state.json')
            self._flexi_init_error=None
            ledger_path = config.get('flexi_path_ledger', str(Path(self._flexi_journal_path).with_name('flexi_path_ledger.json')))
            self._flexi_paths = PathLedger(ledger_path, clock=time.time)
            self._flexi_discovery_scope = None
            self._flexi_discovery_abort = False
            self._flexi_discovery = RouteDiscovery(
                _DiscoveryIO(self), config.get('flexi_route_discovery', str(Path(self._flexi_journal_path).with_name('flexi_route_discovery.json'))),
                clock=time.time)
            self._flexi_native_pending = {}
            try:
                self._flexi_reset_evidence()
            except (InterlockError,OSError) as exc:
                # Preserve the failed journal. This coordinator cannot dispatch.
                self.__dict__.update(self._flexi_build_evidence(self._flexi_routes,ignore_journal=True))
                self._flexi_init_error=str(exc)
                logging.error('[multiACE][flexi] Journal unavailable; Flexi disabled: %s',exc)
        except (RouteError, ProfileError, ProductionError, EvidenceError) as exc:
            raise config.error('Flexi routes/evidence initialization failed: ' + str(exc))

    def _flexi_build_evidence(self, routes, require_no_journal=False, journal_path=None, ignore_journal=False):
        # Construct and validate every replacement before persisting routes.
        profiles = TestProfiles(routes, getattr(self, '_flexi_test_profiles_raw', '{}'))
        production = ProductionService(self, routes, getattr(self, '_flexi_production_raw', '{}'))
        thermal = ThermalGuard(self.printer)
        provider = LiveEvidenceProvider(routes, self._flexi_collect_live)
        journal_path = Path(journal_path or self._flexi_journal_path)
        exists = False
        if not ignore_journal:
            try:
                journal_path.lstat()
                exists = True
            except FileNotFoundError:
                pass
        if require_no_journal and exists:
            raise RouteError('Existing execution journal requires explicit migration')
        journal = AtomicJournal(journal_path) if exists else None
        coordinator = LiveCoordinator(routes, provider, journal=journal, max_age=5.)
        return dict(_flexi_pathchek=None, _flexi_routes=routes, _flexi_test_controller=None, _flexi_test_profiles=profiles,
                    _flexi_thermal=thermal, _flexi_evidence=provider,
                    _flexi_coordinator=coordinator, _flexi_production=production)

    def _flexi_reset_evidence(self):
        self.__dict__.update(self._flexi_build_evidence(self._flexi_routes))

    @staticmethod
    def _flexi_v1_signature(previous, current):
        if previous is None or previous[0]!=current[0]:return current
        missing=lambda value: value in (None,'',[],[0,0,0])
        if all(missing(value) or value==prior for prior,value in zip(previous[1:],current[1:])):
            return previous
        return current

    @staticmethod
    def _flexi_v2_signature(data):
        # Gen2 status carries no tag identity until the asynchronous
        # get_filament_info reply is merged. Keep only identity-bearing fields
        # (never the volatile operational status) so a heartbeat cannot
        # invalidate a reconciled path, while an insert, removal or tag swap
        # always changes the signature. The connection identity is handled
        # separately by _flexi_collect_live and LiveEvidenceProvider.
        return [data.get('uid'), data.get('sku'), data.get('type'),
                data.get('brand'), data.get('color'), data.get('rfid')]

    def _flexi_collect_live(self):
        # Read existing Klipper objects only. No serial requests, writes,
        # movement, heater changes or G-code may be issued here.
        now = time.monotonic()
        lookup = self.printer.lookup_object
        ps = lookup('print_stats', None)
        wh = lookup('webhooks', None)
        toolhead = lookup('toolhead', None)
        ps_state = ps.get_status(0).get('state') if ps else None
        ready = wh is not None and wh.get_status(0).get('state') == 'ready'
        homed = toolhead.get_status(0).get('homed_axes', '') if toolhead else ''
        heads = {}
        for h in range(4):
            sensor = lookup('filament_motion_sensor e%d_filament' % h, None)
            status = sensor.get_status(0) if sensor else {}
            heads[h] = {'enabled': status.get('enabled'), 'detected': status.get('filament_detected'), 'observed': now}
        devices = {}
        cached=getattr(self,'_flexi_v1_metadata',{})
        next_cache={}
        workflow=getattr(getattr(self,'_flexi_production',None),'workflow',None)
        active=(workflow.job or workflow.print_session) if workflow else None
        sources=(active.get('sources') or [active.get('source')]) if active else []
        for ace, path in enumerate(getattr(self, '_ace_devices', [])):
            info = getattr(self, '_info_per_ace', {}).get(ace) or {}
            generation = getattr(self, '_flexi_connection_generations', {}).get(ace)
            serial = getattr(self, '_serials', {}).get(ace)
            connected = (getattr(self, '_connected_per_ace', {}).get(ace) is True
                         and serial is not None and serial.is_open
                         and not getattr(self, '_serial_failed_per_ace', {}).get(ace)
                         and ace not in getattr(self, '_held_units', set()))
            identity='%s:%s:%s' % (self._flexi_epoch,path,generation) if generation else None
            signatures={}
            protocol=getattr(self,'_protocols',{}).get(ace)
            v1=getattr(protocol,'NAME',None)=='v1'
            for slot,data in enumerate(info.get('slots',[])[:4]):
                key=(ace,slot);generations=getattr(self,'_gen1_gate_gen',{})
                if v1:
                    # Preserve the historical V1 gate-generation signature:
                    # a Gen1 status with no gate transition yet is not
                    # evidence for a shared path.
                    if key not in generations:continue
                    current=[generations[key],data.get('uid'),data.get('sku'),data.get('type'),data.get('color')]
                    if connected and identity and data.get('status') in ('ready','loaded','feeding','retracting','assisting','shifting','preload','preloading','rollback','rollback_assisting'):
                        if key in sources:current=self._flexi_v1_signature(cached.get((identity,key)),current)
                        next_cache[(identity,key)]=current
                elif getattr(protocol,'NAME',None)=='v2':
                    # Gen2 has stable UID identity and per-slot status truth;
                    # its merged RFID cache supplies the remaining fields.
                    current=self._flexi_v2_signature(data)
                else:
                    continue
                signatures[slot]=json.dumps(current,sort_keys=True)
            devices[ace] = {
                'identity': identity,
                'connected': connected, 'ready': info.get('status') == 'ready',
                'observed': getattr(self, '_flexi_info_observed', {}).get(ace),
                'slots': {slot: data.get('status') in ('ready', 'loaded') for slot, data in enumerate(info.get('slots', [])[:4])},
                'materials': {slot: data.get('type') for slot, data in enumerate(info.get('slots', [])[:4])},
                'source_identities': signatures}
        self._flexi_v1_metadata=next_cache
        thermal = {h: self._flexi_thermal.assess(h) for h in range(4)}
        return {'epoch': self._flexi_epoch, 'mode': getattr(self, '_ace_mode', None),
                'routes_sha256': self._flexi_evidence.fingerprint, 'observed': now,
                'idle': ready and ps_state in ('standby', 'complete', 'cancelled'),
                # Thermal readiness is deliberately NOT inferred from homing.
                # No production transport is bound and this remains false.
                'motion_ready': False, 'motion_ready_heads': {h: v['ready'] for h,v in thermal.items()},
                'thermal': thermal, 'homed_axes': homed, 'devices': devices, 'heads': heads}

    def _flexi_test_idle(self, gcmd):
        if getattr(self, '_ace_mode', None) != 'flexi':
            raise gcmd.error('Controlled sensor tests require Flexi mode')
        ps = self.printer.lookup_object('print_stats', None)
        if ps is None or ps.get_status(0).get('state') not in ('standby', 'complete', 'cancelled'):
            raise gcmd.error('Printer must be idle for a controlled sensor test')
        bg=self.printer.lookup_object('ace_bg_swap',None)
        if (getattr(self, '_swap_in_progress', False) or getattr(self, '_bg_staged', {}) or getattr(self, '_unload_all_active', False)
                or getattr(self,'_loading_heads',set()) or getattr(bg,'_busy',None)
                or getattr(self,'_calibration',{'state':'idle'}).get('state')!='idle'):
            raise gcmd.error('Another filament operation is active')

    def cmd_ACE_FLEXI_TEST_PREPARE(self, gcmd):
        self._flexi_test_idle(gcmd)
        head=gcmd.get_int('HEAD',2);ace=gcmd.get_int('ACE');slot=gcmd.get_int('SLOT')
        target=gcmd.get_float('TARGET',None);distance=gcmd.get_float('MAX_DISTANCE',None);timeout=gcmd.get_float('TIMEOUT',None)
        operation=gcmd.get('OPERATION','load').lower()
        # Validate before creating the explicitly requested recovery journal.
        self._flexi_routes.require_destination(ace,slot,head)
        if operation=='load':self._flexi_routes.require_available(ace,slot)
        if head!=2 or operation not in ('load','unload'):raise gcmd.error('Only explicit T2 sensor-test load/unload is supported')
        resolved=self._flexi_test_profiles.resolve(ace,slot,head,operation,target,distance,timeout)
        if not resolved['complete']:
            gcmd.respond_info(json.dumps({'ready':False,'profile':resolved,'execution_enabled':False},sort_keys=True))
            return  # Do not create a recovery journal for an incomplete profile.
        if self._flexi_test_controller is None:
            self._flexi_test_controller=ControlledTest(
                self,self._flexi_evidence,self._flexi_thermal,
                self._flexi_journal_path,time.monotonic,profiles=self._flexi_test_profiles)
            self._flexi_coordinator=self._flexi_test_controller.c
        result=self._flexi_test_controller.prepare(operation,ace,slot,head,target,distance,timeout)
        gcmd.respond_info(json.dumps(result,sort_keys=True))

    def cmd_ACE_FLEXI_TEST_CONFIRM_CLEAR(self, gcmd):
        self._flexi_test_idle(gcmd)
        if gcmd.get('CONFIRM','')!='ALL_SHARED_PATHS_CLEAR':raise gcmd.error('Explicit inspection of every shared path is required')
        if self._flexi_test_controller is None:raise gcmd.error('Prepare an explicit test first')
        result=self._flexi_test_controller.confirm_clear('operator: ALL_SHARED_PATHS_CLEAR')
        gcmd.respond_info(json.dumps(result,sort_keys=True))

    def cmd_ACE_FLEXI_TEST_ARM(self, gcmd):
        self._flexi_test_idle(gcmd)
        if self._flexi_test_controller is None:raise gcmd.error('Prepare an explicit test first')
        self._flexi_test_controller.arm(gcmd.get('CONFIRM',''))
        gcmd.respond_info('Single-use T2 sensor test armed for 30 seconds; no motion has started. Printing remains disabled.')

    def cmd_ACE_FLEXI_TEST_RUN(self, gcmd):
        self._flexi_test_idle(gcmd)
        if self._flexi_test_controller is None:raise gcmd.error('Prepare and explicitly arm a test first')
        try:
            result=self._flexi_test_controller.run()
        finally:
            # A lost start acknowledgment can still leave bounded hardware
            # work pending. Always monitor a retained stop/recovery job.
            if self._flexi_test_controller.executor.job is not None:
                self.reactor.register_timer(self._flexi_test_tick,self.reactor.NOW)
        gcmd.respond_info(json.dumps(result,sort_keys=True))

    def _flexi_test_tick(self,eventtime):
        controller=self._flexi_test_controller
        if controller is None or controller.executor.job is None:return self.reactor.NEVER
        try:
            result=controller.tick()
            job=controller.executor.job
            if job is None:return self.reactor.NEVER
            if job['phase']=='persistence_fault':return self.reactor.NEVER
            if job['phase']=='stop_unconfirmed' and controller.clock()>job['deadline']+5.:return self.reactor.NEVER
            return eventtime+(1. if job['phase']=='stop_unconfirmed' else .1)
        except Exception:
            logging.exception('[multiACE][flexi] controlled test failed; no automatic recovery motion')
            try:controller.cancel()
            except Exception:logging.exception('[multiACE][flexi] cancellation could not be confirmed')
            return self.reactor.NEVER

    def cmd_ACE_FLEXI_TEST_CANCEL(self,gcmd):
        if self._flexi_test_controller is None:raise gcmd.error('No controlled test exists')
        gcmd.respond_info(json.dumps(self._flexi_test_controller.cancel(),sort_keys=True))

    def cmd_ACE_FLEXI_TEST_RECOVER(self,gcmd):
        self._flexi_test_idle(gcmd)
        if self._flexi_test_controller is None:raise gcmd.error('Prepare explicit recovery first')
        state=gcmd.get('STATE','clear')
        if state=='clear':
            if gcmd.get('CONFIRM','')!='STOPPED_AND_PATHS_CLEAR':raise gcmd.error('Explicit stopped and shared-path inspection is required')
            self._flexi_test_controller.recover_clear('operator: STOPPED_AND_PATHS_CLEAR')
        elif state=='loaded':
            self._flexi_test_controller.recover_loaded(gcmd.get('CONFIRM',''))
        else:raise gcmd.error('Recovery state must be clear or loaded')
        gcmd.respond_info('Recovery reconciled; no motion or authorization restored.')

    def _flexi_stock_load_refusal(self, gcmd, name):
        # Flexi uses the original MultiACE loader. Stock loading is refused
        # only when the Flexi package cannot enforce routing (journal error).
        if not getattr(self, '_flexi_init_error', None):
            return None
        message = (
            'Stock loading command %s is unavailable: the Flexi journal could '
            'not be opened, so routed shared-path protection cannot be '
            'enforced. No preheat or filament movement was started.' % name)
        error = getattr(gcmd, 'error', None)
        if callable(error):
            raise error(message)
        raise getattr(self.printer, 'command_error', RuntimeError)(message)

    def _install_flexi_stock_load_guards(self):
        """Replace stock loading entrypoints with a Flexi-only refusal.

        The stock touchscreen macro preheats before it reaches FEED_AUTO, and
        FEED_AUTO/FEED_MANUAL change machine state before _do_feed's existing
        Flexi lock. Install these wrappers at ready or on Flexi entry so the
        refusal happens before the first heater or state transition. The
        captured handler is forwarded unchanged in every other mode.
        """
        if getattr(self, '_ace_mode', None) != 'flexi':
            return
        if getattr(self, '_flexi_stock_load_guards', None):
            return
        if not getattr(self, '_flexi_init_error', None):
            # Normal case: the touchscreen and stock load macros stay
            # available and resolve their source through the Flexi routes.
            return
        self._flexi_stock_load_guards = {}
        for name in ('AUTO_FEEDING_BATCH', 'AUTO_FEEDING_PREHEAT',
                     'AUTO_FEEDING', 'MANUAL_FEEDING'):
            try:
                original = self.gcode.register_command(name, None)
            except Exception as exc:
                logging.info('[multiACE][flexi] %s unregister failed: %s'
                             % (name, exc))
                continue
            if original is None:
                logging.info('[multiACE][flexi] %s not registered - no stock '
                             'loading guard needed' % name)
                continue
            try:
                self.gcode.register_command(
                    name, self._make_flexi_stock_load_guard(name, original),
                    desc='[multiACE][flexi] reject stock loading before heat '
                         'or state changes')
            except Exception as exc:
                logging.info('[multiACE][flexi] %s guard registration failed '
                             '(%s) - restoring stock handler' % (name, exc))
                try:
                    self.gcode.register_command(name, original)
                except Exception:
                    pass
                continue
            self._flexi_stock_load_guards[name] = original
        logging.info('[multiACE][flexi] stock loading guards: %s'
                     % ', '.join(sorted(self._flexi_stock_load_guards)))

    def _make_flexi_stock_load_guard(self, name, original):
        def _guard(gcmd):
            if getattr(self, '_ace_mode', None) == 'flexi':
                return self._flexi_stock_load_refusal(gcmd, name)
            return original(gcmd)
        return _guard

    def _flexi_require_execution(self):
        # Original MultiACE filament commands run in Flexi mode. Routing,
        # shared-path ownership and the motor gate enforce the Flexi rules;
        # only an unusable Flexi package (journal/route error) is fail-closed.
        if getattr(self, '_ace_mode', 'normal') != 'flexi':
            return
        if getattr(self, '_flexi_init_error', None) or getattr(self, '_flexi_routes', None) is None:
            raise self.printer.command_error(
                'Flexi routing protection is unavailable (journal or routes could not be loaded); no filament movement was started.')

    def _flexi_head_config_refusal(self, name):
        error = getattr(getattr(self, 'printer', None), 'command_error', RuntimeError)
        raise error('Legacy head settings are not used in Flexi. Configure Filament Sources instead. No printer change was sent.')

    def _flexi_register_command(self, name, handler, *args, **kwargs):
        # Wrap existing command entrypoints without changing other modes.
        # Unregister calls must still return the original handler unchanged.
        if handler is not None and name not in self._FLEXI_SAFE_COMMANDS:
            original = handler
            def handler(gcmd):
                if getattr(self, '_ace_mode', None) == 'flexi' and name in getattr(self, '_FLEXI_RAW_MOTOR_COMMANDS', ()):
                    raise gcmd.error('%s drives an ACE lane directly and is disabled in Flexi mode '
                                     '(it would bypass the routes and the shared-tube record). '
                                     'Use Load/Unload or Find routes instead.' % name)
                if (getattr(self, '_ace_mode', None) == 'flexi'
                        and name in getattr(self, '_FLEXI_HEAD_CONFIG_COMMANDS', ())):
                    return self._flexi_head_config_refusal(name)
                if getattr(self, '_ace_mode', None) == 'flexi' and name in FLEXI_MAINTENANCE_COMMANDS:
                    if name == 'ACE_TAG_READ':
                        ace = gcmd.get_int('ACE', minval=0, maxval=3)
                        gcmd.get_int('SLOT', minval=0, maxval=3)
                        if self._cap(ace, 'v2_command_set'):
                            raise gcmd.error('V2 RFID lane rotation is unavailable in Flexi')
                        self._flexi_metadata_idle(gcmd)
                    return original(gcmd)
                if getattr(self,'_ace_mode',None)=='flexi' and name.startswith('T') and name[1:].isdigit() and gcmd.get('Q',None) is None:
                    service=getattr(self,'_flexi_production',None)
                    if service is not None and not getattr(self,'_flexi_init_error',None):
                        number=int(name[1:])
                        try:
                            if number<4 and service.source_select.effective(number)['type']!='ace':
                                result=service.manual_tool(number,lambda:original(gcmd))
                                if service.pending_start and service.pending_start==(service.print_context()[0],number):
                                    service.pending_start=None
                                return result
                        except Exception as exc:raise gcmd.error(str(exc))
                        # T0-T3: the original MultiACE tool change. The head's
                        # recorded source (head_source) drives feed assist on
                        # its own ACE; no Flexi load workflow and no PAUSE.
                        return original(gcmd)
                if getattr(self,'_ace_mode',None)=='flexi' and not getattr(self,'_flexi_init_error',None):
                    service=getattr(self,'_flexi_production',None)
                    # Virtual tools (T4-T31) use the original MultiACE path:
                    # same source -> plain tool change; other source -> stock
                    # swap via ACE_UNLOAD/LOAD_HEAD (route + ledger checks).
                    if (service is not None and name=='SET_PRINT_FILAMENT_CONFIG'
                            and not self._flexi_internal_display_update(name,gcmd)
                            and service.print_context()[1]=='printing'):
                        fields=set(gcmd.get_command_parameters())
                        display={'CONFIG_EXTRUDER','FILAMENT_TYPE','FILAMENT_COLOR_RGBA','VENDOR','FILAMENT_SUBTYPE'}
                        if fields<=display and 'CONFIG_EXTRUDER' in fields:
                            gcmd.get_int('CONFIG_EXTRUDER',minval=0,maxval=3)
                            # Slicer display metadata never writes an ACE slot override.
                            if self._orig_set_ptc is not None:return self._orig_set_ptc(gcmd)
                    if service is not None and name in ('SM_PRINT_PREEXTRUDE_FILAMENT','SM_PRINT_FLOW_CALIBRATE'):
                        if name=='SM_PRINT_PREEXTRUDE_FILAMENT':
                            head=gcmd.get_int('INDEX',None)
                            if head is None:head=service.active_head()
                            if head>=4:head=self._virtual_tool_target(head)['head']
                        else:
                            head=self._flow_calibrate_head(gcmd)
                            if head is not None and not self._flow_calibrate_would_run(head):return
                        if name=='SM_PRINT_PREEXTRUDE_FILAMENT' and gcmd.get_int('INDEX',0)>=4:
                            # The physical tool is verified; never route the legacy
                            # virtual-tool pre-extrude through its swap machinery.
                            return self.gcode.run_script_from_command('SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d'%head)
                        return original(gcmd)
                if getattr(self,'_ace_mode',None)=='flexi' and name in METADATA_COMMANDS:
                    if name=='ACE_SPOOL_LIST':return original(gcmd)
                    self._flexi_metadata_idle(gcmd)
                    if gcmd.get('HEAD',None) is not None:raise gcmd.error('Flexi spool identity belongs to an ACE slot, not a legacy head binding')
                    a=gcmd.get('ACE',None);slot=gcmd.get('SLOT',None)
                    if name=='ACE_SPOOL_ASSIGN' and (a is None or slot is None):raise gcmd.error('Choose an explicit ACE and slot for Flexi spool assignment')
                    if a is not None or slot is not None:
                        try:self._flexi_routes.destination(int(a),int(slot))
                        except (TypeError,ValueError):raise gcmd.error('Choose a configured ACE slot')
                    result=original(gcmd)
                    service=getattr(self,'_flexi_production',None)
                    if service:service.synchronize_inventory()
                    return result
                if not self._flexi_internal_display_update(name, gcmd) and not self._flexi_native_tool_allowed(name,gcmd):
                    if name=='SET_PRINT_FILAMENT_CONFIG' and getattr(self,'_ace_mode',None)=='flexi':
                        # In Flexi the filament identity belongs to the routed ACE
                        # slot; a display edit could bind it to the wrong slot.
                        raise getattr(self.printer,'command_error',RuntimeError)('Filament identity is locked to the ACE slot in Flexi mode. Set it in the MultiACE spool picker for that slot.')
                    self._flexi_require_execution()
                return original(gcmd)
        if handler is not None and name.startswith(('ACE_FLEXI_TEST_','ACE_FLEXI_PRODUCTION_','ACE_FLEXI_SLOT_')):
            test_handler=handler
            def handler(gcmd):
                try:
                    if getattr(self,'_flexi_init_error',None):raise ValueError('Flexi journal is unavailable; preserve it for recovery')
                    return test_handler(gcmd)
                except Exception as exc:
                    # Operator/preflight errors are command refusals, never
                    # uncaught Python exceptions in Klipper's dispatcher.
                    raise gcmd.error(str(exc))
        return self.gcode.register_command(name, handler, *args, **kwargs)

    def _flexi_metadata_idle(self,gcmd):
        self._flexi_test_idle(gcmd)
        service=getattr(self,'_flexi_production',None);controller=getattr(self,'_flexi_test_controller',None)
        if controller and controller.executor.job:raise gcmd.error('Stop the controlled test before editing spool identity')
        if service:
            w=service.workflow
            if w and (w.job or w.print_session):raise gcmd.error('Stop filament/assist operations before editing spool identity')
        c=getattr(self,'_flexi_coordinator',None)
        if c:
            state=c.snapshot()
            if state['operations'] or state['printing']:raise gcmd.error('Reserved Flexi paths prevent metadata changes')
        if service:service.lease=None;service.risk.grants.clear()

    def cmd_ACE_FLEXI_SLOT_METADATA(self,gcmd):
        self._flexi_metadata_idle(gcmd);payload=self._flexi_decode_production(gcmd)
        slot_metadata_command(payload,self._flexi_routes)
        a,slot=payload['source'];record=payload['record'];key='%d_%d'%(a,slot)
        previous=copy.deepcopy(self._slot_overrides)
        if record is None:self._slot_overrides.pop(key,None)
        else:self._slot_overrides[key]=dict(record,ace=a,slot=slot)
        expected=copy.deepcopy(self._slot_overrides)
        try:
            self._save_slot_overrides()
            if json.loads(Path(self._slot_overrides_file).read_text())!=expected:raise ValueError('Saved slot identity does not match')
        except Exception as exc:
            self._slot_overrides=previous
            raise gcmd.error('Slot identity save failed; movement remains locked: '+str(exc))
        self.cmd_MULTIACE_REFRESH_OVERRIDES(gcmd)
        service=getattr(self,'_flexi_production',None)
        if service:service.synchronize_inventory()
        gcmd.respond_info('Slot identity saved through existing spool metadata; no movement authorized.')

    def _flexi_native_tool_allowed(self,name,gcmd):
        service=getattr(self,'_flexi_production',None)
        if service is None or service.workflow is None:return False
        j=service.workflow.job
        if not j or name!='T%d'%j['head'] or j['phases'][j['index']][1]!='select':return False
        try:
            return (int(gcmd.get('Q','-1'))==int(j['token'],16) and gcmd.get('A','')=='0'
                    and service.native_authorized(j['token'],'select',j['phases'][j['index']][0],j['head']))
        except (TypeError,ValueError):return False

    # ---- routed original-MultiACE loading -------------------------------
    def _flexi_head_shared(self, head):
        try:return len(self._flexi_routes.sources(head))>1
        except Exception:return True

    def _flexi_head_recovery(self, head):
        """Unresolved journal recovery records for this toolhead only."""
        try:state=self._flexi_coordinator.snapshot()
        except Exception:return []
        return [r for r in state.get('recovery_required',[]) if r.get('head')==head]

    def _flexi_native_motor_allowed(self, idx, method, slot):
        """Gate for original MultiACE motor requests (no Flexi token).

        Brakes are handled before this point. A routed slot may always
        retract (unwind). Forward motion or feed assist is allowed only when
        the slot is routed, assigned, its toolhead has no unresolved
        recovery record, and on a shared path the ledger names this slot as
        the owner (or the path is clear).
        """
        if type(idx) is not int or type(slot) is not int:return False
        scope=getattr(self,'_flexi_discovery_scope',None)
        if scope is not None:
            # Route discovery owns the ACEs: only its own slot may move.
            if (idx,slot)==tuple(scope):
                return method in ('feed_filament','unwind_filament','stop_feed_filament','stop_unwind_filament','stop_feed_assist')
            return method in ('stop_feed_filament','stop_unwind_filament','stop_feed_assist')
        try:head=self._flexi_routes.destination(idx,slot)
        except Exception:return False
        if method in ('unwind_filament','stop_unwind_filament','stop_feed_filament','stop_feed_assist'):return True
        if method not in ('feed_filament','start_feed_assist','update_feeding_speed'):return False
        try:self._flexi_routes.require_available(idx,slot)
        except Exception:return False
        pending=self._flexi_native_pending.get(head)
        if self._flexi_head_recovery(head) and not (pending and tuple(pending)==(idx,slot)):return False
        if method=='feed_filament' and self._flexi_flexible_blocked(idx,slot):return False
        allowed,_reason=self._flexi_paths.feed_allowed(head,(idx,slot),self._flexi_head_shared(head))
        return allowed

    def _flexi_display_load_target(self, head):
        """Source for a load that names only the toolhead (touchscreen,
        start gcode): the ledger owner (same filament continues), else the
        operator's selected spool, else the recorded head_source, else the
        only/first routed slot that reports filament. None if none."""
        try:sources=list(self._flexi_routes.sources(head))
        except Exception:return None
        if not sources:return None
        rec=self._flexi_paths.record(head)
        if rec.get('owner') and tuple(rec['owner']) in sources and rec['state']!='clear':
            return tuple(rec['owner'])
        service=getattr(self,'_flexi_production',None)
        try:
            chosen=service.config.get('selection',{}).get(str(head)) if service else None
            if chosen:
                pair=tuple(int(v) for v in chosen.split(':'))
                if pair in sources:return pair
        except Exception:pass
        src=self._head_source.get(head) or {}
        pair=(src.get('ace_index'),src.get('slot'))
        if pair in sources:return pair
        present=[]
        for a,sl in sources:
            try:
                gates=self._gate_status_per_ace.get(a) or []
                if sl<len(gates) and gates[sl]==GATE_AVAILABLE:present.append((a,sl))
            except Exception:pass
        if present:return sorted(present)[0]
        return sorted(sources)[0] if len(sources)==1 else None

    def _flexi_slot_material(self, ace, slot):
        try:
            override=(getattr(self,'_slot_overrides',{}) or {}).get('%d_%d'%(ace,slot)) or {}
            if override.get('material'):return str(override['material'])
            slots=(getattr(self,'_info_per_ace',{}).get(ace) or {}).get('slots') or []
            return str((slots[slot] or {}).get('type') or '') if slot<len(slots) else ''
        except Exception:return ''

    def _flexi_flexible_blocked(self, ace, slot):
        """TPU/TPE stay restricted by default: forward motion of a flexible
        material needs the explicit, unexpired at-own-risk consent for this
        source. The consent never bypasses routing, sensor or path checks."""
        material=self._flexi_slot_material(ace,slot)
        if not material or not flexible(material):return False
        service=getattr(self,'_flexi_production',None)
        grant=(getattr(getattr(service,'risk',None),'grants',{}) or {}).get((ace,slot)) if service else None
        try:
            if grant and grant.get('expires',0)>service.risk.clock() and ('load' in grant.get('actions',()) or grant.get('scope')=='path'):
                return False
        except Exception:pass
        return True

    def _flexi_native_load_check(self, gcmd, head, ace, slot):
        """Called by ACE_LOAD_HEAD after route resolution, before any heat,
        tool selection or motor command. Raises a command error to refuse."""
        if getattr(self,'_ace_mode',None)!='flexi':return
        self._flexi_require_execution()
        discovery=getattr(self,'_flexi_discovery',None)
        if discovery is not None and discovery.running:
            raise gcmd.error('Route discovery is running; loading waits until it finishes. No movement was started.')
        try:
            self._flexi_routes.require_destination(ace,slot,head)
            self._flexi_routes.require_available(ace,slot)
        except Exception as exc:
            raise gcmd.error(str(exc))
        records=self._flexi_head_recovery(head)
        if records:
            raise gcmd.error('T%d has an unresolved filament recovery record (%s). Open recovery and record what you find in the tube before loading; no movement was started.'
                             %(head,records[-1].get('reason') or 'interrupted operation'))
        if self._flexi_flexible_blocked(ace,slot):
            raise gcmd.error('ACE%d slot %d holds flexible filament (%s). TPU/TPE loading needs the at-own-risk confirmation for this path in Flexi settings; no movement was started.'
                             %(ace,slot,self._flexi_slot_material(ace,slot)))
        allowed,reason=self._flexi_paths.feed_allowed(head,(ace,slot),self._flexi_head_shared(head))
        if not allowed:raise gcmd.error(reason+' No movement was started.')

    def _flexi_runout_release(self, head, source):
        """Runout failover may feed another routed source into this tube
        only when the old filament demonstrably left it: the old source's
        ACE slot reports empty AND the toolhead sensor (downstream of any
        joiner) has cleared, i.e. the filament end passed the joiner. Records
        the path clear with that basis; otherwise refuses (print stays paused)."""
        if getattr(self,'_ace_mode',None)!='flexi':return True
        try:
            sensor=self.printer.lookup_object('filament_motion_sensor e%d_filament'%head,None)
            if sensor is None or sensor.get_status(0).get('filament_detected') is not False:return False
            gates=self._gate_status_per_ace.get(source[0]) or []
            if source[1]>=len(gates) or gates[source[1]]==GATE_AVAILABLE:return False
            rec=self._flexi_paths.record(head)
            if rec.get('owner') not in (None,[int(source[0]),int(source[1])]):return False
            self._flexi_paths.operator_clear(head,'runout: ACE%d slot %d ran empty and the filament end passed the T%d sensor'%(source[0],source[1],head))
            return True
        except Exception:
            logging.exception('[multiACE][flexi] runout release check failed')
            return False

    def _flexi_native_feed_started(self, head, ace, slot):
        if getattr(self,'_ace_mode',None)!='flexi':return
        self._flexi_native_pending[head]=(ace,slot)
        self._flexi_paths.begin_load(head,(ace,slot))

    def _flexi_native_load_finished(self, head, ok):
        pending=self._flexi_native_pending.pop(head,None)
        if pending is None or getattr(self,'_ace_mode',None)!='flexi':return
        arrived=False
        try:
            sensor=self.printer.lookup_object('filament_motion_sensor e%d_filament'%head,None)
            src=self._head_source.get(head) or {}
            arrived=bool(ok and sensor is not None and sensor.get_status(0).get('filament_detected')
                         and (src.get('ace_index'),src.get('slot'))==tuple(pending) and not src.get('load_failed'))
        except Exception:
            logging.exception('[multiACE][flexi] load result check failed; path kept uncertain')
        self._flexi_paths.finish_load(head,pending,arrived)
        logging.info('[multiACE][flexi] T%d load of ACE%d slot %d -> %s',head,pending[0],pending[1],
                     'loaded' if arrived else 'uncertain')

    def _flexi_native_unload_finished(self, head, source, verified):
        if getattr(self,'_ace_mode',None)!='flexi' or source is None:return
        self._flexi_paths.finish_unload(head,source,verified)
        logging.info('[multiACE][flexi] T%d unload of ACE%d slot %d -> %s',head,source[0],source[1],
                     'clear' if verified else 'uncertain')

    _FLEXI_VERIFIED_BRAKES = VERIFIED_BRAKES

    def _flexi_motor_request_allowed(self,idx,request,token):
        method=request.get('method');params=request.get('params') or {};slot=params.get('index')
        if method=='raw':return False
        if method=='feed_or_rollback_raw':
            # Gate by direction: rollback (mode 1, 3) like unwind, feed (0, 2) like feed.
            method='unwind_filament' if params.get('mode') in (1,3) else 'feed_filament'
        if method in VERIFIED_BRAKES:
            # Type check only, and deliberately state-free: acceptance is not
            # confirmation, so nothing here clears the lock or records a stop.
            return type(idx) is int and type(slot) is int
        if not token:return self._flexi_native_motor_allowed(idx,method,slot)
        native=getattr(self,'_flexi_pathchek',None)
        if native and native.job and native.job.get('token')==token:
            return native.motor_allowed(idx,request,token)
        if type(idx) is not int or type(slot) is not int:return False
        braking=method in ('stop_feed_filament','stop_feed_assist','stop_unwind_filament')
        policy=getattr(getattr(self,'_flexi_production',None),'source_select',None)
        if not braking and policy is None:
            try:self._flexi_routes.require_available(idx,slot)
            except (AttributeError,ValueError,TypeError):return False
        if not braking and policy is not None:
            try:
                # Only an owned, scoped unload may use an unavailable source.
                service=getattr(self,'_flexi_production',None);w=getattr(service,'workflow',None);j=getattr(w,'job',None)
                controlled=getattr(getattr(getattr(self,'_flexi_test_controller',None),'executor',None),'job',None)
                cleanup=bool((j and j.get('token')==token and j.get('action') in ('unload','switch','failover') and tuple(j.get('old_source') or ())==(idx,slot) and j['phases'][j['index']][0]==(idx,slot) and j['phases'][j['index']][1] in ('select','heat_unload','tipform','retract_path','verify_clear')) or (controlled and controlled.get('token')==token and controlled.get('operation')=='unload' and (controlled['ace'],controlled['slot'])==(idx,slot)))
                policy.require_ace((idx,slot),allow_unavailable=cleanup)
            except (ValueError,TypeError):return False
        controller=getattr(self,'_flexi_test_controller',None)
        job=controller.executor.job if controller is not None else None
        if job and job['token']==token and (job['ace'],job['slot'])==(idx,slot):
            if braking:return True
            if not controller.authorization.allows(job):return False
            return method==('feed_filament' if job['operation']=='load' else 'unwind_filament') and 0<params.get('length',0)<=5 and 0<params.get('speed',0)<=20
        service=getattr(self,'_flexi_production',None);w=service.workflow if service else None
        if w is None:return False
        if w.print_session and w.print_session['token']==token and tuple(w.print_session['source'])==(idx,slot):
            return braking or (method=='start_feed_assist' and service.native_authorized(token,'print',(idx,slot),w.print_session['head']))
        job=w.job
        if not job or job['token']!=token or (idx,slot) not in job['sources']:return False
        if braking:return True
        source,phase=job['phases'][job['index']]
        if source!=(idx,slot) or not service.native_authorized(token,phase,source,job['head']):return False
        if method not in ('feed_filament','unwind_filament'):return False
        if (phase=='feed_path' and method=='feed_filament' and source==(idx,slot)
                and job.get('action')=='load'):
            # The adapter decides whether this load may use the printer's own
            # single feed request; the gate still compares the exact dispatch.
            adapter=w.adapter_for(source)
            path=getattr(adapter,'path',None)
            if path is not None and path.allows_continuous_request(idx,request,token):return True
        profile=job['profiles'][source]['values']
        limit=5 if phase in ('feed_path','retract_path') else 1
        speed=profile['feed_speed'] if method=='feed_filament' and phase=='feed_path' else profile['retract_speed'] if phase=='retract_path' else profile['extrude_speed']
        return 0<params.get('length',0)<=limit and 0<params.get('speed',0)<=max(speed,20 if phase=='tipform' else speed)

    def _flexi_decode_production(self,gcmd):
        data=gcmd.get('DATA','')
        if not isinstance(data,str) or len(data)>98304:raise gcmd.error('Invalid production payload size')
        try:return json.loads(bytes.fromhex(data).decode('utf-8'))
        except (ValueError,UnicodeError):raise gcmd.error('Invalid encoded production payload')

    def _flexi_production_config_idle(self,gcmd):
        # Recipes are metadata, unlike physical route edits. Allow adjustment
        # with a stopped, known loaded head, but never during a print or operation.
        if getattr(self,'_ace_mode',None)!='flexi':return self._flexi_assert_idle(gcmd)
        self._flexi_test_idle(gcmd)
        state=self._flexi_coordinator.snapshot()
        if state['operations'] or state['printing']:raise gcmd.error('Stop all Flexi operations before changing recipes')

    def cmd_ACE_FLEXI_PRODUCTION_CONFIG(self,gcmd):
        with self._flexi_coordinator._lock:
            self._flexi_save_production_config(gcmd)

    def _flexi_save_production_config(self, gcmd):
        self._flexi_production_config_idle(gcmd)
        previous_service=self._flexi_production
        live_owners=copy.deepcopy(previous_service.workflow.owners) if previous_service.workflow else {}
        payload = recipe_save(self._flexi_decode_production(gcmd), self._flexi_routes)
        config = ProductionService.validate_config(self._flexi_routes, payload['config'])
        previous_wizard=self._flexi_production.wizard
        def preflight(raw):
            if payload['base_revision'] != previous_service.recipe_revision():
                raise RouteError('Recipe conflict: reload saved recipes and review changes before saving')
            document = routing_document(raw)
            if document['routes'].context() != self._flexi_routes.context():
                raise RouteError('Configuration conflict: saved and running Flexi routing differ')
            saved = ProductionService.validate_config(self._flexi_routes, document['production'])
            if saved != previous_service.config:
                raise RouteError('Configuration conflict: saved and running Flexi recipes differ')
            if config.get('source_select', {}) != previous_service.config.get('source_select', {}):
                previous_service._source_change_idle()
                for head in range(4):
                    if config.get('source_select', {}).get(str(head)) != previous_service.config.get('source_select', {}).get(str(head)):
                        previous_service._invalidate_source_head(head)
            previous_service.invalidate_for_config(config)

        encoded=json.dumps(config,sort_keys=True,separators=(',',':'))
        error=self._cfg_write_ace_option('flexi_production',encoded,preflight=preflight)
        if error is not None:raise gcmd.error('Production configuration could not be saved: '+str(error))
        self._flexi_production_raw=encoded;self._flexi_production=ProductionService(self,self._flexi_routes,config)
        if live_owners:
            workflow=self._flexi_production._workflow()
            for head,owner in live_owners.items():
                try:
                    sample=self._flexi_evidence.read();path=self._flexi_evidence.path(sample,head);source=tuple(owner['source'])
                    current=self._flexi_production.inventory().get(source,{})
                    if path['state']=='occupied' and tuple(path['owner'])==source and current.get('identity')==owner['identity'] and sample['devices'][source[0]]['identity']==owner.get('device_identity'):
                        workflow.owners[head]=owner
                except (ValueError,KeyError):pass
        if previous_wizard.session:
            if previous_wizard.session['step']<=2:
                previous_wizard.session['context']=self._flexi_production.calibration_context(previous_wizard.session['source'],previous_wizard.session['head'])
            self._flexi_production.wizard=previous_wizard
        gcmd.respond_info('Flexi profiles/spool choices saved; no movement session was authorized.')

    def cmd_ACE_FLEXI_PRODUCTION_REQUEST(self,gcmd):
        payload=self._flexi_decode_production(gcmd)
        try:self._flexi_production.request(payload)
        finally:self._flexi_schedule_production()
        # The web already reads the live workflow from /api/state. Returning
        # the whole status object here turns every click into a console dump.
        gcmd.respond_info('Flexi %s request accepted.'%payload.get('action','operation'))

    def cmd_ACE_FLEXI_PRODUCTION_CALIBRATION(self,gcmd):
        try:result=self._flexi_production.calibration_request(self._flexi_decode_production(gcmd))
        finally:self._flexi_schedule_production()
        gcmd.respond_info(json.dumps(result,sort_keys=True))

    def cmd_ACE_FLEXI_PRODUCTION_ARM(self,gcmd):
        payload=self._flexi_decode_production(gcmd)
        self._flexi_production.arm(payload['sources'],payload['actions'],payload['seconds'],payload.get('confirmation'),test=payload.get('test') is True,wizard_id=payload.get('wizard_id'))
        gcmd.respond_info('Scoped Flexi session armed; no operation started.')

    def cmd_ACE_FLEXI_PRODUCTION_VALIDATE(self,gcmd):
        payload=self._flexi_decode_production(gcmd)
        self._flexi_production.validate_recipe(payload['source'],payload['actions'],payload['observations'],payload.get('confirmation'))
        gcmd.respond_info('Explicit physical recipe observation recorded; execution remains separately authorized.')

    def _flexi_schedule_production(self):
        service=getattr(self,'_flexi_production',None)
        if service is None or service.workflow is None:return
        if (service.workflow.job or service.workflow.print_session) and not getattr(self,'_flexi_production_timer_active',False):
            self._flexi_production_timer_active=True
            self.reactor.register_timer(self._flexi_production_tick,self.reactor.NOW)

    def _flexi_production_tick(self,eventtime):
        service=self._flexi_production
        try:
            service.tick();w=service.workflow
            if w and w.job and not w.job.get('failed'):
                source,phase=w.job['phases'][w.job['index']]
                if phase=='feed_path' and source==(0,0) and w.job.get('calibration_test') is True:
                    path=getattr(w.adapter_for(source),'path',None)
                    if path is not None and path.continuous_load:return eventtime+.105
                return eventtime+.25
            if w and w.print_session:return eventtime+.25
        except Exception:
            logging.exception('[multiACE][flexi] production failed; preserving recovery state')
            try:
                if service.workflow:service.workflow.cancel()
            except Exception:logging.exception('[multiACE][flexi] production stop unconfirmed')
        self._flexi_production_timer_active=False
        return self.reactor.NEVER

    def _flexi_internal_display_update(self, name, gcmd):
        # Do not broadly allowlist SET_PRINT_FILAMENT_CONFIG: user edits can
        # persist slot overrides using the legacy parallel-tubing fallback.
        # Only pass a recorded internal identity update, with its exact
        # metadata-only parameter set. Stock and display guards still run.
        if (getattr(self, '_ace_mode', 'normal') != 'flexi'
                or name != 'SET_PRINT_FILAMENT_CONFIG'):
            return False
        params = gcmd.get_command_parameters()
        if set(params) != {
                'CONFIG_EXTRUDER', 'FILAMENT_TYPE', 'FILAMENT_COLOR_RGBA',
                'VENDOR', 'FILAMENT_SUBTYPE'}:
            return False
        head = gcmd.get_int('CONFIG_EXTRUDER', -1)
        if head not in range(4):
            return False
        incoming = {
            'head': head,
            'type': str(gcmd.get('FILAMENT_TYPE', '') or ''),
            'color': str(gcmd.get('FILAMENT_COLOR_RGBA', '') or '').upper().lstrip('#'),
            'vendor': str(gcmd.get('VENDOR', '') or ''),
            'subtype': str(gcmd.get('FILAMENT_SUBTYPE', '') or ''),
        }
        return self._match_expected_push(incoming) is not None

    def _flexi_assert_idle(self, gcmd, isolated_exit=False):
        coordinator = getattr(self, '_flexi_coordinator', None)
        if coordinator is not None:
            try:
                reservations = coordinator.snapshot()
            except InterlockError as exc:
                raise gcmd.error('Flexi interlocks unavailable: ' + str(exc))
            if reservations['operations'] or reservations['printing'] or (reservations.get('recovery_required') and not isolated_exit):
                raise gcmd.error('Flexi resources are reserved; route and mode changes are refused')
        ps = self.printer.lookup_object('print_stats', None)
        if ps is None or ps.get_status(self.reactor.monotonic()).get('state') not in ('standby', 'complete', 'cancelled'):
            raise gcmd.error('Flexi configuration requires an idle printer with known print state')
        bg = self.printer.lookup_object('ace_bg_swap', None)
        if (getattr(self, '_swap_in_progress', False)
                or getattr(self, '_unload_all_active', False)
                or getattr(self, '_bg_staged', {})
                or (bg is not None and getattr(bg, '_busy', None))
                or self._calibration.get('state') != 'idle'):
            raise gcmd.error('Finish filament and calibration operations before changing Flexi configuration')
        for head in range(4):
            sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
            if sensor is None:
                raise gcmd.error('Toolhead sensor unavailable; Flexi configuration refused')
            if sensor.get_status(0).get('filament_detected') is not False or self._head_source.get(head):
                raise gcmd.error('Unload all toolheads before changing Flexi configuration')

    def cmd_ACE_SET_FLEXI_ROUTES(self, gcmd):
        coordinator = getattr(self, '_flexi_coordinator', None)
        lock = getattr(coordinator, '_lock', None)
        with lock if lock is not None else nullcontext():
            self._flexi_save_routes_locked(gcmd)

    def _flexi_save_routes_locked(self, gcmd):
        if getattr(self,'_flexi_init_error',None):raise gcmd.error('Recover the existing Flexi journal before changing routes')
        self._flexi_assert_idle(gcmd)
        current=getattr(self,'_flexi_coordinator',None)
        prior_journal=getattr(current,'journal',None)
        prior_state=None
        if prior_journal is not None:
            if not isinstance(prior_journal,AtomicJournal):raise gcmd.error('Explicit journal migration requires a known safe journal')
            prior_state=current.snapshot()
            native=prior_state.get('pathchek_job')
            if (prior_state.get('confirmed_sources') or prior_state.get('nozzle_owners') or prior_state.get('nozzle_recovery')
                or prior_state.get('full_workflow') or (native and native.get('phase') not in ('complete','reconciled'))):
                raise gcmd.error('Physically reconcile all ownership before journal migration')
        production = getattr(self, '_flexi_production', None)
        workflow = getattr(production, 'workflow', None)
        if workflow and (workflow.job or workflow.print_session or workflow.owners):
            raise gcmd.error('Release production ownership before editing shared routes')
        if production and getattr(getattr(production, 'source_select', None), 'temporary', {}):
            raise gcmd.error('Restore temporary source overrides before editing routing')
        try:
            update = decode_route_update(gcmd.get('DATA', ''))
            # Planned ACE2/ACE3 routes may be saved before those units are
            # physically connected. Execution still requires live evidence.
            routes = self._flexi_routes.patched(update['changes'], range(len(self._ace_devices)), allow_unconfigured=True).with_status_patch(update.get('slot_status_changes', {}), range(len(self._ace_devices)), allow_unconfigured=True)
            routes=routes.with_topology_patch(update.get('topology_changes',{}))
            approvals=update.get('discovery_approval',{})
            for key,value in update.get('topology_changes',{}).get('manual_overrides',{}).items():
                if value is False and self._flexi_routes.topology.as_dict()['manual_overrides'].get(key,key in self._flexi_routes.as_dict()) is not False and key not in approvals:
                    raise RouteError('Clearing a manual override requires explicit discovery approval')
            manual={key:True for key,head in update['changes'].items() if head is not None}
            for key,evidence_id in approvals.items():
                observation=(prior_state or {}).get('pathchek_discoveries',{}).get(key)
                if (not observation or observation.get('id')!=evidence_id or observation.get('observed_head')!=update['changes'].get(key)
                    or not observation.get('qualified_observation') or not observation.get('physically_reconciled')):
                    raise RouteError('Discovery approval conflict: inspect current qualified evidence and reconcile first')
                manual[key]=False
            if manual:routes=routes.with_topology_patch({'manual_overrides':manual})
            routes.validate_topology()
            options={'flexi_slot_status':routes.availability.encode(),'flexi_topology':routes.topology.encode()}
            next_path=None
            if prior_journal is not None:
                previous=Path(self._flexi_journal_path)
                next_path=str(previous.with_name(previous.stem+'.routes-'+uuid.uuid4().hex+'.json'))
            prepared = {}
            def preflight(raw):
                document = routing_document(raw)
                if document['routes'].context()!=self._flexi_routes.context():
                    raise RouteError('Routing conflict: saved and running routes differ; reload configuration first')
                runtime = ProductionService.validate_config(routes, getattr(self, '_flexi_production_raw', '{}'))
                saved = ProductionService.validate_config(routes, document['production'])
                if saved != runtime:
                    raise RouteError('Configuration conflict: saved and running production settings differ')
                tests = TestProfiles(routes, document['test_profiles'])
                if tests.encode() != TestProfiles(routes, getattr(self, '_flexi_test_profiles_raw', '{}')).encode():
                    raise RouteError('Configuration conflict: saved and running test profiles differ')
                candidate = self._flexi_build_evidence(routes, require_no_journal=True, journal_path=next_path)
                for key, head in routes.as_dict().items():
                    source = tuple(map(int, key.split(':')))
                    candidate['_flexi_production'].profiles.resolve(source, head, 'load')
                    # Saved custom recipes are validated even while Standard is
                    # active, so switching to Custom can never expose a
                    # contradictory recipe that was accepted silently.
                    production = candidate['_flexi_production']
                    if getattr(production.profiles, 'mode', 'custom') != 'custom':
                        type(production.profiles)(routes, production.config['profiles'],
                            defaults=production.standard_profile, mode='custom').resolve(source, head, 'load')
                    for operation in ('load', 'unload'):
                        candidate['_flexi_test_profiles'].resolve(*source, head, operation)
                self._flexi_assert_idle(gcmd)
                if workflow and (workflow.job or workflow.print_session or workflow.owners):
                    raise RouteError('Production ownership changed while preparing route save')
                if next_path is not None:
                    # New inactive generation first. The old journal is never rewritten.
                    journal=AtomicJournal(next_path)
                    migrated=LiveCoordinator(routes,candidate['_flexi_evidence'],journal=journal,max_age=5.)
                    def preserve(state):
                        state.update(pathchek_discoveries=copy.deepcopy(prior_state.get('pathchek_discoveries',{})),
                            pathchek_results=copy.deepcopy(prior_state.get('pathchek_results',[])),
                            pathchek_job=copy.deepcopy(prior_state.get('pathchek_job')),
                            pathchek_qualifications={},recipe_calibrations=[],wizard_results=[],
                            previous_journal=str(prior_journal.path),routing_change_approval=copy.deepcopy(update),qualification_invalidation='Routing/topology/availability changed')
                    migrated._change(preserve)
                    candidate['_flexi_coordinator']=migrated;candidate['_flexi_journal_path']=next_path
                    options['flexi_journal_path']=next_path
                prepared.update(candidate)
            error = self._cfg_write_ace_option('flexi_routes', routes.encode(),
                       expected_sha256=update['base_revision'], preflight=preflight,
                       additional_options=options)
            if error is not None:
                raise RouteError('Could not save Flexi routes: ' + str(error))
        except (ValueError, OSError) as exc:
            raise gcmd.error(str(exc))
        # No constructors/validation after the filesystem commit.
        self.__dict__.update(prepared)
        gcmd.respond_info('Flexi routes and availability saved. Filament execution remains locked.')

    def cmd_ACE_FLEXI_STATUS(self, gcmd):
        status=self._flexi_status()
        # Explicit diagnostics can still request the full document, but the
        # normal web/UI path must not print it to the Klipper console.
        if gcmd.get_int('VERBOSE',0):
            gcmd.respond_info(json.dumps(status,sort_keys=True))
        else:
            gcmd.respond_info('Flexi status refreshed; open the web UI for diagnostics.')

    def _flexi_block_background(self, operation):
        if getattr(self, '_ace_mode', 'normal') != 'flexi':
            return False
        logged = getattr(self, '_flexi_blocked_background_operations', None)
        if logged is None:
            logged = self._flexi_blocked_background_operations = set()
        if operation not in logged:
            logged.add(operation)
            logging.warning('[multiACE][flexi] blocked automatic %s: '
                            'filament execution remains locked', operation)
        return True

    def _flexi_runtime_source(self,head):
        service=getattr(self,'_flexi_production',None);w=service.workflow if service else None
        j=w.job if w is not None else None
        if j and j['head']==head and not j.get('failed'):
            source,phase=j['phases'][min(j['index'],len(j['phases'])-1)]
            if phase in ('seat','prime','purge','tipform','retract_path'):
                return {'ace_index':source[0],'slot':source[1]}
            return None
        owner=w.owners.get(head) if w is not None else None
        if owner and owner.get('nozzle_verified'):
            source=tuple(owner['source'])
            try:
                sample=self._flexi_evidence.read();path=self._flexi_evidence.path(sample,head)
                if path['state']=='occupied' and tuple(path['owner'])==source:return {'ace_index':source[0],'slot':source[1]}
            except Exception:pass
        # Session 2: loads run the original MultiACE loader, so its recorded
        # head_source is authoritative when it is routed to this head and the
        # load did not fail.
        src=(getattr(self,'_head_source',{}) or {}).get(head)
        if isinstance(src,dict) and not src.get('load_failed'):
            try:
                self._flexi_routes.require_destination(src.get('ace_index'),src.get('slot'),head)
                return dict(src)
            except Exception:pass
        return None

    def _flexi_display_source(self, head):
        present = set()
        for ace, info in getattr(self, '_info_per_ace', {}).items():
            # Disconnected/stale device data must not select display metadata.
            if not getattr(self, '_connected_per_ace', {}).get(ace, False):
                continue
            for slot, data in enumerate((info or {}).get('slots', [])[:4]):
                if data.get('status') in ('ready', 'loaded', 'feeding', 'retracting'):
                    present.add((ace, slot))
        service=getattr(self,'_flexi_production',None)
        owner=self._flexi_runtime_source(head) if service and service.workflow else getattr(self,'_head_source',{}).get(head)
        if service and service.workflow and service.workflow.job and service.workflow.job['head']==head:return None
        return self._flexi_routes.display_source(head,present,owner)

    def _flexi_pathchek_read(self):
        now=self.reactor.monotonic()
        live=self._flexi_collect_live()
        stats=self.printer.lookup_object('print_stats',None)
        printer_state=stats.get_status(now).get('state') if stats else 'unknown'
        live['diagnostics']=self._flexi_sensor_monitor.observe(self.printer.lookup_object,now,printer_state,self._flexi_routes)
        live['protocols']={a:getattr(p,'NAME',None) for a,p in getattr(self,'_protocols',{}).items()}
        bg=self.printer.lookup_object('ace_bg_swap',None)
        live['pathchek_safe']=(getattr(self,'_ace_mode',None)=='flexi' and not getattr(self,'_swap_in_progress',False)
            and not getattr(self,'_bg_staged',{}) and not getattr(self,'_unload_all_active',False)
            and not getattr(self,'_loading_heads',set()) and not getattr(bg,'_busy',None)
            and getattr(self,'_calibration',{}).get('state')=='idle')
        # Use existing U1 latch and homing interfaces; never select/move a tool.
        try:
            if any(self._v1_fa_blocked_by_homing(a) for a,p in getattr(self,'_protocols',{}).items() if getattr(p,'NAME',None)=='v1'):
                live['pathchek_safe']=False
            heads=self.printer.lookup_object('extruder_list',None)
            if not isinstance(heads,(list,tuple)) or len(heads)!=4:raise ValueError('Four physical toolhead latch states required')
            live['head_states']=[]
            for h,ext in enumerate(heads):
                expected='extruder' if h==0 else 'extruder%d'%h
                latch=ext.get_park_detector_status().get('state')
                if ext.get_name()!=expected or latch not in ('ACTIVATE','PARKED') or ext.get_status(now).get('activating_move')is not False:
                    raise ValueError('Toolhead selection/latch state is changing or unknown')
                live['head_states'].append(latch)
        except (AttributeError,ValueError,TypeError):live['pathchek_safe']=False
        live['source_select_context']=self._flexi_production.source_select.context()
        live['flexible_permissions']={}
        any_flexible=False
        for a,device in live.get('devices',{}).items():
            device['effective_materials']={slot:(self._override_for(a,slot) or {}).get('material') or raw for slot,raw in device.get('materials',{}).items()}
            any_flexible |= any(flexible(raw) or flexible(device['effective_materials'].get(slot)) for slot,raw in device.get('materials',{}).items())
        if any_flexible:
            # Reuse existing scoped consent and its route/material invalidation.
            self._flexi_production.inventory()
            for key in self._flexi_routes.as_dict():
                a,slot=map(int,key.split(':'));identity=live['devices'].get(a,{}).get('source_identities',{}).get(slot)
                live['flexible_permissions'][key]=all(self._flexi_production.risk.allows((a,slot),identity,action) for action in ('load','unload'))
        config=self.printer.lookup_object('configfile',None)
        settings=config.get_status(now).get('settings',{}) if config else {}
        # Bind polarity/measurement records to actual relevant sensor config.
        live['sensor_configuration']={name:settings.get(name) for name in
            ('filament_motion_sensor e%d_filament'%h for h in range(4))}
        if not all(isinstance(v,dict) and v for v in live['sensor_configuration'].values()):
            live['pathchek_safe']=False
        try:
            document=self._flexi_routing_document_cached()
            live['ace_config_revision']=document['revision']
            if (document['routes'].context()!=self._flexi_routes.context()
                or ProductionService.validate_config(self._flexi_routes,document['production'])!=ProductionService.validate_config(self._flexi_routes,self._flexi_production_raw)):
                live['pathchek_safe']=False
        except (AttributeError,ValueError,OSError):live['pathchek_safe']=False
        return live

    def _flexi_native_pathchek(self):
        session=getattr(self,'_flexi_pathchek',None)
        if session is None or session.c is not self._flexi_coordinator or session.sources is not self._flexi_production.source_select:
            if session and session.job and session.job['phase'] not in ('complete','reconciled'):
                raise RouteError('Cannot replace unreconciled native PathChek resources')
            driver=NativePathChekAdapter(self._flexi_routes,self._flexi_coordinator,
                self._flexi_production.source_select,self._flexi_sensor_monitor,
                self._flexi_collect_live,lambda a:getattr(getattr(self,'_protocols',{}).get(a),'NAME',None),
                clock=self.reactor.monotonic,sender=getattr(self,'send_request_to',None),reject=getattr(self,'_tipform_rejected',None))
            session=NativePathChekSession(self._flexi_routes,self._flexi_coordinator,
                self._flexi_production.source_select,driver,self._flexi_pathchek_read,clock=self.reactor.monotonic)
            self._flexi_pathchek=session
        return session

    def cmd_ACE_FLEXI_PATHCHEK(self,gcmd):
        try:return self._flexi_pathchek_command(gcmd)
        except (ValueError,OSError,KeyError,TypeError) as exc:
            # Invalid input/storage failures must be command errors, not fatal
            # unhandled exceptions in Klipper's G-code dispatcher.
            raise gcmd.error('PathChek: '+str(exc))

    def _flexi_pathchek_command(self,gcmd):
        if getattr(self, '_flexi_init_error', None):
            raise gcmd.error('Flexi journal is unavailable; preserve it for recovery')
        payload=self._flexi_decode_production(gcmd)
        validate_pathchek(payload,self._flexi_routes)
        # Cancellation remains possible with a lost lease; it never feeds.
        if payload['action'] not in ('cancel','heartbeat'):self._flexi_test_idle(gcmd)
        if self._flexi_coordinator.journal is None:
            if payload['action']!='qualify':raise gcmd.error('Record reviewed physical qualifications before starting PathChek')
            # Malformed/unknown measurements must not create an execution journal.
            self._flexi_native_pathchek().check_qualification(tuple(payload['source']),payload['record'],payload['confirmation'])
            workflow=getattr(self._flexi_production,'workflow',None)
            if workflow and (workflow.job or workflow.print_session or workflow.owners):raise gcmd.error('Existing production ownership blocks runtime preparation')
            state=self._flexi_coordinator.snapshot()
            if state['operations'] or state['printing']:raise gcmd.error('Existing resources must be released')
            self._flexi_coordinator=LiveCoordinator(self._flexi_routes,self._flexi_evidence,
                journal=AtomicJournal(self._flexi_journal_path),clock=self.reactor.monotonic,max_age=5.)
        session=self._flexi_native_pathchek()
        try:result=session.request(payload)
        finally:
            if session.job and session.job['phase'] not in ('complete','reconciled','recovery_required') and not getattr(self,'_flexi_pathchek_timer_active',False):
                self._flexi_pathchek_timer_active=True
                self.reactor.register_timer(self._flexi_pathchek_tick,self.reactor.NOW)
        gcmd.respond_info(json.dumps(result,sort_keys=True))

    def _flexi_pathchek_tick(self,eventtime):
        session=getattr(self,'_flexi_pathchek',None)
        try:
            if session:session.tick()
            if session and session.job and session.job['phase'] not in ('complete','reconciled','recovery_required'):
                return eventtime+.05
        except Exception as exc:
            logging.exception('[multiACE][PathChek] watchdog failure; preserving recovery')
            if session:
                session.armed=False
                try:session._fault(str(exc))
                except Exception:logging.exception('[multiACE][PathChek] nested recovery failure; durable reservation remains blocking')
        self._flexi_pathchek_timer_active=False
        return self.reactor.NEVER

    def _flexi_diagnostics_status(self):
        monitor = getattr(self, '_flexi_sensor_monitor', None)
        if monitor is None:
            return {'available': False, 'read_only': True, 'native_dispatch_enabled': False,
                    'reason': 'Diagnostic monitor was not initialized'}
        try:
            now = self.reactor.monotonic()
            stats = self.printer.lookup_object('print_stats', None)
            state = stats.get_status(now).get('state') if stats else 'unknown'
            report = monitor.observe(self.printer.lookup_object, now, state, self._flexi_routes)
            service = self._flexi_production
            adapter = NativePathChekAdapter(self._flexi_routes, self._flexi_coordinator,
                service.source_select, monitor, self._flexi_collect_live,
                lambda a: getattr(getattr(self, '_protocols', {}).get(a), 'NAME', None))
            report.update(available=True, pathchek=adapter.readiness())
            return report
        except Exception as exc:
            # Never present an old cached report as a successful current poll.
            return {'available': False, 'read_only': True, 'native_dispatch_enabled': False,
                    'reason': str(exc)[:256], 'physical_verified': False}

    # The `ace` object is subscribed to by the web/UI status poll at ~15 Hz.
    # Every full Flexi collection re-reads the routing document, re-hashes the
    # route and recipe contexts, derives PathChek readiness and builds the whole
    # inventory; at subscription rate that work saturated the host and starved
    # MCU step delivery (0003-0522 Timer too close during homing). Reuse one
    # collection for a short window. Explicit ACE_FLEXI_STATUS stays uncached.
    FLEXI_STATUS_CACHE_S = 2.0
    # This collection costs 200-500 ms on the reactor thread (routing document
    # reads, inventory, PathChek readiness). Rebuilding it while moves are
    # queued starves the MCU step queue and produces "Timer too close" during
    # long travels, so a moving toolhead only ever reuses the last snapshot.
    FLEXI_STATUS_MOVING_CACHE_S = 30.0

    # ---- route discovery ------------------------------------------------
    def _flexi_discovery_sources(self, ace=None, slot=None):
        if ace is not None and slot is not None:
            return [(int(ace), int(slot))]
        out = []
        for a in sorted(getattr(self, '_gate_status_per_ace', {}) or {}):
            if ace is not None and a != ace:
                continue
            for s, gate in enumerate(self._gate_status_per_ace.get(a) or []):
                if gate == GATE_AVAILABLE:
                    out.append((int(a), s))
        return out

    def _flexi_discovery_status(self):
        discovery = getattr(self, '_flexi_discovery', None)
        if discovery is None or getattr(self, '_ace_mode', None) != 'flexi':
            return {'available': False}
        # get_status is polled several times a second: rebuild at most every
        # 2 s while idle, every poll while a run is active (live progress).
        now = self.reactor.monotonic()
        cached = getattr(self, '_flexi_discovery_status_cache', None)
        if cached is not None and not discovery.running and now - cached[0] < 2.0:
            return cached[1]
        out = discovery.status()
        out['available'] = True
        out['sources'] = ['%d:%d' % s for s in self._flexi_discovery_sources()] if not discovery.running else out.get('sources', [])
        if not discovery.running:
            try:
                out['blockers'] = discovery.blockers(self._flexi_discovery_sources())
            except Exception as exc:
                out['blockers'] = ['Discovery readiness unavailable: %s' % exc]
        self._flexi_discovery_status_cache = (now, out)
        return out

    def cmd_ACE_FLEXI_DISCOVER(self, gcmd):
        """Flexi route discovery. ACTION=start (CONFIRM=DISCOVER_ROUTES,
        optional ACE/SLOT/SPEED/MAX/MARGIN), stop, acknowledge, clear.
        Start moves filament; it is never run by anything but this command."""
        if getattr(self, '_ace_mode', None) != 'flexi':
            raise gcmd.error('Route discovery is a Flexi feature; switch to Flexi mode first.')
        self._flexi_require_execution()
        discovery = self._flexi_discovery
        action = gcmd.get('ACTION', 'status').lower()
        try:
            if action == 'stop':
                self._flexi_discovery_abort = True
                gcmd.respond_info('Route discovery will stop and pull the current slot back.')
                return
            if action == 'acknowledge':
                discovery.acknowledge(); gcmd.respond_info('Discovery failure acknowledged.'); return
            if action == 'clear':
                discovery.clear_results(); gcmd.respond_info('Discovery results cleared.'); return
            if action != 'start':
                gcmd.respond_info(json.dumps(self._flexi_discovery_status(), sort_keys=True)[:4000]); return
            if gcmd.get('CONFIRM', '') != DISCOVERY_CONFIRMATION:
                raise gcmd.error('Route discovery moves filament. Confirm with CONFIRM=%s.' % DISCOVERY_CONFIRMATION)
            ace = gcmd.get_int('ACE', None, minval=0, maxval=15)
            slot = gcmd.get_int('SLOT', None, minval=0, maxval=3)
            if slot is not None and ace is None:
                raise gcmd.error('SLOT needs ACE.')
            raw = {}
            for key, name in (('speed', 'SPEED'), ('max_mm', 'MAX'), ('margin_mm', 'MARGIN'), ('retract_speed', 'RETRACT_SPEED'), ('return_pct', 'RETURN_PCT')):
                value = gcmd.get_float(name, None)
                if value is not None:
                    raw[key] = value
            sources = discovery.begin(self._flexi_discovery_sources(ace, slot), raw)
        except DiscoveryError as exc:
            raise gcmd.error(str(exc))
        self._flexi_discovery_abort = False
        gcmd.respond_info('Route discovery started for %d slot(s). Stop: ACE_FLEXI_DISCOVER ACTION=stop' % len(sources))
        def _run(eventtime, sources=sources):
            result = discovery.run(sources)
            self._flexi_discovery_abort = False
            logging.info('[multiACE][flexi] route discovery %s: %s', result['state'],
                         json.dumps(result['results'], sort_keys=True))
        self.reactor.register_callback(_run)

    def cmd_ACE_FLEXI_PROFILE(self, gcmd):
        """Read-only diagnostic: profile one Flexi status build and log the
        most expensive functions. No movement, no state change."""
        import cProfile, pstats, io
        prof=cProfile.Profile();t0=time.monotonic()
        prof.enable()
        try:self._flexi_status()
        finally:prof.disable()
        total=(time.monotonic()-t0)*1000
        out=io.StringIO();pstats.Stats(prof,stream=out).sort_stats('cumulative').print_stats(30)
        logging.info('[multiACE][flexi][perf] profiled status build %.0f ms %s',total,out.getvalue()[:12000])
        gcmd.respond_info('Flexi status build %.0f ms; profile written to klippy.log'%total)

    def _flexi_routing_document_cached(self):
        """Parsed ace.cfg routing document, re-parsed only when the file
        changes (configparser on the whole file cost ~12 ms per call and ran
        several times per status build)."""
        path = Path(self.ACE_CFG_PATH)
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size, getattr(st, 'st_ino', 0))
        cached = getattr(self, '_flexi_routing_doc_cache', None)
        if cached is not None and cached[0] == key:
            return cached[1]
        document = routing_document(path.read_bytes())
        self._flexi_routing_doc_cache = (key, document)
        return document

    def _flexi_status_cached(self):
        now = self.reactor.monotonic()
        cached = getattr(self, '_flexi_status_cache', None)
        moving = False
        th = self.printer.lookup_object('toolhead', None)
        if th is not None:
            try:
                st = th.get_status(now)
                pt = float(st.get('print_time', 0.))
                moving = pt > float(st.get('estimated_print_time', 0.)) + 0.05
                previous = getattr(self, '_flexi_status_print_time', None)
                if previous is not None and abs(pt - previous) > 1e-6:
                    moving = True
                self._flexi_status_print_time = pt
            except Exception:
                moving = True
        ttl = self.FLEXI_STATUS_MOVING_CACHE_S if moving else self.FLEXI_STATUS_CACHE_S
        if cached is not None and 0 <= now - cached[0] < ttl:
            return cached[1]
        # Session 2: the status is a read model for the UI (Klipper's safety
        # decisions never use it). Rebuilding it every 2 s cost ~150 ms on
        # the printer = a reactor stall per web poll. Rebuild only when an
        # input changed, or after FLEXI_STATUS_MAX_AGE_S for time-based fields.
        key = self._flexi_status_key()
        if (cached is not None and len(cached) > 2 and key is not None
                and cached[2] == key and 0 <= now - cached[0] < self._flexi_status_max_age()):
            return cached[1]
        if moving and cached is not None:
            return cached[1]
        _t0 = time.monotonic()
        status = self._flexi_status()
        _dt = time.monotonic() - _t0
        if _dt > 0.05:
            # Session 2 stall investigation: a slow build is a reactor stall.
            last = getattr(self, '_flexi_perf_logged', 0.)
            if now - last > 30.:
                self._flexi_perf_logged = now
                logging.info('[multiACE][flexi][perf] status build %.0f ms', _dt * 1000)
        # A stub for a mode that never initialised Flexi must not be cached:
        # the real routes can appear without a restart when the mode changes.
        if not status.get('uninitialized'):
            self._flexi_status_cache = (now, status, self._flexi_status_key())
        return status

    FLEXI_STATUS_MAX_AGE_S = 30.0
    # Nothing time-based is live (no lease, workflow job, print session or
    # PathCheck job): an unchanged read model only needs an occasional
    # refresh. Every forced rebuild was a ~60 ms reactor stall (2/min idle).
    FLEXI_STATUS_IDLE_MAX_AGE_S = 300.0

    def _flexi_status_max_age(self):
        try:
            service = getattr(self, '_flexi_production', None)
            workflow = getattr(service, 'workflow', None)
            native = getattr(self, '_flexi_pathchek', None)
            discovery = getattr(self, '_flexi_discovery', None)
            if (getattr(service, 'lease', None) or getattr(workflow, 'job', None)
                    or getattr(workflow, 'print_session', None)
                    or getattr(native, 'job', None)
                    or (discovery is not None and discovery.running)):
                return self.FLEXI_STATUS_MAX_AGE_S
        except Exception:
            return self.FLEXI_STATUS_MAX_AGE_S
        return self.FLEXI_STATUS_IDLE_MAX_AGE_S

    def _flexi_status_key(self):
        """Cheap fingerprint of everything the status is derived from."""
        try:
            def mtime(path):
                try:
                    st = os.stat(path)
                    return (st.st_mtime_ns, st.st_size)
                except OSError:
                    return None
            sensors = []
            for head in range(4):
                sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
                sensors.append(None if sensor is None else repr(sorted(sensor.get_status(0).items())))
            service = getattr(self, '_flexi_production', None)
            workflow = getattr(service, 'workflow', None)
            job = getattr(workflow, 'job', None)
            ledger = getattr(self, '_flexi_paths', None)
            return (getattr(self, '_ace_mode', None),
                    mtime(getattr(self, '_flexi_journal_path', '') or ''),
                    mtime(getattr(ledger, 'path', None) or ''),
                    self._flexi_routes.encode() if getattr(self, '_flexi_routes', None) is not None else None,
                    getattr(self, '_flexi_production_raw', None),
                    repr(sorted((getattr(self, '_flexi_sync_fp', {}) or {}).items())),
                    repr(sorted((getattr(self, '_connected_per_ace', {}) or {}).items())),
                    repr(getattr(self, '_head_source', None)), tuple(sensors),
                    repr(job and (job.get('token'), job.get('index'), job.get('failed'))),
                    repr(getattr(workflow, 'print_session', None) and workflow.print_session.get('token')),
                    repr(getattr(self, '_slot_overrides', None)),
                    repr(sorted(getattr(getattr(service, 'risk', None), 'grants', {}) or {})),
                    repr(getattr(service, 'lease', None)),
                    id(service), getattr(self, '_flexi_init_error', None))
        except Exception:
            return None

    def _flexi_status(self):
        # Routes are only built when the saved mode is Flexi (ace._flexi_init).
        # get_status is queried in EVERY mode, so a printer parked in
        # multi/normal must not raise AttributeError here - that shutdown the
        # whole firmware at boot after a mode switch.
        if getattr(self, '_flexi_routes', None) is None:
            return {
                'routes': {}, 'slot_status': {},
                'topology': {'joiners': {}, 'manual_overrides': {}},
                'uninitialized': True,
                'diagnostics': {},
                'pathchek': {'name': 'Flexi PathChek', 'schema': 2,
                             'default_execution_enabled': False,
                             'native_dispatch_enabled': False, 'job': None,
                             'routes': [], 'qualifications': {},
                             'results': [],
                             'reason': 'Flexi is not initialised in this mode.'},
                'route_verification': {},
                'release_candidate': RC_RELEASE,
            }
        status = self._flexi_routes.status()
        if getattr(self,'_flexi_init_error',None):status['journal_error']=self._flexi_init_error
        status['diagnostics'] = self._flexi_diagnostics_status()
        session=getattr(self,'_flexi_pathchek',None)
        if getattr(self,'_flexi_sensor_monitor',None) is not None:
            try:session=self._flexi_native_pathchek()
            except (AttributeError,ValueError,OSError):pass
        status['pathchek']=session.status() if session else {'name':'Flexi PathChek','schema':2,'default_execution_enabled':False,'native_dispatch_enabled':False,'job':None,'routes':[],'qualifications':{},'results':[],'reason':'Record route-specific physical qualifications before commissioning; no native session is authorized.'}
        try:
            document = self._flexi_routing_document_cached()
            status['route_revision'] = document['revision']
            status['route_conflict'] = document['routes'].context()!=self._flexi_routes.context()
        except (AttributeError, OSError, ValueError) as exc:
            status['route_revision'] = None
            status['route_save_error'] = str(exc)
        try:
            state=self._flexi_coordinator.snapshot()
            discoveries=copy.deepcopy(state.get('pathchek_discoveries',{}))
        except (AttributeError,ValueError,OSError):discoveries={}
        status['discoveries']=discoveries
        views={}
        live_contexts={row['key']:row.get('context') for row in status['pathchek'].get('routes',[])}
        topology=self._flexi_routes.topology.as_dict()
        current_context=hashlib.sha256(self._flexi_routes.context_encode().encode()).hexdigest()
        for key in set(self._flexi_routes.as_dict())|set(discoveries):
            configured=self._flexi_routes.as_dict().get(key);observation=discoveries.get(key)
            valid=bool(observation and observation.get('verified') and observation.get('observed_head')==configured and observation.get('route_context')==current_context and observation.get('qualification_context')==live_contexts.get(key) and observation.get('qualification_digest')==digest(status['pathchek'].get('qualifications',{}).get(key,{})))
            verification='Pass' if valid else 'Conflict' if observation and observation.get('route_context')==current_context and observation.get('observed_head')!=configured else 'Unverified'
            joins=[dict(id=jid,**j) for jid,j in topology['joiners'].items() if key in j['branches']]
            last_job=state.get('pathchek_job') if 'state' in locals() else None
            blocked=bool(last_job and '%d:%d'%tuple(last_job['source'])==key and last_job['phase']=='recovery_required')
            views[key]={'configured_head':configured,'observed_head':observation.get('observed_head') if observation else None,
                'verification':verification,'test_result':'Pass' if valid else 'Fail' if verification=='Conflict' else 'Blocked' if blocked else observation.get('status','Unverified') if observation else 'Unverified',
                'manual_override':topology['manual_overrides'].get(key,configured is not None),
                'evidence':observation,'joiners':joins,'movement_authorized':False}
        status['route_verification']=views
        status['release_candidate'] = RC_RELEASE
        coordinator = getattr(self, '_flexi_coordinator', None)
        status['interlocks'] = coordinator.snapshot() if coordinator is not None else None
        provider = getattr(self, '_flexi_evidence', None)
        observations = provider.status() if provider is not None else {'available': False}
        status['live_evidence_available'] = observations['available']
        status['observations'] = observations
        controller=getattr(self,'_flexi_test_controller',None)
        status['controlled_test']=controller.status() if controller is not None else {'prepared':False,'execution_enabled':False}
        profiles=getattr(self,'_flexi_test_profiles',None)
        status['test_profiles']=json.loads(profiles.encode()) if profiles is not None else {'heads':{},'routes':{}}
        production=getattr(self,'_flexi_production',None)
        try:status['production']=production.status() if production else {'execution_authorized':False,'print_ready':False}
        except Exception as exc:status['production']={'execution_authorized':False,'print_ready':False,'reason':str(exc)}
        status['display_sources'] = {
            str(head): list(source) if source is not None else None
            for head in range(4)
            for source in (self._flexi_display_source(head),)}
        status['feature_support'] = {'native_mode':True,'slot_routing':True,'spool_metadata':True,'route_aware_load_unload':True,'shared_switching':True,'eligible_backup_failover':True,'live_ace_replacement':False,'hardware_qualified':False}
        status['capabilities'] = {
            'manual_filament': False, 'printing': False,
            'automatic_failover': False, 'color_switching': False,
            'live_interlocks': False}

        return status

    def _flexi_revoke_session(self):
        native=getattr(self,'_flexi_pathchek',None)
        if native and native.job and native.job['phase'] not in ('complete','reconciled','recovery_required'):
            native._fault('Operating-mode/session revocation; physical reconciliation required')
        service=getattr(self,'_flexi_production',None)
        if service:
            service.lease=None;service.risk.grants.clear()
            wizard=getattr(service,'wizard',None)
            if wizard:
                wizard.cancel('Operating mode changed; review physical routing before authorization')
                if wizard.session:wizard.session['stale']=True
        provider=getattr(self,'_flexi_evidence',None)
        if provider:
            for head in range(4):provider.invalidate(head)

    def _flexi_enter(self, gcmd):
        if getattr(self,'_flexi_init_error',None):raise gcmd.error('Flexi journal is unavailable; keep the existing file for recovery')
        if self._ace_mode not in ('multi', 'head', 'flexi'):
            raise gcmd.error('Start in multi or head mode before selecting experimental Flexi')
        self._flexi_assert_idle(gcmd)
        try:
            self._flexi_routes.validate_units(range(len(self._ace_devices)), allow_absent=True)
            if not self._flexi_routes.as_dict():
                raise RouteError('Configure at least one Flexi route first')
        except RouteError as exc:
            raise gcmd.error(str(exc))
        self._flexi_revoke_session()
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=ace__mode VALUE=\"'flexi'\"")
        self._ace_mode = 'flexi'
        self._install_flexi_stock_load_guards()
        # Entering Flexi must not inherit the legacy mode's slot preview. The
        # stock display may still hold the active-ACE slot==head colours from
        # the previous multi/head display; clear every toolhead now. This is a
        # display-only push (no movement) and Flexi's own push path keeps the
        # empty heads clear thereafter.
        try:
            self._push_rfid_info()
        except Exception as exc:
            logging.info('[multiACE][flexi] entry display clear failed: %s', exc)
        gcmd.respond_info('Flexi selected. Route editing is available; filament execution is locked.')


class _DiscoveryIO:
    """Hardware access for RouteDiscovery, through the original MultiACE
    request path (so the Flexi motor gate still applies)."""
    BUSY_RETRIES = 4

    SETTLE_S = 15.0

    def __init__(self, host):
        self.h = host
        self.ready_seen = None

    def now(self):
        return self.h.reactor.monotonic()

    def pause(self, seconds):
        self.h.reactor.pause(self.h.reactor.monotonic() + seconds)

    def sensors(self):
        return {head: self.h._head_sensor_present(head) for head in range(4)}

    def printing(self):
        ps = self.h.printer.lookup_object('print_stats', None)
        if ps is None:
            return False
        return ps.get_status(self.h.reactor.monotonic()).get('state') in ('printing', 'paused')

    def busy(self):
        h = self.h
        # Right after a (re)start the toolhead sensors and ACE slot reports
        # are not trustworthy yet: an unreported sensor reads as "no filament".
        try:
            ready = h.printer.get_state_message()[1] == 'ready'
        except Exception:
            ready = False
        if not ready:
            self.ready_seen = None
            return 'Klipper is not ready.'
        if self.ready_seen is None:
            self.ready_seen = self.now()
        if self.now() - self.ready_seen < self.SETTLE_S:
            return 'Klipper just started; waiting for the sensors and ACEs to report. Try again in a few seconds.'
        try:
            for d in h._unit_devices_status():
                if d.get('connected') and not (getattr(h, '_gate_status_per_ace', {}) or {}).get(d.get('place')):
                    return 'ACE%s has not reported its slots yet.' % d.get('place')
        except Exception:
            return 'ACE unit status is unavailable.'
        if getattr(h, '_locate_busy', False):
            return 'An ACE locate jog is running.'
        native = getattr(h, '_flexi_pathchek', None)
        if native is not None and getattr(native, 'job', None):
            return 'A PathCheck operation is active.'
        if getattr(h, '_in_internal_unload_head', False):
            return 'An unload is running.'
        return None

    def paths_not_clear(self):
        out = []
        for head in range(4):
            if self.h._flexi_paths.record(head).get('state') != 'clear' or self.h._flexi_head_recovery(head):
                out.append(head)
        return out

    def slot_ready(self, ace, slot):
        gates = (getattr(self.h, '_gate_status_per_ace', {}) or {}).get(ace) or []
        if not 0 <= slot < len(gates) or gates[slot] != GATE_AVAILABLE:
            return False
        try:
            devices = self.h._unit_devices_status()
        except Exception:
            return False
        return any(d.get('place') == ace and d.get('connected') for d in devices)

    def set_scope(self, source):
        self.h._flexi_discovery_scope = tuple(source) if source is not None else None

    def abort_requested(self):
        return bool(getattr(self.h, '_flexi_discovery_abort', False))

    def _move(self, ace, slot, method, length, speed):
        request = {'method': method, 'params': {'index': slot, 'length': int(length), 'speed': int(speed)}}
        for attempt in range(self.BUSY_RETRIES):
            resp = self.h._tipform_send(ace, copy.deepcopy(request), timeout=3.0)
            msg = str((resp or {}).get('msg', '') or '').strip().upper() if isinstance(resp, dict) else ''
            logging.info('[multiACE][flexi] discovery ACE%d slot %d %s %dmm @%d attempt %d -> %r',
                         ace, slot, method, int(length), int(speed), attempt + 1, resp)
            if msg == 'EMPTY':
                return 'empty'
            if msg == 'FLEXI_MOTION_LOCKED':
                return 'locked'
            if isinstance(resp, dict) and resp.get('code', -1) == 0 and msg in ('SUCCESS', 'OK', ''):
                return 'ok'
            self.pause(1.5)
        return 'rejected'

    def feed(self, ace, slot, length, speed):
        return self._move(ace, slot, 'feed_filament', length, speed)

    def start_retract(self, ace, slot, length, speed):
        return self._move(ace, slot, 'unwind_filament', length, speed)

    def slot_empty(self, ace, slot):
        gates = (getattr(self.h, '_gate_status_per_ace', {}) or {}).get(ace) or []
        if 0 <= slot < len(gates) and gates[slot] != GATE_AVAILABLE:
            return True
        try:
            sl = ((self.h._info_per_ace.get(ace) or {}).get('slots') or [])[slot] or {}
            return bool(self.h._is_empty_status(sl.get('status')))
        except Exception:
            return False

    def stop_unwind(self, ace, slot):
        request = {'method': 'stop_unwind_filament', 'params': {'index': slot}}
        for attempt in range(4):
            resp = self.h._tipform_send(ace, copy.deepcopy(request), timeout=2.0)
            logging.info('[multiACE][flexi] discovery ACE%d slot %d stop_unwind attempt %d -> %r',
                         ace, slot, attempt + 1, resp)
            if not self.h._tipform_rejected(resp):
                return True
            self.pause(0.15)
        return False

    def stop_feed(self, ace, slot):
        self.h._stop_feeding(slot, ace)

    def wait_idle(self, ace, timeout=30.0):
        self.pause(0.4)
        t0 = self.now()
        while not self.h._locate_unit_idle(ace) and self.now() - t0 < timeout:
            self.pause(0.25)

    def mark_uncertain(self, head, source, basis):
        try:
            self.h._flexi_paths.adopt_uncertain(head, tuple(source), basis)
        except Exception:
            logging.exception('[multiACE][flexi] could not record discovery uncertainty')
