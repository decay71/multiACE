import logging
import logging.handlers
import json
import struct
import queue
import traceback
import os
import time
import serial
from serial import SerialException

MULTIACE_VERSION = "0.81b"

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
        
        self._ace_canonical = None
        self._ace_present = set()
        self._ace_startup_failed = False  
        
        self.ace_device_count = config.getint('ace_device_count', 1, minval=1, maxval=8)
        self._active_device_index = 0       

        cfg_print_mode = config.get('print_mode', 'passive')
        if cfg_print_mode != 'passive':
            logging.info(
                '[multiACE] print_mode=%s ignored, passive is the only mode'
                % cfg_print_mode)
        self.print_mode = 'passive'

        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_length = config.getint('retract_length', 100)
        self.feed_length = config.getint('feed_length', 100)
        
        self.load_length = config.getint('load_length', 2000)         
        self.load_retry = config.getint('load_retry', 3)              
        self.load_retry_retract = config.getint('load_retry_retract', 50)  
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.extra_purge_length = config.getfloat('extra_purge_length', 0, minval=0, maxval=200)
        self.swap_default_temp = config.getint('swap_default_temp', 250, minval=180, maxval=300)
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

        self.ace_dryer_temp = {}
        self.ace_dryer_duration = {}
        for i in range(4):
            self.ace_dryer_temp[i] = config.getint('dryer_temp_%d' % i, self.dryer_temp)
            self.ace_dryer_duration[i] = config.getint('dryer_duration_%d' % i, self.dryer_duration)

        self._callback_map = {}
        self._feed_assist_index = -1
        self._request_id = 0

        self._head_source = {0: None, 1: None, 2: None, 3: None}
        
        self._swap_in_progress = False  
        self._auto_feed_enabled = False  
        self._hotplug_gone = {}          
        
        self._serial_failed = False
        self._serial_failed_at = 0.0
        self._serial_failed_pause_sent = False

        log_dir = config.get('log_dir', '/home/lava/printer_data/logs')
        self._usb_log = _setup_file_logger(
            'multiace_usb', os.path.join(log_dir, 'multiace_usb.log'))
        self._state_log = _setup_file_logger(
            'multiace_state', os.path.join(log_dir, 'multiace_state.log'))
        self._state_debug_enabled = config.getboolean('state_debug', True)
        self._usb_debug_enabled = config.getboolean('usb_debug', True)
        if not self._usb_debug_enabled:
            self._usb_log.setLevel(logging.CRITICAL)

        self._usb_stats = {
            'scans': 0,
            'retries': 0,
            'connects': 0,
            'connect_failures': 0,
            'disconnects': 0,
            'start_time': time.monotonic(),
        }

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
        
        self.printer.register_event_handler('print_stats:start_printing', self._on_print_start)
        self.printer.register_event_handler('print_stats:complete', self._on_print_end)
        self.printer.register_event_handler('print_stats:cancelled', self._on_print_end)
        self.printer.register_event_handler('print_stats:error', self._on_print_end)

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
            'ACE_HEAD_STATUS', self.cmd_ACE_HEAD_STATUS,
            desc=self.cmd_ACE_HEAD_STATUS_help)
        self.gcode.register_command(
            'ACE_CLEAR_HEADS', self.cmd_ACE_CLEAR_HEADS,
            desc=self.cmd_ACE_CLEAR_HEADS_help)
        self.gcode.register_command(
            'ACE_UNLOAD_ALL_HEADS', self.cmd_ACE_UNLOAD_ALL_HEADS,
            desc=self.cmd_ACE_UNLOAD_ALL_HEADS_help)
        self.gcode.register_command(
            'ACE_AUTO_FEED', self.cmd_ACE_AUTO_FEED,
            desc=self.cmd_ACE_AUTO_FEED_help)
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
            'ACE_TEST', self.cmd_ACE_TEST,
            desc=self.cmd_ACE_TEST_help)

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

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        try:
            ace_mtime = os.path.getmtime(os.path.abspath(__file__))
            from datetime import datetime
            ace_timestamp = datetime.fromtimestamp(ace_mtime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ace_timestamp = 'unknown'
        self.log_always('[multiACE] v%s (file: %s)' % (MULTIACE_VERSION, ace_timestamp))
        logging.info('[multiACE] version %s' % MULTIACE_VERSION)

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

        self._refresh_ace_devices("startup")

        if self.ace_device_count is not None:
            expected = self.ace_device_count
            if len(self._ace_devices) < expected:
                self.log_always('[multiACE] Waiting for %d ACE device(s), found %d...' % (
                    expected, len(self._ace_devices)))
                deadline = time.monotonic() + 20.0
                attempt = 0
                while time.monotonic() < deadline and len(self._ace_devices) < expected:
                    time.sleep(1.0)
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

        logging.info(f'[multiACE] Connecting to {self.serial_id}')
        self._connected = False
        self._queue = queue.Queue()
        self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

    def _hotplug_monitor(self, eventtime):
        
        if self._auto_feed_enabled or self._swap_in_progress:
            return eventtime + 2.0

        try:
            current = set(self._scan_ace_devices("hotplug"))
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
        logging.info(f'[multiACE] Closing connection to {self.serial_id}')
        self._serial_disconnect()
        self._queue = None

    def _on_print_start(self, *args):
        
        if self._ace_mode == 'multi':
            for head in range(4):
                source = self._head_source.get(head)
                if source is None:
                    continue
                ace_idx = source['ace_index']
                if ace_idx >= len(self._ace_devices):
                    self.log_error('[multiACE] WARNING: Head %d needs ACE %d but only %d ACE(s) available!' % (
                        head, ace_idx, len(self._ace_devices)))
        self._auto_feed_enabled = True
        logging.info('[multiACE] Print started — auto-feed enabled')

        if self.print_mode == 'passive':
            try:
                extruder = self.toolhead.get_extruder()
                head_index = getattr(extruder, 'extruder_index',
                            getattr(extruder, 'extruder_num', None))
            except Exception:
                head_index = None
            if head_index is not None:
                source = self._head_source.get(head_index)
                if source is not None:
                    target_ace = source['ace_index']
                    target_slot = source['slot']
                    if target_ace == self._active_device_index:
                        if self._feed_assist_index != target_slot:
                            try:
                                self._enable_feed_assist(target_slot)
                                self.log_always(
                                    '[multiACE] Passive print start: feed_assist enabled on start ACE %d slot %d (head %d)'
                                    % (target_ace, target_slot, head_index))
                                self._audit_state('PASSIVE_PRINT_START', {
                                    'head': head_index,
                                    'target_ace': target_ace,
                                    'target_slot': target_slot,
                                    'action': 'feed_assist_enabled',
                                })
                            except Exception as e:
                                logging.info('[multiACE] passive print-start feed_assist enable failed: %s' % e)
                                self._audit_state('PASSIVE_PRINT_START', {
                                    'head': head_index,
                                    'action': 'feed_assist_enable_failed',
                                    'error': str(e)[:200],
                                })
                    else:
                        
                        self._audit_state('PASSIVE_PRINT_START', {
                            'head': head_index,
                            'target_ace': target_ace,
                            'active_ace': self._active_device_index,
                            'action': 'cross_ace_coast',
                        })

    def _on_print_end(self, *args):
        
        self._auto_feed_enabled = False
        logging.info('[multiACE] Print ended — auto-feed disabled')
        if self.print_mode == 'passive' and self._feed_assist_index != -1:
            try:
                self._disable_feed_assist()
                self._audit_state('PASSIVE_PRINT_END', {
                    'action': 'feed_assist_disabled',
                })
            except Exception as e:
                logging.info('[multiACE] passive print-end feed_assist disable failed: %s' % e)

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
        
        self._request_id += 1
        if self._request_id >= 300000:
            self._request_id = 0
        return self._request_id

    def _serial_disconnect(self):
        self._usb_stats['disconnects'] += 1
        self._usb_log.info('DISCONNECT serial=%s connected=%s', self.serial_id, self._connected)
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
            self._connected = False
        if self.heartbeat_timer:
            try:
                self.reactor.unregister_timer(self.heartbeat_timer)
            except ValueError:
                pass
        if self.ace_dev_fd:
            self.reactor.set_fd_wake(self.ace_dev_fd, False, False)
            self.ace_dev_fd = None

    def _connect(self, eventtime):
        
        if self._serial_failed:
            return self.reactor.NEVER
        logging.info('[multiACE] Try connecting')
        self._usb_log.info('CONNECT attempt serial=%s', self.serial_id)
        connect_start = time.monotonic()

        def info_callback(self, response):
            if response.get('msg') != 'success':
                self.log_error(f"ACE Error: {response.get('msg')}")
            result = response.get('result', {})
            model = result.get('model', 'Unknown')
            firmware = result.get('firmware', 'Unknown')
            self._usb_log.info('CONNECT info model=%s firmware=%s', model, firmware)
            self.log_always(f"{{2}}ACE: Connected2 to {model} {{0}} \n Firmware Version: {{3}}{firmware}{{0}}", True)

        try:
            self._serial = serial.Serial(
                port=self.serial_id,
                baudrate=self.baud,
                exclusive=True,
                rtscts=True,
                timeout=0,
                write_timeout=0)

            if self._serial.is_open:
                self._connected = True
                self._request_id = 0
                connect_ms = (time.monotonic() - connect_start) * 1000
                self._usb_stats['connects'] += 1
                self._usb_log.info('CONNECT success serial=%s time=%.1fms', self.serial_id, connect_ms)
                logging.info(f'[multiACE] Connected to {self.serial_id}')
                self.ace_dev_fd = self.reactor.register_fd(
                    self._serial.fileno(),
                    self._reader_cb,
                )
                self.heartbeat_timer = self.reactor.register_timer(self._periodic_heartbeat_event, self.reactor.NOW)
                self.send_request(request={"method": "get_info"},
                                  callback=lambda self, response: info_callback(self, response))
                if self._feed_assist_index != -1:
                    self._enable_feed_assist(self._feed_assist_index)
                try:
                    self.reactor.unregister_timer(self.connect_timer)
                except Exception:
                    pass
                return self.reactor.NEVER
        except serial.serialutil.SerialException:
            self._serial = None
            self._usb_stats['connect_failures'] += 1
            self._usb_log.warning('CONNECT failed serial=%s SerialException', self.serial_id)
            logging.info('[multiACE] Conn error')
            logging.info('Error connecting to %s' % self.serial_id)
        except Exception as e:
            self._usb_stats['connect_failures'] += 1
            self._usb_log.warning('CONNECT failed serial=%s error=%s', self.serial_id, str(e))
            logging.info("ACE Error: %s" % str(e))

        return eventtime + 1

    def _calc_crc(self, buffer):
        
        _crc = 0xFFFF
        for byte in buffer:
            data = byte
            data ^= _crc & 0xFF
            data ^= (data & 0x0F) << 4
            _crc = ((data << 8) | (_crc >> 8)) ^ (data >> 4) ^ (data << 3)
        return _crc

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

        if self._serial_failed or self._serial is None:
            self._handle_serial_failure('no_serial', first=False)
            raise Exception('[multiACE] serial unavailable (failed state)')

        try:
            self._serial.write(data)
        except Exception as e:
            err_first = str(e)
            logging.info("ACE: Error writing to serial")
            self.log_always('[multiACE] Serial write to ACE failed: %s — attempting reconnect+retry' % err_first)
            try:
                self._state_log.warning('SERIAL_WRITE_FAILED first_attempt error=%s', err_first)
            except Exception:
                pass

            if self._connected:
                try:
                    self._serial_disconnect()
                except Exception:
                    pass
            
            try:
                self.reactor.pause(self.reactor.monotonic() + 0.35)
            except Exception:
                pass
            try:
                self._connect(self.reactor.monotonic())
            except Exception as ce:
                logging.info('[multiACE] Sync reconnect raised: %s' % str(ce))

            if self._connected and self._serial is not None:
                try:
                    self._serial.write(data)
                    if self._serial_failed:
                        
                        self.log_always('[multiACE] Serial write recovered after reconnect')
                        try:
                            self._state_log.info('SERIAL_WRITE_RECOVERED')
                            self._audit_state('SERIAL_WRITE_RECOVERED', {})
                        except Exception:
                            pass
                    self._serial_failed = False
                    self._serial_failed_pause_sent = False
                    return
                except Exception as e2:
                    err_second = str(e2)
            else:
                err_second = 'reconnect_failed'

            self._handle_serial_failure(err_second, first=True, first_error=err_first)
            raise Exception('[multiACE] serial write to ACE failed (reconnect+retry both failed)')

    def _handle_serial_failure(self, err, first, first_error=None):
        
        was_failed = self._serial_failed
        self._serial_failed = True
        if was_failed:
            
            return
        self._serial_failed_at = self.reactor.monotonic()
        self.log_error('[multiACE] Serial write to ACE failed permanently (%s) — pausing print' % err)
        try:
            self._state_log.error('SERIAL_WRITE_FAILED permanent error=%s', err)
            params = {'error': err}
            if first_error is not None:
                params['first_error'] = first_error
            self._audit_state('SERIAL_WRITE_FAILED', params)
        except Exception:
            pass
        
        try:
            if getattr(self, 'heartbeat_timer', None):
                self.reactor.unregister_timer(self.heartbeat_timer)
                self.heartbeat_timer = None
        except Exception:
            pass
        try:
            if getattr(self, 'connect_timer', None):
                self.reactor.unregister_timer(self.connect_timer)
                self.connect_timer = None
        except Exception:
            pass
        try:
            self._serial_disconnect()
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
                        '[multiACE] ACE serial write failed — print stopped')
                except Exception:
                    pass
                return self.reactor.NEVER
            try:
                self.reactor.register_timer(_do_pause, self.reactor.NOW)
            except Exception:
                pass

    def _pre_load(self, gate):
        self.log_always('[multiACE] Wait ACE preload')
        self.wait_ace_ready()

        feed_length = self.head_feed_length[gate]

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

        if not sensor or not sensor.get_status(0)['filament_detected']:
            self.log_warning('[multiACE] Filament not detected after preload (%dmm)' % feed_length)

        self.log_always("Select AutoLoad from the menu")

    def _periodic_heartbeat_event(self, eventtime):
        def callback(self, response):
            if response is not None:
                for i in range(4):
                    if self.gate_status[i] == GATE_EMPTY and response['result']['slots'][i]['status'] != 'empty':
                        if not self._swap_in_progress and self._auto_feed_enabled:
                            self.log_always('[multiACE] auto_feed')
                            self.reactor.register_async_callback(
                                (lambda et, c=self._pre_load, gate=i: c(gate)))

                    if response['result']['slots'][i]['rfid'] == 2 and self._info['slots'][i]['rfid'] != 2                            and not self._swap_in_progress:  
                        
                        target_heads = self._get_heads_for_ace_slot(
                            self._active_device_index, i)
                        if target_heads:
                            self.log_always('find_rfid (slot %d -> heads %s)' % (i, target_heads))
                            spool_inf = response['result']['slots'][i]
                            self.log_always(str(spool_inf))
                            for head in target_heads:
                                self.gcode.run_script_from_command(
                                    'SET_PRINT_FILAMENT_CONFIG '
                                    'CONFIG_EXTRUDER=%d '
                                    'FILAMENT_TYPE="%s" '
                                    'FILAMENT_COLOR_RGBA=%s '
                                    'VENDOR="%s" '
                                    'FILAMENT_SUBTYPE=""' % (
                                        head,
                                        spool_inf.get('type', 'PLA'),
                                        self.rgb2hex(*spool_inf.get('color', (0, 0, 0))),
                                        spool_inf.get('brand', 'Generic')))
                        else:
                            
                            source = self._head_source.get(i)
                            if not (source and source['ace_index'] != self._active_device_index):
                                self.log_always('[multiACE] find_rfid')
                                spool_inf = response['result']['slots'][i]
                                self.log_always(str(spool_inf))
                                self.gcode.run_script_from_command(
                                    'SET_PRINT_FILAMENT_CONFIG '
                                    'CONFIG_EXTRUDER=%d '
                                    'FILAMENT_TYPE="%s" '
                                    'FILAMENT_COLOR_RGBA=%s '
                                    'VENDOR="%s" '
                                    'FILAMENT_SUBTYPE=""' % (
                                        i,
                                        spool_inf.get('type', 'PLA'),
                                        self.rgb2hex(*spool_inf.get('color', (0, 0, 0))),
                                        spool_inf.get('brand', 'Generic')))
                    self.gate_status[i] = GATE_EMPTY if response['result']['slots'][i]['status'] == 'empty'                        else GATE_AVAILABLE
                self._info = response['result']

        if self._serial_failed:
            return eventtime + 1
        try:
            self.send_request({"method": "get_status"}, callback)
        except Exception as he:
            logging.info('[multiACE] Heartbeat send_request failed: %s' % str(he))

        return eventtime + 1

    def _reader_cb(self, eventtime):
        try:
            if self._serial.in_waiting:
                raw_bytes = self._serial.read(size=self._serial.in_waiting)
                self._process_data(raw_bytes)
        except Exception:
            logging.info(f'ACE error reading/processing: {traceback.format_exc()}')
            logging.info("Unable to communicate with the ACE PRO")
            logging.info("Try reconnecting")
            if self._connected:
                self._serial_disconnect()
                self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

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
        self._info['status'] = 'busy'
        msg_id = self._get_next_request_id()
        self._callback_map[msg_id] = callback
        request['id'] = msg_id
        self._send_request(request)

    def wait_ace_ready(self):
        while self._info['status'] != 'ready':
            curr_ts = self.reactor.monotonic()
            self.reactor.pause(curr_ts + 0.5)

    def is_ace_ready(self):
        return self._info['status'] == 'ready'

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
        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")
            else:
                self._feed_assist_index = index

        if self._feed_assist_index != -1:
            self.wait_ace_ready()
            self._retract(self._feed_assist_index, 5, 10)
        self.wait_ace_ready()
        self.send_request(request={"method": "start_feed_assist", "params": {"index": index}}, callback=callback)
        self.dwell(delay=0.7)

    cmd_ACE_ENABLE_FEED_ASSIST_help = 'Enables ACE feed assist'

    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX')

        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')

        self._enable_feed_assist(index)

    def _disable_feed_assist(self, index=-1):
        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(f"ACE Error: {response.get('msg')}")
                return

            self._feed_assist_index = -1
            self.gcode.respond_info('Disabled ACE feed assist')
        rt_index = self._feed_assist_index
        self.wait_ace_ready()
        self.send_request(request={"method": "stop_feed_assist", "params": {"index": self._feed_assist_index}},
                          callback=callback)
        self.wait_ace_ready()
        self._retract(rt_index, 5, 10)
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
        self._retract(index, self.retract_length, self.retract_speed)

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

    cmd_ACE_SWITCH_help = '[multiACE] Switch active ACE unit. Usage: ACE_SWITCH TARGET=0 [AUTOLOAD=1]'

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
            self._audit_state('SWITCH_FAILED', {'target': target, 'reason': 'no_devices'})
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
            self._audit_state('SWITCH_FAILED', {'target': target, 'reason': 'target_not_found', 'present': len(self._ace_present)})
            return

        switching_ace = target != self._active_device_index

        if not switching_ace and not autoload:
            self.log_always('[multiACE] ACE %d is already active' % target)
            self._audit_state('SWITCH_NOOP', {'target': target, 'reason': 'already_active'})
            return

        if not switching_ace and autoload:
            self.log_always('[multiACE] ACE %d already active, loading available filaments...' % target)
            
        else:
            
            if self._feed_assist_index != -1:
                self._disable_feed_assist()
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

            old_device_index = self._active_device_index
            old_serial_id = self.serial_id
            self._serial_disconnect()
            self._connected = False

            self._usb_log.info('SWITCH target=%d disconnected from ACE %d, scanning for target', target, old_device_index)
            for scan_retry in range(5):
                self._refresh_ace_devices('switch_post_disconnect_%d' % (scan_retry + 1))
                if self._is_ace_present(target):
                    break
                self._usb_stats['retries'] += 1
                self.reactor.pause(self.reactor.monotonic() + 1.0)
            if not self._is_ace_present(target):
                self.log_always('[multiACE] ACE %d not available (present %d). Reconnecting to previous ACE...' % (target, len(self._ace_present)))
                
                self._queue = queue.Queue()
                self._callback_map = {}
                self._request_id = 0
                self.read_buffer = bytearray()
                self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)
                for _ in range(30):
                    self.reactor.pause(self.reactor.monotonic() + 0.5)
                    if self._connected:
                        break
                if self._connected:
                    self.log_always('[multiACE] Reconnected to ACE %d' % self._active_device_index)
                else:
                    self.log_always('[multiACE] WARNING: Could not reconnect to previous ACE either!')
                self._audit_state('SWITCH_FAILED', {'target': target, 'reason': 'target_not_found_after_disconnect', 'reconnected': self._connected})
                return
            self._active_device_index = target
            self.serial_id = self._ace_devices[target]

            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (self.VARS_ACE_ACTIVE_DEVICE, self.serial_id))

            for slot in self._info['slots']:
                slot['rfid'] = 0

            self.log_always('[multiACE] Connecting to ACE %d: %s' % (target, self.serial_id))
            self._queue = queue.Queue()
            self._callback_map = {}
            self._request_id = 0
            self.read_buffer = bytearray()
            self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]
            self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

            for _ in range(30):
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                if self._connected:
                    break
            if not self._connected:
                logging.info('[multiACE] ACE_SWITCH: FAILED to connect to ACE %d at %s — falling back to ACE %d' % (
                    target, self.serial_id, old_device_index))
                self.log_always('[multiACE] Failed to connect to ACE %d. Reconnecting to ACE %d...' % (target, old_device_index))
                
                self._active_device_index = old_device_index
                self.serial_id = old_serial_id
                self._queue = queue.Queue()
                self._callback_map = {}
                self._request_id = 0
                self.read_buffer = bytearray()
                self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)
                for _ in range(30):
                    self.reactor.pause(self.reactor.monotonic() + 0.5)
                    if self._connected:
                        break
                if self._connected:
                    self.log_always('[multiACE] Reconnected to ACE %d' % old_device_index)
                else:
                    self.log_always('[multiACE] WARNING: Could not reconnect to previous ACE either!')
                self._audit_state('SWITCH_FAILED', {'target': target, 'reason': 'connect_failed', 'fallback_ace': old_device_index, 'reconnected': self._connected})
                return

            logging.info('[multiACE] ACE_SWITCH: connected to ACE %d at %s (active_idx=%d)' % (
                target, self.serial_id, self._active_device_index))

            self.reactor.pause(self.reactor.monotonic() + 1.5)

            logging.info('[multiACE] ACE_SWITCH: heartbeat done, info slots rfid: [%s]' % (
                ', '.join(str(s.get('rfid', '?')) for s in self._info.get('slots', []))))

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
                        head, saved[key]['ace_index'] + 1, saved[key]['slot']))

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

        if target_ace == self._active_device_index and self._connected:
            self._audit_state('SWITCH_AUTO_NOOP', {
                'head': head_index, 'target_ace': target_ace,
                'reason': 'already_active'})
            return True

        if not self._ensure_ace_available(target_ace):
            self.log_always('[multiACE] ACE %d not available for head %d (found %d)' % (
                target_ace, head_index, len(self._ace_devices)))
            self._audit_state('SWITCH_AUTO_FAILED', {
                'head': head_index, 'target_ace': target_ace,
                'reason': 'ace_not_available'})
            return False

        self.log_always('[multiACE] Switching to ACE %d for head %d...' % (
            target_ace + 1, head_index))

        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()

        self._serial_disconnect()
        self._connected = False

        self._active_device_index = target_ace
        self.serial_id = self._ace_devices[target_ace]

        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (
                self.VARS_ACE_ACTIVE_DEVICE, self.serial_id))

        for slot in self._info['slots']:
            slot['rfid'] = 0
        self._queue = queue.Queue()
        self._callback_map = {}
        self._request_id = 0
        self.read_buffer = bytearray()
        self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]

        self.connect_timer = self.reactor.register_timer(
            self._connect, self.reactor.NOW)

        for _ in range(30):
            self.reactor.pause(self.reactor.monotonic() + 0.5)
            if self._connected:
                break

        if not self._connected:
            self.log_error('[multiACE] Failed to connect to ACE %d' % target_ace)
            self._audit_state('SWITCH_AUTO_FAILED', {
                'head': head_index, 'target_ace': target_ace,
                'reason': 'connect_failed'})
            return False

        self.reactor.pause(self.reactor.monotonic() + 3.0)
        self._audit_state('SWITCH_AUTO', {
            'head': head_index, 'target_ace': target_ace})
        return True

    def _on_extruder_change(self):
        
        if not any(self._head_source[h] for h in range(4)):
            return

        try:
            extruder = self.toolhead.get_extruder()
            head_index = getattr(extruder, 'extruder_index',
                        getattr(extruder, 'extruder_num', None))
        except Exception:
            head_index = None

        if head_index is None:
            self._audit_state('SWITCH_AUTO_PASSIVE', {
                'head': None,
                'reason': 'no_head_index',
            })
            return

        source = self._head_source.get(head_index)
        if source is None:
            
            self._audit_state('SWITCH_AUTO_PASSIVE', {
                'head': head_index,
                'reason': 'no_head_source',
            })
            return

        target_ace = source['ace_index']
        target_slot = source['slot']

        if target_ace == self._active_device_index:
            
            if self._feed_assist_index != target_slot:
                prev_slot = self._feed_assist_index
                try:
                    self._enable_feed_assist(target_slot)
                    self.log_always(
                        '[multiACE] Passive same-ACE T%d: feed_assist slot %d -> %d'
                        % (head_index, prev_slot, target_slot))
                    self._audit_state('SWITCH_AUTO_PASSIVE', {
                        'head': head_index,
                        'target_ace': target_ace,
                        'target_slot': target_slot,
                        'prev_slot': prev_slot,
                        'action': 'same_ace_swap',
                    })
                except Exception as e:
                    logging.info('[multiACE] passive same-ACE feed_assist swap failed: %s' % e)
                    self._audit_state('SWITCH_AUTO_PASSIVE', {
                        'head': head_index,
                        'target_ace': target_ace,
                        'target_slot': target_slot,
                        'action': 'same_ace_swap_failed',
                        'error': str(e)[:200],
                    })
            else:
                
                self._audit_state('SWITCH_AUTO_PASSIVE', {
                    'head': head_index,
                    'target_ace': target_ace,
                    'target_slot': target_slot,
                    'action': 'same_ace_noop',
                })
        else:
            
            if self._feed_assist_index != -1:
                prev_slot = self._feed_assist_index
                try:
                    self._disable_feed_assist()
                    self.log_always(
                        '[multiACE] Passive cross-ACE T%d: target ACE %d != active %d, disabling feed_assist (was slot %d)'
                        % (head_index, target_ace,
                           self._active_device_index, prev_slot))
                except Exception as e:
                    logging.info('[multiACE] passive cross-ACE disable_feed_assist failed: %s' % e)
            self._audit_state('SWITCH_AUTO_PASSIVE', {
                'head': head_index,
                'target_ace': target_ace,
                'active_ace': self._active_device_index,
                'action': 'cross_ace_disable',
            })

    cmd_ACE_LOAD_HEAD_help = '[multiACE] Load a toolhead from ACE. Usage: ACE_LOAD_HEAD HEAD=0 [ACE=0] [SLOT=0]'
    def cmd_ACE_LOAD_HEAD(self, gcmd):
        
        head = gcmd.get_int('HEAD')
        ace_index = gcmd.get_int('ACE', self._active_device_index)
        slot = gcmd.get_int('SLOT', head)   

        if head < 0 or head > 3:
            raise gcmd.error('[multiACE] HEAD must be 0-3')
        if ace_index < 0 or not self._ensure_ace_available(ace_index):
            self.log_always('[multiACE] ACE %d not available' % ace_index)
            self._audit_state('LOAD_HEAD_FAILED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'ace_not_available'})
            return
        if slot < 0 or slot > 3:
            raise gcmd.error('[multiACE] SLOT must be 0-3')

        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % head, None)
        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_always('[multiACE] Head %d already has filament loaded. Unload first.' % head)
            self._audit_state('LOAD_HEAD_SKIPPED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'filament_present'})
            return

        self.log_always('[multiACE] Loading head %d from ACE %d / Slot %d...' % (
            head, ace_index, slot))

        if ace_index != self._active_device_index:
            if not self._switch_ace_for_head_target(ace_index):
                self._audit_state('LOAD_HEAD_FAILED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'ace_connect_failed'})
                raise gcmd.error(
                    '[multiACE] Failed to connect to ACE %d' % ace_index)

        if self.gate_status[slot] != GATE_AVAILABLE:
            self.log_always('[multiACE] ACE %d / Slot %d has no filament! Aborting load.' % (ace_index, slot))
            self._audit_state('LOAD_HEAD_FAILED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'slot_empty'})
            return

        active_ext = self.toolhead.get_extruder().get_name()
        target_ext = 'extruder' if head == 0 else 'extruder%d' % head
        if active_ext != target_ext:
            logging.info('[multiACE] Load: switching to %s (was %s)' % (target_ext, active_ext))
            self.gcode.run_script_from_command('T%d A0' % head)
            self.toolhead.wait_moves()

        module, channel = self.EXTRUDER_MAP[head]

        wheel_before = self._read_wheel_counts(module, channel)

        try:
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1"
                % (module, channel, head))
        except Exception as e:
            self._audit_state('LOAD_HEAD_FAILED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'feed_auto_error', 'error': str(e)})
            raise

        wheel_after = self._read_wheel_counts(module, channel)
        wheel_delta = self._wheel_delta(wheel_before, wheel_after)
        if wheel_delta is not None:
            
            min_expected = 5
            if wheel_delta['a'] < min_expected and wheel_delta['b'] < min_expected:
                warn = ('[multiACE] WARNING: Head %d load reported OK but wheel barely moved '
                        '(a_delta=%d, b_delta=%d) — filament probably NOT loaded') % (
                    head, wheel_delta['a'], wheel_delta['b'])
                self.log_always(warn)
                logging.warning(warn)
                self._state_log.warning(warn)
                self._audit_state('LOAD_HEAD_SUSPICIOUS', {
                    'head': head, 'ace': ace_index, 'slot': slot,
                    'wheel_delta_a': wheel_delta['a'], 'wheel_delta_b': wheel_delta['b'],
                    'reason': 'wheel_not_moving'
                })

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

    cmd_ACE_UNLOAD_HEAD_help = '[multiACE] Unload a toolhead back to its ACE. Usage: ACE_UNLOAD_HEAD HEAD=0'
    def cmd_ACE_UNLOAD_HEAD(self, gcmd):
        
        head = gcmd.get_int('HEAD')

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
                    self._audit_state('UNLOAD_HEAD_FAILED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'ace_connect_failed'})
                    raise gcmd.error(
                        '[multiACE] Failed to connect to ACE %d for unload!' % ace_index)
        else:
            self.log_always('[multiACE] Unloading head %d (no ACE mapping, using active ACE)...' % head)

        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()

        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=0" % head)

        module, channel = self.EXTRUDER_MAP[head]
        try:
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare"
                % (module, channel, head))
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing"
                % (module, channel, head))
        except Exception as e:
            self.gcode.run_script_from_command(
                "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1" % head)
            self._audit_state('UNLOAD_HEAD_FAILED', {'head': head, 'reason': 'feed_auto_error', 'error': str(e), 'active_device': self._active_device_index})
            raise

        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1" % head)

        machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
        if machine_state_manager is not None:
            self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")

        self._head_source[head] = None
        self._save_head_source()

        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_error('[multiACE] Warning: Filament still detected in head %d after unload!' % head)
        else:
            self.log_always('[multiACE] Head %d unloaded successfully' % head)
        self._audit_state('UNLOAD_HEAD', {'head': head})

    def _switch_ace_for_head_target(self, ace_index):
        
        if ace_index == self._active_device_index and self._connected:
            self._audit_state('SWITCH_TARGET_NOOP', {
                'target_ace': ace_index, 'reason': 'already_active'})
            return True
        if not self._ensure_ace_available(ace_index):
            self._audit_state('SWITCH_TARGET_FAILED', {
                'target_ace': ace_index, 'reason': 'ace_not_available'})
            return False

        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()

        self._serial_disconnect()
        self._connected = False
        self._active_device_index = ace_index
        self.serial_id = self._ace_devices[ace_index]

        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (
                self.VARS_ACE_ACTIVE_DEVICE, self.serial_id))

        for slot in self._info['slots']:
            slot['rfid'] = 0
        self._queue = queue.Queue()
        self._callback_map = {}
        self._request_id = 0
        self.read_buffer = bytearray()
        self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]

        self.connect_timer = self.reactor.register_timer(
            self._connect, self.reactor.NOW)

        for _ in range(30):
            self.reactor.pause(self.reactor.monotonic() + 0.5)
            if self._connected:
                break

        if self._connected:
            self.reactor.pause(self.reactor.monotonic() + 3.0)
            self._audit_state('SWITCH_TARGET', {'target_ace': ace_index})
            return True
        self._audit_state('SWITCH_TARGET_FAILED', {
            'target_ace': ace_index, 'reason': 'connect_failed'})
        return False

    cmd_ACE_HEAD_STATUS_help = '[multiACE] Show active ACE, detected devices, and head-to-ACE/slot mapping'
    def cmd_ACE_HEAD_STATUS(self, gcmd):
        
        device_count = len(self._ace_devices)
        if device_count == 0:
            self.log_always('[multiACE] No ACE devices detected')
            return
        self.log_always('[multiACE] Active ACE: %d of %d' % (
            self._active_device_index + 1, device_count))
        
        for i, device in enumerate(self._ace_devices):
            marker = ' << ACTIVE' if i == self._active_device_index else ''
            self.log_always('  ACE %d: %s%s' % (i + 1, device, marker))
        
        self.log_always('[multiACE] Head Source Mapping:')
        any_loaded = False
        for head in range(4):
            source = self._head_source[head]
            if source:
                any_loaded = True
                self.log_always(
                    '  T%d -> ACE %d / Slot %d  [%s %s %s]' % (
                        head,
                        source['ace_index'] + 1,
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
            self._clear_filament_display(head)
            unloaded_any = True

        if unloaded_any:
            self._save_head_source()

            if self._active_device_index != 0 and len(self._ace_devices) > 0:
                self.log_always('[multiACE] Switching back to ACE 0...')
                self._switch_ace_for_head_target(0)

            self.log_always('[multiACE] All heads unloaded')
        else:
            self.log_always('[multiACE] No filament detected in any head')
        self._audit_state('UNLOAD_ALL')

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

    cmd_ACE_AUTO_FEED_help = '[multiACE] Enable/disable auto-feed. Usage: ACE_AUTO_FEED ENABLE=0|1'
    def cmd_ACE_AUTO_FEED(self, gcmd):
        enable = gcmd.get_int('ENABLE', -1)
        if enable == -1:
            
            state = 'enabled' if self._auto_feed_enabled else 'disabled'
            self.log_always('[multiACE] Auto-feed is %s' % state)
            return
        self._auto_feed_enabled = bool(enable)
        if not self._auto_feed_enabled:
            
            for slot in range(4):
                try:
                    self._stop_feeding(slot)
                except Exception:
                    pass
            if self._feed_assist_index != -1:
                self._disable_feed_assist()
                self.wait_ace_ready()
        state = 'enabled' if self._auto_feed_enabled else 'disabled'
        self.log_always('[multiACE] Auto-feed %s' % state)

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
            self.log_always('  ACE %d: %s%s' % (i + 1, device, active))

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

    cmd_ACE_DEBUG_help = '[multiACE] Toggle debug logging. Usage: ACE_DEBUG [ENABLE=0|1] [USB=0|1] [STATE=0|1]'
    def cmd_ACE_DEBUG(self, gcmd):
        enable = gcmd.get_int('ENABLE', -1)
        usb = gcmd.get_int('USB', -1)
        state_flag = gcmd.get_int('STATE', -1)

        if enable == -1 and usb == -1 and state_flag == -1:
            self.log_always('[multiACE] Debug: usb=%s, state=%s' % (
                'on' if self._usb_debug_enabled else 'off',
                'on' if self._state_debug_enabled else 'off'))
            return

        if enable != -1:
            self._usb_debug_enabled = bool(enable)
            self._state_debug_enabled = bool(enable)
        
        if usb != -1:
            self._usb_debug_enabled = bool(usb)
        if state_flag != -1:
            self._state_debug_enabled = bool(state_flag)

        self._usb_log.setLevel(logging.DEBUG if self._usb_debug_enabled else logging.CRITICAL)

        self.log_always('[multiACE] Debug: usb=%s, state=%s' % (
            'on' if self._usb_debug_enabled else 'off',
            'on' if self._state_debug_enabled else 'off'))
        self._state_log.info('DEBUG usb=%s state=%s',
            'on' if self._usb_debug_enabled else 'off',
            'on' if self._state_debug_enabled else 'off')

    cmd_ACE_TEST_help = '[multiACE] Run load/unload test. PLAN: 0:1=load HEAD:ACE, A0=all from ACE, U=unload all, U0=unload head'
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
                elif ':' in item:
                    parts = item.split(':')
                    if len(parts) == 2:
                        steps.append({'action': 'LOAD', 'head': int(parts[0]), 'ace': int(parts[1])})
                    else:
                        raise gcmd.error('[multiACE] Invalid PLAN item: %s' % item)
                else:
                    raise gcmd.error('[multiACE] Invalid PLAN item: %s (use HEAD:ACE, A0, U, U0)' % item)
        else:
            
            self._refresh_ace_devices('test')
            for i in range(min(len(self._ace_devices), 4)):
                steps.append({'action': 'LOAD', 'head': i, 'ace': i})

        self.log_always('[multiACE] === TEST START: %d steps, unload=%s ===' % (
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

    def get_status(self, eventtime=None):
        return {
            'status': self._info['status'],
            'temp': self._info['temp'],
            'dryer_status': self._info['dryer_status'],
            'gate_status': self.gate_status,
            'active_device': self._active_device_index + 1,
            'device_count': len(self._ace_devices),
            'head_source': self._head_source,
        }

def load_config(config):
    return BunnyAce(config)
