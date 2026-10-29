# Creality CFS Nano on a USB-RS485 dongle (driver 'cfs').
#
# The box speaks Creality's material-box bus: frames
#   F7 ADDR LEN STATUS FUNC DATA CRC8
# with LEN = len(DATA) + 3, CRC-8 (polynomial 0x07, init 0) over
# LEN..DATA, 230400 8N1, half duplex. The host sends STATUS 0xFF (0x00
# for the addressing commands A0..A3). Measured on a CFS Nano
# (CLAUDE.md section 80): the box answers on a fixed address (0x35) once
# GET_SLAVE_INFO (0xA1) was broadcast to 0xFF, device type 8, a 12-byte
# unique id; it stays mute for about 30 s after power-on.
#
# The host vocabulary (get_info, get_status, feed_filament, ...) has no
# one-to-one box command: a status is two box queries, a load is a
# sequence with an unlock. So the codec does not frame host requests on
# the wire. open_transport returns a CfsLink, a pyserial-shaped object:
# ace.py's writer thread hands it one host request per write(), the link
# runs the box transaction(s) half duplex in that call and queues one
# host response, which the reader thread reads back and decode_frames
# turns into the usual response dict. ace.py runs it on the threaded
# engine, like an ACE 2.
#
# Phase 1 (this file): discovery, identity, presence per slot, box
# state. Every motor or mode method is answered 'unsupported' before it
# reaches the wire; loads and unloads come in a later phase.
#
# This module imports no ace.py symbol.

import json
import logging
import os
import queue
import time

from .ace_protocol import AceProtocol

HEAD = 0xF7
BROADCAST = 0xFF
STATUS_HOST = 0xFF
STATUS_ADDR = 0x00

F_PRESENCE = 0x08          # data [bank]; reply byte 0 = slot bit mask
F_BOX_STATE = 0x0A         # reply 4 bytes, last = box state
F_VERSION = 0x14           # reply ASCII
F_SLAVE_INFO = 0xA1        # to 0xFF, data FE FE; reply dev_type, mode, uid

DEV_TYPE_NANO = 8          # the only type measured (Hi/K2 boxes say 1)
UNIQUE_ID_LEN = 12
INSERT_EVENT_STATUS = 0x30 # a 0x0A reply with this status carries events

# Reply wait per box command. A Nano answers in a few ms; 0.5 s covers a
# loaded USB without letting a silent box hold the writer for long.
REPLY_TIMEOUT = 0.5
# Bytes of envelope one read() call may wait for (pyserial timeout).
READ_TIMEOUT = 0.1
# The raw port's read timeout (SERIAL_KWARGS) = one empty read.
READ_MISS_S = 0.05

DEFAULT_USB_IDS = (('1a86', '7523'),)

MODEL = 'Creality CFS Nano'

BOX_STATES = {0x00: 'idle', 0x02: 'print', 0x03: 'locked_after_retract',
              0x04: 'locked_after_feed'}

UNSUPPORTED = -2


def crc8(data):
    crc = 0
    for b in bytearray(data):
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) if crc & 0x80 else (crc << 1)
            crc &= 0xFF
    return crc


def build_frame(addr, status, func, data=b''):
    body = bytes(bytearray([len(data) + 3, status, func])) + bytes(data)
    return bytes(bytearray([HEAD, addr])) + body + bytes(bytearray([crc8(body)]))


def parse_frames(buf):
    """Pop complete frames off `buf` (bytearray). Returns a list of dicts
    {addr, status, func, data}; corrupt frames are dropped and counted in
    the 'bad' entry of the second return value."""
    frames = []
    bad = 0
    while True:
        i = buf.find(bytes(bytearray([HEAD])))
        if i < 0:
            del buf[:]
            break
        if i > 0:
            del buf[:i]
        if len(buf) < 3:
            break
        ln = buf[2]
        if ln < 3:
            del buf[:1]
            bad += 1
            continue
        total = 3 + ln
        if len(buf) < total:
            break
        fr = bytes(buf[:total])
        if crc8(fr[2:-1]) != bytearray(fr)[-1]:
            del buf[:1]
            bad += 1
            continue
        del buf[:total]
        b = bytearray(fr)
        frames.append({'addr': b[1], 'status': b[3], 'func': b[4],
                       'data': bytes(b[5:-1])})
    return frames, bad


def uid_words(unique_id):
    """Three big-endian 32-bit words of the 12-byte unique id (the same
    shape as the ACE 2 discover_device answer)."""
    b = bytearray(unique_id)
    if len(b) < UNIQUE_ID_LEN:
        return ()
    return tuple(int.from_bytes(bytes(b[i:i + 4]), 'big')
                 for i in (0, 4, 8))


def uid_key(words):
    try:
        parts = tuple(int(x or 0) for x in words)
    except (TypeError, ValueError):
        return ''
    if len(parts) != 3 or not any(parts):
        return ''
    return 'uid:' + '-'.join('%08x' % x for x in parts)


class LinkDead(IOError):
    pass


class CfsLink(object):
    """The pyserial surface ace.py uses (is_open, read, in_waiting, write,
    reset_input_buffer, flush, close, fileno) on top of the raw port.
    write() runs one host request against the box; read() hands out the
    response envelopes. A dead raw port makes write() raise (the writer's
    reconnect path) and read() raise on its next call (the reader's)."""

    def __init__(self, raw):
        self._raw = raw
        self._rx = bytearray()
        self._out = queue.Queue()
        self._pending = bytearray()
        self._dead = None
        self.addr = None
        self.dev_type = None
        self.mode = None
        self.unique_id = b''
        self.bad_frames = 0

    # -- pyserial surface -------------------------------------------------

    @property
    def is_open(self):
        return bool(getattr(self._raw, 'is_open', False))

    def fileno(self):
        return self._raw.fileno()

    def flush(self):
        pass

    def reset_input_buffer(self):
        del self._pending[:]

    def close(self):
        self._raw.close()

    def _drain(self):
        while True:
            try:
                chunk = self._out.get_nowait()
            except queue.Empty:
                return
            self._pending.extend(chunk)

    @property
    def in_waiting(self):
        if self._dead is not None and not self._pending:
            raise LinkDead(str(self._dead))
        self._drain()
        return len(self._pending)

    def read(self, size=1):
        if not self._pending:
            if self._dead is not None:
                raise LinkDead(str(self._dead))
            try:
                self._pending.extend(self._out.get(timeout=READ_TIMEOUT))
            except queue.Empty:
                if self._dead is not None:
                    raise LinkDead(str(self._dead))
                return b''
            self._drain()
        data = bytes(self._pending[:size])
        del self._pending[:size]
        return data

    def write(self, data):
        if self._dead is not None:
            raise LinkDead(str(self._dead))
        try:
            request = json.loads(bytes(data).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            logging.info('[multiACE] CFS Nano link: undecodable request dropped')
            return len(data)
        try:
            response = self._execute(request)
        except (IOError, OSError) as e:
            self._dead = e
            raise
        if response is not None:
            self._out.put(json.dumps(response).encode('utf-8') + b'\n')
        return len(data)

    # -- box transactions -------------------------------------------------

    def _transact(self, addr, status, func, data=b'', timeout=REPLY_TIMEOUT):
        """Send one frame and wait for the reply with the same function
        code. None when none arrives in time. Frames for other functions
        or addresses are dropped (the bus is ours; nothing pushes)."""
        frame = build_frame(addr, status, func, data)
        self._raw.write(frame)
        deadline = time.monotonic() + timeout
        # Bounded by time AND by empty reads (one per serial timeout), so a
        # silent box costs `timeout` even where the clock stands still.
        misses = 0
        max_misses = int(timeout / max(READ_MISS_S, 0.001)) + 1
        while True:
            chunk = self._raw.read(1)
            if not chunk:
                misses += 1
                if misses >= max_misses:
                    return None
            if chunk:
                n = self._raw.in_waiting
                if n:
                    chunk += self._raw.read(n)
                self._rx.extend(chunk)
                frames, bad = parse_frames(self._rx)
                self.bad_frames += bad
                for fr in frames:
                    if fr['func'] != func:
                        continue
                    if addr != BROADCAST and fr['addr'] != addr:
                        continue
                    return fr
            if time.monotonic() >= deadline:
                return None

    def _identify(self):
        """GET_SLAVE_INFO broadcast; learns the box address from the
        replying frame. True when a box of a known type answered."""
        fr = self._transact(BROADCAST, STATUS_ADDR, F_SLAVE_INFO,
                            b'\xfe\xfe')
        if fr is None or len(fr['data']) < 2:
            return False
        d = bytearray(fr['data'])
        self.dev_type = d[0]
        self.mode = d[1]
        self.unique_id = bytes(d[2:2 + UNIQUE_ID_LEN])
        if self.dev_type != DEV_TYPE_NANO:
            logging.info('[multiACE] CFS Nano link: device type %d answered '
                         '(only %d, the CFS Nano, is supported)'
                         % (self.dev_type, DEV_TYPE_NANO))
            return False
        self.addr = fr['addr']
        return True

    def _execute(self, request):
        rid = request.get('id', 0)
        method = request.get('method', '')

        def ok(result):
            return {'id': rid, 'code': 0, 'msg': 'success', 'result': result}

        if method == 'discover_device':
            if not self._identify():
                return None
            w = uid_words(self.unique_id)
            return ok({'uid1': w[0] if w else 0, 'uid2': w[1] if w else 0,
                       'uid3': w[2] if w else 0, 'dev_type': self.dev_type,
                       'mode': self.mode})
        if method in ('get_info', 'get_status') and self.addr is None:
            if not self._identify():
                return None
        if method == 'get_info':
            fr = self._transact(self.addr, STATUS_HOST, F_VERSION)
            fw = ''
            if fr is not None:
                fw = bytes(fr['data']).decode('ascii', 'replace').strip(
                    '\x00 ')
            return ok({'model': MODEL, 'firmware': fw or 'Unknown',
                       'dev_type': self.dev_type})
        if method == 'get_status':
            pres = self._transact(self.addr, STATUS_HOST, F_PRESENCE, b'\x00')
            if pres is None or not pres['data']:
                # No fresh presence = no answer (contract 5.3): the
                # watchdog stays the only judge of a silent box.
                return None
            st = self._transact(self.addr, STATUS_HOST, F_BOX_STATE)
            return ok(make_status(bytearray(pres['data'])[0], st))
        return {'id': rid, 'code': UNSUPPORTED,
                'msg': 'unsupported:%s' % method, 'result': {}}


def make_status(mask, box_frame=None):
    """Host status dict from the presence mask (bit n = slot n) and the
    GET_BOX_STATE reply (None when it did not answer)."""
    slots = []
    for i in range(4):
        slots.append({'index': i,
                      'status': 'ready' if mask & (1 << i) else 'empty',
                      'sku': '', 'type': '', 'rfid': 0, 'brand': '',
                      'color': [0, 0, 0]})
    out = {'status': 'ready', 'slots': slots}
    if box_frame is not None and box_frame['data']:
        d = bytearray(box_frame['data'])
        if box_frame['status'] == INSERT_EVENT_STATUS:
            # The reply carries an insert/remove event per slot instead of
            # the box state; consumed by the read, nothing to keep yet.
            out['box_event'] = [int(x) for x in d[:4]]
        else:
            state = d[-1]
            out['box_state'] = BOX_STATES.get(state, 'state_%02x' % state)
    return out


class AceProtocolCfs(AceProtocol):
    NAME = 'cfs'
    DEFAULT_BAUD = 230400
    # Set from ace.py at init from [ace] cfs_usb_ids; a path matches when
    # its USB id is listed AND the box answered the probe.
    USB_IDS = DEFAULT_USB_IDS
    # Discovery claims a path only after probe_device verified it: the
    # dongle is a generic USB-serial chip.
    PROBE_BEFORE_CLAIM = True
    CAPS_STATIC = {
        'transport_engine': 'threaded',
        'blocking_write_on_reactor': False,
        'reconnectable': True,
        'presence_sensor': True,
        'empty_token': 'empty',
        'has_slot_status_truth': False,
        'busy_wait_floor': False,
        'fa_monitor': False,
        'needs_fa_prestop': False,
        'has_rollback_assist': False,
        # Print mode freewheels: the extruder pulls the spool itself.
        'no_freewheel': False,
        'self_stops_on_resistance': True,
        'has_decoder': False,
        'v1_fa_health_log': False,
        # No tag query on the box: the status carries the (empty)
        # identity, so the heartbeat never asks per slot.
        'tag_identity_in_status': True,
        'fw_tag_pair_dedupe': False,
        'has_humidity': False,
        'has_dryer': False,
        'has_exhaust_valve': False,
        'has_feed_check': False,
        'flash_engine': 'none',
        'v2_command_set': False,
        # One motor for all four lanes.
        'lanes_concurrent': False,
        'speed_unit': 'none',
        'busy_reply': 'code1_forbidden',
        'has_stable_id': True,
    }
    SERIAL_KWARGS = {
        'timeout': 0.05,
        'write_timeout': 0.25,
        'exclusive': True,
    }

    @classmethod
    def discover(cls):
        out = []
        by_path_dir = '/dev/serial/by-path/'
        if not os.path.exists(by_path_dir):
            return out
        for entry in sorted(os.listdir(by_path_dir)):
            full_path = os.path.join(by_path_dir, entry)
            real_dev = os.path.basename(os.path.realpath(full_path))
            ids = cls._read_usb_ids(real_dev)
            if ids[0] is not None and ids in cls.USB_IDS:
                out.append(full_path)
        return out

    @classmethod
    def open_raw(cls, path, baud):
        import serial
        return serial.Serial(port=path, baudrate=baud or cls.DEFAULT_BAUD,
                             **cls.SERIAL_KWARGS)

    @classmethod
    def open_transport(cls, path, baud, **kwargs):
        return CfsLink(cls.open_raw(path, cls.DEFAULT_BAUD))

    @classmethod
    def probe_device(cls, path, query_timeout=0.4):
        """Bounded identity probe before a claim: GET_SLAVE_INFO, then the
        version. {'ok', 'category', 'error', 'uid', 'firmware'}."""
        try:
            raw = cls.open_raw(path, cls.DEFAULT_BAUD)
        except Exception as e:
            msg = str(e).lower()
            cat = ('port_in_use' if 'busy' in msg or 'lock' in msg else
                   'permission_denied' if 'permission' in msg else
                   'serial_error')
            return {'ok': False, 'category': cat, 'error': str(e)}
        link = CfsLink(raw)
        try:
            fr = link._transact(BROADCAST, STATUS_ADDR, F_SLAVE_INFO,
                                b'\xfe\xfe', timeout=query_timeout)
            if fr is None:
                return {'ok': False, 'category': 'no_response',
                        'error': 'no GET_SLAVE_INFO reply'}
            d = bytearray(fr['data'])
            if len(d) < 2 + UNIQUE_ID_LEN:
                return {'ok': False, 'category': 'invalid_response',
                        'error': 'short GET_SLAVE_INFO reply'}
            if d[0] != DEV_TYPE_NANO:
                return {'ok': False, 'category': 'invalid_identity',
                        'error': 'device type %d (CFS Nano is %d)'
                                 % (d[0], DEV_TYPE_NANO)}
            words = uid_words(bytes(d[2:2 + UNIQUE_ID_LEN]))
            ver = link._transact(fr['addr'], STATUS_HOST, F_VERSION,
                                 timeout=query_timeout)
            fw = ''
            if ver is not None:
                fw = bytes(ver['data']).decode('ascii', 'replace').strip(
                    '\x00 ')
            return {'ok': True, 'category': 'verified', 'model': MODEL,
                    'firmware': fw, 'uid': words, 'addr': fr['addr']}
        except Exception as e:
            return {'ok': False, 'category': 'serial_error', 'error': str(e)}
        finally:
            try:
                raw.close()
            except Exception:
                pass

    @staticmethod
    def uid_key(words):
        return uid_key(words)

    @classmethod
    def probe_uid(cls, path, query_timeout=0.35):
        res = cls.probe_device(path, query_timeout=query_timeout) or {}
        if res.get('ok'):
            return uid_key(res.get('uid') or ()), ''
        return '', '%s: %s' % (res.get('category', '?'),
                               res.get('error', ''))

    def encode_request(self, request, next_id=None):
        return json.dumps(request).encode('utf-8')

    def decode_frames(self, buffer):
        results = []
        while True:
            i = buffer.find(b'\n')
            if i < 0:
                break
            line = bytes(buffer[:i])
            del buffer[:i + 1]
            if not line:
                continue
            try:
                results.append(json.loads(line.decode('utf-8')))
            except (ValueError, UnicodeDecodeError):
                logging.info('[multiACE] CFS Nano link: bad envelope dropped')
        return results

    def initial_handshake_requests(self):
        return [{'method': 'discover_device'}, {'method': 'get_info'}]

    def make_default_info(self):
        return make_status(0)
