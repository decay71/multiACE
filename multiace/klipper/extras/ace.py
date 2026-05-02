import logging
import logging.handlers
import json
import struct
import queue
import traceback
import os
import time
import hashlib
import serial
from serial import SerialException

MULTIACE_VERSION = "0.91b"
MULTIACE_CODENAME = "Vibrant Fungi"
MULTIACE_BUILD_TAG = "fceb122"
MULTIACE_BUNDLE_SHA1 = "a56bab3"

def _setup_file_logger(name, filepath, max_bytes=1048576, backup_count=3):

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            filepath, maxBytes=max_bytes, backupCount=backup_count)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        logger.addHandler(handler)
    return logger

class AceException(Exception):
    pass

GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1

class BunnyAce:
    VARS_ACE_REVISION = 'ace__revision'
    VARS_ACE_ACTIVE_DEVICE = 'ace__active_device'
    VARS_ACE_HEAD_SOURCE = 'ace__head_source'

    def __init__(self, config):
        self._connected = False
        self._serial = None
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self._name = config.get_name()
        self.send_time = None
        self.ace_dev_fd = None
        self.heartbeat_timer = None

        self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]
        self.read_buffer = bytearray()
        if self._name.startswith('ace '):
            self._name = self._name[4:]

        self.save_variables = self.printer.lookup_object('save_variables', None)
        if self.save_variables:
            revision_var = self.save_variables.allVariables.get(self.VARS_ACE_REVISION, None)
            if revision_var is None:
                config.error("You have custom [save_variables]. "
                             "Copy the contents of ace_vars.cfg to your file and remove [save_variables] in ace.cfg")
        else:
            config.error("There is no [save_variables] in the config. Check installation guide")

        self.serial_id = config.get('serial', '')
        self.baud = config.getint('baud', 115200)
        self._ace_devices = []
        self._active_device_index = 0

        self._ace_canonical = None
        self._ace_startup_failed = False
        self._ace_present = set()

        self.ace_device_count = config.getint('ace_device_count', 1, minval=1, maxval=8)

        cfg_print_mode = config.get('print_mode', None)
        if cfg_print_mode is not None:
            logging.info(
                '[multiACE] print_mode=%s ignored (obsolete in v0.82+)'
                % cfg_print_mode)

        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_length = config.getint('retract_length', 100)
        self.feed_length = config.getint('feed_length', 0)

        self.load_length = config.getint('load_length', 2000)
        self.load_retry = config.getint('load_retry', 3)
        self.load_retry_retract = config.getint('load_retry_retract', 50)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.extra_purge_length = config.getfloat('extra_purge_length', 50, minval=0, maxval=200)
        self.seat_overshoot_length = config.getint('seat_overshoot_length', 30, minval=0, maxval=100)
        self.swap_default_temp = config.getint('swap_default_temp', 250, minval=180, maxval=300)
        self.swap_retract_length = config.getint('swap_retract_length', self.retract_length, minval=20, maxval=2000)
        self.swap_anti_ooze_retract = config.getint('swap_anti_ooze_retract', 3, minval=0, maxval=50)
        self.extrusion_retry = config.getint('extrusion_retry', 7, minval=0, maxval=10)
        self.extrusion_retry_retract = config.getint('extrusion_retry_retract', 30, minval=5, maxval=200)
        self.extrusion_retry_retract_a = config.getint('extrusion_retry_retract_a', 50, minval=5, maxval=200)
        self.wiggle_scheme = (config.get('wiggle_scheme', 'EAEAEAE') or 'EAEAEAE').upper()
        for c in self.wiggle_scheme:
            if c not in ('E', 'A'):
                raise config.error(
                    "wiggle_scheme: invalid char %r (only 'E' and 'A' allowed)" % c)
        config.getint('extrusion_stock_retry', 5, minval=1, maxval=50)
        self.unload_retry = config.getint('unload_retry', 3, minval=1, maxval=10)
        self.dryer_temp = config.getint('dryer_temp', 55, minval=30, maxval=70)
        self.dryer_duration = config.getint('dryer_duration', 240, minval=10, maxval=480)

        self.head_feed_length = {}
        self.head_load_length = {}
        self.head_load_retry = {}
        self.head_load_retry_retract = {}
        for i in range(4):
            self.head_feed_length[i] = config.getint('feed_length_%d' % i, self.feed_length)
            self.head_load_length[i] = config.getint('load_length_%d' % i, self.load_length)
            self.head_load_retry[i] = config.getint('load_retry_%d' % i, self.load_retry)
            self.head_load_retry_retract[i] = config.getint('load_retry_retract_%d' % i, self.load_retry_retract)

        self._ace_section_load_length = {}
        self._ace_section_load_length_slot = {}
        self._ace_section_retract_length = {}
        self._ace_section_retract_length_slot = {}
        for ace_sec in config.get_prefix_sections('ace '):
            sec_name = ace_sec.get_name()
            try:
                ace_i = int(sec_name.split()[1])
            except (IndexError, ValueError):
                continue
            ll = ace_sec.getint('load_length', None, minval=1)
            if ll is not None:
                self._ace_section_load_length[ace_i] = ll
            rl = ace_sec.getint('retract_length', None, minval=1)
            if rl is not None:
                self._ace_section_retract_length[ace_i] = rl
            for slot_i in range(4):
                ll_s = ace_sec.getint('load_length_%d' % slot_i, None, minval=1)
                if ll_s is not None:
                    self._ace_section_load_length_slot[(ace_i, slot_i)] = ll_s
                rl_s = ace_sec.getint('retract_length_%d' % slot_i, None, minval=1)
                if rl_s is not None:
                    self._ace_section_retract_length_slot[(ace_i, slot_i)] = rl_s

        self.ace_dryer_temp = {}
        self.ace_dryer_duration = {}
        for i in range(4):
            self.ace_dryer_temp[i] = config.getint('dryer_temp_%d' % i, self.dryer_temp)
            self.ace_dryer_duration[i] = config.getint('dryer_duration_%d' % i, self.dryer_duration)

        def _parse_idx_list(key):
            raw = config.get(key, '').strip()
            out = set()
            if raw:
                for token in raw.split(','):
                    token = token.strip()
                    if token.isdigit():
                        out.add(int(token))
            return out
        self._fa_print_disable = _parse_idx_list('fa_print_disable')
        self._fa_load_disable = _parse_idx_list('fa_load_disable')
        self.fa_debug = config.getboolean('fa_debug', False)

        self._callback_map = {}
        self._feed_assist_index = -1
        self._request_id = 0

        self._serials = {}
        self._connected_per_ace = {}
        self._serial_failed_per_ace = {}
        self._info_per_ace = {}
        self._feed_assist_per_ace = {}
        self._callback_maps = {}
        self._request_ids = {}
        self._read_buffers = {}
        self._ace_dev_fds = {}
        self._heartbeat_timers = {}
        self._connect_timers_per_ace = {}
        self._gate_status_per_ace = {}

        self._head_source = {0: None, 1: None, 2: None, 3: None}

        self._swap_in_progress = False
        self._test_cancel = False
        self._auto_feed_enabled = False
        self._fa_context = 'idle'
        self._retract_length_override = None
        self._last_unload_ok = True
        self._last_load_ok = True
        self._ghost_heads = set()
        self._hotplug_gone = {}

        self._serial_failed = False
        self._serial_failed_at = 0.0
        self._serial_failed_pause_sent = False

        log_dir = config.get('log_dir', '/home/lava/printer_data/logs')
        self._usb_log = _setup_file_logger(
            'multiace_usb', os.path.join(log_dir, 'multiace_usb.log'))
        self._state_log = _setup_file_logger(
            'multiace_state', os.path.join(log_dir, 'multiace_state.log'))
        self._telemetry_log = _setup_file_logger(
            'multiace_telemetry', os.path.join(log_dir, 'multiace_telemetry.log'))
        self._wiggle_log = _setup_file_logger(
            'multiace_wiggle', os.path.join(log_dir, 'multiace_wiggle.log'))
        self._fa_log = _setup_file_logger(
            'multiace_fa', os.path.join(log_dir, 'multiace_fa.log'))
        self._state_debug_enabled = config.getboolean('state_debug', False)
        self._usb_debug_enabled = config.getboolean('usb_debug', True)
        self._apply_log_levels()
        self._last_switch_auto_ts = None
        self._fa_any_active_since = None
        self._fa_last_active_ts = time.monotonic()
        self._fa_gap_threshold_ms = config.getint(
            'fa_gap_threshold_ms', 3000, minval=100)
        self._fa_settle_after_stop = config.getfloat(
            'fa_settle_after_stop', 1.5, minval=0.0, maxval=10.0)
        self._fa_start_retries = config.getint(
            'fa_start_retries', 5, minval=0, maxval=30)
        self._fa_start_retry_delay = config.getfloat(
            'fa_start_retry_delay', 0.5, minval=0.05, maxval=5.0)

        self._usb_stats = {
            'scans': 0,
            'retries': 0,
            'connects': 0,
            'connect_failures': 0,
            'disconnects': 0,
            'errno5_total': 0,
            'errno5_recovered': 0,
            'errno5_unrecovered': 0,
            'cascades': 0,
            'start_time': time.monotonic(),
        }
        self._errno5_recent = []

        self._info = {
            'status': 'ready',
            'dryer_status': {
                'status': 'stop',
                'target_temp': 0,
                'duration': 0,
                'remain_time': 0
            },
            'temp': 0,
            'enable_rfid': 1,
            'fan_speed': 7000,
            'feed_assist_count': 0,
            'cont_assist_time': 0.0,
            'slots': [
                {
                    'index': 0,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand':'',
                    'color': [0, 0, 0]
                },
                {
                    'index': 1,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand': '',
                    'color': [0, 0, 0]
                },
                {
                    'index': 2,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand': '',
                    'color': [0, 0, 0]
                },
                {
                    'index': 3,
                    'status': 'empty1',
                    'sku': '',
                    'type': '',
                    'rfid': 0,
                    'brand': '',
                    'color': [0, 0, 0]
                }
            ]
        }
        self.extruder_sensor = None

        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)

        self.printer.register_event_handler('print_stats:start', self._on_print_start)
        self.printer.register_event_handler('print_stats:stop', self._on_print_end)

        self.gcode.register_command(
            'ACE_START_DRYING', self.cmd_ACE_START_DRYING,
            desc=self.cmd_ACE_START_DRYING_help)
        self.gcode.register_command(
            'ACE_STOP_DRYING', self.cmd_ACE_STOP_DRYING,
            desc=self.cmd_ACE_STOP_DRYING_help)
        self.gcode.register_command(
            'ACE_ENABLE_FEED_ASSIST', self.cmd_ACE_ENABLE_FEED_ASSIST,
            desc=self.cmd_ACE_ENABLE_FEED_ASSIST_help)
        self.gcode.register_command(
            'ACE_DISABLE_FEED_ASSIST', self.cmd_ACE_DISABLE_FEED_ASSIST,
            desc=self.cmd_ACE_DISABLE_FEED_ASSIST_help)
        self.gcode.register_command(
            'ACE_FEED', self.cmd_ACE_FEED,
            desc=self.cmd_ACE_FEED_help)
        self.gcode.register_command(
            'ACE_RETRACT', self.cmd_ACE_RETRACT,
            desc=self.cmd_ACE_RETRACT_help)

        self.gcode.register_command(
            'ACE_SWITCH', self.cmd_ACE_SWITCH,
            desc=self.cmd_ACE_SWITCH_help)
        self.gcode.register_command(
            'ACE_LIST', self.cmd_ACE_LIST,
            desc=self.cmd_ACE_LIST_help)

        self.gcode.register_command(
            'ACE_RUN_MODE_SWITCH', self.cmd_ACE_RUN_MODE_SWITCH,
            desc=self.cmd_ACE_RUN_MODE_SWITCH_help)

        self.gcode.register_command(
            'ACE_LOAD_HEAD', self.cmd_ACE_LOAD_HEAD,
            desc=self.cmd_ACE_LOAD_HEAD_help)
        self.gcode.register_command(
            'ACE_UNLOAD_HEAD', self.cmd_ACE_UNLOAD_HEAD,
            desc=self.cmd_ACE_UNLOAD_HEAD_help)
        self.gcode.register_command(
            'ACE_SWAP_HEAD', self.cmd_ACE_SWAP_HEAD,
            desc=self.cmd_ACE_SWAP_HEAD_help)
        self.gcode.register_command(
            'ACE_HEAD_STATUS', self.cmd_ACE_HEAD_STATUS,
            desc=self.cmd_ACE_HEAD_STATUS_help)
        self.gcode.register_command(
            'ACE_CLEAR_HEADS', self.cmd_ACE_CLEAR_HEADS,
            desc=self.cmd_ACE_CLEAR_HEADS_help)
        self.gcode.register_command(
            'ACE_UNLOAD_ALL_HEADS', self.cmd_ACE_UNLOAD_ALL_HEADS,
            desc=self.cmd_ACE_UNLOAD_ALL_HEADS_help)
        self.gcode.register_command(
            'ACE_TEST', self.cmd_ACE_TEST,
            desc=self.cmd_ACE_TEST_help)
        self.gcode.register_command(
            'ACE_TEST_CANCEL', self.cmd_ACE_TEST_CANCEL,
            desc='[multiACE] Cancel a running ACE_TEST after current step')
        self.gcode.register_command(
            'ACE_DRY', self.cmd_ACE_DRY,
            desc=self.cmd_ACE_DRY_help)
        self.gcode.register_command(
            'ACE_USB_STATS', self.cmd_ACE_USB_STATS,
            desc=self.cmd_ACE_USB_STATS_help)
        self.gcode.register_command(
            'ACE_DEBUG', self.cmd_ACE_DEBUG,
            desc=self.cmd_ACE_DEBUG_help)
        self.gcode.register_command(
            'ACE_USB_DEBUG', self.cmd_ACE_USB_DEBUG,
            desc=self.cmd_ACE_USB_DEBUG_help)
        self.gcode.register_command(
            'ACE_SEQ', self.cmd_ACE_SEQ,
            desc=self.cmd_ACE_SEQ_help)
        self.gcode.register_command(
            'ACE_PRELOAD', self.cmd_ACE_PRELOAD,
            desc=self.cmd_ACE_PRELOAD_help)
        self.gcode.register_command(
            'MACE_LOG', self.cmd_MACE_LOG,
            desc=self.cmd_MACE_LOG_help)
        self.gcode.register_command(
            'ACE_FA_TEST', self.cmd_ACE_FA_TEST,
            desc=self.cmd_ACE_FA_TEST_help)

    def _refresh_ace_devices(self, context):

        scan = self._scan_ace_devices(context)
        self._ace_present = set(scan)
        if self._ace_canonical is not None:
            self._ace_devices = list(self._ace_canonical)
        else:
            self._ace_devices = scan
        return scan

    def _is_ace_present(self, ace_index):

        if ace_index < 0 or ace_index >= len(self._ace_devices):
            return False
        if self._ace_canonical is None:
            return True
        return self._ace_devices[ace_index] in self._ace_present

    def _ace_path_sort_key(self, path):

        try:
            base = os.path.basename(path)
            segs = base.split(':')
            port_str = segs[1] if len(segs) >= 2 else ''
            port_tuple = tuple(int(x) for x in port_str.split('.') if x != '')
        except (ValueError, IndexError):
            port_tuple = ()
        return (len(port_tuple), port_tuple, path)

    def _scan_ace_devices(self, context='unknown'):
        ace_devices = []
        by_path_dir = '/dev/serial/by-path/'
        scan_start = time.monotonic()
        self._usb_stats['scans'] += 1

        if not os.path.exists(by_path_dir):
            self._usb_log.debug('SCAN [%s] by-path dir missing — 0 devices', context)
            return ace_devices

        all_entries = sorted(os.listdir(by_path_dir))
        for entry in all_entries:
            full_path = os.path.join(by_path_dir, entry)
            real_dev = os.path.basename(os.path.realpath(full_path))

            try:
                sysfs_base = '/sys/class/tty/%s/device/../' % real_dev
                with open(os.path.join(sysfs_base, 'idVendor'), 'r') as f:
                    vendor = f.read().strip()
                with open(os.path.join(sysfs_base, 'idProduct'), 'r') as f:
                    product = f.read().strip()

                if vendor == '28e9' and product == '018a':
                    ace_devices.append(full_path)
                    logging.info('[multiACE] Found device %s (%s) vendor=%s product=%s' % (full_path, real_dev, vendor, product))
            except (IOError, OSError):
                continue

        ace_devices.sort(key=self._ace_path_sort_key)

        scan_ms = (time.monotonic() - scan_start) * 1000
        self._usb_log.info('SCAN [%s] found=%d entries=%d time=%.1fms devices=[%s]',
                           context, len(ace_devices), len(all_entries), scan_ms,
                           ', '.join('%s->%s' % (d, os.path.basename(os.path.realpath(d))) for d in ace_devices))
        return ace_devices

    def _apply_log_levels(self):
        """Apply current debug flags to file-logger levels. Setting a
        logger above CRITICAL turns every .info/.warning/.error/.debug
        call on it into a no-op without touching call sites."""
        off = logging.CRITICAL + 1
        self._usb_log.setLevel(logging.DEBUG if self._usb_debug_enabled else off)
        gated = logging.DEBUG if self._state_debug_enabled else off
        self._telemetry_log.setLevel(gated)
        self._wiggle_log.setLevel(gated)
        self._fa_log.setLevel(logging.DEBUG if self.fa_debug else logging.WARNING)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        for log in (self._state_log, self._usb_log, self._telemetry_log, self._wiggle_log):
            for handler in log.handlers:
                if hasattr(handler, 'doRollover'):
                    try:
                        handler.doRollover()
                    except Exception:
                        pass

        try:
            ace_mtime = os.path.getmtime(os.path.abspath(__file__))
            from datetime import datetime
            ace_timestamp = datetime.fromtimestamp(ace_mtime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ace_timestamp = 'unknown'
        self.log_always('[multiACE] v%s "%s" build=%s (file: %s)' % (
            MULTIACE_VERSION, MULTIACE_CODENAME, MULTIACE_BUILD_TAG, ace_timestamp))
        logging.info('[multiACE] Version %s (%s) build=%s file=%s' % (
            MULTIACE_VERSION, MULTIACE_CODENAME, MULTIACE_BUILD_TAG, ace_timestamp))

        self._ace_mode = 'normal'
        if self.save_variables:
            self._ace_mode = self.save_variables.allVariables.get('ace__mode', 'normal')
        if self._ace_mode == 'normal':
            logging.info('[multiACE] Normal mode — skipping ACE detection')
            return

        if self._ace_mode == 'multi':
            self._restore_head_source()
            self.printer.register_event_handler(
                'extruder:activate_extruder', self._on_extruder_change)
        else:
            logging.info('[multiACE] SingleACE mode — no head_source tracking')

        self._refresh_ace_devices('startup')

        if self.ace_device_count is not None:
            expected = self.ace_device_count
            if len(self._ace_devices) < expected:
                self.log_always('[multiACE] Waiting for %d ACE device(s), found %d...' % (
                    expected, len(self._ace_devices)))
                deadline = time.monotonic() + 20.0
                attempt = 0
                while time.monotonic() < deadline and len(self._ace_devices) < expected:
                    self.reactor.pause(self.reactor.monotonic() + 1.0)
                    attempt += 1
                    self._refresh_ace_devices('startup_wait_%d' % attempt)
            if len(self._ace_devices) < expected:

                self._ace_startup_failed = True
                self.log_error(
                    '[multiACE] USB unstable, expected %d ACEs, found %d - '
                    'ACE inactive, Klipper continues. FIRMWARE_RESTART required '
                    'after reconnecting.' % (expected, len(self._ace_devices)))
                logging.info(
                    '[multiACE] Startup soft-fail (%d/%d ACEs) - skipping connect timer' % (
                        len(self._ace_devices), expected))
                return

            self._ace_canonical = list(self._ace_devices)
            self._ace_present = set(self._ace_canonical)
            self.log_always('[multiACE] All %d expected ACE device(s) found, canonical mapping locked' % expected)

        if self._ace_devices:
            logging.info('[multiACE] Found %d device(s): %s' % (len(self._ace_devices), str(self._ace_devices)))
            self.log_always('[multiACE] Found %d device(s)' % len(self._ace_devices))

            saved_device = self.save_variables.allVariables.get(self.VARS_ACE_ACTIVE_DEVICE, None)
            if saved_device and saved_device in self._ace_devices:
                self._active_device_index = self._ace_devices.index(saved_device)
                logging.info('[multiACE] Restored active device %d: %s' % (self._active_device_index, saved_device))
            else:
                self._active_device_index = 0

            self.serial_id = self._ace_devices[self._active_device_index]
        elif self.serial_id:
            logging.info('[multiACE] No devices auto-detected, using configured serial: %s' % self.serial_id)
        else:
            self._ace_startup_failed = True
            self.log_error(
                '[multiACE] No ACE devices found and no serial configured - '
                'ACE inactive, Klipper continues')
            return

        self._queue = queue.Queue()

        all_ok = True
        CONNECT_ATTEMPTS = 3
        for idx in range(len(self._ace_devices)):
            ok = False
            for attempt in range(CONNECT_ATTEMPTS):
                ok = self._connect_to(idx)
                if ok:
                    break
                if attempt < CONNECT_ATTEMPTS - 1:
                    self._usb_log.info(
                        'RETRY [startup_connect] idx=%d attempt=%d/%d failed, retrying in 1s',
                        idx, attempt + 1, CONNECT_ATTEMPTS)
                    time.sleep(1.0)
            if not ok:
                self.log_error(
                    '[multiACE] Failed to open ACE %d at startup after %d attempts'
                    % (idx, CONNECT_ATTEMPTS))
                all_ok = False
        if not all_ok:
            self.log_error('[multiACE] Not all ACEs opened at startup — continuing with partial set')

        self._activate_ace(self._active_device_index)

    def _hotplug_monitor(self, eventtime):

        if self._auto_feed_enabled or self._swap_in_progress:
            return eventtime + 2.0

        try:
            current = set(self._scan_ace_devices('hotplug'))
            known = set(self._ace_devices)
            now = self.reactor.monotonic()

            for dev in known - current:
                if dev not in self._hotplug_gone:
                    self._hotplug_gone[dev] = now

            for dev in list(self._hotplug_gone.keys()):
                if dev in current:
                    gone_time = now - self._hotplug_gone[dev]
                    del self._hotplug_gone[dev]
                    if gone_time >= 5.0:

                        fresh_devices = sorted(current)
                        if dev in fresh_devices:
                            new_index = fresh_devices.index(dev)
                            self.log_always('[multiACE] ACE %d returned after %.0fs — switching' % (new_index, gone_time))
                            self.reactor.register_async_callback(
                                lambda et, idx=new_index: self.gcode.run_script_from_command(
                                    'ACE_SWITCH TARGET=%d' % idx))
                            return eventtime + 10.0

            for dev, gone_since in list(self._hotplug_gone.items()):
                gone_time = now - gone_since
                if gone_time >= 5.0 and gone_time < 7.0:
                    self.log_always('[multiACE] ACE removed — re-enable to select')

        except Exception as e:
            logging.info('[multiACE] Hotplug monitor error: %s' % str(e))

        return eventtime + 2.0

    def _handle_disconnect(self):
        logging.info('[multiACE] Closing all ACE connections')
        for idx in list(self._serials.keys()):
            try:
                self._disconnect_from(idx)
            except Exception:
                pass
        self._queue = None

    def get_load_length(self, ace_idx, slot):
        """Lookup load_length with per-ACE/per-slot override priority."""
        v = self._ace_section_load_length_slot.get((ace_idx, slot))
        if v is not None:
            return v
        v = self._ace_section_load_length.get(ace_idx)
        if v is not None:
            return v
        return self.head_load_length.get(slot, self.load_length)

    def get_retract_length(self, ace_idx, slot):
        """Lookup retract_length with per-ACE/per-slot override priority."""
        v = self._ace_section_retract_length_slot.get((ace_idx, slot))
        if v is not None:
            return v
        v = self._ace_section_retract_length.get(ace_idx)
        if v is not None:
            return v
        return self.retract_length

    def _fa_trace(self, msg):
        """Log FA/load transitions to multiace_fa.log. _fa_log level is
        gated by fa_debug (DEBUG when on, WARNING when off) so trace
        info is silent in production but failures persist."""
        self._fa_log.info(msg)

    def _on_print_start(self, *args):
        if self._ace_mode == 'multi':
            self._ghost_heads = set()
            stale_heads = []
            ghost_heads = []
            for head in range(4):
                sensor = self.printer.lookup_object(
                    'filament_motion_sensor e%d_filament' % head, None)
                if sensor is None:
                    continue
                detected = sensor.get_status(0)['filament_detected']
                src = self._head_source.get(head)
                if detected and src is None:
                    ghost_heads.append(head)
                elif (not detected) and src is not None:
                    stale_heads.append(head)
                    self._head_source[head] = None
            if stale_heads:
                try:
                    self._save_head_source()
                except Exception:
                    pass
                logging.info(
                    '[multiACE] Print start: cleared stale head_source for '
                    'head(s) %s (sensor reports no filament)'
                    % ', '.join('T%d' % h for h in stale_heads))
            if ghost_heads:
                self._ghost_heads = set(ghost_heads)
                head_list = ', '.join('T%d' % h for h in ghost_heads)
                self.log_error(
                    '[multiACE] GHOST HEAD(S): %s have filament at the '
                    'toolhead sensor but no head_source mapping. '
                    'ACE_SWAP_HEAD will be refused for these heads. '
                    'Recover: ACEC__Unload_All then ACEB__Load_N for each '
                    'affected head, then restart the print.' % head_list)

            for head in range(4):
                source = self._head_source.get(head)
                if source is None:
                    continue
                ace_idx = source['ace_index']
                if ace_idx >= len(self._ace_devices):
                    self.log_error('[multiACE] WARNING: Head %d needs ACE %d but only %d ACE(s) available!' % (
                        head, ace_idx, len(self._ace_devices)))
                    continue
                if not self._connected_per_ace.get(ace_idx, False):
                    self.log_error('[multiACE] WARNING: Head %d needs ACE %d which is not connected!' % (
                        head, ace_idx))
        self._auto_feed_enabled = True
        self._fa_context = 'print'
        logging.info('[multiACE] Print started — auto-feed enabled')
        self._fa_trace('gate OPEN (context=print) via _on_print_start')

        try:
            extruder = self.toolhead.get_extruder()
            head_index = getattr(extruder, 'extruder_index',
                        getattr(extruder, 'extruder_num', None))
        except Exception:
            head_index = None
        if head_index is None:
            return
        source = self._head_source.get(head_index)
        if source is None:
            return
        target_ace = source['ace_index']
        target_slot = source['slot']
        if target_ace >= len(self._ace_devices):
            return
        if not self._connected_per_ace.get(target_ace, False):
            self._audit_state('PRINT_START', {
                'head': head_index,
                'target_ace': target_ace,
                'action': 'ace_not_connected',
            })
            return

        if self._active_device_index != target_ace:
            self._activate_ace(target_ace)
        try:
            self._start_feed_assist_on(target_ace, target_slot)
            self.log_always(
                '[multiACE] Print start: feed_assist enabled on ACE %d slot %d (head %d)'
                % (target_ace, target_slot, head_index))
            self._audit_state('PRINT_START', {
                'head': head_index,
                'target_ace': target_ace,
                'target_slot': target_slot,
                'action': 'feed_assist_enabled',
            })
        except Exception as e:
            logging.info('[multiACE] print-start feed_assist enable failed: %s' % e)
            self._audit_state('PRINT_START', {
                'head': head_index,
                'action': 'feed_assist_enable_failed',
                'error': str(e)[:200],
            })

    def _on_print_end(self, *args):
        self._auto_feed_enabled = False
        self._fa_context = 'idle'
        logging.info('[multiACE] Print ended — auto-feed disabled')
        self._fa_trace('gate CLOSE (context=idle) via _on_print_end')
        stopped_any = False
        for idx in range(len(self._ace_devices)):
            if self._feed_assist_per_ace.get(idx, -1) != -1:
                try:
                    self._stop_feed_assist_on(idx)
                    stopped_any = True
                except Exception as e:
                    logging.info('[multiACE] print-end stop_feed_assist[%d] failed: %s' % (idx, e))
        if stopped_any:
            self._audit_state('PRINT_END', {
                'action': 'feed_assist_disabled',
            })

    def _color_message(self, msg):
        try:
            html_msg = msg.format(
                '</span>',
                '<span style="color:#FFFF00">',
                '<span style="color:#90EE90">',
                '<span style="color:#458EFF">',
                '<b>',
                '</b>'
            )
        except (IndexError, KeyError, ValueError) as e:
            html_msg = msg
        return html_msg

    def log_warning(self, msg):
        c_msg = self._color_message(f'{{1}}{msg}{{0}}')
        self.gcode.respond_raw(c_msg)

    def log_always(self, msg: str, color=False):
        c_msg = self._color_message(msg) if color else msg
        self.gcode.respond_raw(c_msg)

    def log_error(self, msg):
        self.error_msg = msg
        self.gcode.respond_raw(f"!! {msg}")

    def _restore_pos_for_pause(self, saved_pos):
        if not saved_pos:
            return
        try:
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command(
                'G0 Z%.3f F600' % (saved_pos[2] + 3.0))
            self.gcode.run_script_from_command(
                'G0 Y%.3f F12000' % saved_pos[1])
            self.gcode.run_script_from_command(
                'G0 X%.3f F12000' % saved_pos[0])
            self.gcode.run_script_from_command(
                'G0 Z%.3f F600' % (saved_pos[2] + 2.0))
            self.toolhead.wait_moves()
            logging.info(
                '[multiACE] Swap PAUSE: restored pos X=%.2f Y=%.2f Z=%.2f '
                '(pre-PAUSE, prevents RESUME-traverse ram)' % (
                    saved_pos[0], saved_pos[1], saved_pos[2]))
        except Exception as e:
            logging.info(
                '[multiACE] Swap PAUSE: pos restore failed: %s' % e)

    def _swap_back_to_orig_for_pause(self, switched_head, orig_ext_name):
        if not switched_head:
            return
        try:
            orig_head_idx = (0 if orig_ext_name == 'extruder'
                             else int(orig_ext_name.replace('extruder', '')))
            logging.info(
                '[multiACE] Swap PAUSE: switching active extruder back '
                'to %s before pause (was on swap head)' % orig_ext_name)
            self.gcode.run_script_from_command('T%d A0' % orig_head_idx)
            self.toolhead.wait_moves()
        except Exception as e:
            logging.info(
                '[multiACE] Swap PAUSE: T-switch back to %s failed: %s'
                % (orig_ext_name, e))

    def _pause_for_recovery(self, phase, display_msg, detail_msg, recovery_steps):
        short = display_msg[:20]
        try:
            self.gcode.run_script_from_command('M117 %s' % short)
        except Exception:
            pass
        try:
            self.gcode.run_script_from_command(
                'RESPOND TYPE=error MSG="[multiACE] PAUSE %s: %s"' % (
                    phase, detail_msg.replace('"', "'")))
        except Exception:
            pass
        for i, step in enumerate(recovery_steps, 1):
            try:
                self.gcode.run_script_from_command(
                    'RESPOND TYPE=echo MSG="  %d. %s"' % (
                        i, step.replace('"', "'")))
            except Exception:
                pass
        self.error_msg = detail_msg
        self._audit_state('PAUSE_RECOVERY', {
            'phase': phase,
            'display_msg': short,
            'detail': detail_msg,
            'steps': recovery_steps,
        })
        try:
            short_msg = ('[multiACE] %s: %s' % (phase, detail_msg)).replace('"', "'")
            self.gcode.run_script_from_command(
                'RAISE_EXCEPTION ID=522 INDEX=0 CODE=0 '
                'MSG="%s" LEVEL=2' % short_msg[:200])
        except Exception:
            pass
        self.gcode.run_script_from_command('PAUSE')

    def save_variable(self, variable, value, write=False):
        self.save_variables.allVariables[variable] = value
        if write:
            self.write_variables()

    def rgb2hex(self, r, g, b):
        return "%02X%02X%02X" % (r, g, b)

    def delete_variable(self, variable, write=False):
        _ = self.save_variables.allVariables.pop(variable, None)
        if write:
            self.write_variables()

    def write_variables(self):
        mmu_vars_revision = self.save_variables.allVariables.get(self.VARS_ACE_REVISION, 0) + 1
        self.gcode.run_script_from_command(
            f"SAVE_VARIABLE VARIABLE={self.VARS_ACE_REVISION} VALUE={mmu_vars_revision}")

    def _get_next_request_id(self) -> int:
        return self._next_request_id_for(self._active_device_index)

    def _serial_disconnect(self):
        idx = self._active_device_index
        self._disconnect_from(idx)
        self._serial = None
        self._connected = False
        self.heartbeat_timer = None
        self.ace_dev_fd = None

    def _connect(self, eventtime):
        idx = self._active_device_index
        ok = self._connect_to(idx)
        if ok:
            self._activate_ace(idx)
            return self.reactor.NEVER
        return eventtime + 1.0

    def _calc_crc(self, buffer):

        _crc = 0xFFFF
        for byte in buffer:
            data = byte
            data ^= _crc & 0xFF
            data ^= (data & 0x0F) << 4
            _crc = ((data << 8) | (_crc >> 8)) ^ (data >> 4) ^ (data << 3)
        return _crc

    def _make_default_info(self):
        return {
            'status': 'ready',
            'dryer_status': {
                'status': 'stop',
                'target_temp': 0,
                'duration': 0,
                'remain_time': 0
            },
            'temp': 0,
            'enable_rfid': 1,
            'fan_speed': 7000,
            'feed_assist_count': 0,
            'cont_assist_time': 0.0,
            'slots': [
                {'index': i, 'status': 'empty1', 'sku': '', 'type': '',
                 'rfid': 0, 'brand': '', 'color': [0, 0, 0]}
                for i in range(4)
            ],
        }

    def _next_request_id_for(self, idx):
        rid = self._request_ids.get(idx, 0) + 1
        if rid >= 300000:
            rid = 1
        self._request_ids[idx] = rid
        return rid

    def _activate_ace(self, idx):
        if idx < 0 or idx >= len(self._ace_devices):
            return False
        self._active_device_index = idx
        self.serial_id = self._ace_devices[idx]
        self._serial = self._serials.get(idx)
        self._connected = self._connected_per_ace.get(idx, False)
        self._serial_failed = self._serial_failed_per_ace.get(idx, False)
        self._feed_assist_index = self._feed_assist_per_ace.get(idx, -1)
        info = self._info_per_ace.get(idx)
        if info is not None:
            self._info = info
        cb_map = self._callback_maps.get(idx)
        if cb_map is not None:
            self._callback_map = cb_map
        if idx in self._request_ids:
            self._request_id = self._request_ids[idx]
        buf = self._read_buffers.get(idx)
        if buf is not None:
            self.read_buffer = buf
        gate_list = self._gate_status_per_ace.get(idx)
        if gate_list is not None:
            self.gate_status = gate_list
        self.ace_dev_fd = self._ace_dev_fds.get(idx)
        self.heartbeat_timer = self._heartbeat_timers.get(idx)
        try:
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (
                    self.VARS_ACE_ACTIVE_DEVICE, self.serial_id))
        except Exception:
            pass
        return True

    def _connect_to(self, idx):
        if idx >= len(self._ace_devices):
            return False
        serial_path = self._ace_devices[idx]
        logging.info('[multiACE] Try connecting ACE %d (%s)' % (idx, serial_path))
        self._usb_log.info('CONNECT attempt idx=%d serial=%s', idx, serial_path)
        connect_start = time.monotonic()

        old_ht = self._heartbeat_timers.pop(idx, None)
        if old_ht is not None:
            try:
                self.reactor.unregister_timer(old_ht)
            except Exception:
                pass
        old_fd = self._ace_dev_fds.pop(idx, None)
        if old_fd is not None:
            try:
                self.reactor.set_fd_wake(old_fd, False, False)
            except Exception:
                pass
        old_ser = self._serials.pop(idx, None)
        if old_ser is not None:
            try:
                if old_ser.is_open:
                    old_ser.close()
            except Exception:
                pass

        def info_callback(self, response):
            if response.get('msg') != 'success':
                self.log_error(f"ACE Error: {response.get('msg')}")
            result = response.get('result', {})
            model = result.get('model', 'Unknown')
            firmware = result.get('firmware', 'Unknown')
            self._usb_log.info('CONNECT info idx=%d model=%s firmware=%s', idx, model, firmware)
            self.log_always(f"{{2}}ACE %d: Connected to {model} {{0}} \n Firmware Version: {{3}}{firmware}{{0}}" % idx, True)

        try:
            ser = serial.Serial(
                port=serial_path,
                baudrate=self.baud,
                exclusive=True,
                rtscts=True,
                timeout=0,
                write_timeout=0)
            if not ser.is_open:
                return False
            self._serials[idx] = ser
            self._connected_per_ace[idx] = True
            self._serial_failed_per_ace[idx] = False
            self._request_ids[idx] = 0
            self._callback_maps[idx] = {}
            self._read_buffers[idx] = bytearray()
            self._info_per_ace[idx] = self._make_default_info()
            self._feed_assist_per_ace.setdefault(idx, -1)
            self._gate_status_per_ace[idx] = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]
            connect_ms = (time.monotonic() - connect_start) * 1000
            self._usb_stats['connects'] += 1
            self._usb_log.info('CONNECT success idx=%d serial=%s time=%.1fms', idx, serial_path, connect_ms)
            logging.info('[multiACE] Connected to ACE %d (%s)' % (idx, serial_path))
            fd = self.reactor.register_fd(
                ser.fileno(), self._make_reader_cb_for(idx))
            self._ace_dev_fds[idx] = fd
            ht = self.reactor.register_timer(
                self._make_heartbeat_tick_for(idx), self.reactor.NOW)
            self._heartbeat_timers[idx] = ht
            self.send_request_to(idx,
                request={"method": "get_info"},
                callback=lambda self, response: info_callback(self, response))
            return True
        except serial.serialutil.SerialException:
            self._usb_stats['connect_failures'] += 1
            self._usb_log.warning('CONNECT failed idx=%d SerialException', idx)
            logging.info('[multiACE] Conn error idx=%d' % idx)
            return False
        except Exception as e:
            self._usb_stats['connect_failures'] += 1
            self._usb_log.warning('CONNECT failed idx=%d error=%s', idx, str(e))
            logging.info("ACE Error idx=%d: %s" % (idx, str(e)))
            return False

    def _disconnect_from(self, idx):
        self._usb_stats['disconnects'] += 1
        ser = self._serials.get(idx)
        if ser is not None:
            self._usb_log.info('DISCONNECT idx=%d serial=%s', idx,
                               self._ace_devices[idx] if idx < len(self._ace_devices) else '?')
            try:
                if ser.is_open:
                    ser.close()
            except Exception:
                pass
        self._connected_per_ace[idx] = False
        ht = self._heartbeat_timers.pop(idx, None)
        if ht is not None:
            try:
                self.reactor.unregister_timer(ht)
            except Exception:
                pass
        fd = self._ace_dev_fds.pop(idx, None)
        if fd is not None:
            try:
                self.reactor.set_fd_wake(fd, False, False)
            except Exception:
                pass
        self._serials.pop(idx, None)

    def _make_reader_cb_for(self, idx):
        def _reader(eventtime):
            ser = self._serials.get(idx)
            if ser is None or not ser.is_open:
                return
            try:
                if ser.in_waiting:
                    raw_bytes = ser.read(size=ser.in_waiting)
                    self._process_data_for(idx, raw_bytes)
            except Exception:
                logging.info('ACE[%d] error reading/processing: %s' % (
                    idx, traceback.format_exc()))
                logging.info("Unable to communicate with ACE %d" % idx)
        return _reader

    def _process_data_for(self, idx, raw_bytes):
        buf = self._read_buffers.get(idx)
        if buf is None:
            buf = bytearray()
            self._read_buffers[idx] = buf
        buf += raw_bytes
        while len(buf) >= 7:
            start = buf.find(b'\xFF\xAA')
            if start < 0:
                if buf.endswith(b'\xFF'):
                    buf = bytearray(buf[-1:])
                else:
                    buf = bytearray()
                self._read_buffers[idx] = buf
                break
            if start > 0:
                buf = bytearray(buf[start:])
                self._read_buffers[idx] = buf
            if len(buf) < 4:
                break
            payload_len = struct.unpack('<H', bytes(buf[2:4]))[0]
            if payload_len > 2048:
                self.gcode.respond_info(f"ACE[{idx}]: Invalid payload length {payload_len}, dropping sync bytes")
                buf = bytearray(buf[2:])
                self._read_buffers[idx] = buf
                continue
            total_len = 4 + payload_len + 2 + 1
            if len(buf) < total_len:
                break
            packet = buf[:total_len]
            payload = packet[4:4 + payload_len]
            buf = bytearray(buf[total_len:])
            self._read_buffers[idx] = buf
            try:
                ret = json.loads(bytes(payload).decode('utf-8'))
            except (json.decoder.JSONDecodeError, UnicodeDecodeError):
                self.log_error('Invalid JSON/UTF-8 from ACE %d' % idx)
                continue
            msg_id = ret.get('id')
            cb_map = self._callback_maps.get(idx, {})
            if msg_id in cb_map:
                callback = cb_map.pop(msg_id)
                callback(self=self, response=ret)

    def send_request_to(self, idx, request, callback):
        info = self._info_per_ace.get(idx)
        if info is None:
            info = self._make_default_info()
            self._info_per_ace[idx] = info
        info['status'] = 'busy'
        msg_id = self._next_request_id_for(idx)
        cb_map = self._callback_maps.setdefault(idx, {})

        method = request.get('method', '?')
        params = request.get('params', {}) or {}
        slot_repr = params.get('index', params.get('slot', '?'))
        len_repr = params.get('length', '?')
        speed_repr = params.get('speed', '?')
        self._fa_log.info(
            'SEND ACE %d id=%d method=%s slot=%s len=%s speed=%s'
            % (idx, msg_id, method, slot_repr, len_repr, speed_repr))
        original_cb = callback
        def _traced_cb(self, response):
            try:
                self._fa_log.info(
                    'RESP ACE %d id=%s method=%s slot=%s code=%s msg=%s' % (
                        idx, response.get('id', '?'), method, slot_repr,
                        response.get('code', '?'), response.get('msg', '')))
            except Exception:
                pass
            original_cb(self=self, response=response)
        cb_map[msg_id] = _traced_cb

        request['id'] = msg_id
        self._send_request_to(idx, request)

    def _send_request_to(self, idx, request):
        if 'id' not in request:
            request['id'] = self._next_request_id_for(idx)
        payload = json.dumps(request).encode('utf-8')
        if len(payload) > 1024:
            logging.error(f"ACE[{idx}]: Payload too large ({len(payload)} bytes)")
            return
        crc = self._calc_crc(payload)
        attempts = 0
        while crc == 0xAAFF and attempts < 10:
            request['id'] = self._next_request_id_for(idx)
            payload = json.dumps(request).encode('utf-8')
            crc = self._calc_crc(payload)
            attempts += 1
        data = bytes([0xFF, 0xAA])
        data += struct.pack('<H', len(payload))
        data += payload
        data += struct.pack('<H', crc)
        data += bytes([0xFE])
        ser = self._serials.get(idx)
        if ser is None or self._serial_failed_per_ace.get(idx, False):
            raise Exception('[multiACE] serial[%d] unavailable' % idx)
        try:
            ser.write(data)
            return
        except Exception as e:
            err_first = str(e)
            logging.info(
                "ACE[%d]: Error writing to serial: %s — attempting reconnect+retry"
                % (idx, err_first))
            self._usb_stats['errno5_total'] += 1
            now = time.monotonic()
            self._errno5_recent = [
                (i, t) for (i, t) in self._errno5_recent if now - t < 1.5]
            self._errno5_recent.append((idx, now))
            distinct_aces = set(i for (i, _) in self._errno5_recent)
            if len(distinct_aces) >= 2:
                self._usb_stats['cascades'] += 1
                self.log_error(
                    '[multiACE] CASCADE detected: %d ACEs failed in <1.5s '
                    '(total cascades this session: %d)' % (
                        len(distinct_aces), self._usb_stats['cascades']))
                self._errno5_recent = []
            try:
                self._state_log.warning(
                    'SERIAL_WRITE_FAILED_FIRST idx=%d error=%s', idx, err_first)
            except Exception:
                pass

            saved_cb_map = dict(self._callback_maps.get(idx, {}))

            try:
                if ser.is_open:
                    ser.close()
            except Exception:
                pass
            self._connected_per_ace[idx] = False

            reconnected = False
            for attempt, delay in enumerate((0.35, 1.0, 2.0), start=1):
                try:
                    self.reactor.pause(self.reactor.monotonic() + delay)
                except Exception:
                    pass
                try:
                    reconnected = self._connect_to(idx)
                except Exception as ce:
                    logging.info(
                        '[multiACE] Sync reconnect[%d] attempt %d raised: %s'
                        % (idx, attempt, str(ce)))
                    reconnected = False
                if reconnected:
                    break
                logging.info(
                    '[multiACE] Sync reconnect[%d] attempt %d/3 failed'
                    % (idx, attempt))

            if reconnected:
                new_cb_map = self._callback_maps.setdefault(idx, {})
                for mid, cb in saved_cb_map.items():
                    if mid not in new_cb_map:
                        new_cb_map[mid] = cb

                new_ser = self._serials.get(idx)
                if new_ser is not None:
                    try:
                        new_ser.write(data)
                        self._usb_stats['errno5_recovered'] += 1
                        self.log_always(
                            '[multiACE] ACE %d serial write recovered after reconnect'
                            % idx)
                        try:
                            self._state_log.info(
                                'SERIAL_WRITE_RECOVERED idx=%d', idx)
                            self._audit_state(
                                'SERIAL_WRITE_RECOVERED', {'idx': idx})
                        except Exception:
                            pass
                        self._serial_failed_per_ace[idx] = False
                        return
                    except Exception as e2:
                        err_second = str(e2)
                else:
                    err_second = 'no_serial_after_reconnect'
            else:
                err_second = 'reconnect_failed'

            self._usb_stats['errno5_unrecovered'] += 1
            try:
                self._state_log.warning(
                    'SERIAL_WRITE_FAILED idx=%d error=%s first_error=%s',
                    idx, err_second, err_first)
            except Exception:
                pass
            self._handle_per_ace_failure(idx, err_second)
            raise Exception(
                '[multiACE] serial[%d] write failed (reconnect+retry both failed)'
                % idx)

    def _handle_per_ace_failure(self, idx, err):
        was_failed = self._serial_failed_per_ace.get(idx, False)
        self._serial_failed_per_ace[idx] = True
        if not was_failed:
            self.log_error('[multiACE] ACE %d serial failed: %s' % (idx, err))
            try:
                self._state_log.error('ACE_FAILED idx=%d error=%s', idx, err)
                self._audit_state('ACE_FAILED', {'idx': idx, 'error': err})
            except Exception:
                pass
            try:
                self._disconnect_from(idx)
            except Exception:
                pass
            if not self._serial_failed_pause_sent:
                self._serial_failed_pause_sent = True
                def _do_pause(eventtime):
                    try:
                        self.gcode.run_script('PAUSE')
                    except Exception as pe:
                        logging.info('[multiACE] PAUSE call failed: %s' % str(pe))
                    try:
                        self.printer.invoke_async_shutdown(
                            '[multiACE] ACE %d permanently failed — print stopped' % idx)
                    except Exception:
                        pass
                    return self.reactor.NEVER
                try:
                    self.reactor.register_timer(_do_pause, self.reactor.NOW)
                except Exception:
                    pass

    def _start_feed_assist_on(self, idx, slot):
        self._fa_trace('_start_feed_assist_on(idx=%d, slot=%d) called; gate=%s context=%s'
                       % (idx, slot, self._auto_feed_enabled, self._fa_context))
        if not self._auto_feed_enabled:
            logging.info(
                '[multiACE] FA suppressed (gate off): idx=%d slot=%d' % (idx, slot))
            return
        if self._fa_context == 'print' and idx in self._fa_print_disable:
            logging.info(
                '[multiACE] FA suppressed for ACE %d during print (fa_print_disable)' % idx)
            return
        if self._fa_context == 'load' and idx in self._fa_load_disable:
            logging.info(
                '[multiACE] FA suppressed for ACE %d during load (fa_load_disable)' % idx)
            return

        prev_slot = self._feed_assist_per_ace.get(idx, -1)
        if prev_slot == slot:
            logging.info('[multiACE] FA _start skipped: prev_slot=%d == slot=%d (already running)' % (prev_slot, slot))
            return
        logging.info('[multiACE] FA _start proceeding: idx=%d slot=%d prev_slot=%d' % (idx, slot, prev_slot))

        any_active_before = any(
            s != -1 for s in self._feed_assist_per_ace.values())
        now = time.monotonic()
        if not any_active_before and self._fa_context == 'print':
            gap_ms = int((now - self._fa_last_active_ts) * 1000)
            if gap_ms > self._fa_gap_threshold_ms:
                self._telemetry('FA_GAP', {
                    'gap_ms': gap_ms,
                    'resumed_ace': idx,
                    'resumed_slot': slot,
                    'context': self._fa_context,
                })
        self._fa_last_active_ts = now

        self._feed_assist_per_ace[idx] = slot
        if idx == self._active_device_index:
            self._feed_assist_index = slot

        max_retries = self._fa_start_retries
        retry_delay = self._fa_start_retry_delay
        settle_delay = self._fa_settle_after_stop

        def start_callback_factory(attempt):
            def start_callback(self, response):
                code = response.get('code', 0)
                msg = (response.get('msg', '') or '').lower()
                if not self._auto_feed_enabled:
                    return
                if self._feed_assist_per_ace.get(idx, -1) != slot:
                    return
                if code == 0 and (msg == 'success' or msg == ''):
                    if attempt > 0:
                        self._fa_log.warning(
                            'start_feed_assist OK after %d retry(s): ACE %d slot %d'
                            % (attempt, idx, slot))
                    return
                if msg == 'forbidden' and attempt < max_retries:
                    next_attempt = attempt + 1
                    self._fa_log.info(
                        'start_feed_assist FORBIDDEN, retry %d/%d in %.1fs: ACE %d slot %d'
                        % (next_attempt, max_retries, retry_delay, idx, slot))
                    def _retry(eventtime):
                        if not self._auto_feed_enabled:
                            return self.reactor.NEVER
                        if self._feed_assist_per_ace.get(idx, -1) != slot:
                            return self.reactor.NEVER
                        try:
                            self.send_request_to(idx,
                                {"method": "start_feed_assist", "params": {"index": slot}},
                                start_callback_factory(next_attempt))
                            self._fa_log.info(
                                'start_feed_assist RETRY %d/%d sent: ACE %d slot %d'
                                % (next_attempt, max_retries, idx, slot))
                        except Exception as e:
                            self.log_error(
                                '[multiACE] FA start_feed_assist RETRY send failed: %s' % e)
                            self._fa_log.error(
                                'start_feed_assist RETRY send failed: %s' % e)
                        return self.reactor.NEVER
                    self.reactor.register_timer(
                        _retry, self.reactor.monotonic() + retry_delay)
                    return
                final_msg = (
                    '[multiACE] FA start_feed_assist FAILED after %d attempt(s) on ACE %d slot %d: '
                    'code=%s msg=%s — this lane will NOT be fed; check that filament '
                    'is loaded into the ACE slot'
                    % (attempt + 1, idx, slot, code, response.get('msg', '')))
                self.log_error(final_msg)
                self._fa_log.error(final_msg)
            return start_callback

        def _send_start():
            try:
                self.send_request_to(idx,
                    {"method": "start_feed_assist", "params": {"index": slot}},
                    start_callback_factory(0))
                logging.info('[multiACE] FA start_feed_assist SENT to ACE %d slot %d' % (idx, slot))
            except Exception as e:
                logging.info('[multiACE] send start_feed_assist to ACE %d failed: %s' % (idx, e))

        if prev_slot != -1:
            try:
                self.send_request_to(idx,
                    {"method": "stop_feed_assist", "params": {"index": prev_slot}},
                    lambda *a, **kw: None)
                logging.info('[multiACE] FA pre-start stop sent: ACE %d slot %d (before start slot %d, settle %.1fs)'
                             % (idx, prev_slot, slot, settle_delay))
            except Exception as e:
                logging.info('[multiACE] pre-start stop_feed_assist failed: %s' % e)
            def _delayed_start(eventtime):
                if not self._auto_feed_enabled:
                    self._fa_trace(
                        'post-stop delayed start SUPPRESSED (gate closed): idx=%d slot=%d'
                        % (idx, slot))
                    return self.reactor.NEVER
                if self._feed_assist_per_ace.get(idx, -1) != slot:
                    self._fa_trace(
                        'post-stop delayed start SUPPRESSED (slot changed): idx=%d expected=%d actual=%d'
                        % (idx, slot, self._feed_assist_per_ace.get(idx, -1)))
                    return self.reactor.NEVER
                _send_start()
                return self.reactor.NEVER
            self.reactor.register_timer(
                _delayed_start, self.reactor.monotonic() + settle_delay)
        else:
            _send_start()

    def _stop_feed_assist_on(self, idx):
        prev_slot = self._feed_assist_per_ace.get(idx, -1)
        if prev_slot == -1:
            return
        self._feed_assist_per_ace[idx] = -1
        if idx == self._active_device_index:
            self._feed_assist_index = -1
        if not any(s != -1 for s in self._feed_assist_per_ace.values()):
            self._fa_last_active_ts = time.monotonic()

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE[{idx}] Error stopping feed_assist: {response.get('msg')}")

        try:
            self.send_request_to(idx,
                {"method": "stop_feed_assist", "params": {"index": prev_slot}},
                callback)
        except Exception as e:
            logging.info('[multiACE] send stop_feed_assist to ACE %d failed: %s' % (idx, e))

    def _disable_feed_assist_all(self):
        def _noop_cb(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")

        any_running = False
        for idx in sorted(list(self._feed_assist_per_ace.keys())):
            slot = self._feed_assist_per_ace.get(idx, -1)
            if slot == -1:
                continue
            if not self._connected_per_ace.get(idx, False):
                logging.info(
                    '[multiACE] _disable_feed_assist_all: skip ACE %d (disconnected)' % idx)
                self._feed_assist_per_ace[idx] = -1
                continue
            gate_list = self._gate_status_per_ace.get(idx, [GATE_UNKNOWN] * 4)
            if 0 <= slot < len(gate_list) and gate_list[slot] == GATE_EMPTY:
                logging.info(
                    '[multiACE] _disable_feed_assist_all: skip ACE %d slot %d (empty)' % (idx, slot))
                self._feed_assist_per_ace[idx] = -1
                continue
            any_running = True
            try:
                self.wait_ace_ready_on(idx)
                self.send_request_to(idx,
                    {"method": "unwind_filament",
                     "params": {"index": slot, "length": 5, "speed": 80}},
                    _noop_cb)
                self.dwell(delay=(5.0 / 80.0) + 0.1)
                self.wait_ace_ready_on(idx)
                self._stop_feed_assist_on(idx)
                self.wait_ace_ready_on(idx)
            except Exception as e:
                logging.info(
                    '[multiACE] _disable_feed_assist_all: error on idx %d: %s' % (idx, e))
        if self._feed_assist_index != -1:
            self._feed_assist_index = -1
        if any_running:
            self.dwell(0.3)

    def _enable_feed_assist_for_head(self, head):
        source = self._head_source.get(head)
        if source is None:
            logging.info(
                '[multiACE] _enable_feed_assist_for_head: no head_source for head %d, '
                'skipping FA (use ACE_LOAD_HEAD to set source first)' % head)
            return

        target_idx = source['ace_index']
        slot = source['slot']

        self._disable_feed_assist_all()

        if target_idx != self._active_device_index:
            self._activate_ace(target_idx)

        self.wait_ace_ready_on(target_idx)
        self._start_feed_assist_on(target_idx, slot)
        self.wait_ace_ready_on(target_idx)
        self.dwell(delay=0.7)

    def _make_heartbeat_tick_for(self, idx):
        def _tick(eventtime):
            if self._serial_failed_per_ace.get(idx, False):
                return eventtime + 1.0
            ser = self._serials.get(idx)
            if ser is None or not ser.is_open:
                return eventtime + 1.0
            is_active = (idx == self._active_device_index)

            def callback(self, response):
                if response is None:
                    return
                result = response.get('result')
                if result is None:
                    return
                prev_info = self._info_per_ace.get(idx, self._make_default_info())
                prev_slots = prev_info.get('slots', [])
                for i in range(4):
                    try:
                        new_slot = result['slots'][i]
                    except (KeyError, IndexError):
                        continue
                    prev_slot = prev_slots[i] if i < len(prev_slots) else {}
                    if (is_active
                            and self._gate_status_per_ace.get(idx, [GATE_UNKNOWN] * 4)[i] == GATE_EMPTY
                            and new_slot.get('status') != 'empty'
                            and not self._swap_in_progress):
                        self.log_always('[multiACE] auto_feed')
                        self.reactor.register_async_callback(
                            (lambda et, c=self._pre_load, gate=i: c(gate)))
                    if (is_active and new_slot.get('rfid') == 2
                            and prev_slot.get('rfid') != 2
                            and not self._swap_in_progress):
                        target_heads = self._get_heads_for_ace_slot(
                            self._active_device_index, i)
                        if target_heads:
                            self.log_always('find_rfid (slot %d -> heads %s)' % (i, target_heads))
                            self.log_always(str(new_slot))
                            new_type = new_slot.get('type', 'PLA')
                            new_color_hex = self.rgb2hex(*new_slot.get('color', (0, 0, 0)))
                            new_brand = new_slot.get('brand', 'Generic')
                            head_source_changed = False
                            for head in target_heads:
                                src = self._head_source.get(head)
                                if src is None:
                                    continue
                                if (src.get('type') != new_type
                                        or src.get('color') != new_color_hex
                                        or src.get('brand') != new_brand):
                                    src['type'] = new_type
                                    src['color'] = new_color_hex
                                    src['brand'] = new_brand
                                    head_source_changed = True
                            if head_source_changed:
                                try:
                                    self._save_head_source()
                                except Exception as he:
                                    logging.info(
                                        '[multiACE] head_source RFID heal save failed: %s' % he)
                            for head in target_heads:
                                self.gcode.run_script_from_command(
                                    'SET_PRINT_FILAMENT_CONFIG '
                                    'CONFIG_EXTRUDER=%d '
                                    'FILAMENT_TYPE="%s" '
                                    'FILAMENT_COLOR_RGBA=%s '
                                    'VENDOR="%s" '
                                    'FILAMENT_SUBTYPE=""' % (
                                        head,
                                        new_type,
                                        new_color_hex,
                                        new_brand))
                        else:
                            source = self._head_source.get(i)
                            if not (source and source['ace_index']
                                    != self._active_device_index):
                                self.log_always(
                                    '[multiACE] find_rfid (slot %d -> extruder %d, fallback)'
                                    % (i, i))
                                self.log_always(str(new_slot))
                                self.gcode.run_script_from_command(
                                    'SET_PRINT_FILAMENT_CONFIG '
                                    'CONFIG_EXTRUDER=%d '
                                    'FILAMENT_TYPE="%s" '
                                    'FILAMENT_COLOR_RGBA=%s '
                                    'VENDOR="%s" '
                                    'FILAMENT_SUBTYPE=""' % (
                                        i,
                                        new_slot.get('type', 'PLA'),
                                        self.rgb2hex(*new_slot.get('color', (0, 0, 0))),
                                        new_slot.get('brand', 'Generic')))
                    gate_list = self._gate_status_per_ace.setdefault(
                        idx, [GATE_UNKNOWN] * 4)
                    gate_list[i] = GATE_EMPTY if new_slot.get('status') == 'empty' else GATE_AVAILABLE
                self._info_per_ace[idx] = result

                if is_active and not self._swap_in_progress:
                    try:
                        ptc = self.printer.lookup_object('print_task_config', None)
                        if ptc is not None:
                            ptc_status = ptc.get_status()
                            ptc_types = ptc_status.get('filament_type', [''] * 4)
                            ptc_vendors = ptc_status.get('filament_vendor', [''] * 4)
                            slots_list = result.get('slots', [])
                            for slot_idx in range(min(4, len(slots_list))):
                                slot = slots_list[slot_idx]
                                if slot.get('rfid') != 2:
                                    continue
                                target_heads = self._get_heads_for_ace_slot(
                                    self._active_device_index, slot_idx)
                                if not target_heads and slot_idx < 4:
                                    src = self._head_source.get(slot_idx)
                                    if not src:
                                        target_heads = [slot_idx]
                                for head in target_heads:
                                    cur_type = ptc_types[head] if head < len(ptc_types) else ''
                                    cur_vendor = ptc_vendors[head] if head < len(ptc_vendors) else ''
                                    if cur_type in ('', 'NONE') or cur_vendor in ('', 'NONE'):
                                        logging.info(
                                            '[multiACE] display heal: head %d was "%s"/"%s", repushing RFID' % (
                                                head, cur_type, cur_vendor))
                                        self.gcode.run_script_from_command(
                                            'SET_PRINT_FILAMENT_CONFIG '
                                            'CONFIG_EXTRUDER=%d '
                                            'FILAMENT_TYPE="%s" '
                                            'FILAMENT_COLOR_RGBA=%s '
                                            'VENDOR="%s" '
                                            'FILAMENT_SUBTYPE=""' % (
                                                head,
                                                slot.get('type', 'PLA'),
                                                self.rgb2hex(*slot.get('color', (0, 0, 0))),
                                                slot.get('brand', 'Generic')))
                    except Exception as he:
                        logging.info('[multiACE] display heal error: %s' % he)
            try:
                self.send_request_to(idx, {"method": "get_status"}, callback)
            except Exception as he:
                logging.info('[multiACE] Heartbeat[%d] send failed: %s' % (idx, str(he)))
            return eventtime + 1.0
        return _tick

    def _send_request(self, request):
        if 'id' not in request:
            request['id'] = self._get_next_request_id()

        payload = json.dumps(request).encode('utf-8')
        if len(payload) > 1024:
            logging.error(f"ACE: Payload too large ({len(payload)} bytes)")
            return

        crc = self._calc_crc(payload)

        attempts = 0
        while crc == 0xAAFF and attempts < 10:
            request['id'] = self._get_next_request_id()
            payload = json.dumps(request).encode('utf-8')
            crc = self._calc_crc(payload)
            attempts += 1

        data = bytes([0xFF, 0xAA])
        data += struct.pack('<H', len(payload))
        data += payload
        data += struct.pack('<H', crc)
        data += bytes([0xFE])

        idx = self._active_device_index
        ser = self._serials.get(idx)
        if ser is None or self._serial_failed_per_ace.get(idx, False):
            raise Exception('[multiACE] active serial[%d] unavailable' % idx)
        try:
            ser.write(data)
        except Exception as e:
            err = str(e)
            logging.info("ACE[%d]: Error writing to serial: %s" % (idx, err))
            try:
                self._state_log.warning('SERIAL_WRITE_FAILED idx=%d error=%s', idx, err)
            except Exception:
                pass
            self._handle_per_ace_failure(idx, err)
            raise Exception('[multiACE] serial write to ACE %d failed' % idx)

    def _handle_serial_failure(self, err, first, first_error=None):
        self._handle_per_ace_failure(self._active_device_index, err)

    def _pre_load(self, gate):
        feed_length = self.head_feed_length[gate]
        if feed_length <= 0:
            return

        self.log_always('[multiACE] Wait ACE preload')
        self.wait_ace_ready()

        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % gate, None)

        self._feed(gate, feed_length, self.feed_speed, 0)

        while not self.is_ace_ready():
            self.reactor.pause(self.reactor.monotonic() + 0.105)
            if sensor and sensor.get_status(0)['filament_detected']:
                self._stop_feeding(gate)
                self.wait_ace_ready()
                self.log_always('[multiACE] Filament detected during preload')
                break

        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_always("Select AutoLoad from the menu")

    def _reader_cb(self, eventtime):
        idx = self._active_device_index
        ser = self._serials.get(idx)
        if ser is None:
            return
        try:
            if ser.in_waiting:
                raw_bytes = ser.read(size=ser.in_waiting)
                self._process_data_for(idx, raw_bytes)
        except Exception:
            logging.info('ACE[%d] error reading/processing: %s' % (
                idx, traceback.format_exc()))
            self._handle_per_ace_failure(idx, 'reader_exception')

    def _process_data(self, raw_bytes):
        self.read_buffer += raw_bytes
        while len(self.read_buffer) >= 7:

            start = self.read_buffer.find(b'\xFF\xAA')
            if start < 0:

                if self.read_buffer.endswith(b'\xFF'):
                    self.read_buffer = self.read_buffer[-1:]
                else:
                    self.read_buffer = bytearray()
                break

            if start > 0:
                self.read_buffer = self.read_buffer[start:]

            if len(self.read_buffer) < 4:
                break

            payload_len = struct.unpack('<H', self.read_buffer[2:4])[0]
            if payload_len > 2048:
                self.gcode.respond_info(f"ACE: Invalid payload length {payload_len}, dropping sync bytes")
                self.read_buffer = self.read_buffer[2:]
                continue

            total_len = 4 + payload_len + 2 + 1

            if len(self.read_buffer) < total_len:
                break

            packet = self.read_buffer[:total_len]
            payload = packet[4:4 + payload_len]
            crc_data = packet[4 + payload_len:4 + payload_len + 2]
            tail = packet[-1]

            self.read_buffer = self.read_buffer[total_len:]

            try:
                ret = json.loads(payload.decode('utf-8'))
            except (json.decoder.JSONDecodeError, UnicodeDecodeError):
                self.log_error('Invalid JSON/UTF-8 from ACE PRO')
                continue

            msg_id = ret.get('id')
            if msg_id in self._callback_map:
                callback = self._callback_map.pop(msg_id)
                callback(self=self, response=ret)

    def send_request(self, request, callback):
        self.send_request_to(self._active_device_index, request, callback)

    def wait_ace_ready(self):
        self.wait_ace_ready_on(self._active_device_index)

    def wait_ace_ready_on(self, idx, timeout=30.0, max_reconnects=2):
        info = self._info_per_ace.get(idx)
        if info is None:
            return
        deadline = time.monotonic() + timeout
        reconnect_count = 0
        while info.get('status') != 'ready':
            if time.monotonic() > deadline:
                if reconnect_count >= max_reconnects:
                    self.log_error(
                        '[multiACE] ACE %d stuck in status=%s after %d reconnect '
                        'attempts — aborting operation. Power-cycle the ACE.' % (
                            idx, info.get('status', '?'), reconnect_count))
                    self._handle_per_ace_failure(idx, 'stuck_after_reconnects')
                    raise self.printer.command_error(
                        '[multiACE] ACE %d firmware stuck — power-cycle required' % idx)
                reconnect_count += 1
                self.log_error(
                    '[multiACE] ACE %d wait_ace_ready TIMEOUT after %.1fs '
                    '(status=%s) — reconnect %d/%d' % (
                        idx, timeout, info.get('status', '?'),
                        reconnect_count, max_reconnects))
                try:
                    self._disconnect_from(idx)
                except Exception:
                    pass
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                if self._connect_to(idx):
                    self.log_always(
                        '[multiACE] ACE %d reconnected after wait_ace_ready timeout' % idx)
                    info = self._info_per_ace.get(idx)
                    if info is None:
                        return
                    deadline = time.monotonic() + timeout
                    continue
                self._handle_per_ace_failure(idx, 'wait_ace_ready_timeout')
                raise self.printer.command_error(
                    '[multiACE] ACE %d unresponsive — reconnect failed, '
                    'operation aborted' % idx)
            curr_ts = self.reactor.monotonic()
            self.reactor.pause(curr_ts + 0.5)
            info = self._info_per_ace.get(idx)
            if info is None:
                return

    def is_ace_ready(self):
        idx = self._active_device_index
        info = self._info_per_ace.get(idx)
        if info is None:
            return False
        return info.get('status') == 'ready'

    def dwell(self, delay=1.0):
        curr_ts = self.reactor.monotonic()
        self.reactor.pause(curr_ts + delay)

    def _extruder_move(self, length, speed):
        pos = self.toolhead.get_position()
        pos[3] += length
        self.toolhead.move(pos, speed)
        return pos[3]

    cmd_ACE_START_DRYING_help = 'Starts ACE Pro dryer'

    def cmd_ACE_START_DRYING(self, gcmd):
        temperature = gcmd.get_int('TEMP')
        duration = gcmd.get_int('DURATION', 240)

        if duration <= 0:
            raise gcmd.error('Wrong duration')
        if temperature <= 0 or temperature > self.max_dryer_temperature:
            raise gcmd.error('Wrong temperature')

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")
                return

            self.gcode.respond_info('Started ACE drying')

        self.wait_ace_ready()
        self.send_request(
            request={"method": "drying", "params": {"temp": temperature, "fan_speed": 7000, "duration": duration}},
            callback=callback)

    cmd_ACE_STOP_DRYING_help = 'Stops ACE Pro dryer'

    def cmd_ACE_STOP_DRYING(self, gcmd):
        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")
                return

            self.gcode.respond_info('Stopped ACE drying')

        self.wait_ace_ready()
        self.send_request(request={"method": "drying_stop"}, callback=callback)

    def _enable_feed_assist(self, index):
        if self._feed_assist_index != -1 and self._feed_assist_index != index:
            self.wait_ace_ready()
            self._retract(self._feed_assist_index, 5, 80)
        self.wait_ace_ready()
        self._start_feed_assist_on(self._active_device_index, index)
        self.wait_ace_ready()
        self.dwell(delay=0.7)

    cmd_ACE_ENABLE_FEED_ASSIST_help = 'Enables ACE feed assist'

    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX')

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')

        self._enable_feed_assist(index)

    def _disable_feed_assist(self, index=-1):
        rt_index = self._feed_assist_index
        if rt_index == -1:
            return
        self.wait_ace_ready()
        self._stop_feed_assist_on(self._active_device_index)
        self.wait_ace_ready()
        self._retract(rt_index, 5, 80)
        self.dwell(0.3)

    cmd_ACE_DISABLE_FEED_ASSIST_help = 'Disables ACE feed assist'

    def cmd_ACE_DISABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX', self._feed_assist_index)

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')

        self._disable_feed_assist(index)

    def _feed(self, index, length, speed, how_wait=None):
        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")
                return

        self.wait_ace_ready()
        self.send_request(
            request={"method": "feed_filament", "params": {"index": index, "length": length, "speed": speed}},
            callback=callback)
        if how_wait is not None:
            self.dwell(delay=(how_wait / speed) + 0.1)
        else:
            self.dwell(delay=(length / speed) + 0.1)

    cmd_ACE_FEED_help = 'Feeds filament from ACE'

    def cmd_ACE_FEED(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.feed_speed)

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')

        self._feed(index, length, speed)

    def _retract(self, index, length, speed):
        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")
                return

        self.wait_ace_ready()
        self.send_request(
            request={"method": "unwind_filament", "params": {"index": index, "length": length, "speed": speed}},
            callback=callback)
        self.dwell(delay=(length / speed) + 0.1)

    def retract_fil(self, index):
        if self._retract_length_override is not None:
            length = self._retract_length_override
        else:
            length = self.get_retract_length(self._active_device_index, index)
        self._retract(index, length, self.retract_speed)

    cmd_ACE_RETRACT_help = 'Retracts filament back to ACE'

    def cmd_ACE_RETRACT(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.retract_speed)

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')

        self._retract(index, length, speed)

    def _set_feeding_speed(self, index, speed):
        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")

        self.send_request(
            request={"method": "update_feeding_speed", "params": {"index": index, "speed": speed}},
            callback=callback)

    def _stop_feeding(self, index):
        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")
                return

        self.send_request(
            request={"method": "stop_feed_filament", "params": {"index": index}},
            callback=callback)

    cmd_ACE_SWITCH_help = 'Switch active ACE unit. Usage: ACE_SWITCH TARGET=0 [AUTOLOAD=1]'

    EXTRUDER_MAP = {
        0: ('left', 1),
        1: ('left', 0),
        2: ('right', 0),
        3: ('right', 1),
    }

    def _push_rfid_info(self):

        logging.info('[multiACE] _push_rfid_info: active_device=%d, head_source=%s' % (
            self._active_device_index, str({k: (v['ace_index'] if v else None) for k, v in self._head_source.items()})))
        for i in range(4):
            source = self._head_source.get(i)

            if source:
                logging.info('[multiACE] _push_rfid_info: slot %d - skipped (loaded from ACE %d)' % (i, source['ace_index']))
                continue

            slot = self._info['slots'][i]

            if slot.get('rfid', 0) == 2:

                target_heads = self._get_heads_for_ace_slot(
                    self._active_device_index, i)
                if not target_heads:
                    target_heads = [i]
                logging.info('[multiACE] _push_rfid_info: slot %d - pushing RFID to heads %s (type=%s)' % (
                    i, target_heads, slot.get('type', '?')))
                for head in target_heads:
                    self.gcode.run_script_from_command(
                        'SET_PRINT_FILAMENT_CONFIG '
                        'CONFIG_EXTRUDER=%d '
                        'FILAMENT_TYPE="%s" '
                        'FILAMENT_COLOR_RGBA=%s '
                        'VENDOR="%s" '
                        'FILAMENT_SUBTYPE=""' % (
                            head,
                            slot.get('type', 'PLA'),
                            self.rgb2hex(*slot.get('color', (0, 0, 0))),
                            slot.get('brand', 'Generic')))
            else:

                logging.info('[multiACE] _push_rfid_info: slot %d - clearing (no RFID, not loaded)' % i)
                self.gcode.run_script_from_command(
                    'SET_PRINT_FILAMENT_CONFIG '
                    'CONFIG_EXTRUDER=%d '
                    'FILAMENT_TYPE="" '
                    'FILAMENT_COLOR_RGBA=000000FF '
                    'VENDOR="" '
                    'FILAMENT_SUBTYPE=""' % i)

    def cmd_ACE_SWITCH(self, gcmd):
        target = gcmd.get_int('TARGET')
        autoload = gcmd.get_int('AUTOLOAD', 0)

        if self._swap_in_progress:
            self.log_always('[multiACE] ACE switch already in progress, please wait')
            return
        self._swap_in_progress = True

        try:
            self._do_ace_switch(gcmd, target, autoload)
        finally:
            self._swap_in_progress = False

    def _do_ace_switch(self, gcmd, target, autoload):

        self._refresh_ace_devices('switch')

        if not self._ace_devices:
            self.log_always('[multiACE] No ACE devices detected')
            return

        if not self._is_ace_present(target):
            self._usb_log.info('RETRY [switch] target=%d not present, starting retries', target)
            for retry in range(5):
                self._usb_stats['retries'] += 1
                self.reactor.pause(self.reactor.monotonic() + 1.0)
                self._refresh_ace_devices('switch_retry_%d' % (retry + 1))
                self._usb_log.info('RETRY [switch] attempt=%d/%d present=%d target=%d', retry + 1, 5, len(self._ace_present), target)
                if self._is_ace_present(target):
                    break
        if not self._is_ace_present(target):
            self.log_always('[multiACE] ACE %d not available (present %d). Try again.' % (target, len(self._ace_present)))
            return

        switching_ace = target != self._active_device_index

        if not switching_ace and not autoload:
            self.log_always('[multiACE] ACE %d is already active' % target)
            return

        if not switching_ace and autoload:
            self.log_always('[multiACE] ACE %d already active, loading available filaments...' % target)
        else:
            if target >= len(self._ace_devices) or not self._connected_per_ace.get(target, False):
                self.log_always('[multiACE] ACE %d not connected' % target)
                return

            current_slot = self._feed_assist_per_ace.get(self._active_device_index, -1)
            if current_slot != -1:
                try:
                    self._stop_feed_assist_on(self._active_device_index)
                except Exception as e:
                    logging.info('[multiACE] switch: stop_feed_assist failed: %s' % e)
                self.wait_ace_ready()

            if autoload:
                self.log_always('[multiACE] Unloading from ACE %d...' % self._active_device_index)
                for gate in range(4):
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % gate, None)
                    filament_in_head = sensor and sensor.get_status(0)['filament_detected']
                    module, channel = self.EXTRUDER_MAP[gate]
                    if filament_in_head:
                        self.log_always('[multiACE] Extruder %d: filament in head, full unload' % gate)
                        self.gcode.run_script_from_command(
                            "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare" % (module, channel, gate))
                        self.gcode.run_script_from_command(
                            "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing" % (module, channel, gate))
                    else:
                        self.log_always('[multiACE] Extruder %d: not in head, skipping unload' % gate)
                machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
                if machine_state_manager is not None:
                    self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")
                self.log_always('[multiACE] Unload complete')

            self.log_always('[multiACE] Activating ACE %d' % target)
            self._activate_ace(target)
            self._push_rfid_info()

        if autoload:
            self.log_always('[multiACE] Loading from ACE %d...' % target)
            loaded_any = False

            for gate in range(4):
                sensor = self.printer.lookup_object(
                    'filament_motion_sensor e%d_filament' % gate, None)
                filament_in_head = sensor and sensor.get_status(0)['filament_detected']

                if filament_in_head:
                    self.log_always('[multiACE] Extruder %d: filament already in head, skipping' % gate)
                elif self.gate_status[gate] == GATE_AVAILABLE:
                    module, channel = self.EXTRUDER_MAP[gate]
                    self.log_always('[multiACE] Extruder %d: loading...' % gate)
                    self.gcode.run_script_from_command(
                        "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1" % (module, channel, gate))
                    loaded_any = True
                else:
                    self.log_always('[multiACE] Extruder %d: no filament in ACE, skipping' % gate)

            if loaded_any:
                self.log_always('[multiACE] Load complete from ACE %d' % target)
            else:
                self.log_always('[multiACE] Nothing to load')

        self._audit_state('SWITCH', {'target': target, 'autoload': autoload})

    def _get_heads_for_ace_slot(self, ace_index, slot):

        heads = []
        for head, source in self._head_source.items():
            if source and source['ace_index'] == ace_index and source['slot'] == slot:
                heads.append(head)
        return heads

    def _restore_head_source(self):

        saved = self.save_variables.allVariables.get(self.VARS_ACE_HEAD_SOURCE, None)
        if saved and isinstance(saved, dict):
            for head in range(4):
                key = str(head)
                if key in saved and saved[key]:
                    self._head_source[head] = saved[key]
                    logging.info('[multiACE] Restored head %d -> ACE %d / Slot %d' % (
                        head, saved[key]['ace_index'], saved[key]['slot']))

    def _save_head_source(self):

        save_data = {}
        for head in range(4):
            save_data[str(head)] = self._head_source[head]

        value_str = json.dumps(save_data).replace('null', 'None')
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE='%s'"
            % (self.VARS_ACE_HEAD_SOURCE, value_str))

    def _ensure_ace_available(self, ace_index):

        for attempt in range(5):
            self._refresh_ace_devices('ensure_%d' % (attempt + 1))
            if self._is_ace_present(ace_index):
                if attempt > 0:
                    self._usb_log.info('ENSURE ace=%d found after %d retries', ace_index, attempt)
                return True
            self._usb_stats['retries'] += 1
            self.reactor.pause(self.reactor.monotonic() + 1.0)
        self._usb_log.warning('ENSURE ace=%d FAILED after 5 attempts (present %d)', ace_index, len(self._ace_present))
        return False

    def _switch_ace_for_head(self, head_index):
        source = self._head_source.get(head_index)
        if not source:
            return False

        target_ace = source['ace_index']

        if target_ace == self._active_device_index:
            self._audit_state('SWITCH_AUTO_NOOP', {
                'head': head_index, 'target_ace': target_ace,
                'reason': 'already_active'})
            return True

        if target_ace >= len(self._ace_devices):
            self.log_always('[multiACE] ACE %d out of range for head %d' % (
                target_ace, head_index))
            self._audit_state('SWITCH_AUTO_FAILED', {
                'head': head_index, 'target_ace': target_ace,
                'reason': 'ace_out_of_range'})
            return False

        if not self._connected_per_ace.get(target_ace, False):
            self.log_error('[multiACE] ACE %d not connected for head %d' % (
                target_ace, head_index))
            self._audit_state('SWITCH_AUTO_FAILED', {
                'head': head_index, 'target_ace': target_ace,
                'reason': 'not_connected'})
            return False

        self.log_always('[multiACE] Activating ACE %d for head %d' % (
            target_ace + 1, head_index))

        self._activate_ace(target_ace)

        self._audit_state('SWITCH_AUTO', {
            'head': head_index, 'target_ace': target_ace})
        return True

    def _on_extruder_change(self):
        self._fa_trace('_on_extruder_change fired; gate=%s context=%s active_ace=%d'
                       % (self._auto_feed_enabled, self._fa_context, self._active_device_index))
        if not any(self._head_source[h] for h in range(4)):
            return

        try:
            extruder = self.toolhead.get_extruder()
            head_index = getattr(extruder, 'extruder_index',
                        getattr(extruder, 'extruder_num', None))
        except Exception:
            head_index = None

        if head_index is None:
            self._audit_state('SWITCH_AUTO', {
                'head': None,
                'reason': 'no_head_index',
            })
            return

        source = self._head_source.get(head_index)
        if source is None:
            self._audit_state('SWITCH_AUTO', {
                'head': head_index,
                'reason': 'no_head_source',
            })
            return

        target_ace = source['ace_index']
        target_slot = source['slot']

        if target_ace >= len(self._ace_devices) or not self._connected_per_ace.get(target_ace, False):
            self._audit_state('SWITCH_AUTO_FAILED', {
                'head': head_index,
                'target_ace': target_ace,
                'reason': 'not_connected',
            })
            self.log_error('[multiACE] T%d: target ACE %d not connected' % (
                head_index, target_ace))
            return

        prev_active = self._active_device_index
        prev_slot = self._feed_assist_per_ace.get(prev_active, -1)

        if prev_active != target_ace and prev_slot != -1:
            try:
                self._stop_feed_assist_on(prev_active)
            except Exception as e:
                logging.info('[multiACE] stop_feed_assist on ACE %d failed: %s' % (prev_active, e))

        if prev_active != target_ace:
            self._activate_ace(target_ace)

        current_target_slot = self._feed_assist_per_ace.get(target_ace, -1)
        if current_target_slot != target_slot:
            target_ace_local = target_ace
            target_slot_local = target_slot
            head_index_local = head_index
            def _deferred_fa_start(eventtime):
                if not self._auto_feed_enabled:
                    self._fa_trace(
                        '_on_extruder_change deferred start SUPPRESSED '
                        '(gate closed): head=%d idx=%d slot=%d'
                        % (head_index_local, target_ace_local, target_slot_local))
                    return self.reactor.NEVER
                try:
                    cur_ext = self.toolhead.get_extruder()
                    cur_head = getattr(cur_ext, 'extruder_index',
                                       getattr(cur_ext, 'extruder_num', None))
                except Exception:
                    cur_head = None
                if cur_head != head_index_local:
                    self._fa_trace(
                        '_on_extruder_change deferred start SUPPRESSED '
                        '(stale head): expected=%d actual=%s'
                        % (head_index_local, cur_head))
                    return self.reactor.NEVER
                try:
                    self._start_feed_assist_on(target_ace_local, target_slot_local)
                except Exception as e:
                    logging.info(
                        '[multiACE] deferred start_feed_assist ACE %d slot %d failed: %s'
                        % (target_ace_local, target_slot_local, e))
                return self.reactor.NEVER
            self.reactor.register_timer(
                _deferred_fa_start, self.reactor.monotonic() + 0.1)

        self._audit_state('SWITCH_AUTO', {
            'head': head_index,
            'target_ace': target_ace,
            'target_slot': target_slot,
            'prev_active': prev_active,
            'prev_slot': prev_slot,
        })

        now = time.monotonic()
        gap_ms = None
        if self._last_switch_auto_ts is not None:
            gap_ms = int((now - self._last_switch_auto_ts) * 1000)
        self._last_switch_auto_ts = now
        self._telemetry('SWITCH', {
            'head': head_index,
            'prev_ace': prev_active,
            'prev_slot': prev_slot,
            'target_ace': target_ace,
            'target_slot': target_slot,
            'gap_ms_since_last_switch': gap_ms,
            'print_active': self._fa_context == 'print',
            'ace_changed': prev_active != target_ace,
        })

    cmd_ACE_LOAD_HEAD_help = '[multiACE] Load a toolhead from ACE. Usage: ACE_LOAD_HEAD HEAD=0 [ACE=0] [SLOT=0]'
    def cmd_ACE_LOAD_HEAD(self, gcmd):

        head = gcmd.get_int('HEAD')
        ace_index = gcmd.get_int('ACE', self._active_device_index)
        slot = gcmd.get_int('SLOT', head)
        self._last_load_ok = True

        if head < 0 or head > 3:
            raise gcmd.error('[multiACE] HEAD must be 0-3')
        if ace_index < 0 or not self._ensure_ace_available(ace_index):
            self.log_always('[multiACE] ACE %d not available' % ace_index)
            return
        if slot < 0 or slot > 3:
            raise gcmd.error('[multiACE] SLOT must be 0-3')

        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % head, None)
        if sensor and sensor.get_status(0)['filament_detected']:
            if self._head_source.get(head) is not None:
                self.log_always(
                    '[multiACE] Head %d already has filament loaded. Unload first.' % head)
                return
            if len(self._ace_devices) == 1:
                only_idx = 0
                info = self._info_per_ace.get(only_idx, self._make_default_info())
                slots = info.get('slots', [])
                slot_info = slots[slot] if slot < len(slots) else {}
                self._head_source[head] = {
                    'ace_index': only_idx,
                    'slot': slot,
                    'type': slot_info.get('type', 'PLA'),
                    'color': self.rgb2hex(*slot_info.get('color', (0, 0, 0))),
                    'brand': slot_info.get('brand', 'Generic'),
                }
                self._save_head_source()
                self.log_always(
                    '[multiACE] Head %d already loaded, mapped to the only connected ACE / Slot %d'
                    % (head, slot))
            else:
                self.log_error(
                    '[multiACE] Head %d has filament but no head_source recorded. '
                    'With %d ACEs connected the source cannot be inferred safely. '
                    'Run Unload All to clear all heads, then load fresh.'
                    % (head, len(self._ace_devices)))
            return

        self.log_always('[multiACE] Loading head %d from ACE %d / Slot %d...' % (
            head, ace_index, slot))

        if ace_index != self._active_device_index:
            if not self._switch_ace_for_head_target(ace_index):
                raise gcmd.error(
                    '[multiACE] Failed to connect to ACE %d' % ace_index)

        if self.gate_status[slot] != GATE_AVAILABLE:
            self.log_always('[multiACE] ACE %d / Slot %d has no filament! Aborting load.' % (ace_index, slot))
            return

        active_ext = self.toolhead.get_extruder().get_name()
        target_ext = 'extruder' if head == 0 else 'extruder%d' % head
        if active_ext != target_ext:
            logging.info('[multiACE] Load: switching to %s (was %s)' % (target_ext, active_ext))
            self.gcode.run_script_from_command('T%d A0' % head)
            self.toolhead.wait_moves()

        module, channel = self.EXTRUDER_MAP[head]

        self._head_source[head] = {
            'ace_index': ace_index,
            'slot': slot,
            'type': '',
            'color': '000000',
            'brand': '',
        }
        self._save_head_source()

        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1" % head)

        ff_module = 'filament_feed %s' % module
        try:
            ff = self.printer.lookup_object(ff_module, None)
            if ff is None:
                logging.info('[multiACE] channel_state reset: %s not loaded' % ff_module)
            elif channel >= len(ff.channel_state):
                logging.info(
                    '[multiACE] channel_state reset: channel %d out of range (%d)' % (
                        channel, len(ff.channel_state)))
            else:
                prev_state = ff.channel_state[channel]
                ff.channel_state[channel] = 'inited'
                if 'load_finish' in ff.config:
                    ff.config['load_finish'][channel] = False
                logging.info(
                    '[multiACE] channel_state reset: %s ch=%d prev=%s -> inited, load_finish=False' % (
                        ff_module, channel, prev_state))
        except Exception as e:
            logging.info('[multiACE] channel_state reset error: %s' % e)

        wheel_before = self._read_wheel_counts(module, channel)

        try:
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1"
                % (module, channel, head))
        except Exception as e:
            self._audit_state('LOAD_HEAD_FAILED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'feed_auto_error', 'error': str(e)})
            try:
                self._head_source[head] = None
                self._save_head_source()
            except Exception:
                pass
            self._last_load_ok = False
            raise

        rfid_deadline = time.monotonic() + 3.0
        while time.monotonic() < rfid_deadline:
            if self._info['slots'][slot].get('rfid', 0) != 0:
                break
            time.sleep(0.1)
        if self._info['slots'][slot].get('rfid', 0) == 0:
            logging.info('[multiACE] LOAD_HEAD: RFID not ready for slot %d after wait' % slot)

        slot_info = self._info['slots'][slot]
        self._head_source[head] = {
            'ace_index': ace_index,
            'slot': slot,
            'type': slot_info.get('type', 'PLA'),
            'color': self.rgb2hex(*slot_info.get('color', (0, 0, 0))),
            'brand': slot_info.get('brand', 'Generic'),
        }
        self._save_head_source()
        self._ghost_heads.discard(head)

        self.gcode.run_script_from_command(
            'SET_PRINT_FILAMENT_CONFIG '
            'CONFIG_EXTRUDER=%d '
            'FILAMENT_TYPE="%s" '
            'FILAMENT_COLOR_RGBA=%s '
            'VENDOR="%s" '
            'FILAMENT_SUBTYPE=""' % (
                head,
                self._head_source[head]['type'],
                self._head_source[head]['color'],
                self._head_source[head]['brand']))

        self.log_always('[multiACE] Head %d loaded from ACE %d / Slot %d' % (
            head, ace_index, slot))
        self._audit_state('LOAD_HEAD', {'head': head, 'ace': ace_index, 'slot': slot})

    cmd_ACE_UNLOAD_HEAD_help = (
        '[multiACE] Unload a toolhead back to its ACE. '
        'Usage: ACE_UNLOAD_HEAD HEAD=0 [RETRACT_LENGTH=<mm>] [KEEP_HEAT=<temp>]')
    def cmd_ACE_UNLOAD_HEAD(self, gcmd):

        head = gcmd.get_int('HEAD')
        retract_override = gcmd.get_int('RETRACT_LENGTH', 0)
        keep_heat = gcmd.get_int('KEEP_HEAT', 0)

        self._last_unload_ok = True

        if head < 0 or head > 3:
            raise gcmd.error('[multiACE] HEAD must be 0-3')

        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % head, None)
        if sensor and not sensor.get_status(0)['filament_detected']:
            self.log_always('[multiACE] Note: Sensor on head %d not detecting filament (may be stationary)' % head)

        source = self._head_source.get(head)
        if source:
            ace_index = source['ace_index']
            slot = source['slot']
            self.log_always('[multiACE] Unloading head %d (ACE %d / Slot %d)...' % (
                head, ace_index, slot))

            if ace_index != self._active_device_index:
                if not self._switch_ace_for_head_target(ace_index):
                    raise gcmd.error(
                        '[multiACE] Failed to connect to ACE %d for unload!' % ace_index)
        else:
            self.log_always('[multiACE] Unloading head %d (no ACE mapping, using active ACE)...' % head)

        def _noop_cb(self, response):
            pass
        active_idx = self._active_device_index
        stop_slots = set()
        tracked = self._feed_assist_per_ace.get(active_idx, -1)
        if 0 <= tracked <= 3:
            stop_slots.add(tracked)
        if source is not None:
            src_slot = source.get('slot', -1)
            if 0 <= src_slot <= 3:
                stop_slots.add(src_slot)
        for slot_idx in sorted(stop_slots):
            try:
                self.send_request_to(active_idx,
                    {"method": "stop_feed_assist", "params": {"index": slot_idx}},
                    _noop_cb)
            except Exception as e:
                logging.info(
                    '[multiACE] targeted stop_feed_assist slot %d failed: %s' % (slot_idx, e))
        self._feed_assist_per_ace[active_idx] = -1
        if active_idx == self._active_device_index:
            self._feed_assist_index = -1
        self.wait_ace_ready()
        self._fa_trace('targeted-stop FA on ACE %d slots=%s before unload' % (
            active_idx, sorted(stop_slots)))

        if not self._swap_in_progress:
            self.gcode.run_script_from_command(
                "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=0" % head)

        module, channel = self.EXTRUDER_MAP[head]
        self._retract_length_override = retract_override if retract_override > 0 else None
        try:
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare"
                % (module, channel, head))
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing"
                % (module, channel, head))
        except Exception as e:
            self._audit_state('UNLOAD_HEAD_FAILED', {'head': head, 'reason': 'feed_auto_error', 'error': str(e), 'active_device': self._active_device_index})
            raise
        finally:
            self._retract_length_override = None

        if keep_heat > 0:
            self.gcode.run_script_from_command('M104 S%d' % keep_heat)

        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1" % head)

        machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
        if machine_state_manager is not None:
            self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")

        self._head_source[head] = None
        self._save_head_source()
        self._push_rfid_info()

        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_error('[multiACE] Warning: Filament still detected in head %d after unload!' % head)
        else:
            self.log_always('[multiACE] Head %d unloaded successfully' % head)
        self._audit_state('UNLOAD_HEAD', {'head': head})

    cmd_ACE_TEST_help = (
        '[multiACE] Run load/unload test. PLAN items (comma-sep): '
        '0:1=load HEAD:ACE, H0:1=swap HEAD to ACE, A0=all from ACE, '
        'U=unload all, U0..U3=unload head, S0..S3=switch ACE, W5=wait 5s')
    def cmd_ACE_TEST(self, gcmd):
        plan_str = gcmd.get('PLAN', '')
        do_unload = gcmd.get_int('UNLOAD', 1)

        was_debug = self._state_debug_enabled
        self._state_debug_enabled = True
        self._state_log.info('TEST_START plan="%s" unload=%d', plan_str, do_unload)

        try:
            hs_dump = json.dumps({str(h): self._head_source[h] for h in range(4)})
        except Exception:
            hs_dump = str(self._head_source)
        self._state_log.info('TEST_START head_source=%s active_device=%d',
                             hs_dump, self._active_device_index)
        self._audit_state('TEST_START', {'plan': plan_str, 'unload': do_unload})

        steps = []
        if plan_str:
            for item in plan_str.split(','):
                item = item.strip()
                if not item:
                    continue
                if item == 'U':
                    steps.append({'action': 'UNLOAD_ALL'})
                elif item.startswith('U') and item[1:].isdigit():
                    steps.append({'action': 'UNLOAD', 'head': int(item[1:])})
                elif item.startswith('A') and item[1:].isdigit():
                    ace = int(item[1:])
                    for h in range(4):
                        steps.append({'action': 'LOAD', 'head': h, 'ace': ace})
                elif item.startswith('H') and ':' in item[1:]:
                    parts = item[1:].split(':')
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        steps.append({'action': 'SWAP', 'head': int(parts[0]), 'ace': int(parts[1])})
                    else:
                        raise gcmd.error('[multiACE] Invalid PLAN item: %s (use H0:1)' % item)
                elif item.startswith('S') and item[1:].isdigit():
                    steps.append({'action': 'SWITCH', 'ace': int(item[1:])})
                elif item.startswith('W') and item[1:].replace('.', '', 1).isdigit():
                    steps.append({'action': 'WAIT', 'seconds': float(item[1:])})
                elif ':' in item:
                    parts = item.split(':')
                    if len(parts) == 2:
                        steps.append({'action': 'LOAD', 'head': int(parts[0]), 'ace': int(parts[1])})
                    else:
                        raise gcmd.error('[multiACE] Invalid PLAN item: %s' % item)
                else:
                    raise gcmd.error(
                        '[multiACE] Invalid PLAN item: %s '
                        '(use HEAD:ACE, A0, U, U0..U3, S0..S3, W<seconds>)' % item)
        else:
            self._refresh_ace_devices('test')
            for i in range(min(len(self._ace_devices), 4)):
                steps.append({'action': 'LOAD', 'head': i, 'ace': i})

        self.log_always('[multiACE] === TEST START: %d steps, unload=%s ===' % (
            len(steps), 'yes' if do_unload else 'no'))

        try:
            self.gcode.run_script_from_command('G28')
            self.toolhead.wait_moves()
        except Exception as e:
            self.log_always('[multiACE] TEST: homing failed: %s' % e)

        self._test_cancel = False
        results = []
        step_nr = 0
        for step in steps:
            if self._test_cancel:
                self.log_always('[multiACE] TEST CANCELLED by user — stopping after step %d' % step_nr)
                results.append({'step': step_nr + 1, 'action': 'CANCEL', 'status': 'CANCELLED'})
                break
            step_nr += 1
            action = step['action']

            if action == 'LOAD':
                head = step['head']
                ace = step['ace']
                self.log_always('[multiACE] --- Step %d/%d: LOAD HEAD=%d ACE=%d SLOT=%d ---' % (
                    step_nr, len(steps), head, ace, head))
                try:
                    self.gcode.run_script_from_command(
                        'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace, head))
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % head, None)
                    detected = sensor and sensor.get_status(0)['filament_detected']
                    src = self._head_source.get(head)
                    if detected and src is not None:
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'PASS', 'head': head, 'ace': ace})
                        self.log_always('[multiACE] Step %d: PASS (sensor=ok, mapping=ok)' % step_nr)
                    else:
                        reason = []
                        if not detected:
                            reason.append('sensor=no_filament')
                        if src is None:
                            reason.append('mapping=missing')
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'FAIL',
                                        'head': head, 'ace': ace, 'reason': ', '.join(reason)})
                        self.log_always('[multiACE] Step %d: FAIL (%s)' % (step_nr, ', '.join(reason)))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'LOAD', 'status': 'ERROR',
                                    'head': head, 'ace': ace, 'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')

            elif action == 'UNLOAD':
                head = step['head']
                self.log_always('[multiACE] --- Step %d/%d: UNLOAD HEAD=%d ---' % (
                    step_nr, len(steps), head))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_HEAD HEAD=%d' % head)
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % head, None)
                    still_loaded = sensor and sensor.get_status(0)['filament_detected']
                    if not still_loaded:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'PASS', 'head': head})
                        self.log_always('[multiACE] Step %d: PASS (sensor=clear)' % step_nr)
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'FAIL',
                                        'head': head, 'reason': 'filament still detected'})
                        self.log_always('[multiACE] Step %d: FAIL (filament still detected)' % step_nr)
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'ERROR',
                                    'head': head, 'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')

            elif action == 'UNLOAD_ALL':
                self.log_always('[multiACE] --- Step %d/%d: UNLOAD ALL ---' % (
                    step_nr, len(steps)))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                    all_clear = True
                    for h in range(4):
                        sensor = self.printer.lookup_object(
                            'filament_motion_sensor e%d_filament' % h, None)
                        if sensor and sensor.get_status(0)['filament_detected']:
                            all_clear = False
                    if all_clear:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                        self.log_always('[multiACE] Step %d: PASS (all sensors clear)' % step_nr)
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'FAIL',
                                        'reason': 'filament still detected'})
                        self.log_always('[multiACE] Step %d: FAIL (filament still detected)' % step_nr)
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'ERROR',
                                    'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')

            elif action == 'SWITCH':
                ace = step['ace']
                self.log_always('[multiACE] --- Step %d/%d: SWITCH TARGET=%d ---' % (
                    step_nr, len(steps), ace))
                try:
                    self.gcode.run_script_from_command('ACE_SWITCH TARGET=%d' % ace)
                    if self._active_device_index == ace:
                        results.append({'step': step_nr, 'action': 'SWITCH', 'status': 'PASS', 'ace': ace})
                        self.log_always('[multiACE] Step %d: PASS (active=%d)' % (step_nr, ace))
                    else:
                        results.append({'step': step_nr, 'action': 'SWITCH', 'status': 'FAIL',
                                        'ace': ace, 'reason': 'active=%d' % self._active_device_index})
                        self.log_always('[multiACE] Step %d: FAIL (active=%d, expected %d)' % (
                            step_nr, self._active_device_index, ace))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'SWITCH', 'status': 'ERROR',
                                    'ace': ace, 'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))

            elif action == 'SWAP':
                head = step['head']
                ace = step['ace']
                self.log_always('[multiACE] --- Step %d/%d: SWAP HEAD=%d ACE=%d ---' % (
                    step_nr, len(steps), head, ace))
                try:
                    self.gcode.run_script_from_command(
                        'ACE_SWAP_HEAD HEAD=%d ACE=%d' % (head, ace))
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % head, None)
                    detected = sensor and sensor.get_status(0)['filament_detected']
                    src = self._head_source.get(head)
                    if detected and src is not None and src['ace_index'] == ace:
                        results.append({'step': step_nr, 'action': 'SWAP', 'status': 'PASS',
                                        'head': head, 'ace': ace})
                        self.log_always('[multiACE] Step %d: PASS (sensor=ok, mapping=ACE %d)' % (step_nr, ace))
                    else:
                        reason = []
                        if not detected:
                            reason.append('sensor=no_filament')
                        if src is None:
                            reason.append('mapping=missing')
                        elif src['ace_index'] != ace:
                            reason.append('mapping=ACE %d (expected %d)' % (src['ace_index'], ace))
                        results.append({'step': step_nr, 'action': 'SWAP', 'status': 'FAIL',
                                        'head': head, 'ace': ace, 'reason': ', '.join(reason)})
                        self.log_always('[multiACE] Step %d: FAIL (%s)' % (step_nr, ', '.join(reason)))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'SWAP', 'status': 'ERROR',
                                    'head': head, 'ace': ace, 'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')

            elif action == 'WAIT':
                seconds = step['seconds']
                self.log_always('[multiACE] --- Step %d/%d: WAIT %.1fs ---' % (
                    step_nr, len(steps), seconds))
                try:
                    self.reactor.pause(self.reactor.monotonic() + seconds)
                    results.append({'step': step_nr, 'action': 'WAIT', 'status': 'PASS', 'seconds': seconds})
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'WAIT', 'status': 'ERROR',
                                    'seconds': seconds, 'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))

        if do_unload:
            step_nr += 1
            self.log_always('[multiACE] --- Final: UNLOAD ALL ---')
            try:
                self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                all_clear = True
                for h in range(4):
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % h, None)
                    if sensor and sensor.get_status(0)['filament_detected']:
                        all_clear = False
                if all_clear:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                    self.log_always('[multiACE] Final unload: PASS')
                else:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'FAIL',
                                    'reason': 'filament still detected'})
                    self.log_always('[multiACE] Final unload: FAIL (filament still detected)')
            except Exception as e:
                results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'ERROR',
                                'reason': str(e)})
                self.log_always('[multiACE] Final unload: ERROR (%s)' % str(e))

        passed = sum(1 for r in results if r['status'] == 'PASS')
        failed = sum(1 for r in results if r['status'] == 'FAIL')
        errors = sum(1 for r in results if r['status'] == 'ERROR')
        total = len(results)
        self.log_always('[multiACE] === TEST COMPLETE: %d/%d PASS, %d FAIL, %d ERROR ===' % (
            passed, total, failed, errors))

        self._state_log.info('TEST_RESULT %s', json.dumps(results, default=str))
        self._state_debug_enabled = was_debug

    def _get_swap_temp(self, head):

        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            fp = self.printer.lookup_object('filament_parameters', None)
            if ptc is not None and fp is not None:
                status = ptc.get_status()
                temp = fp.get_load_temp(
                    status['filament_vendor'][head],
                    status['filament_type'][head],
                    status['filament_sub_type'][head])
                if temp and temp >= 170:
                    return int(temp)
        except Exception:
            pass

        try:
            extruder_name = 'extruder' if head == 0 else 'extruder%d' % head
            extruder = self.printer.lookup_object(extruder_name, None)
            if extruder is not None:
                target = extruder.get_heater().target_temp
                if target >= 170:
                    return int(target)
        except Exception:
            pass

        return self.swap_default_temp

    cmd_ACE_SWAP_HEAD_help = '[multiACE] Mid-print filament swap. Usage: ACE_SWAP_HEAD HEAD=0 ACE=1 [SLOT=0]'
    def cmd_ACE_SWAP_HEAD(self, gcmd):
        head = gcmd.get_int('HEAD')
        ace_index = gcmd.get_int('ACE')
        slot = gcmd.get_int('SLOT', head)

        if head < 0 or head > 3:
            raise gcmd.error('[multiACE] HEAD must be 0-3')
        if ace_index < 0 or not self._ensure_ace_available(ace_index):
            raise gcmd.error('ACE %d not available' % ace_index)
        if slot < 0 or slot > 3:
            raise gcmd.error('[multiACE] SLOT must be 0-3')
        if head in self._ghost_heads:
            raise gcmd.error(
                '[multiACE] SWAP refused: head %d is a ghost (filament at '
                'toolhead but no head_source mapping recorded). FA routing '
                'would have to guess which ACE to drive. '
                'Recover: ACEC__Unload_All then ACEB__Load_%d, then restart '
                'the print.' % (head, head))

        source = self._head_source.get(head)
        if source and source['ace_index'] == ace_index and source['slot'] == slot:
            logging.info('[multiACE] Swap: HEAD %d already on ACE %d / Slot %d — skipping' % (
                head, ace_index, slot))
            swap_temp = self._get_swap_temp(head)
            if swap_temp >= 170:
                heater = 'extruder' if head == 0 else 'extruder%d' % head
                self.gcode.run_script_from_command(
                    'SET_HEATER_TEMPERATURE HEATER=%s TARGET=%d' % (heater, swap_temp))
                self.gcode.run_script_from_command(
                    'TEMPERATURE_WAIT SENSOR=%s MINIMUM=%d' % (heater, swap_temp - 5))
            return

        if ace_index in self._fa_load_disable:
            self.log_error(
                '[multiACE] SWAP refused: ACE %d is in fa_load_disable. '
                'This ACE expects manual filament feed — swap not supported. '
                'Load head %d manually instead.' % (ace_index, head))
            return

        target_gate = self._gate_status_per_ace.get(ace_index)
        if (target_gate is not None and slot < len(target_gate)
                and target_gate[slot] != GATE_AVAILABLE):
            cur_src = self._head_source.get(head)
            self._telemetry('SWAP_SUMMARY', {
                'head': head,
                'from_ace': cur_src['ace_index'] if cur_src else None,
                'from_slot': cur_src['slot'] if cur_src else None,
                'to_ace': ace_index,
                'to_slot': slot,
                'status': 'slot_empty_pre_unload',
                'total_ms': 0,
                'unload_ms': None,
                'load_ms': None,
                'context': self._fa_context,
            })
            self._pause_for_recovery(
                phase='swap slot_empty (pre-unload)',
                display_msg='A%dS%d leer' % (ace_index, slot),
                detail_msg=('ACE %d Slot %d leer - siehe Fluidd log fuer Recovery'
                            % (ace_index, slot)),
                recovery_steps=[
                    'Load filament into ACE %d slot %d' % (ace_index, slot),
                    'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d   (re-run swap)'
                        % (head, ace_index, slot),
                    'RESUME                            (continue the print)',
                ],
            )
            return

        swap_temp = self._get_swap_temp(head)

        self.log_always('[multiACE] === Mid-print swap: HEAD %d -> ACE %d / Slot %d (temp=%d) ===' % (
            head, ace_index, slot, swap_temp))

        swap_start_ts = time.monotonic()
        unload_start_ts = None
        unload_end_ts = None
        load_start_ts = None
        load_end_ts = None
        swap_status = 'ok'
        prev_source = self._head_source.get(head)
        prev_ace_src = prev_source['ace_index'] if prev_source else None
        prev_slot_src = prev_source['slot'] if prev_source else None

        self._swap_in_progress = True
        fa_prev_auto = self._auto_feed_enabled
        fa_prev_context = self._fa_context
        self._auto_feed_enabled = False
        self._fa_context = 'idle'
        self._fa_trace('gate CLOSE for swap unload (was auto=%s context=%s)' % (
            fa_prev_auto, fa_prev_context))

        try:
            gcode_move = self.printer.lookup_object('gcode_move')
            saved_pos = self.toolhead.get_position()[:3]
            saved_speed = gcode_move.speed
            saved_absolute = gcode_move.absolute_coord
            saved_e_base = gcode_move.base_position[3]
            saved_e_last = gcode_move.last_position[3]
            logging.info('[multiACE] Swap: saved pos X=%.2f Y=%.2f Z=%.2f (pre-T-switch)' % (
                saved_pos[0], saved_pos[1], saved_pos[2]))

            orig_ext_name = self.toolhead.get_extruder().get_name()
            target_ext = 'extruder' if head == 0 else 'extruder%d' % head
            switched_head = (orig_ext_name != target_ext)
            if switched_head:
                logging.info('[multiACE] Swap: switching to %s (was %s)' % (target_ext, orig_ext_name))
                self.gcode.run_script_from_command('T%d A0' % head)
                self.toolhead.wait_moves()

            saved_heater_target = 0
            try:
                extruder_obj = self.toolhead.get_extruder()
                if extruder_obj is not None:
                    saved_heater_target = int(extruder_obj.get_heater().target_temp)
            except Exception:
                pass
            logging.info('[multiACE] Swap: saved heater=%d (swap head)' % saved_heater_target)

            prev_ace = self._active_device_index
            if self._feed_assist_per_ace.get(prev_ace, -1) != -1:
                self._stop_feed_assist_on(prev_ace)

            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('G1 Z2 F600')
            self.gcode.run_script_from_command('G90')
            self.toolhead.wait_moves()

            self.gcode.run_script_from_command('M83')

            sensor_obj = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % head, None)
            sensor_present = (sensor_obj is not None and
                              sensor_obj.get_status(0)['filament_detected'])
            empty_head = (not sensor_present) and (prev_source is None)

            if empty_head:
                logging.info(
                    '[multiACE] Swap: head %d is empty '
                    '(sensor=False, head_source=None) — skipping unload, '
                    'proceeding directly to load' % head)
                unload_start_ts = time.monotonic()
                unload_end_ts = unload_start_ts
            else:
                logging.info('[multiACE] Swap: delegating unload to ACE_UNLOAD_HEAD')
                unload_start_ts = time.monotonic()
                self.gcode.run_script_from_command(
                    'ACE_UNLOAD_HEAD HEAD=%d RETRACT_LENGTH=%d KEEP_HEAT=%d' % (
                        head, self.swap_retract_length, swap_temp))
                unload_end_ts = time.monotonic()
                logging.info('[multiACE] Swap: unload done (retract %dmm, heat held @ %d)' % (
                    self.swap_retract_length, swap_temp))

                if not self._last_unload_ok:
                    swap_status = 'unload_failed'
                    self._swap_back_to_orig_for_pause(
                        switched_head, orig_ext_name)
                    self._pause_for_recovery(
                        phase='swap unload_failed',
                        display_msg='Unload H%d jam' % head,
                        detail_msg=('Head %d unload jam - siehe Fluidd log fuer Recovery'
                                    % head),
                        recovery_steps=[
                            'ACE_UNLOAD_HEAD HEAD=%d           (try unload again)' % head,
                            'ACE_SWITCH TARGET=%d             (switch to target ACE)' % ace_index,
                            'ACE_LOAD_HEAD HEAD=%d            (load target filament)' % head,
                            'RESUME                           (continue the print)',
                        ],
                    )
                    return

            if ace_index != self._active_device_index:
                self.log_always('[multiACE] Swap: switching to ACE %d...' % ace_index)
                if not self._switch_ace_for_head_target(ace_index):
                    raise gcmd.error('[multiACE] Failed to connect to ACE %d' % ace_index)

            if self.gate_status[slot] != GATE_AVAILABLE:
                swap_status = 'slot_empty'
                self._swap_back_to_orig_for_pause(
                    switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                self._pause_for_recovery(
                    phase='swap slot_empty (post-unload)',
                    display_msg='A%dS%d leer' % (ace_index, slot),
                    detail_msg=('ACE %d Slot %d leer (post-unload) - siehe Fluidd log'
                                % (ace_index, slot)),
                    recovery_steps=[
                        'Load filament into ACE %d slot %d' % (ace_index, slot),
                        'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (load head)'
                            % (head, ace_index, slot),
                        'RESUME                            (continue the print)',
                    ],
                )
                return

            logging.info('[multiACE] Swap: delegating load to ACE_LOAD_HEAD (ACE %d / Slot %d)' % (ace_index, slot))
            load_start_ts = time.monotonic()
            try:
                self.gcode.run_script_from_command(
                    'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace_index, slot))
            except Exception as load_e:
                logging.info(
                    '[multiACE] Swap LOAD raised before completion: %s '
                    '(routing to swap_back+pos_restore+pause)' % load_e)
                self._swap_back_to_orig_for_pause(
                    switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                raise
            load_end_ts = time.monotonic()

            if not self._last_load_ok:
                swap_status = 'load_failed'
                self._swap_back_to_orig_for_pause(
                    switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                self._pause_for_recovery(
                    phase='swap load_failed',
                    display_msg='Load H%d slip' % head,
                    detail_msg=('Head %d Load slip - siehe Fluidd log fuer Recovery'
                                % head),
                    recovery_steps=[
                        'ACE_UNLOAD_HEAD HEAD=%d           (clear partial filament)'
                            % head,
                        'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (reload)'
                            % (head, ace_index, slot),
                        'RESUME                           (continue the print)',
                    ],
                )
                return

            logging.info('[multiACE] Swap: load done')

            self._auto_feed_enabled = True
            self._fa_context = fa_prev_context if fa_prev_context in ('print', 'load') else 'print'
            try:
                self._start_feed_assist_on(ace_index, slot)
                self.wait_ace_ready()
                self._fa_trace('gate RE-OPEN for post-load wipe (context=%s) on ACE %d slot %d' % (
                    self._fa_context, ace_index, slot))
            except Exception as fa_e:
                logging.info('[multiACE] post-load FA re-enable failed: %s' % fa_e)

            self.gcode.run_script_from_command('M109 S%d' % swap_temp)
            self.gcode.run_script_from_command('ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD')
            self.toolhead.wait_moves()

            self.gcode.run_script_from_command('G91')
            if self.swap_anti_ooze_retract > 0:
                self.gcode.run_script_from_command(
                    'G1 E-%d F1800' % self.swap_anti_ooze_retract)
            self.gcode.run_script_from_command('G90')
            self.toolhead.wait_moves()

            self.gcode.run_script_from_command('M104 S%d' % saved_heater_target)
            if saved_heater_target >= 190:
                self.gcode.run_script_from_command('M109 S%d' % saved_heater_target)
            logging.info('[multiACE] Swap: restored swap head heater target=%d' % saved_heater_target)

            if switched_head:
                orig_head = 0 if orig_ext_name == 'extruder' else int(
                    orig_ext_name.replace('extruder', ''))
                logging.info('[multiACE] Swap: switching back to %s' % orig_ext_name)
                self.gcode.run_script_from_command('T%d A0' % orig_head)
                self.toolhead.wait_moves()

            e_diff = gcode_move.last_position[3] - saved_e_last
            gcode_move.base_position[3] = saved_e_base + e_diff

            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 3.0))
            self.gcode.run_script_from_command('G0 Y%.3f F12000' % saved_pos[1])
            self.gcode.run_script_from_command('G0 X%.3f F12000' % saved_pos[0])
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 2.0))
            self.toolhead.wait_moves()

            try:
                orig_extruder_obj = self.printer.lookup_object(
                    orig_ext_name, None) if orig_ext_name else None
                orig_heater = (orig_extruder_obj.get_heater()
                               if orig_extruder_obj is not None else None)
                eventtime = self.reactor.monotonic()
                cur_temp, _ = (orig_heater.get_temp(eventtime)
                               if orig_heater is not None else (0.0, 0.0))
                min_temp = (orig_heater.min_extrude_temp
                            if orig_heater is not None else 170.0)
            except Exception:
                cur_temp = 0.0
                min_temp = 170.0
            if cur_temp >= min_temp:
                self.gcode.run_script_from_command('G91')
                self.gcode.run_script_from_command(
                    'G1 E%d F1800' % self.swap_anti_ooze_retract)
                self.toolhead.wait_moves()
            else:
                logging.info(
                    '[multiACE] Swap: skipping anti-ooze undo G1 E+%d '
                    '(orig %s at %.1f<%.0f min_extrude_temp); '
                    'adjusting gcode E base virtually'
                    % (self.swap_anti_ooze_retract, orig_ext_name or '?',
                       cur_temp, min_temp))
                gcode_move.base_position[3] -= float(
                    self.swap_anti_ooze_retract)

            if saved_absolute:
                self.gcode.run_script_from_command('G90')

            e_diff2 = gcode_move.last_position[3] - saved_e_last
            gcode_move.base_position[3] = saved_e_base + e_diff2

            self.gcode.run_script_from_command('G1 F%d' % (saved_speed * 60))

            logging.info('[multiACE] Swap: restored pos X=%.2f Y=%.2f Z=%.2f (+2mm travel hop)' % (
                saved_pos[0], saved_pos[1], saved_pos[2]))

            self.log_always('[multiACE] === Swap complete: Head %d now on ACE %d / Slot %d ===' % (
                head, ace_index, slot))
        finally:
            self._swap_in_progress = False
            self._auto_feed_enabled = fa_prev_auto
            self._fa_context = fa_prev_context
            if fa_prev_auto:
                try:
                    active_ext = self.toolhead.get_extruder().get_name()
                    active_head = (0 if active_ext == 'extruder'
                                   else int(active_ext.replace('extruder', '')))
                    active_source = self._head_source.get(active_head)
                    if active_source is not None:
                        self._start_feed_assist_on(
                            active_source['ace_index'], active_source['slot'])
                    else:
                        logging.info(
                            '[multiACE] post-swap FA: active head %d has no head_source, skipping start' % active_head)
                except Exception as e:
                    logging.info('[multiACE] post-swap FA start failed: %s' % e)
            self._fa_trace('gate restored (context=%s auto=%s) after ACE_SWAP_HEAD'
                           % (fa_prev_context, fa_prev_auto))
            self._audit_state('SWAP_HEAD', {'head': head, 'ace': ace_index, 'slot': slot})

            def _dur_ms(start, end):
                if start is None or end is None:
                    return None
                return int((end - start) * 1000)
            swap_end_ts = time.monotonic()
            self._telemetry('SWAP_SUMMARY', {
                'head': head,
                'from_ace': prev_ace_src,
                'from_slot': prev_slot_src,
                'to_ace': ace_index,
                'to_slot': slot,
                'status': swap_status,
                'total_ms': _dur_ms(swap_start_ts, swap_end_ts),
                'unload_ms': _dur_ms(unload_start_ts, unload_end_ts),
                'load_ms': _dur_ms(load_start_ts, load_end_ts),
                'context': fa_prev_context,
            })

    def _switch_ace_for_head_target(self, ace_index):
        if ace_index == self._active_device_index:
            self._audit_state('SWITCH_TARGET_NOOP', {
                'target_ace': ace_index, 'reason': 'already_active'})
            return True
        if ace_index < 0 or ace_index >= len(self._ace_devices):
            self._audit_state('SWITCH_TARGET_FAILED', {
                'target_ace': ace_index, 'reason': 'ace_out_of_range'})
            return False
        if not self._connected_per_ace.get(ace_index, False):
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self._connected_per_ace.get(ace_index, False):
                    break
                self.reactor.pause(self.reactor.monotonic() + 0.2)
            if not self._connected_per_ace.get(ace_index, False):
                self._audit_state('SWITCH_TARGET_FAILED', {
                    'target_ace': ace_index, 'reason': 'not_connected'})
                return False

        self._activate_ace(ace_index)
        self._audit_state('SWITCH_TARGET', {'target_ace': ace_index})
        return True

    cmd_ACE_HEAD_STATUS_help = '[multiACE] Show active ACE, detected devices, and head-to-ACE/slot mapping'
    def cmd_ACE_HEAD_STATUS(self, gcmd):
        try:
            ace_mtime = os.path.getmtime(os.path.abspath(__file__))
            from datetime import datetime
            ts = datetime.fromtimestamp(ace_mtime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ts = 'unknown'
        self.log_always('[multiACE] v%s "%s" build=%s file=%s' % (
            MULTIACE_VERSION, MULTIACE_CODENAME, MULTIACE_BUILD_TAG, ts))

        actual_bundle = self._compute_bundle_sha1()
        expected_bundle = MULTIACE_BUNDLE_SHA1
        marker = 'MATCH' if expected_bundle == actual_bundle else 'MISMATCH'
        self.log_always('[multiACE] bundle: expected=%s actual=%s [%s]' % (
            expected_bundle, actual_bundle, marker))

        s = self._usb_stats
        uptime_min = (time.monotonic() - s['start_time']) / 60.0
        self.log_always(
            '[multiACE] USB stats (uptime %.1fmin): errno5=%d (recovered=%d, lost=%d), '
            'cascades=%d, connects=%d, disconnects=%d' % (
                uptime_min, s['errno5_total'], s['errno5_recovered'],
                s['errno5_unrecovered'], s['cascades'],
                s['connects'], s['disconnects']))

        device_count = len(self._ace_devices)
        if device_count == 0:
            self.log_always('[multiACE] No ACE devices detected')
            return
        self.log_always('[multiACE] Active ACE: %d of %d' % (
            self._active_device_index, device_count))

        for i, device in enumerate(self._ace_devices):
            marker = ' << ACTIVE' if i == self._active_device_index else ''
            self.log_always('  ACE %d: %s%s' % (i, device, marker))

        self.log_always('[multiACE] Head Source Mapping:')
        any_loaded = False
        for head in range(4):
            source = self._head_source[head]
            if source:
                any_loaded = True
                self.log_always(
                    '  T%d -> ACE %d / Slot %d  [%s %s %s]' % (
                        head,
                        source['ace_index'],
                        source['slot'],
                        source.get('brand', ''),
                        source.get('type', ''),
                        source.get('color', '')))
            else:
                self.log_always('  T%d -> (empty)' % head)
        if not any_loaded:
            self.log_always('  No heads loaded')

    cmd_ACE_CLEAR_HEADS_help = '[multiACE] Clear head-to-ACE/slot mapping and display info. Usage: ACE_CLEAR_HEADS [HEAD=0]'
    def cmd_ACE_CLEAR_HEADS(self, gcmd):
        head = gcmd.get_int('HEAD', -1)
        if head >= 0:
            if head > 3:
                raise gcmd.error('[multiACE] HEAD must be 0-3')
            self._head_source[head] = None
            self._clear_filament_display(head)
            self.log_always('[multiACE] Cleared head %d mapping + display' % head)
        else:
            self._head_source = {0: None, 1: None, 2: None, 3: None}
            for h in range(4):
                self._clear_filament_display(h)
            self.log_always('[multiACE] Cleared all head mappings + display')
        self._save_head_source()
        self._audit_state('CLEAR_HEADS', {'head': head})

    def _push_slot_rfid_to_extruder(self, head):
        try:
            slots = self._info.get('slots', [{}] * 4)
            if head < 0 or head >= len(slots):
                return
            si = slots[head]
            if si.get('rfid') != 2:
                return
            self.gcode.run_script_from_command(
                'SET_PRINT_FILAMENT_CONFIG '
                'CONFIG_EXTRUDER=%d '
                'FILAMENT_TYPE="%s" '
                'FILAMENT_COLOR_RGBA=%s '
                'VENDOR="%s" '
                'FILAMENT_SUBTYPE=""' % (
                    head,
                    si.get('type', 'PLA'),
                    self.rgb2hex(*si.get('color', (0, 0, 0))),
                    si.get('brand', 'Generic')))
        except Exception as e:
            logging.info(
                '[multiACE] _push_slot_rfid_to_extruder(%d) failed: %s' % (head, e))

    def _clear_filament_display(self, head):

        try:
            self.gcode.run_script_from_command(
                'SET_PRINT_FILAMENT_CONFIG '
                'CONFIG_EXTRUDER=%d '
                'FILAMENT_TYPE="" '
                'FILAMENT_COLOR_RGBA=00000000 '
                'VENDOR="" '
                'FILAMENT_SUBTYPE=""' % head)
        except Exception:
            pass

    cmd_ACE_UNLOAD_ALL_HEADS_help = '[multiACE] Unload all toolheads that have filament loaded'
    def cmd_ACE_UNLOAD_ALL_HEADS(self, gcmd):

        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()

        unloaded_any = False
        for head in range(4):
            sensor = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % head, None)
            if not sensor or not sensor.get_status(0)['filament_detected']:
                continue

            source = self._head_source.get(head)
            if source and source['ace_index'] != self._active_device_index:
                self.log_always('[multiACE] Switching to ACE %d for head %d retract...' % (
                    source['ace_index'], head))
                switched = False
                for attempt in range(5):
                    if self._switch_ace_for_head_target(source['ace_index']):
                        switched = True
                        break
                    self.log_always('[multiACE] ACE %d not reachable (attempt %d/5), retrying...' % (
                        source['ace_index'], attempt + 1))
                    time.sleep(1.0)
                if not switched:
                    self.log_error('[multiACE] Failed to connect to ACE %d after 5 retries, skipping head %d' % (
                        source['ace_index'], head))
                    continue

            self.log_always('[multiACE] Unloading head %d...' % head)
            module, channel = self.EXTRUDER_MAP[head]

            self._audit_state('UNLOAD_ALL_STEP', {
                'head': head,
                'active_device': self._active_device_index,
                'expected_ace': source['ace_index'] if source else None,
                'expected_slot': source['slot'] if source else None,
            })

            try:
                self.gcode.run_script_from_command(
                    "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare" % (module, channel, head))
                self.gcode.run_script_from_command(
                    "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing" % (module, channel, head))
            except Exception as e:
                self.log_always('[multiACE] WARNING: Unload head %d failed: %s' % (head, str(e)))

            machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
            if machine_state_manager is not None:
                self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")

            self._head_source[head] = None
            self._push_slot_rfid_to_extruder(head)
            unloaded_any = True

        if unloaded_any:
            self._save_head_source()

            if self._active_device_index != 0 and len(self._ace_devices) > 0:
                self.log_always('[multiACE] Switching back to ACE 0...')
                self._switch_ace_for_head_target(0)

            self._push_rfid_info()
            self.log_always('[multiACE] All heads unloaded')
        else:
            self.log_always('[multiACE] No filament detected in any head')

        cleared = []
        for h in range(4):
            sensor = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % h, None)
            detected = sensor and sensor.get_status(0)['filament_detected']
            if not detected and self._head_source.get(h) is not None:
                self._head_source[h] = None
                cleared.append(h)
        if cleared:
            self._save_head_source()
            self._push_rfid_info()
            self.log_always(
                '[multiACE] Cleared stale head_source for heads: %s' %
                ', '.join('T%d' % h for h in cleared))

        self._audit_state('UNLOAD_ALL')

    def cmd_ACE_TEST_CANCEL(self, gcmd):
        self._test_cancel = True
        self.log_always('[multiACE] TEST cancel requested — stopping after current step')

    cmd_ACE_DRY_help = '[multiACE] Start drying on ACE. Usage: ACE_DRY ACE=0 [TEMP=] [DURATION=]'
    def cmd_ACE_DRY(self, gcmd):

        ace_idx = gcmd.get_int('ACE')
        if ace_idx < 0 or ace_idx >= len(self._ace_devices):
            self.log_always('[multiACE] ACE %d not available' % ace_idx)
            return
        temp = gcmd.get_int('TEMP', self.ace_dryer_temp.get(ace_idx, self.dryer_temp))
        duration = gcmd.get_int('DURATION', self.ace_dryer_duration.get(ace_idx, self.dryer_duration))
        self.gcode.run_script_from_command('ACE_SWITCH TARGET=%d' % ace_idx)
        self.gcode.run_script_from_command('ACE_START_DRYING TEMP=%d DURATION=%d' % (temp, duration))
        self.log_always('[multiACE] Drying ACE %d at %d°C for %d min' % (ace_idx, temp, duration))

    cmd_ACE_RUN_MODE_SWITCH_help = '[multiACE] Switch mode: normal (stock), single (one ACE), multi (multi-ACE)'
    def cmd_ACE_RUN_MODE_SWITCH(self, gcmd):
        mode = gcmd.get('MODE', '').lower()
        if mode not in ('normal', 'single', 'multi'):
            raise gcmd.error('[multiACE] Invalid mode: %s. Use normal, single, or multi.' % mode)

        current = self._ace_mode

        if mode in ('single', 'multi') and current in ('single', 'multi'):
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=ace__mode VALUE=\"'%s'\"" % mode)
            self._ace_mode = mode
            if mode == 'multi':
                self._restore_head_source()
                self.printer.register_event_handler(
                    'extruder:activate_extruder', self._on_extruder_change)
            self.log_always('[multiACE] Switched to %s mode. No reboot needed.' % mode.upper())
            return

        save_vars = self.printer.lookup_object('save_variables')
        vars_path = save_vars.filename
        script_dir = os.path.dirname(os.path.abspath(vars_path))
        script = os.path.join(script_dir, 'ace_mode_switch.sh')
        if not os.path.exists(script):
            raise gcmd.error('[multiACE] Mode switch script not found: %s' % script)

        file_mode = 'normal' if mode == 'normal' else 'ace'

        self.log_always('[multiACE] Running mode switch to %s...' % mode.upper())

        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=ace__mode VALUE=\"'%s'\"" % mode)

        try:
            import subprocess
            result = subprocess.run(['bash', script, file_mode],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=30)
            if result.returncode != 0:
                raise gcmd.error(
                    '[multiACE] Mode switch script failed (rc=%d): %s' % (
                        result.returncode, result.stderr.decode('utf-8', 'replace')))
        except subprocess.TimeoutExpired:
            raise gcmd.error('[multiACE] Mode switch script timed out after 30s')
        except Exception as e:
            raise gcmd.error('[multiACE] Failed to run mode switch script: %s' % str(e))

        self.gcode.run_script_from_command(
            'RAISE_EXCEPTION ID=6666 INDEX=6 CODE=6 MESSAGE="[multiACE] Switched to %s mode. Please reboot!" ONESHOT=0 LEVEL=2' % mode.upper())

        raise gcmd.error(
            '[multiACE] Switched to %s mode. Please reboot the printer to activate!' % mode.upper())

    cmd_ACE_LIST_help = 'List all detected ACE devices (up to 4)'

    def cmd_ACE_LIST(self, gcmd):
        if not self._ace_devices:
            self.log_always('[multiACE] No ACE devices detected')
            return

        self.log_always('Found %d ACE device(s):' % len(self._ace_devices))
        for i, device in enumerate(self._ace_devices):
            active = ' << ACTIVE' if i == self._active_device_index else ''
            self.log_always('  ACE %d: %s%s' % (i, device, active))

    cmd_ACE_USB_STATS_help = '[multiACE] Show USB connection statistics'
    def cmd_ACE_USB_STATS(self, gcmd):
        s = self._usb_stats
        uptime = time.monotonic() - s['start_time']
        hours = uptime / 3600
        retry_rate = (s['retries'] / s['scans'] * 100) if s['scans'] > 0 else 0
        self.log_always('[multiACE] USB Statistics (%.1f hours):' % hours)
        self.log_always('  Scans: %d  Retries: %d (%.1f%%)' % (s['scans'], s['retries'], retry_rate))
        self.log_always('  Connects: %d  Failures: %d  Disconnects: %d' % (
            s['connects'], s['connect_failures'], s['disconnects']))

    cmd_ACE_DEBUG_help = '[multiACE] Toggle state audit + telemetry + wiggle logging. Usage: ACE_DEBUG [ENABLE=0|1]'
    def cmd_ACE_DEBUG(self, gcmd):
        enable = gcmd.get_int('ENABLE', -1)
        if enable == -1:
            state = 'enabled' if self._state_debug_enabled else 'disabled'
            self.log_always('[multiACE] State debug is %s (gates state audit, telemetry, wiggle)' % state)
            return
        self._state_debug_enabled = bool(enable)
        self._apply_log_levels()
        state = 'enabled' if self._state_debug_enabled else 'disabled'
        self.log_always('[multiACE] State debug %s (telemetry + wiggle follow)' % state)
        self._state_log.info('STATE_DEBUG %s', state)

    cmd_ACE_USB_DEBUG_help = '[multiACE] Toggle USB logging. Usage: ACE_USB_DEBUG [ENABLE=0|1]'
    def cmd_ACE_USB_DEBUG(self, gcmd):
        enable = gcmd.get_int('ENABLE', -1)
        if enable == -1:
            state = 'enabled' if self._usb_debug_enabled else 'disabled'
            self.log_always('[multiACE] USB debug is %s' % state)
            return
        self._usb_debug_enabled = bool(enable)
        self._apply_log_levels()
        state = 'enabled' if self._usb_debug_enabled else 'disabled'
        self.log_always('[multiACE] USB debug %s' % state)

    def _file_sha1_short(self, path):
        """Short sha1 of a file on disk — used by ACE_HEAD_STATUS to let
        the user verify each deployed file matches the repo version.
        Returns 'missing' if the file doesn't exist, 'err' on read error."""
        try:
            if not os.path.isfile(path):
                return 'missing'
            h = hashlib.sha1()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()[:7]
        except Exception:
            return 'err'

    def _compute_bundle_sha1(self):
        """Short sha1 computed over the concatenated byte contents of the
        non-ace.py deploy files, in a fixed order that must match the
        BUNDLE_FILES order in multiace/tools/git-hooks/post-commit.

        ACE_HEAD_STATUS compares this runtime value against the baked-in
        MULTIACE_BUNDLE_SHA1 (set by the hook at commit time). Mismatch
        means at least one of the bundled deploy files is stale on disk.
        """
        extras_dir = os.path.dirname(os.path.abspath(__file__))
        kinematics_dir = os.path.join(os.path.dirname(extras_dir), 'kinematics')
        config_dir = '/home/lava/printer_data/config/extended'
        bundle_paths = [
            os.path.join(extras_dir, 'filament_feed.py'),
            os.path.join(extras_dir, 'filament_switch_sensor.py'),
            os.path.join(kinematics_dir, 'extruder.py'),
            os.path.join(config_dir, 'ace.cfg'),
        ]
        h = hashlib.sha1()
        for p in bundle_paths:
            try:
                with open(p, 'rb') as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        h.update(chunk)
            except Exception:
                h.update(b'<missing:' + p.encode() + b'>')
        return h.hexdigest()[:7]

    def _read_wheel_counts(self, module, channel):

        try:
            feed = self.printer.lookup_object('filament_feed %s' % module, None)
            if feed is None:
                return None
            return {
                'a': feed.wheel[channel].get_counts(),
                'b': feed.wheel_2[channel].get_counts(),
            }
        except Exception as e:
            logging.info('[multiACE] wheel count read failed: %s', str(e))
            return None

    def _wheel_delta(self, before, after):

        if before is None or after is None:
            return None
        return {
            'a': after['a'] - before['a'],
            'b': after['b'] - before['b'],
        }

    cmd_ACE_SEQ_help = '[multiACE] Run scripted load/unload sequence. PLAN: 0:1=load HEAD:ACE, A0=all from ACE, U=unload all, U0=unload head. UNLOAD=0|1 (default 1) runs final ACE_UNLOAD_ALL_HEADS.'
    def cmd_ACE_SEQ(self, gcmd):

        plan_str = gcmd.get('PLAN', '')
        do_unload = gcmd.get_int('UNLOAD', 1)

        was_debug = self._state_debug_enabled
        self._state_debug_enabled = True
        self._state_log.info('SEQ_START plan="%s" unload=%d', plan_str, do_unload)
        try:
            hs_dump = json.dumps({str(h): self._head_source[h] for h in range(4)})
        except Exception:
            hs_dump = str(self._head_source)
        self._state_log.info('SEQ_START head_source=%s active_device=%d',
                             hs_dump, self._active_device_index)
        self._audit_state('SEQ_START', {'plan': plan_str, 'unload': do_unload})

        steps = []
        if plan_str:
            for item in plan_str.split(','):
                item = item.strip()
                if not item:
                    continue
                if item == 'U':
                    steps.append({'action': 'UNLOAD_ALL'})
                elif item.startswith('U') and item[1:].isdigit():
                    steps.append({'action': 'UNLOAD', 'head': int(item[1:])})
                elif item.startswith('A') and item[1:].isdigit():
                    ace = int(item[1:])
                    for h in range(4):
                        steps.append({'action': 'LOAD', 'head': h, 'ace': ace})
                elif ':' in item:
                    parts = item.split(':')
                    if len(parts) == 2:
                        steps.append({'action': 'LOAD', 'head': int(parts[0]), 'ace': int(parts[1])})
                    else:
                        raise gcmd.error('[multiACE] Invalid PLAN item: %s' % item)
                else:
                    raise gcmd.error('[multiACE] Invalid PLAN item: %s (use HEAD:ACE, A0, U, U0)' % item)
        else:
            self._refresh_ace_devices('seq')
            for i in range(min(len(self._ace_devices), 4)):
                steps.append({'action': 'LOAD', 'head': i, 'ace': i})

        self.log_always('[multiACE] === SEQ START: %d steps, unload=%s ===' % (
            len(steps), 'yes' if do_unload else 'no'))

        results = []
        step_nr = 0
        for step in steps:
            step_nr += 1
            action = step['action']

            if action == 'LOAD':
                head = step['head']
                ace = step['ace']
                self.log_always('[multiACE] --- Step %d/%d: LOAD HEAD=%d ACE=%d SLOT=%d ---' % (
                    step_nr, len(steps), head, ace, head))
                try:
                    self.gcode.run_script_from_command(
                        'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace, head))
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % head, None)
                    detected = sensor and sensor.get_status(0)['filament_detected']
                    src = self._head_source.get(head)
                    if detected and src is not None:
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'PASS', 'head': head, 'ace': ace})
                        self.log_always('[multiACE] Step %d: PASS (sensor=ok, mapping=ok)' % step_nr)
                    else:
                        reason = []
                        if not detected:
                            reason.append('sensor=no_filament')
                        if src is None:
                            reason.append('mapping=missing')
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'FAIL',
                                        'head': head, 'ace': ace, 'reason': ', '.join(reason)})
                        self.log_always('[multiACE] Step %d: FAIL (%s)' % (step_nr, ', '.join(reason)))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'LOAD', 'status': 'ERROR',
                                    'head': head, 'ace': ace, 'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')

            elif action == 'UNLOAD':
                head = step['head']
                self.log_always('[multiACE] --- Step %d/%d: UNLOAD HEAD=%d ---' % (
                    step_nr, len(steps), head))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_HEAD HEAD=%d' % head)
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % head, None)
                    still_loaded = sensor and sensor.get_status(0)['filament_detected']
                    if not still_loaded:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'PASS', 'head': head})
                        self.log_always('[multiACE] Step %d: PASS (sensor=clear)' % step_nr)
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'FAIL',
                                        'head': head, 'reason': 'filament still detected'})
                        self.log_always('[multiACE] Step %d: FAIL (filament still detected)' % step_nr)
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'ERROR',
                                    'head': head, 'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')

            elif action == 'UNLOAD_ALL':
                self.log_always('[multiACE] --- Step %d/%d: UNLOAD ALL ---' % (
                    step_nr, len(steps)))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                    all_clear = True
                    for h in range(4):
                        sensor = self.printer.lookup_object(
                            'filament_motion_sensor e%d_filament' % h, None)
                        if sensor and sensor.get_status(0)['filament_detected']:
                            all_clear = False
                    if all_clear:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                        self.log_always('[multiACE] Step %d: PASS (all sensors clear)' % step_nr)
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'FAIL',
                                        'reason': 'filament still detected'})
                        self.log_always('[multiACE] Step %d: FAIL (filament still detected)' % step_nr)
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'ERROR',
                                    'reason': str(e)})
                    self.log_always('[multiACE] Step %d: ERROR (%s)' % (step_nr, str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')

        if do_unload:
            self.log_always('[multiACE] --- Final: UNLOAD ALL ---')
            try:
                self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                all_clear = True
                for h in range(4):
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % h, None)
                    if sensor and sensor.get_status(0)['filament_detected']:
                        all_clear = False
                if all_clear:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                    self.log_always('[multiACE] Final unload: PASS')
                else:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'FAIL',
                                    'reason': 'filament still detected'})
                    self.log_always('[multiACE] Final unload: FAIL (filament still detected)')
            except Exception as e:
                results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'ERROR',
                                'reason': str(e)})
                self.log_always('[multiACE] Final unload: ERROR (%s)' % str(e))

        passed = sum(1 for r in results if r['status'] == 'PASS')
        failed = sum(1 for r in results if r['status'] == 'FAIL')
        errors = sum(1 for r in results if r['status'] == 'ERROR')
        total = len(results)
        self.log_always('[multiACE] === SEQ COMPLETE: %d/%d PASS, %d FAIL, %d ERROR ===' % (
            passed, total, failed, errors))

        result_json = json.dumps(results, default=str)
        self._state_log.info('SEQ_RESULT %s', result_json)

        gcmd.respond_info('SEQ_RESULT ' + result_json)
        self._state_debug_enabled = was_debug

    cmd_ACE_PRELOAD_help = '[multiACE] Preload heads from a UI-built plan. Same syntax as ACE_SEQ but UNLOAD defaults to 0 (no final unload).'
    def cmd_ACE_PRELOAD(self, gcmd):

        plan_str = gcmd.get('PLAN', '')
        do_unload = gcmd.get_int('UNLOAD', 0)
        if not plan_str:
            raise gcmd.error('[multiACE] ACE_PRELOAD requires a PLAN parameter')
        self.gcode.run_script_from_command(
            'ACE_SEQ PLAN=%s UNLOAD=%d' % (plan_str, do_unload))

    cmd_MACE_LOG_help = '[multiACE] Emit MSG to klippy.log (diagnostic tracepoint for macros).'
    def cmd_MACE_LOG(self, gcmd):
        msg = gcmd.get('MSG', '')
        logging.info('[mace_log] %s', msg)

    cmd_ACE_FA_TEST_help = (
        '[multiACE] Stress-test FA stop+start across slots without a print. '
        'Usage: ACE_FA_TEST [ACE=0] [SCENARIO=cycle|pingpong|burst|matrix] '
        '[SLOTS=0,1,2,3] [DELAY=0.5] [REPEATS=2] [INTER=0] '
        '[RETRIES=0] [RETRY_DELAY=0.2]'
    )
    def cmd_ACE_FA_TEST(self, gcmd):
        ace_idx = gcmd.get_int('ACE', 0, minval=0)
        scenario = gcmd.get('SCENARIO', 'cycle').lower()
        slots_str = gcmd.get('SLOTS', '0,1,2,3')
        delay = gcmd.get_float('DELAY', 0.5, minval=0.05)
        repeats = gcmd.get_int('REPEATS', 2, minval=1, maxval=200)
        inter = gcmd.get_float('INTER', 0.0, minval=0.0)
        retries = gcmd.get_int('RETRIES', 0, minval=0, maxval=100)
        retry_delay = gcmd.get_float('RETRY_DELAY', 0.2, minval=0.05)

        try:
            slots = [int(s.strip()) for s in slots_str.split(',') if s.strip()]
        except ValueError:
            raise gcmd.error('[ACE_FA_TEST] invalid SLOTS=%r' % slots_str)
        for s in slots:
            if not (0 <= s <= 3):
                raise gcmd.error('[ACE_FA_TEST] slot %d out of range 0..3' % s)

        if ace_idx >= len(self._ace_devices) or not self._connected_per_ace.get(ace_idx, False):
            raise gcmd.error('[ACE_FA_TEST] ACE %d not connected' % ace_idx)

        steps = []
        if scenario == 'cycle':
            seq = list(slots) * repeats
            prev = None
            for s in seq:
                if prev is not None:
                    steps.append(('stop', prev))
                steps.append(('start', s))
                prev = s
            if prev is not None:
                steps.append(('stop', prev))
        elif scenario == 'pingpong':
            if len(slots) < 2:
                raise gcmd.error('[ACE_FA_TEST] pingpong needs at least 2 slots')
            seq = []
            for r in range(repeats):
                for s in slots:
                    seq.append(s)
            prev = None
            for s in seq:
                if prev is not None:
                    steps.append(('stop', prev))
                steps.append(('start', s))
                prev = s
            if prev is not None:
                steps.append(('stop', prev))
        elif scenario == 'burst':
            for s in slots:
                for _ in range(repeats):
                    steps.append(('start', s))
                    steps.append(('stop', s))
        elif scenario == 'matrix':
            for r in range(repeats):
                for f in slots:
                    for t in slots:
                        if t == f:
                            continue
                        steps.append(('start', f))
                        steps.append(('stop', f))
                        steps.append(('start', t))
                        steps.append(('stop', t))
        else:
            raise gcmd.error('[ACE_FA_TEST] unknown SCENARIO=%s (use cycle|pingpong|burst|matrix)' % scenario)

        results = {}
        retry_counts = {}

        def is_forbidden(response):
            if not response:
                return False
            msg = response.get('msg', '') or ''
            return msg.lower() == 'forbidden'

        def make_callback(step_idx, action, slot, attempt):
            def cb(self=None, response=None, **kw):
                code = response.get('code', 0) if response else None
                msg = response.get('msg', '') if response else ''
                results.setdefault(step_idx, []).append((attempt, action, slot, code, msg))
                logging.info(
                    '[ACE_FA_TEST] RESP step=%d attempt=%d %s slot=%d code=%s msg=%s'
                    % (step_idx, attempt, action, slot, code, msg))
                if action == 'start' and is_forbidden(response) and attempt < retries:
                    next_attempt = attempt + 1
                    retry_counts[step_idx] = next_attempt
                    def retry_send(eventtime):
                        try:
                            self.send_request_to(ace_idx,
                                {"method": "start_feed_assist", "params": {"index": slot}},
                                make_callback(step_idx, action, slot, next_attempt))
                            logging.info(
                                '[ACE_FA_TEST] RETRY step=%d attempt=%d %s slot=%d (after FORBIDDEN)'
                                % (step_idx, next_attempt, action, slot))
                        except Exception as e:
                            logging.info(
                                '[ACE_FA_TEST] RETRY step=%d attempt=%d %s slot=%d failed: %s'
                                % (step_idx, next_attempt, action, slot, e))
                        return self.reactor.NEVER
                    self.reactor.register_timer(
                        retry_send, self.reactor.monotonic() + retry_delay)
            return cb

        gcmd.respond_info(
            '[ACE_FA_TEST] ACE=%d scenario=%s slots=%s delay=%.2fs repeats=%d steps=%d '
            'inter=%.2fs retries=%d retry_delay=%.2fs — running, watch klippy.log'
            % (ace_idx, scenario, slots, delay, repeats, len(steps), inter,
               retries, retry_delay))

        start_t = self.reactor.monotonic()
        for i, (action, slot) in enumerate(steps):
            t = start_t + (i + 1) * delay + i * inter

            def make_step(step_idx, action, slot):
                method = 'start_feed_assist' if action == 'start' else 'stop_feed_assist'
                def fire(eventtime):
                    try:
                        self.send_request_to(ace_idx,
                            {"method": method, "params": {"index": slot}},
                            make_callback(step_idx, action, slot, 0))
                        logging.info('[ACE_FA_TEST] SENT step=%d attempt=0 %s slot=%d' % (step_idx, action, slot))
                    except Exception as e:
                        logging.info('[ACE_FA_TEST] SEND step=%d %s slot=%d failed: %s' % (step_idx, action, slot, e))
                    return self.reactor.NEVER
                return fire

            self.reactor.register_timer(make_step(i, action, slot), t)

        retry_budget = retries * retry_delay if retries else 0.0
        summary_t = (start_t + (len(steps) + 1) * delay + len(steps) * inter
                     + retry_budget + 1.0)

        def summary(eventtime):
            sent = len(steps)
            recv_steps = len(results)
            no_ack_total = sent - recv_steps
            start_steps = [(i, a, s) for i, (a, s) in enumerate(steps) if a == 'start']
            attempts_hist = {}
            failed = []
            no_ack_starts = []
            for i, _, slot in start_steps:
                attempts = results.get(i, [])
                if not attempts:
                    no_ack_starts.append((i, slot))
                    continue
                final = attempts[-1]
                final_msg = (final[4] or '').lower()
                n_attempts = len(attempts)
                if final_msg == 'success':
                    attempts_hist[n_attempts] = attempts_hist.get(n_attempts, 0) + 1
                else:
                    failed.append((i, slot, n_attempts, final_msg or 'empty'))

            n_starts = len(start_steps)
            n_ok = sum(attempts_hist.values())
            max_att = max(attempts_hist.keys()) if attempts_hist else 0

            self.log_always(
                '[ACE_FA_TEST] DONE: %d starts | %d ok | %d failed | %d no_ack'
                % (n_starts, n_ok, len(failed), len(no_ack_starts)))
            if attempts_hist:
                hist_str = '  '.join(
                    '%dx=%d' % (k, attempts_hist[k])
                    for k in sorted(attempts_hist.keys()))
                self.log_always(
                    '[ACE_FA_TEST]   attempts: %s   max=%d'
                    % (hist_str, max_att))
            if failed:
                self.log_always(
                    '[ACE_FA_TEST]   FAILED (still %s after retries):' %
                    ('FORBIDDEN' if any(f[3] == 'forbidden' for f in failed) else 'non-success'))
                for step_i, slot, n_att, msg in failed[:10]:
                    self.log_always(
                        '[ACE_FA_TEST]     step=%d slot=%d (%d attempts) → %s'
                        % (step_i, slot, n_att, msg))
                if len(failed) > 10:
                    self.log_always('[ACE_FA_TEST]     ... %d more' % (len(failed) - 10))
            if no_ack_starts:
                self.log_always(
                    '[ACE_FA_TEST]   NO_ACK (firmware never responded):')
                for step_i, slot in no_ack_starts[:10]:
                    self.log_always(
                        '[ACE_FA_TEST]     step=%d slot=%d' % (step_i, slot))
            return self.reactor.NEVER

        self.reactor.register_timer(summary, summary_t)

    def _audit_state(self, action, params=None):

        if not self._state_debug_enabled:
            return
        try:

            state = {
                'action': action,
                'params': params or {},
                'active_device': self._active_device_index,
                'device_count': len(self._ace_devices),
                'connected': self._connected,
                'serial': self.serial_id,
                'mode': getattr(self, '_ace_mode', 'unknown'),
                'swap_in_progress': self._swap_in_progress,
                'auto_feed': self._auto_feed_enabled,
                'fa_context': self._fa_context,
                'feed_assist': self._feed_assist_index,
                'gate_status': self.gate_status[:],
                'head_source': {},
            }
            for h in range(4):
                src = self._head_source.get(h)
                state['head_source'][h] = {
                    'ace': src['ace_index'], 'slot': src['slot'],
                    'type': src.get('type', ''), 'color': src.get('color', '')
                } if src else None

            sensors = {}
            for h in range(4):
                sensor = self.printer.lookup_object(
                    'filament_motion_sensor e%d_filament' % h, None)
                sensors[h] = sensor.get_status(0)['filament_detected'] if sensor else None
            state['sensors'] = sensors

            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc:
                ptc_status = ptc.get_status()
                ptc_info = {}
                for h in range(4):
                    ptc_info[h] = {
                        'type': ptc_status.get('filament_type', [''] * 4)[h],
                        'color': ptc_status.get('filament_color', [''] * 4)[h],
                        'vendor': ptc_status.get('filament_vendor', [''] * 4)[h],
                    }
                state['print_task_config'] = ptc_info

            self._state_log.info('STATE %s', json.dumps(state, default=str))

            warnings = []
            if action == 'LOAD_HEAD':
                head = params.get('head')
                if head is not None:
                    src = self._head_source.get(head)
                    if src is None:
                        warnings.append('head_source[%d] is None after LOAD' % head)
                    if sensors.get(head) is False:
                        warnings.append('sensor[%d] not detecting filament after LOAD' % head)
            elif action == 'UNLOAD_HEAD':
                head = params.get('head')
                if head is not None:
                    src = self._head_source.get(head)
                    if src is not None:
                        warnings.append('head_source[%d] still set after UNLOAD' % head)
            elif action == 'SWITCH':
                target = params.get('target')
                if target is not None and self._active_device_index != target:
                    warnings.append('active_device=%d but target was %d' % (self._active_device_index, target))
                if not self._connected:
                    warnings.append('not connected after SWITCH')
            elif action == 'CLEAR_HEADS':
                head = params.get('head', -1)
                if head >= 0:
                    if self._head_source.get(head) is not None:
                        warnings.append('head_source[%d] not cleared' % head)
                else:
                    for h in range(4):
                        if self._head_source.get(h) is not None:
                            warnings.append('head_source[%d] not cleared' % h)
            elif action == 'UNLOAD_ALL':
                for h in range(4):
                    if sensors.get(h) is True:
                        warnings.append('sensor[%d] still detecting after UNLOAD_ALL' % h)

            if warnings:
                warn_msg = '[multiACE] STATE WARNINGS after %s: %s' % (action, '; '.join(warnings))
                self._state_log.warning(warn_msg)
                logging.warning(warn_msg)
        except Exception as e:
            self._state_log.error('STATE audit error: %s', str(e))

    def _telemetry(self, event, data):
        try:
            self._telemetry_log.info('%s %s', event, json.dumps(data, default=str))
        except Exception as e:
            logging.info('[multiACE] telemetry %s failed: %s' % (event, e))

    def get_status(self, eventtime=None):
        return {
            'status': self._info['status'],
            'temp': self._info['temp'],
            'dryer_status': self._info['dryer_status'],
            'gate_status': self.gate_status,
            'active_device': self._active_device_index,
            'device_count': len(self._ace_devices),
            'head_source': self._head_source,
        }

def load_config(config):
    return BunnyAce(config)
