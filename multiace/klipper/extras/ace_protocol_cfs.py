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
# Discovery, identity, presence per slot and box state answer at once.
# Motions run as a job inside the link: the host request starts it and is
# answered right away, every later write() (the 1 Hz status heartbeat)
# advances it, and get_status reports 'busy' until it is done, which is
# what ace.py waits on. Measured commands (CLAUDE.md section 80):
#   feed_filament    long: unlock, 10 <mask> 00 (feeds ~44 mm/s until the
#                    box stops by itself), resumed when it stops early
#                    after real progress; short: 10 <mask> 05 <mm> (dosed,
#                    ~3.7 mm/s, the seat press)
#   stop_feed_filament  10 <mask> 02, the box is locked afterwards
#   unwind_filament  11 <mask> <mm> in pieces of at most 255 mm
#   start_feed_assist   04 <mask> 00, print mode: clutch free, the
#                    extruder pulls the spool (there is no feed assist)
#   locate_unlock    04 00 01 alone (ACE_LOCATE: the cam clacks)
# 04 00 01 unlocks the box (feed mode) before every motion.
#
# This module imports no ace.py symbol.

import json
import logging
import os
import queue
import struct
import time

from .ace_protocol import AceProtocol

HEAD = 0xF7
BROADCAST = 0xFF
STATUS_HOST = 0xFF
STATUS_ADDR = 0x00

F_MODE = 0x04              # 04 <mask> 00 print mode, 04 00 01 feed mode
F_PRESENCE = 0x08          # data [bank]; reply byte 0 = slot bit mask
F_BOX_STATE = 0x0A         # reply 4 bytes, last = box state
F_WHEEL = 0x0E             # data 01; reply 4 big-endian floats, mm
F_EXTRUDE = 0x10           # [mask][stage(][mm)]
F_RETRUDE = 0x11           # [mask][mm], mm 5..255 = retract that far
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
BOX_IDLE = 0x00
BOX_PRINT = 0x02
BOX_LOCKED = (0x03, 0x04)
BOX_SELF_STOP = 0x04
# Reply status of a feed the box ended by itself: 0x09 the wheel stood
# while the motor ran (resistance), 0x0A the maximum distance ran out.
SELF_STOP_STATUS = (0x09, 0x0A)
REPLY_REJECTED = 0x03

STAGE_INIT = 0x00
STAGE_STOP = 0x02
STAGE_DOSED = 0x05

# Measured on the CFS Nano: init feed and length retract ~44 mm/s, the
# dosed feed ~3.7 mm/s. Used for deadlines only; the box sets the pace.
FEED_SPEED = 44.0
DOSED_SPEED = 3.7
# feed_filament up to this length runs as a dosed feed (seat press,
# nudges); longer ones as the self-stopping init feed.
DOSED_MAX = 100
RETRACT_CHUNK_MAX = 255
RETRACT_CHUNK_MIN = 5
# Each retract piece is measured on the wheel (before/after): a piece
# that moved less than its length minus RETRACT_PIECE_TOL counts short;
# the missing distance is retracted again, at most RETRACT_TOPUPS_MAX
# times per job. A job that still misses more than RETRACT_SHORT_TOL
# (or that fraction of its length) reports 'short' in its motion.
RETRACT_PIECE_TOL = 5.0
RETRACT_TOPUPS_MAX = 2
RETRACT_SHORT_TOL = 10.0
RETRACT_SHORT_FRAC = 0.02
# The unlock clacks the channel cam: ~1.4 s on a loaded slot.
MODE_TIMEOUT = 3.0
MOTION_REPLY_TIMEOUT = 1.0
# An init feed that stopped early is restarted while it still made this
# much progress in its last segment (in a bowden it stopped after ~800 mm).
FEED_RESTART_MIN_MM = 30.0
FEED_RESTARTS_MAX = 4
# A retract piece that ends before the next job step (STEP_S, the 1 Hz
# status poll) is waited for on the bus, at most RETRACT_AWAIT_MAX, so the
# next piece starts right away (each wait delays one status answer).
STEP_S = 1.0
RETRACT_AWAIT_MARGIN = 0.7
RETRACT_AWAIT_MAX = 1.8
# The wheel moving less than this between two job steps (1 Hz) counts as
# standing; this many steps in a row = the feed has stopped.
FEED_FLAT_MM = 1.0
FEED_FLAT_TICKS = 2
FEED_START_GRACE_S = 5.0
# An unlock the box rejects = it is busy with its own work (the pull-in
# of a spool inserted meanwhile; one motor). A motion waits for it and
# get_status reports busy, for at most this long.
BOX_BUSY_MAX_S = 60.0

UNSUPPORTED = -2
FAILED = -1


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

    def __init__(self, raw, clock=None):
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
        # Clock for job deadlines; resolved per call so a test clock works.
        self._now = clock or (lambda: time.monotonic())
        self._late = {}             # func -> frame that arrived unasked
        self._job = None
        self._box_state = None      # last known, None = unknown
        self._mask = None           # last presence mask
        self._event = None          # insert event read by a motion step
        self._print_mask = None     # print mode wanted once the job ends
        self._motion_seq = 0
        self._last_motion = None    # status 'motion' after a job ended
        self._last_unlock = None    # 'ok' | 'rejected' | 'unanswered'
        self._reject_since = None   # first rejection of a run of them
        self._reject_logged = None
        self._last_start = None
        self._fresh_box = None      # 0x0A reply read during this write

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
        or addresses are kept in self._late (late motion replies)."""
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
                    if fr['func'] != func or (
                            addr != BROADCAST and fr['addr'] != addr):
                        # A reply the box sends when a motion ends (11, the
                        # dosed 10) arrives unasked: keep the latest per
                        # function for the job that waits on it.
                        self._late[fr['func']] = fr
                        continue
                    return fr
            if time.monotonic() >= deadline:
                return None

    def _await_late(self, func, timeout):
        """Listen without sending until the unasked reply of `func` is in
        self._late or `timeout` passes. Bounded by empty reads as well, like
        _transact. True when it arrived."""
        if func in self._late:
            return True
        deadline = self._now() + timeout
        misses = 0
        max_misses = int(timeout / max(READ_MISS_S, 0.001)) + 1
        while True:
            chunk = self._raw.read(1)
            if not chunk:
                misses += 1
                if misses >= max_misses:
                    return False
            else:
                n = self._raw.in_waiting
                if n:
                    chunk += self._raw.read(n)
                self._rx.extend(chunk)
                frames, bad = parse_frames(self._rx)
                self.bad_frames += bad
                for fr in frames:
                    self._late[fr['func']] = fr
                if func in self._late:
                    return True
                # Empty reads are bounded by count (one serial timeout
                # each); the clock only cuts a stream of other frames.
                if self._now() >= deadline:
                    return False

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

    # -- motion -----------------------------------------------------------

    def _fire(self, func, data):
        """Send a motion frame whose reply only comes when the motion ends;
        the job looks for it in self._late."""
        self._late.pop(func, None)
        self._raw.write(build_frame(self.addr, STATUS_HOST, func, data))

    def _read_box(self):
        """Box state from 0x0A, or None. An insert event reply (status
        0x30) is kept for the next get_status and gives no state."""
        fr = self._transact(self.addr, STATUS_HOST, F_BOX_STATE)
        self._fresh_box = fr
        if fr is None or not fr['data']:
            return None, fr
        if fr['status'] == INSERT_EVENT_STATUS:
            self._event = fr
            return None, fr
        self._box_state = bytearray(fr['data'])[-1]
        return self._box_state, fr

    def _read_wheel(self, slot):
        """Measuring wheel of `slot` in mm (a counter, only differences
        mean anything), None when the box answers empty (print mode, an
        init feed before its second stage)."""
        fr = self._transact(self.addr, STATUS_HOST, F_WHEEL, b'\x01')
        if fr is None or len(fr['data']) < 4 * (slot + 1):
            return None
        d = bytes(fr['data'])
        return struct.unpack('>f', d[4 * slot:4 * slot + 4])[0]

    def _feed_mode(self, force=False):
        """Unlock (04 00 01) unless the box is known idle (always with
        force). True when it stands idle afterwards."""
        if not force:
            st, _fr = self._read_box()
            if st == BOX_IDLE:
                return True
        fr = self._transact(self.addr, STATUS_HOST, F_MODE, b'\x00\x01',
                            timeout=MODE_TIMEOUT)
        if fr is None:
            self._last_unlock = 'unanswered'
            logging.info('[multiACE] CFS Nano: unlock (04 00 01) unanswered')
            return False
        if fr['status'] == REPLY_REJECTED:
            self._last_unlock = 'rejected'
            if self._reject_since is None:
                self._reject_since = self._now()
            st = bytearray(fr['data'])[0] if fr['data'] else None
            if st != self._reject_logged:
                # one line per box state, not per retry
                self._reject_logged = st
                logging.info('[multiACE] CFS Nano: unlock (04 00 01) rejected, '
                             'box state %s - waiting for the box'
                             % ('?' if st is None else '%02x' % st))
            return False
        if self._reject_since is not None:
            logging.info('[multiACE] CFS Nano: unlock accepted after %.1f s '
                         'of rejections' % (self._now() - self._reject_since))
        self._last_unlock = 'ok'
        self._reject_since = None
        self._reject_logged = None
        self._box_state = BOX_IDLE
        self._fresh_box = None
        return True

    def _box_busy(self):
        """True while unlocks are being rejected, at most BOX_BUSY_MAX_S."""
        return (self._last_unlock == 'rejected'
                and self._reject_since is not None
                and self._now() - self._reject_since < BOX_BUSY_MAX_S)

    def _note_reject(self, what, fr):
        """Log a rejected command once per box state (its data byte)."""
        st = bytearray(fr['data'])[0] if fr['data'] else None
        if self._reject_since is None:
            self._reject_since = self._now()
        if st != self._reject_logged:
            self._reject_logged = st
            logging.info('[multiACE] CFS Nano: %s rejected, box state %s - '
                         'waiting for the box'
                         % (what, '?' if st is None else '%02x' % st))

    def _wait_box(self, job, now):
        """Park a job until the box takes commands again."""
        if job.get('wait_unlock') is None:
            job['wait_unlock'] = now
        # the cap runs from the first rejection until a motion completes,
        # also across a piece the box takes and then rejects again
        job.setdefault('busy_since', now)

    def _begin_motion(self, job):
        """Send the first box command of a motion job. False when the box
        did not take it."""
        now = self._now()
        if job['kind'] == 'retract':
            self._fire_chunk(job)
            return True
        if job['kind'] == 'dosed':
            n = int(job['target'])
            job['t0'] = now
            job['deadline'] = now + n / DOSED_SPEED + 4.0
            self._fire(F_EXTRUDE, bytes(bytearray([job['mask'], STAGE_DOSED,
                                                   n])))
            return True
        job['deadline'] = now + job['target'] / FEED_SPEED * 1.5 + 20.0
        return self._start_segment(job)

    def _print_mode(self, mask):
        fr = self._transact(self.addr, STATUS_HOST, F_MODE,
                            bytes(bytearray([mask, 0x00])),
                            timeout=MODE_TIMEOUT)
        if fr is None or fr['status'] == REPLY_REJECTED:
            logging.info('[multiACE] CFS Nano: print mode (04 %02x 00) %s'
                         % (mask, 'unanswered' if fr is None else 'rejected'))
            return False
        self._box_state = BOX_PRINT
        self._print_mask = None
        self._fresh_box = None
        return True

    def _start_segment(self, job):
        """One self-stopping init feed. The second stage 10 <mask> 05 is
        rejected while the feed runs but makes the wheel readable."""
        m = job['mask']
        fr = self._transact(self.addr, STATUS_HOST, F_EXTRUDE,
                            bytes(bytearray([m, STAGE_INIT])),
                            timeout=MOTION_REPLY_TIMEOUT)
        if fr is None or fr['status'] != 0:
            self._last_start = 'unanswered' if fr is None else 'rejected'
            if fr is not None:
                self._note_reject('feed start', fr)
            return False
        self._last_start = 'ok'
        self._box_state = None
        self._transact(self.addr, STATUS_HOST, F_EXTRUDE,
                       bytes(bytearray([m, STAGE_DOSED])),
                       timeout=MOTION_REPLY_TIMEOUT)
        job['seg_w0'] = self._read_wheel(job['slot'])
        job['seg_t0'] = self._now()
        job['last_w'] = job['seg_w0']
        job['flat'] = 0
        return True

    def _stop_feed(self, job):
        fr = self._transact(self.addr, STATUS_HOST, F_EXTRUDE,
                            bytes(bytearray([job['mask'], STAGE_STOP])),
                            timeout=MOTION_REPLY_TIMEOUT)
        if fr is not None:
            self._box_state = 0x03      # a stop leaves the box locked
        return fr is not None

    def _fire_chunk(self, job):
        n = int(min(RETRACT_CHUNK_MAX, max(RETRACT_CHUNK_MIN,
                                           round(job['rem']))))
        job['rem'] = max(0.0, job['rem'] - n)
        job['chunk'] = n
        job['chunk_w0'] = self._read_wheel(job['slot'])
        job['chunk_eta'] = self._now() + n / FEED_SPEED
        job['chunk_end'] = job['chunk_eta'] + 4.0
        self._fire(F_RETRUDE, bytes(bytearray([job['mask'], n])))

    def _retract_piece_moved(self, job, fr):
        """Distance the finished retract piece really moved, from the
        wheel (it counts down on a retract). A short piece gives its
        missing part back to the job (bounded); an unreadable wheel counts
        the commanded length."""
        n = float(job['chunk'])
        w0 = job.pop('chunk_w0', None)
        w1 = self._read_wheel(job['slot'])
        status = 'none' if fr is None else '%02x' % fr['status']
        if w0 is None or w1 is None:
            job['unmeasured'] = job.get('unmeasured', 0.0) + n
            logging.info('[multiACE] CFS Nano: retract slot %d piece %.0f mm '
                         'not measured (wheel unreadable), reply %s'
                         % (job['slot'], n, status))
            return n
        moved = max(0.0, min(n, w0 - w1))
        if moved < n - RETRACT_PIECE_TOL:
            topup = (job.get('topups', 0) < RETRACT_TOPUPS_MAX
                     and not job.get('stopped'))
            logging.info('[multiACE] CFS Nano: retract slot %d piece %.0f mm '
                         'moved %.1f mm (wheel %.1f -> %.1f), reply %s%s'
                         % (job['slot'], n, moved, w0, w1, status,
                            ' - retracting the rest again' if topup else ''))
            if topup:
                job['topups'] = job.get('topups', 0) + 1
                job['rem'] += n - moved
        return moved

    @staticmethod
    def _retract_short(job):
        """Missing retract distance of a finished job, 0 within tolerance."""
        if job.get('stopped'):
            return 0.0
        target = float(job.get('target', 0.0))
        miss = target - float(job.get('done', 0.0))
        if miss > max(RETRACT_SHORT_TOL, target * RETRACT_SHORT_FRAC):
            return miss
        return 0.0

    def _end_job(self, why):
        job = self._job
        self._job = None
        self._last_motion = self._motion_of(job, False)
        logging.info('[multiACE] CFS Nano: %s slot %d ended (%s) after '
                     '%.0f mm' % (job['kind'], job['slot'], why,
                                  job.get('done', 0.0)))
        if self._print_mask is not None:
            self._print_mode(self._print_mask)

    @staticmethod
    def _motion_of(job, running):
        out = {'seq': int(job.get('seq', 0)), 'slot': int(job['slot']),
               'kind': str(job['kind']),
               'mm': round(float(job.get('done', 0.0)), 1),
               'running': bool(running)}
        if job.get('stop'):
            out['stop'] = str(job['stop'])
        if job['kind'] == 'retract':
            out['target'] = round(float(job.get('target', 0.0)), 1)
            if not running:
                out['short'] = round(CfsLink._retract_short(job), 1)
        return out

    def _segment_progress(self, job):
        """Progress of the running init segment in mm, and the wheel
        reading it came from (None = estimated from the time)."""
        w = self._read_wheel(job['slot'])
        if w is not None and job.get('seg_w0') is not None:
            return max(0.0, w - job['seg_w0']), w
        # No wheel reading: estimate from the time at the measured pace.
        return (self._now() - job['seg_t0']) * FEED_SPEED, None

    def _tick(self):
        """Advance the running job by one step (called per write)."""
        job = self._job
        now = self._now()
        if job.get('wait_unlock') is not None:
            # The box rejected the unlock (busy with its own work): retry
            # each step until it takes it, then start or resume.
            if self._feed_mode(force=True):
                resuming = job.get('resuming', False)
                self._last_start = 'ok'
                ok = (self._start_segment(job) if resuming
                      else self._begin_motion(job))
                if ok:
                    waited = now - job.pop('wait_unlock')
                    job.pop('resuming', None)
                    if resuming:
                        job['deadline'] += waited
                elif (self._last_start != 'rejected'
                      or now - job['busy_since'] >= BOX_BUSY_MAX_S):
                    self._end_job('start %s' % self._last_start)
                # rejected again: keep waiting
            elif (self._last_unlock != 'rejected'
                  or now - job['busy_since'] >= BOX_BUSY_MAX_S):
                self._end_job('box busy, unlock %s' % self._last_unlock)
            return
        if job['kind'] == 'feed':
            st, fr = self._read_box()
            prog, w = self._segment_progress(job)
            job['done'] = job['acc'] + prog
            # The wheel standing still is the box's own stop as well: the
            # state it reports after a stop in a bowden is not settled.
            # A restarted init can take a few seconds to get going (HW
            # 2026-10-31: still after 2 s, moving a moment later), so the
            # wheel only counts as standing once the segment is past that.
            if (w is not None and job.get('last_w') is not None
                    and now >= job['seg_t0'] + FEED_START_GRACE_S):
                job['flat'] = (job.get('flat', 0) + 1
                               if abs(w - job['last_w']) < FEED_FLAT_MM else 0)
            job['last_w'] = w
            seen = (st, None if fr is None else fr['status'])
            if seen != job.get('seen'):
                job['seen'] = seen
                logging.info('[multiACE] CFS Nano: feed slot %d box state=%s '
                             'status=%s wheel=%s at %.0f mm'
                             % (job['slot'],
                                '?' if st is None else '%02x' % st,
                                '?' if fr is None else '%02x' % fr['status'],
                                '-' if w is None else '%.1f' % w,
                                job['done']))
            stopped = (st == BOX_SELF_STOP or (
                fr is not None and fr['status'] in SELF_STOP_STATUS)
                or job.get('flat', 0) >= FEED_FLAT_TICKS)
            if job['done'] >= job['target']:
                self._stop_feed(job)
                self._end_job('length reached')
            elif stopped:
                if (prog >= FEED_RESTART_MIN_MM
                        and job['restarts'] < FEED_RESTARTS_MAX
                        and job['done'] < job['target'] - FEED_RESTART_MIN_MM):
                    job['acc'] = job['done']
                    job['restarts'] += 1
                    logging.info('[multiACE] CFS Nano: feed slot %d stopped '
                                 'by the box after %.0f mm - resuming (%d)'
                                 % (job['slot'], job['done'],
                                    job['restarts']))
                    if not self._feed_mode(force=True):
                        if self._last_unlock == 'rejected':
                            self._wait_box(job, now)
                            job['resuming'] = True
                        else:
                            self._end_job('resume failed')
                    elif not self._start_segment(job):
                        if self._last_start == 'rejected':
                            self._wait_box(job, now)
                            job['resuming'] = True
                        else:
                            self._end_job('resume failed')
                else:
                    # Why the box stopped, for the load-fail wording: the
                    # wheel stood while the motor ran (status 09 or a flat
                    # wheel) = slip / tangle at the box; 0A = the distance
                    # ran out with the filament moving = path downstream.
                    job['stop'] = ('slip' if (job.get('flat', 0) >= FEED_FLAT_TICKS
                                              or (fr is not None
                                                  and fr['status'] == 0x09))
                                   else 'distance')
                    self._end_job('stopped by the box, status %s%s'
                                  % ('?' if fr is None
                                     else '%02x' % fr['status'],
                                     ', wheel still'
                                     if job.get('flat', 0) >= FEED_FLAT_TICKS
                                     else ''))
            elif now >= job['deadline']:
                self._stop_feed(job)
                self._end_job('timeout')
        elif job['kind'] == 'dosed':
            fr = self._late.get(F_EXTRUDE)
            if fr is not None and fr['status'] == REPLY_REJECTED:
                self._late.pop(F_EXTRUDE, None)
                self._note_reject('dosed feed', fr)
                self._wait_box(job, now)
                return
            if fr is not None or now >= job['deadline']:
                self._late.pop(F_EXTRUDE, None)
                self._box_state = BOX_IDLE
                job['done'] = job['target']
                self._end_job('done')
        else:
            # The piece ends before the next step: stay on the bus for its
            # reply and send the next piece at once instead of a step later.
            if (F_RETRUDE not in self._late
                    and now + STEP_S >= job.get('chunk_eta', now + 1e9)):
                self._await_late(F_RETRUDE, min(
                    RETRACT_AWAIT_MAX,
                    max(0.0, job['chunk_eta'] - now) + RETRACT_AWAIT_MARGIN))
                now = self._now()
            fr = self._late.get(F_RETRUDE)
            if fr is not None or now >= job['chunk_end']:
                self._late.pop(F_RETRUDE, None)
                if fr is not None and fr['status'] == REPLY_REJECTED:
                    # the piece did not run: give it back and wait
                    job['rem'] += job['chunk']
                    job['chunk'] = 0
                    job.pop('chunk_eta', None)
                    self._note_reject('retract piece', fr)
                    self._wait_box(job, now)
                    return
                job['done'] += self._retract_piece_moved(job, fr)
                job.pop('busy_since', None)
                self._box_state = BOX_IDLE
                if job['rem'] > 0:
                    self._fire_chunk(job)
                else:
                    short = self._retract_short(job)
                    self._end_job('short by %.0f mm' % short if short
                                  else 'done')

    def _motion(self, method, params, rid):
        def reply(code, msg='success'):
            return {'id': rid, 'code': code, 'msg': msg, 'result': {}}
        if method in ('stop_feed_filament', 'stop_unwind_filament'):
            job = self._job
            if job is None:
                return reply(0)
            if job.get('wait_unlock') is not None:
                # nothing runs yet: drop the job, no box command
                self._end_job('stopped by the host')
                return reply(0)
            if job['kind'] == 'retract':
                job['rem'] = 0.0        # the running piece ends by itself
                job['stopped'] = True   # no top-up, not short
                return reply(0)
            if method == 'stop_unwind_filament':
                return reply(0)
            if not self._stop_feed(job):
                return reply(FAILED, 'stop unanswered')
            if job['kind'] == 'feed':
                job['done'] = job['acc'] + self._segment_progress(job)[0]
            elif job['kind'] == 'dosed':
                # no wheel reply for a dosed feed: estimate at its pace
                job['done'] = min(job['target'],
                                  (self._now() - job['t0']) * DOSED_SPEED)
            self._end_job('stopped by the host')
            return reply(0)
        if method in ('stop_feed_assist', 'update_feeding_speed',
                      'update_unwinding_speed'):
            if method == 'stop_feed_assist':
                self._print_mask = None
            return reply(0)
        try:
            slot = int(params.get('index', -1))
        except (TypeError, ValueError):
            slot = -1
        if not 0 <= slot <= 3:
            return reply(FAILED, 'bad index')
        mask = 1 << slot
        if method == 'start_feed_assist':
            # No feed assist: print mode frees the clutch so the extruder
            # pulls the spool. During a motion it follows when that ends.
            if self._job is not None:
                self._print_mask = mask
                return reply(0)
            if self._box_state == BOX_PRINT and self._print_mask is None:
                return reply(0)
            if not self._print_mode(mask):
                return reply(FAILED, 'print mode unanswered')
            return reply(0)
        if self._job is not None:
            return reply(1, 'FORBIDDEN')
        try:
            length = float(params.get('length', 0))
        except (TypeError, ValueError):
            length = 0.0
        if length <= 0:
            return reply(FAILED, 'bad length')
        if method == 'unwind_filament':
            job = {'kind': 'retract', 'slot': slot, 'mask': mask,
                   'rem': length, 'target': length, 'done': 0.0}
        elif length <= DOSED_MAX:
            job = {'kind': 'dosed', 'slot': slot, 'mask': mask,
                   'target': float(int(min(255, max(1, round(length))))),
                   'done': 0.0}
        else:
            job = {'kind': 'feed', 'slot': slot, 'mask': mask,
                   'target': length, 'acc': 0.0, 'done': 0.0, 'restarts': 0}
        self._motion_seq += 1
        job['seq'] = self._motion_seq
        if not self._feed_mode():
            if self._last_unlock != 'rejected':
                return reply(FAILED, 'unlock failed')
            # Busy box: the job waits (status busy) and starts once the
            # box takes the unlock.
            self._wait_box(job, self._now())
            self._job = job
            return reply(0)
        self._last_start = 'ok'
        if not self._begin_motion(job):
            if self._last_start != 'rejected':
                return reply(FAILED, 'feed start unanswered')
            self._wait_box(job, self._now())
        self._job = job
        return reply(0)

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
        if method not in ('get_info', 'get_status',
                          'locate_unlock') + MOTION_METHODS:
            return {'id': rid, 'code': UNSUPPORTED,
                    'msg': 'unsupported:%s' % method, 'result': {}}
        if self.addr is None:
            if not self._identify():
                return None
        self._fresh_box = None
        if self._job is not None:
            try:
                self._tick()
            except (IOError, OSError):
                raise
            except Exception:
                logging.exception('[multiACE] CFS Nano: job step failed')
                self._job = None
        if method == 'get_info':
            fr = self._transact(self.addr, STATUS_HOST, F_VERSION)
            fw = ''
            if fr is not None:
                fw = bytes(fr['data']).decode('ascii', 'replace').strip(
                    '\x00 ')
            return ok({'model': MODEL, 'firmware': fw or 'Unknown',
                       'dev_type': self.dev_type})
        if method == 'get_status':
            return self._status(ok)
        if method == 'locate_unlock':
            if self._job is not None:
                return {'id': rid, 'code': 1, 'msg': 'FORBIDDEN',
                        'result': {}}
            if not self._feed_mode(force=True):
                return {'id': rid, 'code': FAILED,
                        'msg': 'unlock %s' % self._last_unlock, 'result': {}}
            return ok({})
        return self._motion(method, request.get('params') or {}, rid)

    def _status(self, ok):
        if self._job is None and self._box_state in BOX_LOCKED:
            # A stop or a self-stop locks the box, and a locked box takes
            # no new spool in: unlock once nothing runs.
            self._feed_mode()
        pres = self._transact(self.addr, STATUS_HOST, F_PRESENCE, b'\x00')
        if pres is None:
            # No fresh presence = no answer (contract 5.3): the watchdog
            # stays the only judge of a silent box.
            return None
        if pres['data']:
            self._mask = bytearray(pres['data'])[0]
        elif self._mask is None:
            return None
        # An empty presence reply (print mode answers some queries empty)
        # keeps the last mask: the box did answer.
        st, self._event = self._event, None
        if st is None:
            # A job step may have read the box this write already.
            st = self._fresh_box
            if st is None:
                _state, st = self._read_box()
                self._event = None      # an event read here is shown now
        out = make_status(self._mask, st)
        # Distance of the running (or last) motion: the calibration reads
        # it instead of guessing from the time.
        if self._job is not None:
            out['motion'] = self._motion_of(self._job, True)
        elif self._last_motion is not None:
            out['motion'] = dict(self._last_motion)
        if self._job is not None or self._box_busy():
            out['status'] = 'busy'
        return ok(out)


MOTION_METHODS = ('feed_filament', 'unwind_filament', 'stop_feed_filament',
                  'stop_unwind_filament', 'start_feed_assist',
                  'stop_feed_assist', 'update_feeding_speed',
                  'update_unwinding_speed')


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
        # Long motions (a 2 m retract runs ~50 s) report 'busy' throughout.
        'busy_wait_floor': True,
        'fa_monitor': False,
        'needs_fa_prestop': False,
        'has_rollback_assist': False,
        # Print mode freewheels: the extruder pulls the spool itself.
        'no_freewheel': False,
        # start_feed_assist switches print mode = clutch free; sent at the
        # start of an unload so the tip-form pull runs free as well
        'freewheel_print_mode': True,
        # No LED command: an unlock (04 00 01) makes the cam pick the
        # channel, which clacks without moving filament.
        'locate_method': 'unlock',
        # Short feeds run dosed (~3.7 mm/s) and the box ignores the speed,
        # so the calibration feeds once to the sensor and reads the
        # distance from the status (motion.mm, from the wheel).
        'calibration_feed': 'single',
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
