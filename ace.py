import logging
import json
import struct
import queue
import traceback
import os              # [mUlt1ACE] Added for auto-detection filesystem access
import serial
from serial import SerialException


class AceException(Exception):
    pass


GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1  # Available to load from either buffer or spool


class BunnyAce:
    VARS_ACE_REVISION = 'ace__revision'
    VARS_ACE_ACTIVE_DEVICE = 'ace__active_device'  # [mUlt1ACE] Persists active ACE selection across reboots

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

        # [mUlt1ACE] serial defaults to '' to enable auto-detection (original: '/dev/ttyACM0')
        self.serial_id = config.get('serial', '')
        self.baud = config.getint('baud', 115200)
        self._ace_devices = []              # [mUlt1ACE] List of auto-detected ACE device paths
        self._active_device_index = 0       # [mUlt1ACE] Index into _ace_devices for active unit

        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_length = config.getint('retract_length', 100)
        self.feed_length = config.getint('feed_length', 100)
        # [mUlt1ACE] New parameters for load phase in filament_feed.py
        self.load_length = config.getint('load_length', 2000)         # Max feed distance during load
        self.load_retry = config.getint('load_retry', 3)              # Number of retries if sensor not reached
        self.load_retry_retract = config.getint('load_retry_retract', 50)  # Mini-retract before retry (mm)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)

        # [mUlt1ACE] Per-toolhead overrides (optional, fallback to global defaults)
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

        self._callback_map = {}
        self._feed_assist_index = -1
        self._request_id = 0

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
        # [mUlt1ACE] New GCode commands for multi-ACE management
        self.gcode.register_command(
            'ACE_SWITCH', self.cmd_ACE_SWITCH,
            desc=self.cmd_ACE_SWITCH_help)
        self.gcode.register_command(
            'ACE_LIST', self.cmd_ACE_LIST,
            desc=self.cmd_ACE_LIST_help)

    # [mUlt1ACE] New method: Scans USB bus for ACE Pro devices via sysfs vendor/product ID
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

    # [mUlt1ACE] Rewritten: Original only connected to configured serial port.
    # Now auto-detects all ACE devices, restores last active selection from
    # saved variables, and falls back to configured serial if no devices found.
    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

        # Auto-detect ACE devices
        self._ace_devices = self._scan_ace_devices()

        if self._ace_devices:
            logging.info('ACE: Found %d device(s): %s' % (len(self._ace_devices), str(self._ace_devices)))
            self.log_always('ACE: Found %d device(s)' % len(self._ace_devices))

            # Restore last active device from saved variables
            saved_device = self.save_variables.allVariables.get(self.VARS_ACE_ACTIVE_DEVICE, None)
            if saved_device and saved_device in self._ace_devices:
                self._active_device_index = self._ace_devices.index(saved_device)
                logging.info('ACE: Restored active device %d: %s' % (self._active_device_index + 1, saved_device))
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

    def _handle_disconnect(self):
        logging.info(f'ACE: Closing connection to {self.serial_id}')
        self._serial_disconnect()
        self._queue = None

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
        self.log_always('Try connecting25')

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
                self.reactor.unregister_timer(self.connect_timer)
                return self.reactor.NEVER
        except serial.serialutil.SerialException:
            self._serial = None
            logging.info('ACE: Conn error')
            self.log_error(f'Error connecting to {self.serial_id}')
        except Exception as e:
            self.log_error(f"ACE Error: {e}")

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
            self.log_error("ACE: Error writing to serial")
            self.log_warning("Try reconnecting")
            self._serial_disconnect()
            self.connect_timer = self.reactor.register_timer(self._connect, self.reactor.NOW)

    # [mUlt1ACE] Rewritten: Original blindly fed feed_length mm without sensor check.
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
                        self.log_always('auto_feed')
                        self.reactor.register_async_callback(
                            (lambda et, c=self._pre_load, gate=i: c(gate)))

                    if response['result']['slots'][i]['rfid'] == 2 and self._info['slots'][i]['rfid'] != 2:
                        self.log_always('find_rfid')
                        spool_inf = response['result']['slots'][i]
                        self.log_always(str(spool_inf))
                        self.gcode.run_script_from_command(f'SET_PRINT_FILAMENT_CONFIG '
                                                           f'CONFIG_EXTRUDER={i} '
                                                           f'FILAMENT_TYPE="{spool_inf.get("type", "PLA")}" '                                               
                                                           f'FILAMENT_COLOR_RGBA={self.rgb2hex(*spool_inf.get("color", (0,0,0)))} '
                                                           f'VENDOR="{spool_inf.get("brand", "Generic")}" '
                                                           f'FILAMENT_SUBTYPE=""')
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
            self.log_error("Unable to communicate with the ACE PRO")
            self.log_warning("Try reconnecting")
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

    # [mUlt1ACE] New GCode command
    cmd_ACE_SWITCH_help = 'Switch active ACE unit (supports up to 4). AUTOLOAD=1 for full unload/load cycle.'

    # [mUlt1ACE] Mapping of extruder index to Snapmaker FEED_AUTO parameters.
    # Determined by observing touchscreen FEED_AUTO commands in klippy.log.
    EXTRUDER_MAP = {
        0: ('left', 1),
        1: ('left', 0),
        2: ('right', 0),
        3: ('right', 1),
    }

    # [mUlt1ACE] New method: Forces RFID data from current ACE to the Snapmaker display.
    # Called after ACE switch to update filament type/color/brand on the touchscreen.
    # The heartbeat normally only pushes RFID data when it changes (rfid == 2 transition),
    # so after switching ACE units we need to manually push the new ACE's spool info.
    def _push_rfid_info(self):
        """Force push RFID data from current ACE to display"""
        for i in range(4):
            slot = self._info['slots'][i]
            if slot.get('rfid', 0) == 2:
                self.gcode.run_script_from_command(
                    'SET_PRINT_FILAMENT_CONFIG '
                    'CONFIG_EXTRUDER=%d '
                    'FILAMENT_TYPE="%s" '
                    'FILAMENT_COLOR_RGBA=%s '
                    'VENDOR="%s" '
                    'FILAMENT_SUBTYPE=""' % (
                        i,
                        slot.get('type', 'PLA'),
                        self.rgb2hex(*slot.get('color', (0, 0, 0))),
                        slot.get('brand', 'Generic')))

    # [mUlt1ACE] New: Hot-swaps between ACE units without reboot.
    # Unload: only toolheads where sensor detects filament in head.
    # Load: only gates where ACE has filament (GATE_AVAILABLE).
    # Resets machine state after unload to prevent "Unloading Anomaly".
    # Pushes RFID info after switch. Saves selection to ace_vars.cfg.
    def cmd_ACE_SWITCH(self, gcmd):
        target = gcmd.get_int('TARGET')
        autoload = gcmd.get_int('AUTOLOAD', 0)

        if not self._ace_devices:
            raise gcmd.error('No ACE devices detected')

        if target < 1 or target > len(self._ace_devices):
            raise gcmd.error('TARGET must be between 1 and %d (found %d ACE devices)' % (len(self._ace_devices), len(self._ace_devices)))

        target_index = target - 1
        switching_ace = target_index != self._active_device_index

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
                self.log_always('Unloading from ACE %d...' % (self._active_device_index + 1))

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

            # Switch to new device
            self._active_device_index = target_index
            self.serial_id = self._ace_devices[target_index]

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

        # Load phase - check sensors before loading
        if autoload:
            if switching_ace:
                # Wait for connection
                for _ in range(30):
                    self.reactor.pause(self.reactor.monotonic() + 0.5)
                    if self._connected:
                        break
                if not self._connected:
                    self.log_error('Failed to connect to ACE %d' % target)
                    return

                # Wait for heartbeat to update gate status and RFID
                self.reactor.pause(self.reactor.monotonic() + 3.0)

                # Push RFID info from new ACE to display
                self._push_rfid_info()

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

    # [mUlt1ACE] New GCode command: Lists all detected ACE devices with active marker
    cmd_ACE_LIST_help = 'List all detected ACE devices (up to 4)'

    def cmd_ACE_LIST(self, gcmd):
        if not self._ace_devices:
            self.log_always('No ACE devices detected')
            return

        self.log_always('Found %d ACE device(s):' % len(self._ace_devices))
        for i, device in enumerate(self._ace_devices):
            active = ' << ACTIVE' if i == self._active_device_index else ''
            self.log_always('  ACE %d: %s%s' % (i + 1, device, active))

    # [mUlt1ACE] Extended: Added active_device and device_count to status output
    # Original only returned status, temp, dryer_status, gate_status
    def get_status(self, eventtime=None):
        return {
            'status': self._info['status'],
            'temp': self._info['temp'],
            'dryer_status': self._info['dryer_status'],
            'gate_status': self.gate_status,
            'active_device': self._active_device_index + 1,
            'device_count': len(self._ace_devices),
        }


def load_config(config):
    return BunnyAce(config)

