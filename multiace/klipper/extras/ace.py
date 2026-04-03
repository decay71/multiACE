import logging
import json
import struct
import queue
import traceback
import os
import serial
from serial import SerialException

MULTIACE_VERSION = "0.80b"


class AceException(Exception):
    pass


GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1  # Available to load from either buffer or spool


class BunnyAce:
    VARS_ACE_REVISION = 'ace__revision'
    VARS_ACE_ACTIVE_DEVICE = 'ace__active_device'  # Persists active ACE selection across reboots
    VARS_ACE_HEAD_SOURCE = 'ace__head_source'      # Persists head-to-ACE/slot mapping

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

        # serial defaults to '' to enable auto-detection (original: '/dev/ttyACM0')
        self.serial_id = config.get('serial', '')
        self.baud = config.getint('baud', 115200)
        self._ace_devices = []              # List of auto-detected ACE device paths
        self._active_device_index = 0       # Index into _ace_devices for active unit

        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_length = config.getint('retract_length', 100)
        self.feed_length = config.getint('feed_length', 100)
        # New parameters for load phase in filament_feed.py
        self.load_length = config.getint('load_length', 2000)         # Max feed distance during load
        self.load_retry = config.getint('load_retry', 3)              # Number of retries if sensor not reached
        self.load_retry_retract = config.getint('load_retry_retract', 50)  # Mini-retract before retry (mm)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.extra_purge_length = config.getfloat('extra_purge_length', 0, minval=0, maxval=200)
        self.swap_default_temp = config.getint('swap_default_temp', 250, minval=180, maxval=300)
        self.dryer_temp = config.getint('dryer_temp', 55, minval=30, maxval=70)
        self.dryer_duration = config.getint('dryer_duration', 240, minval=10, maxval=480)

        # Per-toolhead overrides (optional, fallback to global defaults)
        # Allows different PTFE tube lengths per toolhead, e.g. feed_length_0: 1800
        self.head_feed_length = {}
        self.head_load_length = {}
        self.head_load_retry = {}
        self.head_load_retry_retract = {}
        for i in range(4):
            self.head_feed_length[i] = config.getint('feed_length_%d' % i, self.feed_length)
            self.head_load_length[i] = config.getint('load_length_%d' % i, self.load_length)
            self.head_load_retry[i] = config.getint('load_retry_%d' % i, self.load_retry)
            self.head_load_retry_retract[i] = config.getint('load_retry_retract_%d' % i, self.load_retry_retract)

        # Per-ACE dryer overrides (optional, fallback to global defaults)
        self.ace_dryer_temp = {}
        self.ace_dryer_duration = {}
        for i in range(4):
            self.ace_dryer_temp[i] = config.getint('dryer_temp_%d' % i, self.dryer_temp)
            self.ace_dryer_duration[i] = config.getint('dryer_duration_%d' % i, self.dryer_duration)

        self._callback_map = {}
        self._feed_assist_index = -1
        self._request_id = 0

        # head_source: tracks which ACE/slot feeds each toolhead
        # Key: extruder index (0-3), Value: dict with ace_index, slot, type, color, brand
        self._head_source = {0: None, 1: None, 2: None, 3: None}
        # State flags
        self._swap_in_progress = False  # Blocks runout handlers + heartbeat during mid-print swap
        self._auto_feed_enabled = False  # Auto-feed on filament insert; enabled during print, disabled outside
        self._hotplug_gone = {}          # USB hotplug: {device_path: monotonic_time} for debounce tracking

        # Default data to prevent exceptions
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
        # Auto-enable/disable auto-feed based on print state
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
        # New GCode commands for multi-ACE management
        self.gcode.register_command(
            'ACE_SWITCH', self.cmd_ACE_SWITCH,
            desc=self.cmd_ACE_SWITCH_help)
        self.gcode.register_command(
            'ACE_LIST', self.cmd_ACE_LIST,
            desc=self.cmd_ACE_LIST_help)
        # Mode switch via file swap + Klipper restart
        self.gcode.register_command(
            'ACE_RUN_MODE_SWITCH', self.cmd_ACE_RUN_MODE_SWITCH,
            desc=self.cmd_ACE_RUN_MODE_SWITCH_help)
        # Head source management for multi-ACE printing
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

    # New method: Scans USB bus for ACE Pro devices via sysfs vendor/product ID
    # Returns list of /dev/serial/by-path/ paths for all connected ACE units
    # Uses USB IDs (vendor=28e9, product=018a) to positively identify ACE Pro hardware
    def _scan_ace_devices(self):
        ace_devices = []
        by_path_dir = '/dev/serial/by-path/'

        if not os.path.exists(by_path_dir):
            return ace_devices

        for entry in sorted(os.listdir(by_path_dir)):
            full_path = os.path.join(by_path_dir, entry)
            real_dev = os.path.basename(os.path.realpath(full_path))

            # Check USB vendor/product ID via sysfs
            try:
                sysfs_base = '/sys/class/tty/%s/device/../' % real_dev
                with open(os.path.join(sysfs_base, 'idVendor'), 'r') as f:
                    vendor = f.read().strip()
                with open(os.path.join(sysfs_base, 'idProduct'), 'r') as f:
                    product = f.read().strip()

                # ACE Pro: vendor 28e9, product 018a
                if vendor == '28e9' and product == '018a':
                    ace_devices.append(full_path)
                    logging.info('ACE: Found device %s (%s) vendor=%s product=%s' % (full_path, real_dev, vendor, product))
            except (IOError, OSError):
                continue

        return ace_devices

    # Rewritten: Original only connected to configured serial port.
    # Now auto-detects all ACE devices, restores last active selection from
    # saved variables, and falls back to configured serial if no devices found.
    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        # Log version and file timestamp at startup
        try:
            ace_mtime = os.path.getmtime(os.path.abspath(__file__))
            from datetime import datetime
            ace_timestamp = datetime.fromtimestamp(ace_mtime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ace_timestamp = 'unknown'
        self.log_always('multiACE v%s (file: %s)' % (MULTIACE_VERSION, ace_timestamp))
        logging.info('multiACE version %s' % MULTIACE_VERSION)

        # Operating modes:
        #   'normal' — Stock Snapmaker feeders, no ACE (ace.py loaded but inactive)
        #   'single' — One ACE active, slot=extruder 1:1, no head_source tracking
        #   'multi'  — Multiple ACEs, head_source tracking, auto-switch on toolchange
        self._ace_mode = 'normal'
        if self.save_variables:
            self._ace_mode = self.save_variables.allVariables.get('ace__mode', 'normal')
        if self._ace_mode == 'normal':
            logging.info('ACE: Normal mode — skipping ACE detection')
            return

        # In multi mode, restore head_source and register toolchange handler
        if self._ace_mode == 'multi':
            self._restore_head_source()
            self.printer.register_event_handler(
                'extruder:activate_extruder', self._on_extruder_change)
        else:
            logging.info('ACE: SingleACE mode — no head_source tracking')

        # Auto-detect ACE devices
        self._ace_devices = self._scan_ace_devices()

        if self._ace_devices:
            logging.info('ACE: Found %d device(s): %s' % (len(self._ace_devices), str(self._ace_devices)))
            self.log_always('ACE: Found %d device(s)' % len(self._ace_devices))

            # Restore last active device from saved variables
            saved_device = self.save_variables.allVariables.get(self.VARS_ACE_ACTIVE_DEVICE, None)
            if saved_device and saved_device in self._ace_devices:
                self._active_device_index = self._ace_devices.index(saved_device)
                logging.info('ACE: Restored active device %d: %s' % (self._active_device_index, saved_device))
            else:
                self._active_device_index = 0

            self.serial_id = self._ace_devices[self._active_device_index]
        elif self.serial_id:
            logging.info('ACE: No devices auto-detected, using configured serial: %s' % self.serial_id)
        else:
            self.log_error('ACE: No devices found and no serial configured!')
            return

        logging.info(f'ACE: Connecting to {self.serial_id}')
        self._connected = False
        self._queue = queue.Queue()
        self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

        # USB hotplug monitor disabled for release — USB resets of inactive
        # ACEs cause false triggers. Use manual ACE_SWITCH macros instead.
        # self.reactor.register_timer(self._hotplug_monitor, self.reactor.monotonic() + 10.0)

    def _hotplug_monitor(self, eventtime):
        """Monitor USB for ACE hotplug with 5s debounce.
        ACEs that disappear for <5s are ignored (normal USB reset).
        ACEs gone >5s get a 'removed' message. When they return: auto-switch.
        Never modifies _ace_devices directly — only triggers ACE_SWITCH."""
        if self._auto_feed_enabled or self._swap_in_progress:
            return eventtime + 2.0

        try:
            current = set(self._scan_ace_devices())
            known = set(self._ace_devices)
            now = self.reactor.monotonic()

            # Track newly disappeared devices
            for dev in known - current:
                if dev not in self._hotplug_gone:
                    self._hotplug_gone[dev] = now

            # Check returned devices
            for dev in list(self._hotplug_gone.keys()):
                if dev in current:
                    gone_time = now - self._hotplug_gone[dev]
                    del self._hotplug_gone[dev]
                    if gone_time >= 5.0:
                        # Real return — find index and switch
                        fresh_devices = sorted(current)
                        if dev in fresh_devices:
                            new_index = fresh_devices.index(dev)
                            self.log_always('ACE %d returned after %.0fs — switching' % (new_index, gone_time))
                            self.reactor.register_async_callback(
                                lambda et, idx=new_index: self.gcode.run_script_from_command(
                                    'ACE_SWITCH TARGET=%d' % idx))
                            return eventtime + 10.0  # Cooldown

            # Notify about devices gone >5s (only once in 5-7s window)
            for dev, gone_since in list(self._hotplug_gone.items()):
                gone_time = now - gone_since
                if gone_time >= 5.0 and gone_time < 7.0:
                    self.log_always('ACE removed — re-enable to select')

        except Exception as e:
            logging.info('Hotplug monitor error: %s' % str(e))

        return eventtime + 2.0

    def _handle_disconnect(self):
        logging.info(f'ACE: Closing connection to {self.serial_id}')
        self._serial_disconnect()
        self._queue = None

    def _on_print_start(self, *args):
        """Auto-enable auto-feed when print starts.
        Also checks if all ACEs needed by loaded heads are still connected."""
        # Check if all needed ACEs are available
        if self._ace_mode == 'multi':
            for head in range(4):
                source = self._head_source.get(head)
                if source is None:
                    continue
                ace_idx = source['ace_index']
                if ace_idx >= len(self._ace_devices):
                    self.log_error('WARNING: Head %d needs ACE %d but only %d ACE(s) available!' % (
                        head, ace_idx, len(self._ace_devices)))
        self._auto_feed_enabled = True
        logging.info('Print started — auto-feed enabled')

    def _on_print_end(self, *args):
        """Auto-disable auto-feed when print ends/cancels/errors."""
        self._auto_feed_enabled = False
        logging.info('Print ended — auto-feed disabled')

    def _color_message(self, msg):
        try:
            html_msg = msg.format(
                '</span>',  # {0}
                '<span style="color:#FFFF00">',  # {1}
                '<span style="color:#90EE90">',  # {2}
                '<span style="color:#458EFF">',  # {3}
                '<b>',  # {5}
                '</b>'  # {6}
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
        """Get next sequential request ID for ACE serial protocol. Wraps at 300000."""
        self._request_id += 1
        if self._request_id >= 300000:
            self._request_id = 0
        return self._request_id

    def _serial_disconnect(self):
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
            self._connected = False
        if self.heartbeat_timer:
            self.reactor.unregister_timer(self.heartbeat_timer)
        if self.ace_dev_fd:
            self.reactor.set_fd_wake(self.ace_dev_fd, False, False)
            self.ace_dev_fd = None

    def _connect(self, eventtime):
        logging.info('ACE: Try connecting')

        def info_callback(self, response):
            if response.get('msg') != 'success':
                self.log_error(f"ACE Error: {response.get('msg')}")
            result = response.get('result', {})
            model = result.get('model', 'Unknown')
            firmware = result.get('firmware', 'Unknown')
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
                logging.info(f'ACE: Connected to {self.serial_id}')
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
            logging.info('ACE: Conn error')
            logging.info('Error connecting to %s' % self.serial_id)
        except Exception as e:
            logging.info("ACE Error: %s" % str(e))

        return eventtime + 1

    def _calc_crc(self, buffer):
        """CRC-16 calculation for ACE Pro serial protocol (CRC-CCITT variant)."""
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
        # Re-generate payload if CRC matches sync bytes 0xFFAA to prevent freezing
        # as suggested by protocol description.
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
        try:
            self._serial.write(data)
        except Exception:
            logging.info("ACE: Error writing to serial")
            logging.info("Try reconnecting")
            if self._connected:
                self._serial_disconnect()
                self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

    # Rewritten: Original blindly fed feed_length mm without sensor check.
    # Now uses reactive sensor polling (how_wait=0) during the feed — polls the
    # toolhead filament sensor every 0.105s and calls _stop_feeding() immediately
    # when filament is detected. Uses per-toolhead feed_length overrides.
    def _pre_load(self, gate):
        self.log_always('Wait ACE preload')
        self.wait_ace_ready()

        feed_length = self.head_feed_length[gate]

        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % gate, None)

        # Reactive feed with sensor polling
        self._feed(gate, feed_length, self.feed_speed, 0)

        while not self.is_ace_ready():
            self.reactor.pause(self.reactor.monotonic() + 0.105)
            if sensor and sensor.get_status(0)['filament_detected']:
                self._stop_feeding(gate)
                self.wait_ace_ready()
                self.log_always('Filament detected during preload')
                break

        if not sensor or not sensor.get_status(0)['filament_detected']:
            self.log_warning('Filament not detected after preload (%dmm)' % feed_length)

        self.log_always("Select AutoLoad from the menu")

    def _periodic_heartbeat_event(self, eventtime):
        def callback(self, response):
            if response is not None:
                for i in range(4):
                    if self.gate_status[i] == GATE_EMPTY and response['result']['slots'][i]['status'] != 'empty':
                        if not self._swap_in_progress and self._auto_feed_enabled:
                            self.log_always('auto_feed')
                            self.reactor.register_async_callback(
                                (lambda et, c=self._pre_load, gate=i: c(gate)))

                    if response['result']['slots'][i]['rfid'] == 2 and self._info['slots'][i]['rfid'] != 2 \
                            and not self._swap_in_progress:  # Suppress RFID push during swap
                        # Only push RFID to display for heads mapped to this ACE
                        # Find which head(s) use this ACE + slot, push RFID only for those
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
                            # No mapping for this slot — fallback: slot i -> extruder i
                            # Skip if head i is loaded from a different ACE
                            source = self._head_source.get(i)
                            if not (source and source['ace_index'] != self._active_device_index):
                                self.log_always('find_rfid')
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
                    self.gate_status[i] = GATE_EMPTY if response['result']['slots'][i]['status'] == 'empty' \
                        else GATE_AVAILABLE
                self._info = response['result']


        self.send_request({"method": "get_status"}, callback)

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
            # Find sync bytes
            start = self.read_buffer.find(b'\xFF\xAA')
            if start < 0:
                # No sync bytes found, but we might have partial sync at the end
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
            if payload_len > 2048:  # Sanity check: protocol says max 1024, but allow some headroom
                self.gcode.respond_info(f"ACE: Invalid payload length {payload_len}, dropping sync bytes")
                self.read_buffer = self.read_buffer[2:]
                continue

            total_len = 4 + payload_len + 2 + 1  # head + payload + crc + tail

            if len(self.read_buffer) < total_len:
                break

            packet = self.read_buffer[:total_len]
            payload = packet[4:4 + payload_len]
            crc_data = packet[4 + payload_len:4 + payload_len + 2]
            tail = packet[-1]

            # if tail != 0xFE:
            #     self.gcode.respond_info(f"Invalid tail byte from ACE: {tail:02X}, dropping sync bytes")
            #     self.read_buffer = self.read_buffer[2:]  # Drop current sync bytes and continue searching
            #     continue
            #
            # calc_crc = struct.pack('<H', self._calc_crc(payload))
            # if crc_data != calc_crc:
            #     self.gcode.respond_info('Invalid CRC from ACE PRO, dropping sync bytes')
            #     self.read_buffer = self.read_buffer[2:]  # Drop current sync bytes and continue searching
            #     continue

            # Packet is valid, consume it from buffer
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

    # New GCode command
    cmd_ACE_SWITCH_help = 'Switch active ACE unit. Usage: ACE_SWITCH TARGET=0 [AUTOLOAD=1]'

    # Mapping of extruder index to Snapmaker FEED_AUTO parameters.
    # Determined by observing touchscreen FEED_AUTO commands in klippy.log.
    EXTRUDER_MAP = {
        0: ('left', 1),
        1: ('left', 0),
        2: ('right', 0),
        3: ('right', 1),
    }

    # Forces RFID data from current ACE to the Snapmaker display.
    # With head_source: only pushes to heads mapped to this ACE.
    # Without head_source: legacy behavior (slot i -> extruder i).
    def _push_rfid_info(self):
        """Force push RFID data from current ACE to display.
        Only pushes for slots on the active ACE.
        Loaded heads (any ACE) are never touched - preserves manual settings."""
        logging.info('_push_rfid_info: active_device=%d, head_source=%s' % (
            self._active_device_index, str({k: (v['ace_index'] if v else None) for k, v in self._head_source.items()})))
        for i in range(4):
            source = self._head_source.get(i)

            # Head loaded (from any ACE) - don't touch, preserve display settings
            if source:
                logging.info('_push_rfid_info: slot %d - skipped (loaded from ACE %d)' % (i, source['ace_index']))
                continue

            # Unloaded slot on active ACE
            slot = self._info['slots'][i]

            if slot.get('rfid', 0) == 2:
                # RFID read - push data
                target_heads = self._get_heads_for_ace_slot(
                    self._active_device_index, i)
                if not target_heads:
                    target_heads = [i]
                logging.info('_push_rfid_info: slot %d - pushing RFID to heads %s (type=%s)' % (
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
                # No RFID, not loaded - clear display
                logging.info('_push_rfid_info: slot %d - clearing (no RFID, not loaded)' % i)
                self.gcode.run_script_from_command(
                    'SET_PRINT_FILAMENT_CONFIG '
                    'CONFIG_EXTRUDER=%d '
                    'FILAMENT_TYPE="" '
                    'FILAMENT_COLOR_RGBA=000000FF '
                    'VENDOR="" '
                    'FILAMENT_SUBTYPE=""' % i)

    # New: Hot-swaps between ACE units without reboot.
    # Unload: only toolheads where sensor detects filament in head.
    # Load: only gates where ACE has filament (GATE_AVAILABLE).
    # Resets machine state after unload to prevent "Unloading Anomaly".
    # Pushes RFID info after switch. Saves selection to ace_vars.cfg.
    def cmd_ACE_SWITCH(self, gcmd):
        target = gcmd.get_int('TARGET')
        autoload = gcmd.get_int('AUTOLOAD', 0)

        # Prevent concurrent switches
        if self._swap_in_progress:
            self.log_always('ACE switch already in progress, please wait')
            return
        self._swap_in_progress = True

        try:
            self._do_ace_switch(gcmd, target, autoload)
        finally:
            self._swap_in_progress = False

    def _do_ace_switch(self, gcmd, target, autoload):
        # Rescan devices to get current state
        self._ace_devices = self._scan_ace_devices()

        if not self._ace_devices:
            self.log_always('No ACE devices detected')
            return

        # Retry scan if target not found (inactive ACE may be in USB reset)
        if target < 0 or target >= len(self._ace_devices):
            for retry in range(5):
                self.reactor.pause(self.reactor.monotonic() + 1.0)
                self._ace_devices = self._scan_ace_devices()
                if target < len(self._ace_devices):
                    break
        if target < 0 or target >= len(self._ace_devices):
            self.log_always('ACE %d not available (found %d). Try again.' % (target, len(self._ace_devices)))
            return

        switching_ace = target != self._active_device_index

        if not switching_ace and not autoload:
            self.log_always('ACE %d is already active' % target)
            return

        if not switching_ace and autoload:
            self.log_always('ACE %d already active, loading available filaments...' % target)
            # Skip unload/switch, jump to load phase
        else:
            # Disable feed assist before switching
            if self._feed_assist_index != -1:
                self._disable_feed_assist()
                self.wait_ace_ready()

            # Unload phase - only unload filament that is actually in toolhead
            if autoload:
                self.log_always('Unloading from ACE %d...' % self._active_device_index)

                for gate in range(4):
                    sensor = self.printer.lookup_object(
                        'filament_motion_sensor e%d_filament' % gate, None)
                    filament_in_head = sensor and sensor.get_status(0)['filament_detected']
                    module, channel = self.EXTRUDER_MAP[gate]

                    if filament_in_head:
                        self.log_always('Extruder %d: filament in head, full unload' % gate)
                        self.gcode.run_script_from_command(
                            "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare" % (module, channel, gate))
                        self.gcode.run_script_from_command(
                            "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing" % (module, channel, gate))
                    else:
                        self.log_always('Extruder %d: not in head, skipping unload' % gate)

                # Reset machine state to prevent "Unloading Anomaly"
                machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
                if machine_state_manager is not None:
                    self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")

                self.log_always('Unload complete')

            # Disconnect current
            self._serial_disconnect()
            self._connected = False

            # Switch to new device — retry scan up to 5 times (inactive ACE may be in USB reset)
            for scan_retry in range(5):
                self._ace_devices = self._scan_ace_devices()
                if target < len(self._ace_devices):
                    break
                self.reactor.pause(self.reactor.monotonic() + 1.0)
            if target >= len(self._ace_devices):
                self.log_always('ACE %d not available (found %d). Try again.' % (target, len(self._ace_devices)))
                return
            self._active_device_index = target
            self.serial_id = self._ace_devices[target]

            # Save selection
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (self.VARS_ACE_ACTIVE_DEVICE, self.serial_id))

            # Reset RFID info so heartbeat picks up new ACE's data
            for slot in self._info['slots']:
                slot['rfid'] = 0

            # Reconnect
            self.log_always('Connecting to ACE %d: %s' % (target, self.serial_id))
            self._queue = queue.Queue()
            self._callback_map = {}
            self._request_id = 0
            self.read_buffer = bytearray()
            self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]
            self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

            # Wait for connection (30 x 0.5s = 15s timeout)
            for _ in range(30):
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                if self._connected:
                    break
            if not self._connected:
                logging.info('ACE_SWITCH: FAILED to connect to ACE %d at %s' % (target, self.serial_id))
                self.log_always('Failed to connect to ACE %d' % target)
                return

            logging.info('ACE_SWITCH: connected to ACE %d at %s (active_idx=%d)' % (
                target, self.serial_id, self._active_device_index))

            # Wait for heartbeat to update gate status and RFID
            self.reactor.pause(self.reactor.monotonic() + 1.5)

            logging.info('ACE_SWITCH: heartbeat done, info slots rfid: [%s]' % (
                ', '.join(str(s.get('rfid', '?')) for s in self._info.get('slots', []))))

            # Push RFID info from new ACE to display
            self._push_rfid_info()

        # Load phase - check sensors before loading
        if autoload:
            self.log_always('Loading from ACE %d...' % target)
            loaded_any = False

            for gate in range(4):
                sensor = self.printer.lookup_object(
                    'filament_motion_sensor e%d_filament' % gate, None)
                filament_in_head = sensor and sensor.get_status(0)['filament_detected']

                if filament_in_head:
                    self.log_always('Extruder %d: filament already in head, skipping' % gate)
                elif self.gate_status[gate] == GATE_AVAILABLE:
                    module, channel = self.EXTRUDER_MAP[gate]
                    self.log_always('Extruder %d: loading...' % gate)
                    self.gcode.run_script_from_command(
                        "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1" % (module, channel, gate))
                    loaded_any = True
                else:
                    self.log_always('Extruder %d: no filament in ACE, skipping' % gate)

            if loaded_any:
                self.log_always('Load complete from ACE %d' % target)
            else:
                self.log_always('Nothing to load')

    # ============================================================
    # Head Source Management
    # Tracks which ACE/slot feeds each toolhead for multi-ACE printing
    # ============================================================

    def _get_heads_for_ace_slot(self, ace_index, slot):
        """Return list of head indices mapped to this ACE + slot"""
        heads = []
        for head, source in self._head_source.items():
            if source and source['ace_index'] == ace_index and source['slot'] == slot:
                heads.append(head)
        return heads

    def _restore_head_source(self):
        """Restore head_source mapping from saved variables"""
        saved = self.save_variables.allVariables.get(self.VARS_ACE_HEAD_SOURCE, None)
        if saved and isinstance(saved, dict):
            for head in range(4):
                key = str(head)
                if key in saved and saved[key]:
                    self._head_source[head] = saved[key]
                    logging.info('ACE: Restored head %d -> ACE %d / Slot %d' % (
                        head, saved[key]['ace_index'] + 1, saved[key]['slot']))

    def _save_head_source(self):
        """Persist head_source mapping to saved variables"""
        # Convert to string-keyed dict for serialization
        save_data = {}
        for head in range(4):
            save_data[str(head)] = self._head_source[head]
        # Use JSON but replace null->None for Python's ast.literal_eval
        value_str = json.dumps(save_data).replace('null', 'None')
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE='%s'"
            % (self.VARS_ACE_HEAD_SOURCE, value_str))

    def _ensure_ace_available(self, ace_index):
        """Scan for ACE device, retry up to 5x if not found (USB reset cycle)."""
        for attempt in range(5):
            self._ace_devices = self._scan_ace_devices()
            if ace_index < len(self._ace_devices):
                return True
            self.reactor.pause(self.reactor.monotonic() + 1.0)
        return False

    def _switch_ace_for_head(self, head_index):
        """Switch ACE connection to match head_source for given head.
        Only switches if needed. Returns True if ACE is ready."""
        source = self._head_source.get(head_index)
        if not source:
            return False

        target_ace = source['ace_index']

        # Already on the right ACE
        if target_ace == self._active_device_index and self._connected:
            return True

        if not self._ensure_ace_available(target_ace):
            self.log_always('ACE %d not available for head %d (found %d)' % (
                target_ace, head_index, len(self._ace_devices)))
            return False

        self.log_always('Switching to ACE %d for head %d...' % (
            target_ace + 1, head_index))

        # Disable current feed assist
        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()

        # Disconnect current
        self._serial_disconnect()
        self._connected = False

        # Switch to target
        self._active_device_index = target_ace
        self.serial_id = self._ace_devices[target_ace]

        # Save selection
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (
                self.VARS_ACE_ACTIVE_DEVICE, self.serial_id))

        # Reset state for new connection
        for slot in self._info['slots']:
            slot['rfid'] = 0
        self._queue = queue.Queue()
        self._callback_map = {}
        self._request_id = 0
        self.read_buffer = bytearray()
        self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]

        # Connect
        self.connect_timer = self.reactor.register_timer(
            self._connect, self.reactor.NOW)

        # Wait for connection
        for _ in range(30):
            self.reactor.pause(self.reactor.monotonic() + 0.5)
            if self._connected:
                break

        if not self._connected:
            self.log_error('Failed to connect to ACE %d' % target_ace)
            return False

        # Wait for heartbeat
        self.reactor.pause(self.reactor.monotonic() + 3.0)
        return True

    def _on_extruder_change(self):
        """Event handler for toolhead extruder changes.
        Automatically switches ACE connection and feed_assist
        to match the head_source mapping of the new active extruder."""
        # Skip if no head_source configured
        if not any(self._head_source[h] for h in range(4)):
            return

        # Get active extruder index
        try:
            extruder = self.toolhead.get_extruder()
            head_index = getattr(extruder, 'extruder_index',
                        getattr(extruder, 'extruder_num', None))
            if head_index is None:
                return
        except Exception:
            return

        source = self._head_source.get(head_index)
        if not source:
            return

        # Switch ACE if needed
        if source['ace_index'] != self._active_device_index:
            self.log_always('Toolchange T%d -> switching to ACE %d' % (
                head_index, source['ace_index']))
            if not self._switch_ace_for_head(head_index):
                self.log_error('ACE switch failed for T%d!' % head_index)
                return

        # Enable feed assist on the correct slot
        slot = source['slot']
        if self._feed_assist_index != slot:
            self.log_always('Enabling feed assist on slot %d for T%d' % (
                slot, head_index))
            self._enable_feed_assist(slot)

    cmd_ACE_LOAD_HEAD_help = 'Load a toolhead from ACE. Usage: ACE_LOAD_HEAD HEAD=0 [ACE=0] [SLOT=0]'
    def cmd_ACE_LOAD_HEAD(self, gcmd):
        """Load a specific head from a specific ACE unit and slot.
        ACE defaults to active ACE. Switches ACE if needed, performs FEED_AUTO load, records mapping."""
        head = gcmd.get_int('HEAD')
        ace_index = gcmd.get_int('ACE', self._active_device_index)
        slot = gcmd.get_int('SLOT', head)   # Defaults to HEAD (same wiring)

        if head < 0 or head > 3:
            raise gcmd.error('HEAD must be 0-3')
        if ace_index < 0 or not self._ensure_ace_available(ace_index):
            self.log_always('ACE %d not available' % ace_index)
            return
        if slot < 0 or slot > 3:
            raise gcmd.error('SLOT must be 0-3')

        # Check if head already has filament
        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % head, None)
        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_always('Head %d already has filament loaded. Unload first.' % head)
            return

        # Switch to target ACE
        self.log_always('Loading head %d from ACE %d / Slot %d...' % (
            head, ace_index, slot))

        if ace_index != self._active_device_index:
            if not self._switch_ace_for_head_target(ace_index):
                raise gcmd.error(
                    'Failed to connect to ACE %d' % ace_index)

        # Check if slot has filament
        if self.gate_status[slot] != GATE_AVAILABLE:
            self.log_always('ACE %d / Slot %d has no filament! Aborting load.' % (ace_index, slot))
            return

        # Ensure correct extruder is active before FEED_AUTO
        active_ext = self.toolhead.get_extruder().get_name()
        target_ext = 'extruder' if head == 0 else 'extruder%d' % head
        if active_ext != target_ext:
            logging.info('Load: switching to %s (was %s)' % (target_ext, active_ext))
            self.gcode.run_script_from_command('T%d A0' % head)
            self.toolhead.wait_moves()

        # Perform load via FEED_AUTO
        module, channel = self.EXTRUDER_MAP[head]
        self.gcode.run_script_from_command(
            "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1"
            % (module, channel, head))

        # FEED_AUTO handles errors internally — if we get here, load succeeded
        # Store RFID info from current ACE
        slot_info = self._info['slots'][slot]
        self._head_source[head] = {
            'ace_index': ace_index,
            'slot': slot,
            'type': slot_info.get('type', 'PLA'),
            'color': self.rgb2hex(*slot_info.get('color', (0, 0, 0))),
            'brand': slot_info.get('brand', 'Generic'),
        }
        self._save_head_source()

        # Push RFID info for this head
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

        self.log_always('Head %d loaded from ACE %d / Slot %d' % (
            head, ace_index, slot))

    cmd_ACE_UNLOAD_HEAD_help = 'Unload a toolhead back to its ACE. Usage: ACE_UNLOAD_HEAD HEAD=0'
    def cmd_ACE_UNLOAD_HEAD(self, gcmd):
        """Unload a specific head back to its ACE slot.
        Switches to the correct ACE if needed, runs full unload sequence,
        clears head_source mapping."""
        head = gcmd.get_int('HEAD')

        if head < 0 or head > 3:
            raise gcmd.error('HEAD must be 0-3')

        # Check if head has filament (warning only - motion sensor unreliable when stationary)
        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % head, None)
        if sensor and not sensor.get_status(0)['filament_detected']:
            self.log_always('Note: Sensor on head %d not detecting filament (may be stationary)' % head)

        source = self._head_source.get(head)
        if source:
            # Switch to the correct ACE for retract
            ace_index = source['ace_index']
            slot = source['slot']
            self.log_always('Unloading head %d (ACE %d / Slot %d)...' % (
                head, ace_index, slot))

            if ace_index != self._active_device_index:
                if not self._switch_ace_for_head_target(ace_index):
                    raise gcmd.error(
                        'Failed to connect to ACE %d for unload!' % ace_index)
        else:
            self.log_always('Unloading head %d (no ACE mapping, using active ACE)...' % head)

        # Disable feed assist before unloading
        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()

        # Disable runout sensor — retract triggers false runout events
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=0" % head)

        # Run full unload via FEED_AUTO (prepare heats nozzle, doing retracts)
        module, channel = self.EXTRUDER_MAP[head]
        self.gcode.run_script_from_command(
            "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare"
            % (module, channel, head))
        self.gcode.run_script_from_command(
            "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing"
            % (module, channel, head))

        # Re-enable runout sensor
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1" % head)

        # Reset machine state to prevent "Unloading Anomaly"
        machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
        if machine_state_manager is not None:
            self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")

        # Clear head_source mapping
        self._head_source[head] = None
        self._save_head_source()

        # Verify unload
        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_error('Warning: Filament still detected in head %d after unload!' % head)
        else:
            self.log_always('Head %d unloaded successfully' % head)

    def _switch_ace_for_head_target(self, ace_index):
        """Connect to a specific ACE by index (0-based). Used by ACE_LOAD_HEAD."""
        if ace_index == self._active_device_index and self._connected:
            return True
        if not self._ensure_ace_available(ace_index):
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
            return True
        return False

    cmd_ACE_HEAD_STATUS_help = 'Show head-to-ACE/slot mapping'
    def cmd_ACE_HEAD_STATUS(self, gcmd):
        self.log_always('Head Source Mapping:')
        for head in range(4):
            source = self._head_source[head]
            if source:
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

    cmd_ACE_CLEAR_HEADS_help = 'Clear head-to-ACE/slot mapping. Usage: ACE_CLEAR_HEADS [HEAD=0]'
    def cmd_ACE_CLEAR_HEADS(self, gcmd):
        head = gcmd.get_int('HEAD', -1)
        if head >= 0:
            if head > 3:
                raise gcmd.error('HEAD must be 0-3')
            self._head_source[head] = None
            self.log_always('Cleared head %d mapping' % head)
        else:
            self._head_source = {0: None, 1: None, 2: None, 3: None}
            self.log_always('Cleared all head mappings')
        self._save_head_source()

    cmd_ACE_UNLOAD_ALL_HEADS_help = 'Unload all toolheads that have filament loaded'
    def cmd_ACE_UNLOAD_ALL_HEADS(self, gcmd):
        """Unload all toolheads with filament. In multi mode, switches ACE
        as needed to retract to the correct ACE per head."""
        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()

        unloaded_any = False
        for head in range(4):
            sensor = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % head, None)
            if not sensor or not sensor.get_status(0)['filament_detected']:
                continue

            # In multi mode, switch to correct ACE for this head
            source = self._head_source.get(head)
            if source and source['ace_index'] != self._active_device_index:
                self.log_always('Switching to ACE %d for head %d retract...' % (
                    source['ace_index'], head))
                if not self._switch_ace_for_head_target(source['ace_index']):
                    self.log_error('Failed to connect to ACE %d, skipping head %d' % (
                        source['ace_index'], head))
                    continue

            self.log_always('Unloading head %d...' % head)
            module, channel = self.EXTRUDER_MAP[head]
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare" % (module, channel, head))
            self.gcode.run_script_from_command(
                "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing" % (module, channel, head))

            # Reset state after each head to prevent runout trigger
            machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
            if machine_state_manager is not None:
                self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")

            self._head_source[head] = None
            unloaded_any = True

        if unloaded_any:
            self._save_head_source()

            # Switch back to ACE 0
            if self._active_device_index != 0 and len(self._ace_devices) > 0:
                self.log_always('Switching back to ACE 0...')
                self._switch_ace_for_head_target(0)

            self.log_always('All heads unloaded')
        else:
            self.log_always('No filament detected in any head')

    cmd_ACE_DRY_help = 'Start drying on ACE. Usage: ACE_DRY ACE=0 [TEMP=] [DURATION=]'
    def cmd_ACE_DRY(self, gcmd):
        """Switch to specified ACE and start drying with per-ACE config values."""
        ace_idx = gcmd.get_int('ACE')
        if ace_idx < 0 or ace_idx >= len(self._ace_devices):
            self.log_always('ACE %d not available' % ace_idx)
            return
        temp = gcmd.get_int('TEMP', self.ace_dryer_temp.get(ace_idx, self.dryer_temp))
        duration = gcmd.get_int('DURATION', self.ace_dryer_duration.get(ace_idx, self.dryer_duration))
        self.gcode.run_script_from_command('ACE_SWITCH TARGET=%d' % ace_idx)
        self.gcode.run_script_from_command('ACE_START_DRYING TEMP=%d DURATION=%d' % (temp, duration))
        self.log_always('Drying ACE %d at %d°C for %d min' % (ace_idx, temp, duration))

    cmd_ACE_AUTO_FEED_help = 'Enable/disable auto-feed. Usage: ACE_AUTO_FEED ENABLE=0|1'
    def cmd_ACE_AUTO_FEED(self, gcmd):
        enable = gcmd.get_int('ENABLE', -1)
        if enable == -1:
            # No parameter: show current state
            state = 'enabled' if self._auto_feed_enabled else 'disabled'
            self.log_always('Auto-feed is %s' % state)
            return
        self._auto_feed_enabled = bool(enable)
        if not self._auto_feed_enabled:
            # Stop any running feed and disable feed assist
            for slot in range(4):
                try:
                    self._stop_feeding(slot)
                except Exception:
                    pass
            if self._feed_assist_index != -1:
                self._disable_feed_assist()
                self.wait_ace_ready()
        state = 'enabled' if self._auto_feed_enabled else 'disabled'
        self.log_always('Auto-feed %s' % state)

    # Mode switch: swaps filament_feed.py / extruder.py
    # between stock and ACE versions, then restarts Klipper.
    # Called by SET_ACE_MODE macro in ace.cfg.
    cmd_ACE_RUN_MODE_SWITCH_help = 'Switch mode: normal (stock), single (one ACE), multi (multi-ACE)'
    def cmd_ACE_RUN_MODE_SWITCH(self, gcmd):
        mode = gcmd.get('MODE', '').lower()
        if mode not in ('normal', 'single', 'multi'):
            raise gcmd.error('Invalid mode: %s. Use normal, single, or multi.' % mode)

        current = self._ace_mode

        # Single <-> Multi: no file swap, no reboot — just save variable
        if mode in ('single', 'multi') and current in ('single', 'multi'):
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=ace__mode VALUE=\"'%s'\"" % mode)
            self._ace_mode = mode
            if mode == 'multi':
                self._restore_head_source()
                self.printer.register_event_handler(
                    'extruder:activate_extruder', self._on_extruder_change)
            self.log_always('Switched to %s mode. No reboot needed.' % mode.upper())
            return

        # Normal <-> ACE (single or multi): file swap + reboot required
        # Locate script relative to ace_vars.cfg (same directory)
        save_vars = self.printer.lookup_object('save_variables')
        vars_path = save_vars.filename
        script_dir = os.path.dirname(os.path.abspath(vars_path))
        script = os.path.join(script_dir, 'ace_mode_switch.sh')
        if not os.path.exists(script):
            raise gcmd.error('Mode switch script not found: %s' % script)

        # Shell script only knows 'ace' or 'normal' for file swap
        file_mode = 'normal' if mode == 'normal' else 'ace'

        self.log_always('Running mode switch to %s...' % mode.upper())

        # Save the actual mode (normal/single/multi)
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=ace__mode VALUE=\"'%s'\"" % mode)

        # Run file swap script (non-blocking, logs to /tmp/ace_mode_switch.log)
        try:
            import subprocess
            subprocess.Popen(['bash', script, file_mode],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            raise gcmd.error('Failed to run mode switch script: %s' % str(e))

        # Show reboot message on Snapmaker display (all 6s for easy recognition)
        self.gcode.run_script_from_command(
            'RAISE_EXCEPTION ID=6666 INDEX=6 CODE=6 MESSAGE="Switched to %s mode. Please reboot!" ONESHOT=0 LEVEL=2' % mode.upper())

        # Persistent red banner in Fluidd
        raise gcmd.error(
            'Switched to %s mode. Please reboot the printer to activate!' % mode.upper())

    # New GCode command: Lists all detected ACE devices with active marker
    cmd_ACE_LIST_help = 'List all detected ACE devices (up to 4)'

    def cmd_ACE_LIST(self, gcmd):
        if not self._ace_devices:
            self.log_always('No ACE devices detected')
            return

        self.log_always('Found %d ACE device(s):' % len(self._ace_devices))
        for i, device in enumerate(self._ace_devices):
            active = ' << ACTIVE' if i == self._active_device_index else ''
            self.log_always('  ACE %d: %s%s' % (i + 1, device, active))

    # Extended: Added active_device and device_count to status output
    # Original only returned status, temp, dryer_status, gate_status
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

