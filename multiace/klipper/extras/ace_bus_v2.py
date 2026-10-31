# ACE 2 daisy chain: several ACE 2 Pro units on ONE RS-485 cable.
#
# Opt-in ([ace] v2_chain: true). With the option off this module is never
# imported and the ACE 2 driver opens one serial port per unit, as before.
#
# Why a bus layer: ACE 2 units chained through their chain ports share one
# half-duplex RS-485 line behind a single USB serial port. Every unit boots at
# device address 0, so a host that talks to address 0 reaches every unit at
# once. The protocol has per-unit addresses (DISCOVER_DEVICE / ASSIGN_DEVICE_ID,
# measured by tobecwb in multiACE #120): byte 2 of every frame is the address,
# 0x0n host->unit, 0x8n unit->host, and the CRC covers it.
#
# Layering: one V2Bus per physical cable owns the serial port and a single bus
# thread. Each unit on it is handed to multiACE as a UnitPort - an object with
# the slice of the pyserial API the V2 reader/writer threads use (write, read,
# in_waiting, is_open, close). multiACE's per-unit layer (threads, silence
# watchdog, reconnect-or-pause, unit places by uid) runs unchanged on top. The
# host keeps encoding frames at address 0x00 and decoding replies at 0x80; the
# bus puts the unit's address in, fixes the CRC, runs one request at a time on
# the wire and routes each reply back to its unit.
#
# Rules (from tobecwb's measurements, #120, and a two-unit chain in use since
# 2026-10-27):
#   - A unit drops back to address 0 after ~3.5 s without a frame addressed
#     to it: every addressed unit is touched at least every KEEPALIVE_S, from
#     its own ASSIGN on (not after the whole chain is up).
#   - The ASSIGN acknowledgement arrives on the OLD address.
#   - A DISCOVER broadcast returns one frame per unaddressed unit, back to
#     back: read the whole burst.
#   - Replies are usually < 45 ms but occasionally slower than 0.2 s: a unit
#     is lost only after LOST_AFTER_MISSES misses of REPLY_TIMEOUT_S.
#   - A unit that only missed replies keeps its address: probe it there
#     first. A unit that restarted answers DISCOVER at 0 again and is
#     re-assigned by its uid.
#
# Lazy addressing: a cable with exactly one unit stays at address 0, byte for
# byte what the driver sends without the bus, no keep-alive. Addresses are
# assigned only once a second unit answers a DISCOVER.
#
# Failure mapping onto multiACE's existing recovery:
#   - the cable itself fails (I/O error): every UnitPort on the bus raises
#     OSError(EIO) -> reconnect-or-pause per unit; the bus reopens the cable.
#   - one unit stops answering: only its UnitPort raises -> that unit's
#     reconnect-or-pause; open_unit() waits a bounded time for the bus to find
#     it again.
#
# Refused on a shared bus (forwarded only in lone mode): ASSIGN_DEVICE_ID (it
# would re-address a unit behind the bus) and the IAP flash commands (a unit
# loses its address when it reboots into the new image). Flash a unit alone on
# its cable.
#
# This module imports nothing from ace.py.

import collections
import errno
import logging
import threading
import time

from .ace_protocol_v2 import crc16_kermit, pb_uint32, pb_decode

PREAMBLE = b'\xff\xaa'
END = 0xFE
HEADER = 7
TRAILER = 3
BAUD = 230400

CMD_DISCOVER = 0
CMD_ASSIGN = 1
CMD_IAP_UPGRADE = 2
CMD_IAP_FIRMWARE = 3
CMD_IAP_FINISH = 4
CMD_GET_INFO = 7
SHARED_BUS_BLOCKED = (CMD_ASSIGN, CMD_IAP_UPGRADE, CMD_IAP_FIRMWARE,
                      CMD_IAP_FINISH)

KEEPALIVE_S = 1.0          # touch every addressed unit at least this often
REPLY_TIMEOUT_S = 1.0      # host request: wait this long for its reply
PROBE_TIMEOUT_S = 0.5      # keep-alive / probe reply window
LOST_AFTER_MISSES = 3      # consecutive misses before a unit counts as lost
DISCOVER_WAIT_S = 0.2      # collect a broadcast burst (replies < 45 ms)
RESCAN_S = 5.0             # look for new / restarted units at address 0
LOST_PROBE_S = 1.0         # probe a lost unit at its address this often ...
LOST_PROBE_SLOW_S = 5.0    # ... and this often once it has been gone 10 s
REOPEN_S = 1.0             # retry interval for a failed cable
MAX_UNITS = 4              # multiACE places four units
OPEN_WAIT_S = 3.0          # open_unit(): wait for a unit to (re)appear
FIRST_SCAN_WAIT_S = 2.5    # first discover() of a NEW bus: wait this long
                           # (once per process) for its first enumeration,
                           # so a cold start does not soft-fail and restart
INTERNAL_SEQ = 0xF000      # start of the bus's own sequence numbers

# Virtual path = <cable path>@<uid hex>. Not '#' or ';': the path ends up
# in G-code (SAVE_VARIABLE of the active device), where both start a
# comment and would cut the value off.
SEP = '@'

_buses = {}
_buses_lock = threading.Lock()


# ------------------------------------------------------------------ frames

def build(addr, seq, cmd, payload=b''):
    inner = bytes([addr & 0xFF, seq & 0xFF, (seq >> 8) & 0xFF, cmd & 0xFF,
                   len(payload)]) + bytes(payload)
    crc = crc16_kermit(inner)
    return PREAMBLE + inner + bytes([crc & 0xFF, (crc >> 8) & 0xFF, END])


def readdress(frame, addr):
    """The same frame with another address byte and a fixed CRC."""
    inner = bytes([addr & 0xFF]) + bytes(frame[3:-TRAILER])
    crc = crc16_kermit(inner)
    return PREAMBLE + inner + bytes([crc & 0xFF, (crc >> 8) & 0xFF, END])


def parse(buf):
    """Pop every complete, CRC-valid frame off the front of bytearray buf."""
    out = []
    while True:
        j = buf.find(PREAMBLE)
        if j < 0:
            keep = 1 if buf.endswith(b'\xff') else 0
            del buf[:len(buf) - keep]
            return out
        if j:
            del buf[:j]
        if len(buf) < HEADER:
            return out
        total = HEADER + buf[6] + TRAILER
        if len(buf) < total:
            return out
        frame = bytes(buf[:total])
        crc = frame[-3] | (frame[-2] << 8)
        if frame[-1] != END or crc != crc16_kermit(frame[2:-TRAILER]):
            del buf[:2]
            continue
        del buf[:total]
        out.append({'raw': frame, 'addr': frame[2],
                    'seq': frame[3] | (frame[4] << 8), 'cmd': frame[5],
                    'payload': frame[HEADER:-TRAILER]})


def uid_of(payload):
    fields = pb_decode(payload)
    uid = []
    for num in (1, 2, 3):
        vals = fields.get(num) or []
        uid.append(int(vals[0][1]) if vals and vals[0][0] == 0 else 0)
    uid = tuple(uid)
    return uid if any(uid) else None


def uid_hex(uid):
    return '-'.join('%08x' % x for x in uid)


def assign_payload(uid, addr):
    return (pb_uint32(1, uid[0]) + pb_uint32(2, uid[1])
            + pb_uint32(3, uid[2]) + pb_uint32(4, addr))


# --------------------------------------------------------------- unit port

class UnitPort(object):
    """One unit as multiACE's V2 threads see it. Raises OSError(EIO) when
    the unit is lost or the cable failed, exactly where a real port would."""

    def __init__(self, bus, unit, timeout=0.1):
        self._bus = bus
        self._unit = unit
        self.port = bus.virtual_path(unit)
        self.timeout = timeout
        self._open = True
        self._rx = bytearray()
        self._cond = threading.Condition()
        self._tx = bytearray()

    # pyserial surface used by the V2 driver --------------------------------
    @property
    def is_open(self):
        return self._open

    @property
    def in_waiting(self):
        self._check()
        with self._cond:
            return len(self._rx)

    def read(self, size=1):
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while True:
                self._check()
                if self._rx:
                    data = bytes(self._rx[:size])
                    del self._rx[:size]
                    return data
                left = deadline - time.monotonic()
                if left <= 0:
                    return b''
                self._cond.wait(left)

    def write(self, data):
        self._check()
        self._tx.extend(data)
        for frame in parse(self._tx):
            self._bus.submit(self._unit, frame)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self._cond:
            del self._rx[:]

    def close(self):
        self._open = False
        self._bus.detach(self._unit, self)
        with self._cond:
            self._cond.notify_all()

    # bus side ---------------------------------------------------------------
    def deliver(self, data):
        with self._cond:
            self._rx.extend(data)
            self._cond.notify_all()

    def wake(self):
        with self._cond:
            self._cond.notify_all()

    def _check(self):
        if not self._open:
            raise OSError(errno.EBADF, 'port closed')
        if not self._bus.alive:
            raise OSError(errno.EIO, 'ACE 2 bus %s lost' % self._bus.path)
        if self._unit.failed_port is self:
            raise OSError(errno.EIO, 'ACE 2 unit %s stopped answering'
                          % uid_hex(self._unit.uid))


class Unit(object):
    def __init__(self, uid):
        self.uid = uid
        self.addr = 0
        self.present = False
        self.port = None
        self.failed_port = None
        self.last_tx = 0.0
        self.misses = 0
        self.lost_since = 0.0
        self.firmware = ''


# --------------------------------------------------------------------- bus

class V2Bus(object):
    """One physical cable. All wire traffic happens on the bus thread."""

    def __init__(self, path, serial_factory=None):
        self.path = path
        self._serial_factory = serial_factory or _open_serial
        self.ser = None
        self.alive = False
        self.addressed = False
        self.units = {}            # uid -> Unit
        self._order = []           # uids in first-seen order (addresses)
        self._queue = []           # (unit, frame, internal)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._rxbuf = bytearray()
        self._seq = INTERNAL_SEQ
        # The bus's own recent sequence numbers: their replies are never
        # handed to the host (a host id may reach any value, so no range).
        self._own_seqs = collections.deque(maxlen=64)
        self._last_scan = 0.0
        self.first_scan_done = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name='multiACE-v2bus', daemon=True)
        self._thread.start()

    # ---- host side (any thread) -------------------------------------------
    def virtual_path(self, unit):
        return '%s%s%s' % (self.path, SEP, uid_hex(unit.uid))

    def present_paths(self):
        with self._lock:
            return [self.virtual_path(u) for u in
                    (self.units[k] for k in self._order) if u.present]

    def unit_for(self, hexuid):
        with self._lock:
            for u in self.units.values():
                if uid_hex(u.uid) == hexuid:
                    return u
        return None

    def open_unit(self, hexuid, wait=OPEN_WAIT_S):
        deadline = time.monotonic() + wait
        while True:
            unit = self.unit_for(hexuid)
            if unit is not None and unit.present and self.alive:
                with self._lock:
                    port = UnitPort(self, unit)
                    unit.port = port
                    unit.failed_port = None
                return port
            if time.monotonic() >= deadline:
                raise OSError(errno.ENODEV, 'ACE 2 unit %s not on bus %s'
                              % (hexuid, self.path))
            self._last_scan = 0.0          # ask for a rescan now
            self._wake.set()
            time.sleep(0.1)

    def detach(self, unit, port):
        with self._lock:
            if unit.port is port:
                unit.port = None

    def submit(self, unit, frame):
        if frame['addr'] & 0x80:
            return
        if self.addressed and frame['cmd'] in SHARED_BUS_BLOCKED:
            logging.info('[multiACE] v2 bus %s: command %d to %s refused on '
                         'a shared bus (flash a unit alone on its cable)',
                         self.path, frame['cmd'], uid_hex(unit.uid))
            return
        with self._lock:
            self._queue.append((unit, frame, False))
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    # ---- bus thread ---------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            if not self.alive and not self._reopen():
                self._wake.wait(REOPEN_S)
                self._wake.clear()
                continue
            try:
                self._step()
            except Exception as e:
                logging.info('[multiACE] v2 bus %s lost: %s', self.path, e)
                self._fail_bus()
        self._close_serial()

    def _reopen(self):
        try:
            self.ser = self._serial_factory(self.path)
        except Exception as e:
            logging.info('[multiACE] v2 bus %s: cannot open (%s)', self.path, e)
            return False
        self.alive = True
        self._rxbuf = bytearray()
        self._last_scan = 0.0
        logging.info('[multiACE] v2 bus %s opened', self.path)
        return True

    def _fail_bus(self):
        self.alive = False
        self._close_serial()
        with self._lock:
            del self._queue[:]
            for u in self.units.values():
                u.present = False
                if u.port is not None:
                    u.port.wake()

    def _close_serial(self):
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _next_seq(self):
        self._seq = INTERNAL_SEQ if self._seq >= 0xFFFF else self._seq + 1
        self._own_seqs.append(self._seq)
        return self._seq

    def _read_frames(self, wait):
        """Every frame that arrives within `wait` (blocking, bus thread)."""
        out, deadline = [], time.monotonic() + wait
        while True:
            data = self.ser.read(self.ser.in_waiting or 1)
            if data:
                self._rxbuf.extend(data)
                out += [f for f in parse(self._rxbuf) if f['addr'] & 0x80]
            if time.monotonic() >= deadline:
                return out

    def _transact(self, addr, seq, frame_or_cmd, payload=b'', wait=PROBE_TIMEOUT_S,
                  until_reply=True):
        if isinstance(frame_or_cmd, (bytes, bytearray)):
            raw = readdress(frame_or_cmd, addr)
        else:
            raw = build(addr, seq, frame_or_cmd, payload)
        self.ser.write(raw)
        out, deadline = [], time.monotonic() + wait
        while time.monotonic() < deadline:
            got = self._read_frames(min(0.02, max(0.0, deadline - time.monotonic())))
            out += got
            if until_reply and any((f['addr'] & 0x7F) == addr
                                   and f['seq'] == seq for f in got):
                break
        return out

    def _step(self):
        now = time.monotonic()
        if now - self._last_scan >= RESCAN_S:
            self._scan()
            return
        with self._lock:
            item = None
            while self._queue:
                unit, frame, internal = self._queue.pop(0)
                if unit.present:
                    item = (unit, frame, internal)
                    break
        if item is not None:
            unit, frame, internal = item
            self._request(unit, frame['raw'], frame['seq'], REPLY_TIMEOUT_S)
            return
        for unit in list(self.units.values()):
            if not self.addressed:
                break
            if unit.present:
                due = KEEPALIVE_S
            else:
                due = (LOST_PROBE_S if now - unit.lost_since < 10.0
                       else LOST_PROBE_SLOW_S)
            if now - unit.last_tx >= due:
                seq = self._next_seq()
                self._request(unit, build(0, seq, CMD_GET_INFO), seq,
                              PROBE_TIMEOUT_S, internal=True)
                return
        self._wake.wait(0.02)
        self._wake.clear()

    def _request(self, unit, raw, seq, wait, internal=False):
        unit.last_tx = time.monotonic()
        frames = self._transact(unit.addr, seq, raw, wait=wait)
        mine = [f for f in frames if (f['addr'] & 0x7F) == unit.addr]
        self._route_strays([f for f in frames if f not in mine])
        if not mine:
            unit.misses += 1
            if unit.present and unit.misses >= LOST_AFTER_MISSES:
                self._mark_lost(unit)
            return
        if not unit.present:
            logging.info('[multiACE] v2 bus %s: unit %s answering again at '
                         'address %d', self.path, uid_hex(unit.uid), unit.addr)
        unit.present = True
        unit.misses = 0
        for f in mine:
            if f['seq'] in self._own_seqs and not (not internal
                                                   and f['seq'] == seq):
                continue                     # reply to the bus's own frame
            if unit.port is not None:
                unit.port.deliver(readdress(f['raw'], 0x80))

    def _route_strays(self, frames):
        for f in frames:
            if (f['addr'] & 0x7F) == 0 and self.addressed:
                self._last_scan = 0.0        # someone is back at address 0

    def _mark_lost(self, unit):
        unit.present = False
        unit.lost_since = time.monotonic()
        unit.failed_port = unit.port
        logging.info('[multiACE] v2 bus %s: unit %s (address %d) lost after '
                     '%d missed replies', self.path, uid_hex(unit.uid),
                     unit.addr, unit.misses)
        if unit.port is not None:
            unit.port.wake()
        self._last_scan = 0.0

    # ---- discovery ------------------------------------------------------------
    def _discover_burst(self):
        uids = []
        for f in self._transact(0, self._next_seq(), CMD_DISCOVER,
                                wait=DISCOVER_WAIT_S, until_reply=False):
            if f['cmd'] == CMD_DISCOVER and (f['addr'] & 0x7F) == 0:
                uid = uid_of(f['payload'])
                if uid and uid not in uids:
                    uids.append(uid)
        return uids

    def _known(self, uid):
        with self._lock:
            unit = self.units.get(uid)
            if unit is None:
                if len(self.units) >= MAX_UNITS:
                    logging.info('[multiACE] v2 bus %s: more than %d units, '
                                 'ignoring %s', self.path, MAX_UNITS,
                                 uid_hex(uid))
                    return None
                unit = Unit(uid)
                self.units[uid] = unit
                self._order.append(uid)
            return unit

    def _scan(self):
        try:
            self._scan_once()
        finally:
            self.first_scan_done.set()

    def _scan_once(self):
        self._last_scan = time.monotonic()
        uids = self._discover_burst()
        if not uids:
            return
        lone = (not self.addressed and len(uids) == 1
                and all(u.uid == uids[0] or not u.present
                        for u in self.units.values()))
        if lone:
            unit = self._known(uids[0])
            if unit is not None and not unit.present:
                unit.addr = 0
                unit.present = True
                unit.misses = 0
                logging.info('[multiACE] v2 bus %s: single unit %s at '
                             'address 0', self.path, uid_hex(unit.uid))
            return
        if not self.addressed:
            logging.info('[multiACE] v2 bus %s: %d units answer - switching '
                         'to addressed mode', self.path,
                         len(uids) + sum(1 for u in self.units.values()
                                         if u.present and u.uid not in uids))
            self.addressed = True
        # Self-thinning: assign one uid per round until address 0 is silent.
        for _ in range(MAX_UNITS * 3):
            if not uids:
                return
            unit = self._known(uids[0])
            if unit is None:
                return
            if not unit.addr:
                taken = {u.addr for u in self.units.values() if u is not unit}
                unit.addr = next(a for a in range(1, MAX_UNITS + 1)
                                 if a not in taken)
            # the ACK comes back on the OLD address (0): do not wait for it
            self._transact(0, self._next_seq(), CMD_ASSIGN,
                           assign_payload(unit.uid, unit.addr),
                           wait=PROBE_TIMEOUT_S, until_reply=False)
            seq = self._next_seq()
            ok = any(f['cmd'] == CMD_DISCOVER and uid_of(f['payload']) == unit.uid
                     for f in self._transact(unit.addr, seq, CMD_DISCOVER))
            if ok:
                unit.present = True
                unit.misses = 0
                unit.last_tx = time.monotonic()
                logging.info('[multiACE] v2 bus %s: unit %s at address %d',
                             self.path, uid_hex(unit.uid), unit.addr)
            # keep the others alive while the rest is enumerated
            for other in self.units.values():
                if (other.present and other is not unit
                        and time.monotonic() - other.last_tx >= KEEPALIVE_S):
                    s = self._next_seq()
                    self._request(other, build(0, s, CMD_GET_INFO), s,
                                  PROBE_TIMEOUT_S, internal=True)
            uids = self._discover_burst()


# ------------------------------------------------------------ module API

def _open_serial(path):
    import serial
    ser = serial.Serial()
    # short blocking reads: the bus thread waits on the wire, not spins
    ser.port, ser.baudrate, ser.timeout = path, BAUD, 0.01
    ser.open()
    return ser


def is_bus_path(path):
    return SEP in str(path or '')


def split_path(path):
    cable, _, hexuid = str(path).rpartition(SEP)
    return cable, hexuid


def bus_for(cable, serial_factory=None):
    with _buses_lock:
        bus = _buses.get(cable)
        if bus is None:
            bus = V2Bus(cable, serial_factory=serial_factory)
            _buses[cable] = bus
        return bus


def discover_units(cable_paths):
    """Virtual paths of the units currently on these cables. A bus seen for
    the first time in this process gets one bounded wait for its first
    enumeration (FIRST_SCAN_WAIT_S); every later call returns at once. A
    bus lives as long as the process, so a Klipper restart finds its units
    addressed and present immediately."""
    out = []
    for cable in cable_paths:
        bus = bus_for(cable)
        if not bus.first_scan_done.is_set():
            bus.first_scan_done.wait(FIRST_SCAN_WAIT_S)
        out += bus.present_paths()
    return out


def open_unit(path):
    cable, hexuid = split_path(path)
    return bus_for(cable).open_unit(hexuid)


def uid_key_for(path):
    _, hexuid = split_path(path)
    return 'uid:' + hexuid if hexuid else ''


def stop_all():
    with _buses_lock:
        buses = list(_buses.values())
        _buses.clear()
    for bus in buses:
        bus.stop()
