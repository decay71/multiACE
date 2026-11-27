# ACE 2 Pro running the community firmware ace2k (driver 'ace2k').
#
# ace2k (https://github.com/tobecwb/ace2k, GPL-3.0) turns the ACE 2 Pro
# into a Klipper MCU: it speaks Klipper's own MCU protocol at 250000
# baud, with its command set in the data dictionary the unit hands out
# (identify). multiACE does NOT load ace2k's host modules ([mcu ace2k] +
# [ace2k]): a lost USB link would shut the printer down there. It opens
# its own session to the unit instead (UNIT_DRIVER_DESIGN, decision D1,
# "Weg B"), so a lost link runs through the usual reconnect ladder.
#
# Like the CFS driver, the codec does not frame host requests on the
# wire. open_transport returns an Ace2kLink, a pyserial-shaped object:
# ace.py's writer thread hands it one host request per write(), the link
# runs the Klipper exchange(s) in that call and queues one host
# response, which the reader thread reads back. Every write also drains
# the reports the unit sent since the last one and sends a keepalive
# (the unit stops a running lane after 3 s without a valid host frame).
#
# Connect: the link reads the dictionary, sends the link proof
# (ace2k_version: an image flashed and never asked falls back into the
# bootloader's recovery after 180 s), turns the unit's automatic load off
# (auto_load is on in the firmware: a tip pushed into an idle lane would
# be pulled in by itself) and asks for the periodic reports it builds the
# status from: insert sensors (presence per lane), the feed state, the
# chamber temperature and humidity, the tag states.
#
# Motion (M6b). Moves run as a job per lane inside the link, like the CFS
# driver: the host request starts it and is answered at once, every later
# write() (the 1 Hz status heartbeat) reads the unit's feed events and
# advances it, and get_status reports 'busy' while a job runs, which is
# what ace.py waits on. The unit ends every move with one ace2k_feed_event
# (its kind and the encoder travel); the status carries the last motion
# (seq, slot, kind, mm, stop) like the CFS one.
#   feed_filament     ace2k_feed_start mode 0 (feed); a long one (the load
#                     push through the bowden) with free_tip, a short one
#                     (the seat press into the gears) without; pieces of
#                     at most ACE2K_LANE_MOVE_MAX_UM
#   unwind_filament   mode 1 (rollback), in pieces as well
#   stop_feed_filament / stop_unwind_filament   ace2k_feed_stop lane
#   start_feed_assist mode 6, the follow (both directions, from the
#                     buffer's plunger): the drive gear does not freewheel,
#                     so the extruder can only pull with the follow armed
#   stop_feed_assist  ace2k_feed_stop on a following lane
# A move that ends in an error kind (stuck, tangled, motor_stalled,
# timeout, unload_incomplete) latches the lane in error; the link clears
# it (ace2k_feed_clear) so the next command runs, and reports the kind.
# The dryer runs in the unit; the host starts, stops and reads it.
#
# Tags (M6d). The unit reads a tag only while its lane moves: a move that
# carries a tag past the reader opens a session (ace2k_rfid_tag), and the
# host must answer with read steps (ace2k_rfid_step) within 500 ms, or the
# unit closes it. The link serves sessions after every host request AND
# from the reader thread's idle polls (under one lock), so a session that
# opens between two requests is answered in time. The NTAG layouts are
# multiACE's own (Anycubic and OpenSpool, ace_rc522), so a spool reads the
# same on ace2k as on ACE2-Open; the MIFARE brands Bambu Lab and Snapmaker
# follow ace2k. A decoded read lands in the status as slot 'tag_read' (with
# a counter); ace.py takes it through the host-read ingest (display,
# spool binding, persistence). tag_read_load runs the unit's own load on an
# insert (ace2k_feed_start mode 5: settle, pull to the parking distance,
# search past it, back to the parking distance); tag_read_motion runs the
# read with motion (forward up to the search reach, then back to where it
# started) for ACE_TAG_READ, where the filament already sits in the lane.
# A read with motion from a bare insert came back to the insert sensor and
# dropped the filament out. The firmware cannot write tags.
#
# Unit key: the unit reports the STM32's 96-bit device id (ace2k_uid_query,
# also answered in a Klipper shutdown). Read as three little-endian words
# it is the same 'uid:...' key a stock ACE 2 reports in discover_device, so
# place, name and the parked per-unit state stay with the device across a
# reflash between stock and ace2k. The key is read by a probe before the
# claim (the place table needs it before the connect).
#
# Taken from ace2k with attribution (GPL-3.0, like multiACE):
# - the Klipper block framing, CRC, VLQ and the ask/acknowledge rules of
#   firmware/tools/ace2k_flash/klipper.py;
# - the feed mode / event / refusal names of klippy/extras/ace2k_feed_proto.py;
# - the message names and field layouts of docs/protocol.md;
# - the tag session rules (docs/protocol.md "RFID sessions"), the Bambu Lab
#   and Snapmaker layouts and key derivations of klippy/extras/ace2k_tags/
#   (bambu.py, snapmaker.py, crypto.py), themselves after the
#   RFID-Tag-Guide of Bambu-Research-Group and Snapmaker's u1-klipper.
# The message format handling follows Klipper's msgproto.py.
#
# This module imports no ace.py symbol.

import hashlib
import hmac
import json
import logging
import os
import queue
import struct
import threading
import time
import zlib

from .ace_protocol import AceProtocol

# --- Klipper's MCU protocol (from ace2k's flasher, klipper.py) ------------

ACE2K_BAUD = 250000
KLIPPER_SYNC = 0x7E
KLIPPER_DEST = 0x10
KLIPPER_SEQ_MASK = 0x0F
KLIPPER_MIN = 5
KLIPPER_MAX = 64
IDENTIFY_RESPONSE_ID = 0
IDENTIFY_ID = 1
IDENTIFY_CHUNK = 40
DICTIONARY_MAX = 65536
VLQ_GROUPS = (
    (28, -0x4000000, 0xC000000),
    (21, -0x80000, 0x180000),
    (14, -0x1000, 0x3000),
    (7, -0x20, 0x60),
)
VLQ_SIGN_BITS = 0x60


def klipper_crc(data):
    """Klipper's crc16_ccitt over the header and payload of a block."""
    crc = 0xFFFF
    for byte in bytearray(data):
        b = byte ^ (crc & 0xFF)
        b ^= (b << 4) & 0xFF
        crc = ((b << 8) | (crc >> 8)) ^ (b >> 4) ^ (b << 3)
        crc &= 0xFFFF
    return crc


def klipper_vlq(value):
    """Klipper's variable-length encoding of an integer."""
    out = bytearray()
    for shift, low, high in VLQ_GROUPS:
        if value >= high or value < low:
            out.append((value >> shift) & 0x7F | 0x80)
    out.append(value & 0x7F)
    return bytes(out)


def parse_klipper_vlq(data, pos):
    """(unsigned 32-bit value, position after it). IndexError when the
    data ends inside the VLQ."""
    c = data[pos]
    pos += 1
    value = c & 0x7F
    if (c & VLQ_SIGN_BITS) == VLQ_SIGN_BITS:
        value |= -0x20
    while c & 0x80:
        c = data[pos]
        pos += 1
        value = (value << 7) | (c & 0x7F)
    return value & 0xFFFFFFFF, pos


def klipper_block(seq, payload):
    """A block with sequence number seq that carries payload."""
    head = bytes(bytearray([KLIPPER_MIN + len(payload),
                            (seq & KLIPPER_SEQ_MASK) | KLIPPER_DEST])) \
        + bytes(payload)
    crc = klipper_crc(head)
    return head + bytes(bytearray([crc >> 8, crc & 0xFF, KLIPPER_SYNC]))


def parse_klipper_blocks(buf):
    """The valid blocks at the front of buf as [(seq, payload)], removed
    from buf. A byte that cannot start a valid block is dropped."""
    blocks = []
    while buf:
        if buf[0] == KLIPPER_SYNC:
            del buf[0]
            continue
        n = buf[0]
        if n < KLIPPER_MIN or n > KLIPPER_MAX:
            del buf[0]
            continue
        if len(buf) < n:
            break
        block = bytes(buf[:n])
        b = bytearray(block)
        crc = (b[-3] << 8) | b[-2]
        ok = (b[-1] == KLIPPER_SYNC
              and (b[1] & ~KLIPPER_SEQ_MASK & 0xFF) == KLIPPER_DEST)
        if not ok or klipper_crc(block[:-3]) != crc:
            del buf[0]
            continue
        del buf[:n]
        blocks.append((b[1] & KLIPPER_SEQ_MASK, block[2:-3]))
    return blocks


# --- message formats (after Klipper's msgproto.py) ------------------------

_INT_TYPES = ('%u', '%i', '%hu', '%hi', '%c')
_BUF_TYPES = ('%s', '%*s', '%.*s')


def parse_format(fmt):
    """'name a=%u b=%*s' -> ('name', [('a', '%u'), ('b', '%*s')])."""
    parts = str(fmt).split()
    if not parts:
        return '', []
    params = []
    for part in parts[1:]:
        if '=' not in part:
            continue
        key, typ = part.split('=', 1)
        params.append((key, typ))
    return parts[0], params


def _int_of(raw, typ):
    if typ == '%i':
        return raw - (1 << 32) if raw & 0x80000000 else raw
    if typ == '%hu':
        return raw & 0xFFFF
    if typ == '%hi':
        v = raw & 0xFFFF
        return v - 0x10000 if v & 0x8000 else v
    if typ == '%c':
        return raw & 0xFF
    return raw


def decode_params(params, data, pos):
    """Decode the parameters of one message; returns (dict, new pos)."""
    out = {}
    for key, typ in params:
        if typ in _BUF_TYPES:
            n = bytearray(data)[pos]
            pos += 1
            out[key] = bytes(data[pos:pos + n])
            if len(out[key]) != n:
                raise IndexError('buffer cut short')
            pos += n
        else:
            raw, pos = parse_klipper_vlq(data, pos)
            out[key] = _int_of(raw, typ)
    return out, pos


def encode_params(params, values):
    out = bytearray()
    for key, typ in params:
        v = values[key]
        if typ in _BUF_TYPES:
            v = bytes(v)
            out.append(len(v))
            out.extend(v)
        else:
            out.extend(klipper_vlq(int(v)))
    return bytes(out)


class Dictionary(object):
    """The commands and responses of one unit's data dictionary, looked
    up by message NAME (the first word): the formats travel with the
    image, so the field lists are read from it, never assumed."""

    def __init__(self, data):
        self.raw = data or {}
        self.commands = {}      # name -> (id, params)
        self.responses = {}     # id -> (name, params)
        self.response_ids = {}  # name -> id
        for fmt, mid in (self.raw.get('commands') or {}).items():
            name, params = parse_format(fmt)
            self.commands[name] = (int(mid), params)
        for section in ('responses', 'output'):
            for fmt, mid in (self.raw.get(section) or {}).items():
                name, params = parse_format(fmt)
                self.responses[int(mid)] = (name, params)
                self.response_ids[name] = int(mid)
        self.config = dict(self.raw.get('config') or {})
        self.static_strings = {}
        enums = self.raw.get('enumerations') or {}
        for text, sid in (enums.get('static_string_id') or {}).items():
            try:
                self.static_strings[int(sid)] = str(text)
            except (TypeError, ValueError):
                pass

    def has(self, name):
        return name in self.commands

    def encode(self, name, **values):
        mid, params = self.commands[name]
        return klipper_vlq(mid) + encode_params(params, values)

    def decode(self, body):
        """All messages of one block: [(name, params dict)]. Unknown ids
        end the block (their length cannot be known)."""
        out = []
        pos = 0
        while pos < len(body):
            try:
                mid, pos = parse_klipper_vlq(body, pos)
            except IndexError:
                break
            if mid & 0x80000000:
                mid -= 1 << 32
            entry = self.responses.get(mid)
            if entry is None:
                break
            name, params = entry
            try:
                vals, pos = decode_params(params, body, pos)
            except IndexError:
                break
            out.append((name, vals))
        return out

    def constant(self, key, default=None):
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default


# --- ace2k vocabulary (from ace2k_feed_proto.py and protocol.md) ----------

MODE_NAMES = {
    0: 'idle', 1: 'feeding', 2: 'rolling_back', 3: 'unloading',
    4: 'loading', 5: 'assisting', 6: 'assisting_back', 7: 'error',
    8: 'following',
}
MOVING_MODES = (1, 2, 3, 4)
MOVING_MODE_NAMES = tuple(MODE_NAMES[m] for m in MOVING_MODES)
TAG_STATES = {0: 'unknown', 1: 'pending', 2: 'searching', 3: 'reading',
              4: 'read', 5: 'no_tag'}
REQUIRED_COMMANDS = ('ace2k_version', 'ace2k_sensors_query',
                     'ace2k_feed_query')
MODEL = 'ACE 2 Pro (ace2k)'

# Report rates (seconds between reports). The unit's own host asks the
# sensors at 10 Hz; the 1 Hz heartbeat reads them once a second, and an
# insert edge arrives as its own event in between.
REPORT_PERIODS = (
    ('ace2k_sensors_query', 1.0),
    ('ace2k_feed_query', 1.0),
    ('ace2k_env_query', 1.0),
    ('ace2k_rfid_query', 1.0),
    ('ace2k_lane_counters_query', 1.0),
    ('ace2k_dryer_query', 1.0),
)
# Seconds of a dryer cycle that are logged once a second (_dryer_trace).
DRYER_TRACE_S = 120
# The unit's dryer (protocol.md, "The dryer"): it runs and protects a cycle
# on its own, the host starts and stops it.
DRYER_STATES = {0: 'idle', 1: 'starting', 2: 'heating', 3: 'cooldown',
                4: 'fault'}
DRYER_RUNNING = (1, 2)
DRYER_GAIN_SOURCES = {0: 'none', 1: 'learning', 2: 'learned', 3: 'stored'}
# The unit learns its heater rate in the first 60 s of a cycle.
DRYER_GAIN_ASK_S = 65.0
DRYER_FAULTS = {1: 'not_heating', 2: 'response', 3: 'sides',
                4: 'chamber_over', 5: 'chamber_stale', 6: 'ntc', 7: 'mains',
                8: 'cutout', 9: 'heat', 10: 'fans', 11: 'flaps', 12: 'lid'}
DRYER_REFUSALS = {1: 'busy', 2: 'range', 3: 'faulted', 4: 'sensors',
                  5: 'no_mains', 6: 'cutout', 7: 'not_cool', 8: 'shutdown',
                  9: 'measuring'}
DRYER_EVENTS = {0: 'done', 1: 'stopped', 2: 'fault', 3: 'lowered',
                4: 'interrupted', 5: 'hot_ambient', 6: 'vented',
                7: 'cleared', 8: 'auto_started', 9: 'auto_dry',
                10: 'auto_failed', 11: 'auto_stopped', 12: 'auto_off',
                13: 'stopped_link'}
DRYER_METHODS = ('drying', 'drying_stop')
# The firmware's own load parameters, re-sent with auto_load off. The read
# with motion runs at the load speed (ace2k's own default 60 mm/s).
LOAD_PARK_UM = 300000
LOAD_SPEED_UM_S = 60000
# A report older than this is not fresh (status rule 5.3).
REPORT_FRESH_S = 3.5

# Motion. The unit takes speeds within its ACE2K_LANE_SPEED_* bounds and
# lengths up to ACE2K_LANE_MOVE_MAX_UM (read from the dictionary; these are
# the v0.13.0 values, used when an image does not declare them).
SPEED_MIN_MM_S = 9.0
SPEED_MAX_MM_S = 70.0
MOVE_MAX_MM = 2000.0
# The follow's speed (ace2k's own default assist_speed).
FOLLOW_SPEED_MM_S = 50.0
# A feed longer than this is the load push through the bowden (free_tip:
# nothing holds the front tip); a shorter one is a press into the gears.
FREE_TIP_MIN_MM = 100.0
# A piece that ends done with less than this left ends the job.
REMAIN_MIN_MM = 2.0
# A feed that ends blocked or in a fault with less filament travel than
# this did not get the filament out of the unit: slip at the MMU.
SLIP_MOVED_MM = 30.0
# A retract that comes back more than this short is reported 'short'.
RETRACT_SHORT_TOL_MM = 10.0
# relax_buffer (buffer_relax cap): a rollback armed with the unit's at-rest
# and held stops (ace2k_feed_stop_when, v0.13.0+). It takes the slack out
# of a lane whose plunger stands at its pulled end (buffer full) and ends
# as soon as the plunger reads rest, or when the tip is held ahead. The
# four pulled switches share one input, so a lane left full pauses the
# forward assist of every other lane on the unit.
RELAX_END_KINDS = frozenset(('at_rest', 'held'))
# Full-buffer watch: a resting lane that holds the shared pulled reading
# pauses the follow of every other lane of the unit. After this long the
# link rolls it back itself (bounded, the unit ends it at rest or held),
# once per full episode.
RELAX_WATCH_S = 3.0
RELAX_WATCH_MM = 40.0
RELAX_WATCH_SPEED_MM_S = 15.0
# Stuck-plunger watch: a following lane answers its own full buffer by
# taking filament back. A plunger that hangs at the full end keeps that
# going until the strand is out of the toolhead. A lane that stays full
# this long although the follow already took back this often is stopped.
# Lanes in use and lanes set by hand (ace2k v0.15.0, host interface 29).
# The unit sorts out a lane whose tag it does not know by moving it a
# little (the tie-break), but only while the host keeps telling it which
# lanes are in use: the mask goes out on every change and every second (a
# mask older than 3 s is no mask). A lane the user named by hand is marked
# so the reader gives it no tag. A start the unit refuses as busy while
# its own tie-break runs is sent again for up to START_BUSY_RETRY_S.
IN_USE_PERIOD_S = 1.0
BY_HAND_EMPTY_DROP_S = 5.0
START_BUSY_RETRY_S = 30.0
FOLLOW_FULL_S = 5.0
FOLLOW_FULL_BURSTS = 4
# A piece whose end event never came: ended once the report shows the
# lane idle this long after the start, or at its deadline (expected time
# x 1.5 + DEADLINE_EXTRA_S).
LOST_EVENT_S = 2.5
# Once the report shows the lane idle without an end event: wait this long
# for the event (an image before 2026-11-12 can send the idle report first)
# and for the lane encoder report.
LOST_ENCODER_WAIT_S = 1.5
DEADLINE_EXTRA_S = 10.0
STOP_EVENT_WAIT_S = 0.3
FEED_MODES = {'feed': 0, 'retract': 1, 'load': 5, 'follow': 6}
# Bits of the sensors report's switches word (ace2k lane/sensors.h): insert
# 0-3, rest 4-7, pushed (taut) 8-11, and the shared pulled reading (a buffer
# full on some lane: the four switches are wired-OR onto one input).
SW_REST_SHIFT = 4
SW_PUSHED_SHIFT = 8
SW_PULLED_BIT = 12
# The follow instrument's window (seconds).
FOLLOW_LOG_WINDOW_S = 10.0
# Lane modes of a running follow in the state report (8 following; the
# one-way assists 5 and 6 never come from this link).
FOLLOW_MODES = (5, 6, 8)
KIND_NAMES = {
    0: 'done', 1: 'stopped', 2: 'stopped_link', 3: 'stopped_shutdown',
    4: 'runout', 5: 'loaded', 6: 'unloaded', 7: 'stuck', 8: 'tangled',
    9: 'motor_stalled', 10: 'timeout', 11: 'assist_stall',
    12: 'assist_overrun', 13: 'unload_incomplete', 14: 'behind', 15: 'snag',
    16: 'tail', 17: 'tail_out', 18: 'blocked', 19: 'held', 20: 'at_rest',
    21: 'stopped_sensors',
}
ERROR_KINDS = frozenset(range(7, 14))
NOTICE_KINDS = frozenset((14, 15, 16))
REFUSAL_NAMES = {
    0: 'ok', 1: 'busy', 2: 'in_error', 3: 'no_filament', 4: 'no_link',
    5: 'bounds', 6: 'other_lane', 7: 'no_move', 8: 'following',
    9: 'sensors_stale',
}
MOTION_METHODS = ('feed_filament', 'unwind_filament', 'stop_feed_filament',
                  'stop_unwind_filament', 'start_feed_assist',
                  'stop_feed_assist', 'update_feeding_speed',
                  'update_unwinding_speed')

# Tags (M6d). The read with motion's reach (filament travel forward
# before it turns back); ace2k starts from 750 mm, multiACE's own insert
# sweep on ACE2-Open goes 700.
TAG_SEARCH_UM = 700000
# A step with no data frame within this is given up (the unit's own idle
# timer is 500 ms from the last step).
TAG_STEP_TIMEOUT_S = 0.5
# Allowance for the sessions' pauses on top of the search's travel.
TAG_SEARCH_EXTRA_S = 30.0
TAG_SESSION_MAX_STEPS = 24
SAK_NTAG = 0x00
SAK_MIFARE_1K = 0x08
NTAG_UID_BYTES = 7
# The Bambu Lab keys: HKDF-SHA256 over the first four UID bytes, this salt
# and context ("RFID-A" and a NUL); the Snapmaker keys: HMAC-SHA256 with
# this salt. Published by Bambu-Research-Group and in Snapmaker's
# u1-klipper; taken from ace2k's config/ace2k_tags.cfg.
BAMBU_HKDF_SALT = bytes(bytearray.fromhex('9a759cf2c4f7caff222cb9769b41bc96'))
BAMBU_HKDF_INFO = bytes(bytearray.fromhex('524649442d4100'))
SNAPMAKER_SALT_A = b'Snapmaker_qwertyuiop[,.;]'

ASK_TIMEOUT = 0.5
PIECE_TIMEOUT = 0.3
DICTIONARY_DEADLINE_S = 20.0
# The pre-claim probe (dictionary + uid) must finish inside the caller's
# wait (PROBE_DEADLINE_S on the codec, the reactor runs meanwhile).
PROBE_DICTIONARY_S = 3.0
UID_LEN = 12
READ_TIMEOUT = 0.1
READ_MISS_S = 0.05
DEFAULT_USB_IDS = (('1a86', '55d3'),)
UNSUPPORTED = -2
FAILED = -1


def chip_words(uid):
    """The 12-byte device id as three little-endian 32-bit words: the
    word order and value a stock ACE 2 reports in discover_device (uid1..3).
    () when the id is short."""
    b = bytes(bytearray(uid or b''))
    if len(b) < UID_LEN:
        return ()
    return struct.unpack('<3I', b[:UID_LEN])


def uid_key(words):
    """'uid:<8hex>-<8hex>-<8hex>' from the three words, '' when unknown
    or all zero (the same form as the ACE 2 and CFS keys)."""
    try:
        parts = tuple(int(x or 0) for x in words)
    except (TypeError, ValueError):
        return ''
    if len(parts) != 3 or not any(parts):
        return ''
    return 'uid:' + '-'.join('%08x' % x for x in parts)


def port_label(path):
    """Short name of the port for the log: the USB topology tail of a
    by-path name (0:1.3.3:1.0), else the base name."""
    path = str(path or '')
    tail = path.rsplit('usb-', 1)[-1] if 'usb-' in path else ''
    return tail or os.path.basename(path)


# --- tag decoding (M6d) ----------------------------------------------------
#
# A session reads what one brand needs, one step at a time; TagPlan decides
# the next step and decodes. NTAG: pages 0-11 first (the capability
# container in page 3 gives the user size), then the Anycubic record or the
# OpenSpool NDEF message as far as it reaches. MIFARE Classic 1K: the
# brands in order (Bambu Lab, then Snapmaker), each with its own sector
# keys; a refused key sends the session on to the next brand.

NTAG_FIRST_READ = (0, 0, 3, b'')     # op 0, page 0, three groups (0-11)
NTAG_GROUP_PAGES = 4
NTAG_STEP_GROUPS = 3
NTAG_PAGE_MAX = 244                  # highest first page a step may name
ANYCUBIC_LAST_PAGE = 39              # the NTAG213 user area (4-39)
MIFARE_BLOCKS_PER_SECTOR = 4


def hmac_sha256(key, data):
    return hmac.new(bytes(key), bytes(data), hashlib.sha256).digest()


def hkdf_sha256(ikm, salt, info, length):
    """RFC 5869, as ace2k's crypto.py."""
    prk = hmac_sha256(salt, ikm)
    out, t, i = b'', b'', 1
    while len(out) < length:
        t = hmac_sha256(prk, t + bytes(info) + bytes(bytearray([i])))
        out += t
        i += 1
    return out[:length]


def _cstr(buf, off, n):
    return bytes(buf[off:off + n]).split(b'\0', 1)[0].decode(
        'utf-8', 'replace').strip()


def _u16(buf, off):
    return struct.unpack_from('<H', bytes(buf), off)[0]


def _type_with_name(material, name):
    """'PLA' + 'PLA Matte' -> 'PLA Matte'; 'PLA' + 'Matte' -> 'PLA Matte'.
    ace.py splits the result into base and sub-type."""
    material = (material or '').strip()
    name = (name or '').strip()
    if not name:
        return material
    if not material or name.upper().startswith(material.upper()):
        return name
    return '%s %s' % (material, name)


def _rgb(hex6):
    try:
        return [int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)]
    except (TypeError, ValueError):
        return [0, 0, 0]


class _Bambu(object):
    """Bambu Lab: keys A from HKDF-SHA256 over the first four UID bytes.
    Blocks 1-2 variant and material, 4-6 name, colour, weight and
    temperatures, 12 production date, 14 length."""
    name = 'bambu'
    sectors = {0: 0b110, 1: 0b111, 3: 0b101}

    @staticmethod
    def key_for(uid, sector):
        okm = hkdf_sha256(bytes(uid)[:4], BAMBU_HKDF_SALT, BAMBU_HKDF_INFO,
                          6 * 16)
        return okm[6 * sector:6 * sector + 6]

    @staticmethod
    def decode(blocks):
        b = blocks
        material = _cstr(b[2], 0, 16)
        name = _cstr(b[4], 0, 16)
        if not (material or name):
            return None
        return {'fmt': 'bambu', 'brand': 'Bambu Lab',
                'type': _type_with_name(material, name),
                'color': list(bytearray(b[5][0:3])),
                'weight_g': _u16(b[5], 4),
                'article': _cstr(b[1], 8, 8)}


class _Snapmaker(object):
    """Snapmaker: key A of sector i = HMAC-SHA256(PRK, 'key_a_<i>' 0x01)[:6],
    PRK = HMAC-SHA256(salt, first four UID bytes). Record in sectors 0-2
    (OpenRFID layout); the RSA signature is not checked."""
    name = 'snapmaker'
    sectors = {0: 0b111, 1: 0b111, 2: 0b111}
    MAIN = {1: 'PLA', 2: 'PETG', 3: 'ABS', 4: 'TPU', 5: 'PVA'}
    SUB = {1: 'Basic', 2: 'Matte', 3: 'SnapSpeed', 4: 'Silk', 5: 'Support',
           6: 'HF', 7: '95A', 8: '95A HF'}

    @staticmethod
    def key_for(uid, sector):
        prk = hmac_sha256(SNAPMAKER_SALT_A, bytes(uid)[:4])
        return hmac_sha256(prk, ('key_a_%d' % sector).encode('ascii')
                           + b'\x01')[:6]

    @classmethod
    def decode(cls, blocks):
        img = bytearray(176)
        for blk in range(11):
            if blk % MIFARE_BLOCKS_PER_SECTOR < 3 and blk in blocks:
                img[blk * 16:blk * 16 + 16] = blocks[blk]
        material = cls.MAIN.get(_u16(img, 66))
        if not material:
            return None
        color = list(img[80:83]) if img[72] else [0, 0, 0]
        return {'fmt': 'snapmaker',
                'brand': _cstr(img, 16, 16) or 'Snapmaker',
                'type': _type_with_name(material,
                                        cls.SUB.get(_u16(img, 68), '')),
                'color': color, 'weight_g': _u16(img, 130),
                'article': str(struct.unpack_from('<I', bytes(img), 96)[0])}


MIFARE_BRANDS = (_Bambu, _Snapmaker)


def _ntag_decoders():
    """multiACE's own NTAG decoders (ace_rc522, the same the ACE2-Open
    read uses); None when the module is missing on this install."""
    try:
        from .ace_rc522 import AceTagReader
    except ImportError:
        return None
    return AceTagReader


class TagPlan(object):
    """What one session still has to read, and its record."""

    def __init__(self, uid, sak):
        self.uid = bytes(uid or b'')
        self.sak = int(sak)
        if self.sak == SAK_NTAG and len(self.uid) == NTAG_UID_BYTES:
            self.kind = 'ntag'
        elif self.sak == SAK_MIFARE_1K:
            self.kind = 'mifare'
        else:
            self.kind = 'other'
        self.pages = {}
        self.blocks = {}
        self.auth_failed = set()     # (brand, sector)
        self.brand = 0               # index into MIFARE_BRANDS

    # -- NTAG ---------------------------------------------------------------

    def _user_last_page(self):
        cc = self.pages.get(3)
        if cc and cc[0] == 0xE1 and cc[2]:
            return 3 + (cc[2] * 8) // 4
        return ANYCUBIC_LAST_PAGE

    def _ntag_bytes(self, first, last):
        out = bytearray()
        for p in range(first, last + 1):
            if p not in self.pages:
                break
            out += self.pages[p]
        return bytes(out)

    def _ntag_last_needed(self):
        """Last page the record needs (None: nothing to read on it)."""
        head = self._ntag_bytes(4, 11)
        if len(head) < 4:
            return None
        if head[0:2] == b'\x7b\x00':
            return ANYCUBIC_LAST_PAGE
        i = 0
        while i < len(head):
            t = head[i]
            if t == 0x00:
                i += 1
                continue
            if t in (0x01, 0x02) and i + 1 < len(head):
                i += 2 + head[i + 1]
                continue
            if t != 0x03 or i + 1 >= len(head):
                return None
            ln, hdr = head[i + 1], 2
            if ln == 0xFF:
                if i + 3 >= len(head):
                    return None
                ln, hdr = (head[i + 2] << 8) | head[i + 3], 4
            end = i + hdr + ln            # bytes from page 4
            return min(4 + (end + 3) // 4, self._user_last_page())
        return None

    def _ntag_step(self):
        if any(p not in self.pages for p in range(0, 12)):
            return NTAG_FIRST_READ
        last = self._ntag_last_needed()
        if last is None:
            return None
        page = 4
        while page <= last:
            if page not in self.pages:
                groups = min(NTAG_STEP_GROUPS,
                             (last - page) // NTAG_GROUP_PAGES + 1)
                return (0, min(page, NTAG_PAGE_MAX), groups, b'')
            page += 1
        return None

    def _ntag_decode(self):
        last = self._ntag_last_needed()
        if last is None:
            return None
        data = self._ntag_bytes(4, last)
        rc = _ntag_decoders()
        if rc is None:
            return None
        if data[0:2] == b'\x7b\x00':
            any_ = rc._anycubic_decode(data)
            if any_ and (any_.get('material') or '').strip():
                return {'fmt': 'anycubic', 'type': any_['material'],
                        'color': _rgb(any_.get('color') or ''),
                        'brand': any_.get('brand', ''),
                        'sku': any_.get('sku', '')}
            return None
        osp = rc._openspool_decode(data)
        if osp and (osp.get('material') or '').strip():
            return {'fmt': 'openspool', 'type': osp['material'],
                    'color': _rgb(osp.get('color') or ''),
                    'brand': osp.get('vendor', '')}
        return None

    # -- MIFARE -------------------------------------------------------------

    def _mifare_step(self):
        while self.brand < len(MIFARE_BRANDS):
            fmt = MIFARE_BRANDS[self.brand]
            declined = False
            for sector, mask in sorted(fmt.sectors.items()):
                if (fmt.name, sector) in self.auth_failed:
                    declined = True
                    break
                wanted = [sector * MIFARE_BLOCKS_PER_SECTOR + i
                          for i in range(3) if mask >> i & 1]
                if any(b not in self.blocks for b in wanted):
                    return (1, sector, mask, fmt.key_for(self.uid, sector))
            if not declined:
                return None
            self.brand += 1
            self.blocks = {}
        return None

    def _mifare_decode(self):
        if self.brand >= len(MIFARE_BRANDS):
            return None
        fmt = MIFARE_BRANDS[self.brand]
        for sector, mask in fmt.sectors.items():
            for i in range(3):
                if mask >> i & 1 and (sector * MIFARE_BLOCKS_PER_SECTOR + i
                                      not in self.blocks):
                    return None
        try:
            return fmt.decode(self.blocks)
        except (IndexError, KeyError, struct.error):
            return None

    # -- the session's interface --------------------------------------------

    def next_step(self):
        """(op, arg, blocks, key) or None when nothing more is needed."""
        if self.kind == 'ntag':
            return self._ntag_step()
        if self.kind == 'mifare':
            return self._mifare_step()
        return None

    def feed(self, step, frames):
        """Take one step's frames: 'ok', 'gone' (the unit lost the tag) or
        'error' (a malformed answer; the step may be sent once more)."""
        op, arg = step[0], step[1]
        for block, status, data in frames:
            if status == 0:
                if op == 0:
                    for k in range(4):
                        piece = bytes(data[k * 4:k * 4 + 4])
                        if len(piece) == 4:
                            self.pages[block + k] = piece
                else:
                    self.blocks[block] = bytes(data)
            elif status == 1:
                if op != 0 and self.brand < len(MIFARE_BRANDS):
                    self.auth_failed.add((MIFARE_BRANDS[self.brand].name,
                                          arg))
                return 'ok'
            elif status == 2:
                return 'gone'
            else:
                return 'error'
        return 'ok'

    def result(self):
        if self.kind == 'ntag':
            return self._ntag_decode()
        if self.kind == 'mifare':
            return self._mifare_decode()
        return None


class LinkDead(IOError):
    pass


class UnitShutdown(IOError):
    """The unit sits in a Klipper shutdown: it starts nothing until it is
    reset. The link sends the reset and gives up this connection; the
    reconnect ladder opens a fresh one."""


class Ace2kLink(object):
    """The pyserial surface ace.py uses (is_open, read, in_waiting, write,
    reset_input_buffer, flush, close, fileno) on top of the raw port.
    write() runs one host request against the unit; read() hands out the
    response envelopes."""

    def __init__(self, raw, clock=None, label=''):
        self._raw = raw
        self.label = label
        # Log prefix: with two ace2k units 'lane 0' alone does not say which
        # one moved, so every link line names the port like the follow line.
        self._lp = ('[multiACE] ace2k %s: ' % label if label
                    else '[multiACE] ace2k: ')
        self._rx = bytearray()
        self._out = queue.Queue()
        self._pending = bytearray()
        self._dead = None
        self._now = clock or (lambda: time.monotonic())
        self.seq = 0
        self.dictionary = None
        self.version = ''
        self.uid = b''
        self.proven = None
        self.clock_freq = None
        self.bad_frames = 0
        self._ready = False
        self._shutdown = None      # static string of a reported shutdown
        self._sensors = None       # (time, switches)
        self._feed = None          # (time, dict)
        self._env = None           # (time, dict)
        self._enc = None           # (time, [encoder_um x 4])
        self._dryer = None         # (time, dict)
        self._dryer_ack = None     # seq of the newest event to acknowledge
        self._dryer_ack_due = False
        self._dryer_minutes = 0    # length of the cycle the host started
        self._dryer_trace_t0 = None   # start of the cycle being traced
        # The heater rate the unit learned (ace2k_dryer_gain_query, images
        # from v0.17): asked at connect, a minute into a cycle and at its
        # end, logged when it changes.
        self._gain_due = 0.0
        self._gain_seen = None
        self._dryer_trace_prev = 0    # dryer state of the last report
        self._rfid = None          # (time, list of 4 states)
        self._tag_uid = [b'', b'', b'', b'']
        self._events = []          # feed events not yet worked through
        self._logged = set()
        self._jobs = {}            # lane -> running move job
        self._follow = {}          # lane -> seq of the running follow
        self._follow_t0 = {}       # lane -> time the follow started
        self._follow_want = set()  # lanes the host wants following
        # A follow the host wants that ended by itself (stuck, tangled, ...)
        # and was not started again: lane -> (why, count). Reported in the
        # status (slot 'follow_lost') so the host knows the lane no longer
        # gives filament (no freewheel: the extruder pulls on a standing
        # gear). Gone with the next start or the host's stop.
        self._follow_lost = {}
        self._follow_lost_n = 0
        self._lane_seq = {}        # lane -> last start seq the unit took
        self._motion_seq = 0
        self._last_motion = None
        self._lane_motion = [None, None, None, None]  # last move per lane
        self._full_since = {}      # resting lane -> time it went full
        self._full_relaxed = set() # lanes the watch already rolled back
        self._follow_full = {}     # following lane -> (time, bursts) at full
        # v0.15.0: the host's lanes in use (None until it said so), what
        # was last sent and when; the lanes wanted set by hand (lane ->
        # time wanted) and the unit's own announcement of its marks.
        self._in_use = None
        self._in_use_sent = (0.0, None)
        self._by_hand_want = {}
        self._by_hand_unit = None
        self._by_hand_sent = {}
        self._lanes_first = True
        # Net filament each lane moved (feed +, retract -) since its insert
        # switch last changed. A read queued at an insert during a print
        # needs it: the load an insert starts assumes the tip still sits
        # at the insert sensor.
        self._moved_mm = [0.0, 0.0, 0.0, 0.0]
        # lane -> seq of the last move ended without its end event
        self._lost_seq = {}
        # Tags (M6d). One lock over every exchange: the writer thread runs
        # host requests, the reader thread serves tag sessions while idle.
        self._io = threading.RLock()
        self._sessions = {}        # lane -> open tag session
        self._tag_rec = [None, None, None, None]   # lane -> last read record
        self._tag_rec_n = 0
        self._tag_reads = {}       # lane -> running read (motion or load)
        # The load an insert starts (connect sets them from the unit's
        # bounds; these are the defaults sent there).
        self._load_park_um = LOAD_PARK_UM
        self._load_speed_um_s = LOAD_SPEED_UM_S
        self._tag_read_seq = {}    # lane -> last read seq the link sent
        self._tag_search_n = [0, 0, 0, 0]
        self._tag_search_end = [None, None, None, None]
        # Follow instrument: plunger readings and burst counters, summed per
        # window while a follow runs (one klippy.log line per window).
        self._bursts = None        # last reported burst counter per lane
        self._fw = None            # the open follow window

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

    def _drain_out(self):
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
        self._drain_out()
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
                self._idle_service()
                return b''
            self._drain_out()
        data = bytes(self._pending[:size])
        del self._pending[:size]
        return data

    def write(self, data):
        if self._dead is not None:
            raise LinkDead(str(self._dead))
        try:
            request = json.loads(bytes(data).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            logging.info('[multiACE] ace2k link: undecodable request dropped')
            return len(data)
        try:
            with self._io:
                response = self._execute(request)
        except (IOError, OSError) as e:
            self._dead = e
            raise
        if response is not None:
            self._out.put(json.dumps(response).encode('utf-8') + b'\n')
        try:
            with self._io:
                self._serve_sessions()
        except (IOError, OSError) as e:
            self._dead = e
            raise
        return len(data)

    def _idle_service(self):
        """Reader thread, between responses: read what the unit sent and
        serve a tag session that opened since the last host request. Never
        waits for the writer: a running request serves sessions itself."""
        if not self._ready or self._dead is not None:
            return
        if not self._io.acquire(False):
            return
        try:
            self._pump()
            self._serve_sessions()
        except (IOError, OSError) as e:
            self._dead = e
        except Exception:
            logging.exception(self._lp + 'idle service failed')
        finally:
            self._io.release()

    # -- Klipper exchanges ------------------------------------------------

    def _log_once(self, key, text):
        if key in self._logged:
            return
        self._logged.add(key)
        logging.info(self._lp + '%s' % text)

    def _feed_rx(self, chunk):
        """Parse what came in: [(seq, body)] blocks; every message of a
        non-empty block goes through _dispatch."""
        self._rx.extend(chunk)
        out = []
        for seq, body in parse_klipper_blocks(self._rx):
            out.append((seq, body))
        return out

    def _read_some(self):
        try:
            chunk = self._raw.read(1)
        except (TypeError, ValueError):
            # pyserial on a port another thread just closed.
            raise LinkDead('port closed')
        if chunk:
            n = self._raw.in_waiting
            if n:
                chunk += self._raw.read(n)
        return chunk

    def _xfer(self, payload, want=None, timeout=ASK_TIMEOUT, again=True):
        """Send one block and wait. want = the response NAME (or the raw
        id for identify): returns its parameters (dict, or raw bytes for
        an id) or None. want None: returns True once the unit took the
        block. Retry rules from ace2k's flasher: a refusal (an ack that
        names another sequence) is resent with the number the unit
        expects; a block nothing answered is resent once, behind enough
        sync bytes to end any block a collision left half read."""
        lost = False
        for _ in range(3):
            sent = self.seq
            sync = bytes(bytearray([KLIPPER_SYNC])) * (
                KLIPPER_MAX + 1 if lost else 1)
            self._raw.write(sync + klipper_block(sent, payload))
            self.seq = (sent + 1) & KLIPPER_SEQ_MASK
            answer, heard = self._wait(want, sent, timeout)
            if answer is not None:
                return answer
            if heard is not None and heard == self.seq:
                # The unit took the block.
                if want is None:
                    return True
                if not again:
                    return None
                continue
            if heard is not None:
                self.seq = heard
                continue
            if lost:
                return None
            self.seq, lost = sent, True
        return None

    def _wait(self, want, sent, timeout):
        """Read until the answer, the acknowledgement of this block or a
        refusal, or the timeout. Bounded by time AND by empty reads, so a
        silent unit costs `timeout` where the clock stands still."""
        deadline = self._now() + timeout
        misses = 0
        max_misses = int(timeout / max(READ_MISS_S, 0.001)) + 1
        while True:
            chunk = self._read_some()
            if not chunk:
                misses += 1
                if misses >= max_misses:
                    return None, None
            else:
                # Every block of the chunk is dispatched, also the ones
                # behind the answer (a session the same exchange opened).
                found, heard = None, None
                for seq, body in self._feed_rx(chunk):
                    if not body:
                        if seq != sent and heard is None:
                            heard = seq
                        continue
                    got = self._handle_body(body, want)
                    if got is not None and found is None:
                        found = got
                if found is not None:
                    return found, None
                if heard is not None:
                    return None, heard
            if self._now() >= deadline:
                return None, None

    def _handle_body(self, body, want):
        """Dispatch every message of a block; returns the wanted one's
        parameters when it is among them."""
        if isinstance(want, int):
            try:
                mid, pos = parse_klipper_vlq(body, 0)
            except IndexError:
                return None
            if mid == (want & 0xFFFFFFFF):
                return bytes(body[pos:])
            if self.dictionary is None:
                return None
        if self.dictionary is None:
            return None
        found = None
        for name, vals in self.dictionary.decode(body):
            self._dispatch(name, vals)
            if want is not None and name == want and found is None:
                found = vals
        return found

    def _pump(self, timeout=0.0):
        """Read what is waiting (and up to `timeout` more) without sending:
        the periodic reports land in the caches."""
        deadline = self._now() + timeout
        misses = 0
        max_misses = int(timeout / max(READ_MISS_S, 0.001)) + 1
        while True:
            chunk = self._read_some()
            if not chunk:
                misses += 1
                if misses >= max_misses:
                    return
            else:
                for _seq, body in self._feed_rx(chunk):
                    if body:
                        self._handle_body(body, None)
            if self._now() >= deadline and not chunk:
                return

    def _dispatch(self, name, vals):
        now = self._now()
        if name == 'ace2k_sensors_state':
            self._flog_advance(now)
            sw = int(vals.get('switches', 0))
            if self._sensors is not None:
                self._note_inserts(self._sensors[1], sw)
            self._sensors = (now, sw)
        elif name == 'ace2k_sensors_event':
            # An edge between two reports (insert, rest, pushed, pulled):
            # taken at once, the next report confirms it.
            kind = int(vals.get('kind', -1))
            if self._sensors is not None and kind in (0, 1, 2, 3):
                self._flog_advance(now)
                lane = int(vals.get('lane', 255))
                level = bool(vals.get('level'))
                sw = self._sensors[1]
                bit = 0
                if kind == 3:
                    bit = 1 << SW_PULLED_BIT
                    if level and not sw & bit and self._fw is not None:
                        self._fw['full_edges'] += 1
                elif 0 <= lane < 4:
                    bit = 1 << (lane + (0, SW_REST_SHIFT,
                                        SW_PUSHED_SHIFT)[kind])
                if bit:
                    old = sw
                    sw = sw | bit if level else sw & ~bit
                    self._note_inserts(old, sw)
                    self._sensors = (self._sensors[0], sw)
        elif name == 'ace2k_feed_state':
            self._flog_advance(now)
            raw = bytearray(vals.get('bursts', b''))
            self._bursts = [raw[2 * i] | (raw[2 * i + 1] << 8)
                            for i in range(min(4, len(raw) // 2))]
            if self._fw is not None and self._fw['b0'] is None:
                self._fw['b0'] = list(self._bursts)
            self._feed = (now, {
                'mode': bytearray(vals.get('mode', b'')),
                'error': bytearray(vals.get('error', b'')),
                'seq': bytearray(vals.get('seq', b'')),
            })
        elif name == 'ace2k_env_state':
            self._env = (now, dict(vals))
        elif name == 'ace2k_dryer_state':
            self._dryer = (now, dict(vals))
            self._dryer_trace(now, vals)
        elif name == 'ace2k_dryer_event':
            kind = int(vals.get('kind', -1))
            arg = int(vals.get('arg', 0))
            seq = int(vals.get('seq', 0))
            if seq != self._dryer_ack:
                text = DRYER_EVENTS.get(kind, 'kind_%d' % kind)
                if kind == 2:
                    text += ' (%s)' % DRYER_FAULTS.get(arg, arg)
                elif arg:
                    text += ' (%d)' % arg
                logging.info(self._lp + 'dryer event: %s' % text)
            self._dryer_ack = seq
            self._dryer_ack_due = True
        elif name == 'ace2k_lane_counters':
            raw = bytes(bytearray(vals.get('encoder_um', b'')))
            if len(raw) >= 16:
                self._enc = (now, [
                    int.from_bytes(raw[4 * i:4 * i + 4], 'little',
                                   signed=True) for i in range(4)])
        elif name == 'ace2k_rfid_state':
            st = list(bytearray(vals.get('state', b'')))[:4]
            self._rfid = (now, st)
            for lane, s in enumerate(st):
                if s == 0:
                    self._forget_lane_tag(lane)
        elif name == 'ace2k_rfid_tag':
            # The unit opened a tag session (a move passed a tag): served
            # by _serve_sessions once this exchange is over.
            lane = int(vals.get('lane', 255))
            if 0 <= lane < 4:
                self._tag_uid[lane] = bytes(vals.get('uid', b''))
                self._sessions[lane] = {
                    'session': int(vals.get('session', 0)),
                    'uid': bytes(vals.get('uid', b'')),
                    'atqa': int(vals.get('atqa', 0)),
                    'sak': int(vals.get('sak', 0)),
                    'frames': [], 'closed': False}
        elif name == 'ace2k_rfid_data':
            lane = int(vals.get('lane', 255))
            sess = self._sessions.get(lane)
            if sess is not None and sess['session'] == int(
                    vals.get('session', -1)):
                sess['frames'].append((int(vals.get('block', 0)),
                                       int(vals.get('status', 0)),
                                       bytes(vals.get('data', b''))))
        elif name == 'ace2k_rfid_event':
            lane = int(vals.get('lane', 255))
            kind = int(vals.get('kind', -1))
            if 0 <= lane < 4:
                sess = self._sessions.get(lane)
                if sess is not None and kind in (0, 1, 2, 4, 5):
                    # The lane left 'reading': the unit ended the session.
                    sess['closed'] = True
                if kind == 0:
                    self._forget_lane_tag(lane)
        elif name == 'ace2k_rfid_by_hand_state':
            mask = int(vals.get('mask', 0)) & 0x0F
            prev = self._by_hand_unit
            if prev is not None:
                for lane in range(4):
                    # The mark went: the strand left the bay. The want
                    # ends with it, so a spool put in next gets no mark.
                    if prev & (1 << lane) and not mask & (1 << lane):
                        self._by_hand_want.pop(lane, None)
            self._by_hand_unit = mask
        elif name == 'ace2k_rfid_lane_state':
            lane = int(vals.get('lane', 255))
            if 0 <= lane < 4 and vals.get('uid'):
                self._tag_uid[lane] = bytes(vals.get('uid'))
        elif name == 'ace2k_feed_event':
            # Worked through by _tick (a start must not run from here: this
            # is inside an exchange).
            self._events.append(dict(vals))
        elif name in ('shutdown', 'is_shutdown'):
            sid = int(vals.get('static_string_id', -1))
            text = (self.dictionary.static_strings.get(sid, 'id %d' % sid)
                    if self.dictionary is not None else 'id %d' % sid)
            self._shutdown = text

    # -- connect ----------------------------------------------------------

    def _read_dictionary(self, budget=DICTIONARY_DEADLINE_S):
        """The unit's data dictionary (zlib JSON) read in 40-byte pieces,
        as Klipper reads it. None when nothing answers."""
        data = bytearray()
        deadline = self._now() + budget
        heard = False
        while len(data) < DICTIONARY_MAX:
            if self._now() >= deadline:
                return None
            q = (klipper_vlq(IDENTIFY_ID) + klipper_vlq(len(data))
                 + klipper_vlq(IDENTIFY_CHUNK))
            params = self._xfer(q, IDENTIFY_RESPONSE_ID,
                                PIECE_TIMEOUT if heard else ASK_TIMEOUT)
            if params is None:
                if heard:
                    continue
                return None
            heard = True
            try:
                offset, pos = parse_klipper_vlq(params, 0)
                n = bytearray(params)[pos]
            except IndexError:
                return None
            chunk = bytes(params[pos + 1:pos + 1 + n])
            if offset != len(data) or len(chunk) != n:
                continue
            data += chunk
            if len(chunk) < IDENTIFY_CHUNK:
                break
        try:
            return json.loads(zlib.decompress(bytes(data)).decode('utf-8'))
        except (zlib.error, ValueError, UnicodeDecodeError):
            return None

    def _call(self, name, response, **values):
        if not self.dictionary.has(name):
            return None
        return self._xfer(self.dictionary.encode(name, **values), response)

    def _send(self, name, **values):
        if not self.dictionary.has(name):
            return None
        return self._xfer(self.dictionary.encode(name, **values), None)

    def _connect(self):
        """Dictionary, shutdown check, link proof, auto_load off, reports.
        Raises IOError when the unit cannot be used on this connection."""
        raw = self._read_dictionary()
        if raw is None:
            raise LinkDead('ace2k: no data dictionary (not an ace2k unit, '
                           'or it does not answer at 250000 baud)')
        d = Dictionary(raw)
        missing = [c for c in REQUIRED_COMMANDS if not d.has(c)]
        if missing:
            raise LinkDead('ace2k: dictionary lacks %s' % ', '.join(missing))
        self.dictionary = d
        self.clock_freq = d.constant('CLOCK_FREQ', 0) or None
        if not self.clock_freq:
            raise LinkDead('ace2k: dictionary names no CLOCK_FREQ')
        cfg = self._call('get_config', 'config')
        if cfg is not None and cfg.get('is_shutdown'):
            self._reset('the unit was in a Klipper shutdown')
        # The link proof: the first answered ace2k_version.
        ver = self._call('ace2k_version', 'ace2k_version_response')
        if ver is None:
            raise LinkDead('ace2k: ace2k_version unanswered')
        self.version = bytes(ver.get('version', b'')).decode(
            'utf-8', 'replace').strip('\x00 ')
        lp = self._call('ace2k_linkproof_state',
                        'ace2k_linkproof_state_response')
        if lp is not None:
            self.proven = bool(lp.get('proven'))
            if not self.proven:
                # Asked once more, as ace2k's host does: the store may
                # come after the version answer.
                self._call('ace2k_version', 'ace2k_version_response')
        uid = self._call('ace2k_uid_query', 'ace2k_uid_response')
        if uid is not None:
            self.uid = bytes(uid.get('uid', b''))
        if d.has('ace2k_feed_load_set'):
            lo = d.constant('ACE2K_LANE_SPEED_MIN_UM_S', LOAD_SPEED_UM_S)
            hi = d.constant('ACE2K_LANE_SPEED_MAX_UM_S', LOAD_SPEED_UM_S)
            mx = d.constant('ACE2K_LANE_MOVE_MAX_UM', LOAD_PARK_UM)
            speed = min(max(LOAD_SPEED_UM_S, lo), hi)
            park = min(LOAD_PARK_UM, mx)
            self._load_park_um, self._load_speed_um_s = park, speed
            if self._send('ace2k_feed_load_set', park_um=park,
                          speed_um_s=speed, auto_load=0) is None:
                logging.info(self._lp + 'auto_load off not '
                             'acknowledged')
        for cmd, period in REPORT_PERIODS:
            if d.has(cmd):
                ticks = int(round(period * self.clock_freq))
                self._send(cmd, rest_ticks=ticks)
        if d.has('ace2k_rfid_search_set'):
            reach = min(TAG_SEARCH_UM, d.constant(
                'ACE2K_FEED_SEARCH_MAX_UM', TAG_SEARCH_UM) or TAG_SEARCH_UM)
            self._send('ace2k_rfid_search_set', search_um=reach)
        if d.has('ace2k_rfid_forget') and not self._lane_report():
            # A lane the unit kept as 'read' from before this connection
            # opens no session on its next move; forgotten, it reads again
            # (the host's record is gone with the old link). An image with
            # lanes set by hand waits for the host's first list instead
            # (set_lanes): a forget would clear the marks.
            for lane in range(4):
                self._send('ace2k_rfid_forget', lane=lane)
        self._ready = True
        logging.info('[multiACE] ace2k unit: version %s, link proof %s, '
                     'key %s, %d commands'
                     % (self.version or '?',
                        {True: 'stored', False: 'pending',
                         None: 'unknown'}[self.proven],
                        uid_key(chip_words(self.uid)) or 'none',
                        len(d.commands)))

    def probe(self, budget=PROBE_DICTIONARY_S):
        """The pre-claim probe: dictionary and device id, nothing that
        changes the unit (no link proof, no reset, no reports). Works in a
        Klipper shutdown too (the uid query is answered there). Returns the
        probe_device dict."""
        raw = self._read_dictionary(budget)
        if raw is None:
            return {'ok': False, 'category': 'no_response',
                    'error': 'no data dictionary at 250000 baud'}
        d = Dictionary(raw)
        if not d.has('ace2k_uid_query') or not d.has('ace2k_version'):
            return {'ok': False, 'category': 'invalid_identity',
                    'error': 'dictionary is not an ace2k one'}
        self.dictionary = d
        ans = self._call('ace2k_uid_query', 'ace2k_uid_response')
        words = chip_words((ans or {}).get('uid', b''))
        if not words:
            return {'ok': False, 'category': 'no_response',
                    'error': 'ace2k_uid_query unanswered'}
        version = str(raw.get('version', '') or
                      d.config.get('ACE2K_VERSION', '') or '')
        return {'ok': True, 'category': 'verified', 'model': MODEL,
                'firmware': 'ace2k %s' % (version or '?'), 'uid': words}

    def _reset(self, why):
        """Send Klipper's reset (no answer: the unit restarts) and give up
        this connection."""
        logging.info(self._lp + '%s - sending reset' % why)
        try:
            if self.dictionary is not None and self.dictionary.has('reset'):
                self._raw.write(klipper_block(self.seq,
                                              self.dictionary.encode('reset')))
                self.seq = (self.seq + 1) & KLIPPER_SEQ_MASK
        except Exception:
            pass
        raise UnitShutdown('ace2k: %s' % why)

    # -- host requests ----------------------------------------------------

    def _execute(self, request):
        rid = request.get('id', 0)
        method = request.get('method', '')

        def ok(result):
            return {'id': rid, 'code': 0, 'msg': 'success', 'result': result}

        if method not in ('get_info', 'get_status', 'tag_read_motion',
                          'tag_read_load', 'relax_buffer', 'set_lanes',
                          'flash_led') + DRYER_METHODS + MOTION_METHODS:
            return {'id': rid, 'code': UNSUPPORTED,
                    'msg': 'unsupported:%s' % method, 'result': {}}
        if not self._ready:
            self._connect()
        # Keepalive + drain: one round trip every request (the heartbeat
        # asks once a second, well inside the unit's 3 s link window).
        self._call('get_clock', 'clock')
        self._pump()
        if self._shutdown is not None:
            text, self._shutdown = self._shutdown, None
            self._reset('the unit reported a shutdown (%s)' % text)
        try:
            self._tick()
            self._tag_tick()
            self._dryer_tick()
        except (IOError, OSError):
            raise
        except Exception:
            logging.exception(self._lp + 'motion step failed')
        if method == 'tag_read_motion':
            return self._tag_read_motion(request.get('params') or {}, rid)
        if method == 'tag_read_load':
            return self._tag_read_load(request.get('params') or {}, rid)
        if method == 'relax_buffer':
            return self._relax_buffer(request.get('params') or {}, rid)
        if method == 'set_lanes':
            return ok(self._set_lanes(request.get('params') or {}))
        if method == 'flash_led':
            return self._identify(request.get('params') or {}, rid)
        if method in DRYER_METHODS:
            return self._dryer_request(method, request.get('params') or {},
                                       rid)
        if method == 'get_info':
            return ok({'model': MODEL,
                       'firmware': 'ace2k %s' % (self.version or '?')})
        if method == 'get_status':
            return self._status(ok)
        return self._motion(method, request.get('params') or {}, rid)

    # -- dryer, identify --------------------------------------------------

    def _dryer_tick(self):
        """Acknowledge the dryer events seen: the unit repeats each one
        every 5 s until the host does."""
        if self._dryer_ack_due:
            self._dryer_ack_due = False
            self._send('ace2k_dryer_event_ack', seq=int(self._dryer_ack))
        if self._gain_due is not None and self._now() >= self._gain_due:
            self._gain_due = None
            ans = self._call('ace2k_dryer_gain_query',
                             'ace2k_dryer_gain_state')
            if ans is not None:
                seen = (int(ans.get('gain', 0)), int(ans.get('source', 0)))
                if seen != self._gain_seen:
                    self._gain_seen = seen
                    logging.info(
                        self._lp + 'dryer heater rate: %s%s'
                        % (DRYER_GAIN_SOURCES.get(seen[1], seen[1]),
                           ' (gain %d)' % seen[0] if seen[1] >= 2 else ''))

    def _dryer_trace(self, now, vals):
        """One log line per dryer report for the first DRYER_TRACE_S of a
        cycle, and one when the cycle ends in a fault: state, target, drive
        target, duty, both outlet NTCs and the chamber. The numbers the
        unit's rise protection judges, for a start that faults early.
        """
        try:
            state = int(vals.get('state', 0))
            prev, self._dryer_trace_prev = self._dryer_trace_prev, state
            active = state in (1, 2)
            if active and prev not in (1, 2):
                self._dryer_trace_t0 = now
                self._gain_due = now + DRYER_GAIN_ASK_S
            elif not active and prev in (1, 2):
                self._gain_due = now
            t0 = self._dryer_trace_t0
            if t0 is None:
                return
            faulted = state == 4 and prev != 4
            if not active:
                self._dryer_trace_t0 = None
                if not faulted:
                    return
            elif now - t0 > DRYER_TRACE_S:
                return
            env = self._env[1] if self._env else {}
            valid = int(env.get('valid', 0))

            def _c(key, bit):
                if not valid & bit:
                    return 'n/a'
                return '%.1f' % (int(env.get(key, 0)) / 1000.0)
            fault = int(vals.get('fault', 0))
            logging.info(
                self._lp + 'dryer +%ds %s target=%d drive=%.1f duty=%d '
                'ntc_l=%s ntc_r=%s chamber=%s fault=%s'
                % (int(round(now - t0)),
                   DRYER_STATES.get(state, 'state_%d' % state),
                   int(vals.get('target_c', 0)),
                   int(vals.get('drive_dc', 0)) / 10.0,
                   int(vals.get('duty', 0)),
                   _c('ptc_left_mc', 1), _c('ptc_right_mc', 2),
                   _c('chamber_mc', 4),
                   DRYER_FAULTS.get(fault, fault) if fault else 'none'))
        except Exception:
            logging.exception(self._lp + 'dryer trace failed')

    def _dryer_state(self):
        if not self._fresh(self._dryer):
            return None
        return int(self._dryer[1].get('state', 0))

    def _dryer_call(self, name, **values):
        ans = self._call(name, 'ace2k_dryer_response', **values)
        if ans is None:
            return 'no_reply'
        if ans.get('accepted'):
            return 'ok'
        reason = int(ans.get('reason', 0))
        return DRYER_REFUSALS.get(reason, 'refused_%d' % reason)

    def _dryer_request(self, method, params, rid):
        def reply(code, msg):
            return {'id': rid, 'code': code, 'msg': msg, 'result': {}}
        d = self.dictionary
        if not d.has('ace2k_dryer_start'):
            return reply(UNSUPPORTED, 'unsupported:%s' % method)
        if method == 'drying_stop':
            res = self._dryer_call('ace2k_dryer_stop')
            if res == 'faulted':
                res = self._dryer_call('ace2k_dryer_clear')
            logging.info(self._lp + 'dryer stop: %s' % res)
            return reply(0 if res == 'ok' else -1,
                         'success' if res == 'ok' else res)
        lo = d.constant('ACE2K_DRYER_TARGET_MIN_C', 15) or 15
        hi = d.constant('ACE2K_DRYER_TARGET_MAX_C', 65) or 65
        mx = d.constant('ACE2K_DRYER_MINUTES_MAX', 1440) or 1440
        try:
            want = int(round(float(params.get('temp', 0))))
            minutes = int(round(float(params.get('duration', 0))))
        except (TypeError, ValueError):
            return reply(-1, 'range')
        temp = min(max(want, lo), hi)
        minutes = min(max(minutes, 1), mx)
        if self._dryer_state() == 4:
            # A fault whose cause is gone clears; anything else refuses
            # the start below with its own reason.
            self._dryer_call('ace2k_dryer_clear')
        res = self._dryer_call('ace2k_dryer_start', target_c=temp,
                               minutes=minutes)
        logging.info(self._lp + 'dryer start %d C (asked %d) for %d min: %s'
                     % (temp, want, minutes, res))
        if res != 'ok':
            return reply(-1, res)
        self._dryer_minutes = minutes
        return reply(0, 'success')

    def _dryer_status(self):
        """The host's dryer_status from the unit's report; None without a
        dryer in this image."""
        dryer = getattr(self, '_dryer', None)
        if dryer is None or not self._fresh(dryer):
            return None
        v = dryer[1]
        state = int(v.get('state', 0))
        fault = int(v.get('fault', 0))
        running = state in DRYER_RUNNING
        return {
            'status': 'drying' if running else 'stop',
            'target_temp': int(v.get('target_c', 0)) if running else 0,
            'duration': int(self._dryer_minutes) if running else 0,
            'remain_time': int(v.get('remaining_min', 0)) * 60,
            'state': DRYER_STATES.get(state, 'state_%d' % state),
            'fault': DRYER_FAULTS.get(fault, '') if fault else '',
        }

    def _identify(self, params, rid):
        """Blink the four lane LEDs (ace2k_identify, 10 s, ends by
        itself). A request to switch the pattern off needs no command."""
        def reply(code, msg):
            return {'id': rid, 'code': code, 'msg': msg, 'result': {}}
        if not self.dictionary.has('ace2k_identify'):
            return reply(UNSUPPORTED, 'unsupported:flash_led')
        if not int(params.get('components', 1) or 0):
            return reply(0, 'success')
        ans = self._call('ace2k_identify', 'ace2k_identify_response')
        return reply(0, 'success') if ans is not None else reply(-1,
                                                                 'no_reply')

    # -- motion -----------------------------------------------------------

    def _speed_um(self, speed_mm_s):
        d = self.dictionary
        lo = d.constant('ACE2K_LANE_SPEED_MIN_UM_S',
                        int(SPEED_MIN_MM_S * 1000))
        hi = d.constant('ACE2K_LANE_SPEED_MAX_UM_S',
                        int(SPEED_MAX_MM_S * 1000))
        try:
            v = int(round(float(speed_mm_s) * 1000.0))
        except (TypeError, ValueError):
            v = hi
        return min(max(v, lo), hi)

    def _move_max_mm(self):
        return self.dictionary.constant(
            'ACE2K_LANE_MOVE_MAX_UM', int(MOVE_MAX_MM * 1000)) / 1000.0

    def _next_seq(self, lane):
        last = self._lane_seq.get(lane)
        if last is None:
            # The unit's last accepted start on this lane (state report):
            # the session's first start must not repeat it.
            seqs = bytearray((self._feed[1] if self._feed else {}).get(
                'seq', b''))
            last = seqs[lane] if lane < len(seqs) else 0
        return (int(last) % 255) + 1

    def _start(self, lane, mode, length_mm, speed_mm_s, free_tip=0):
        """One ace2k_feed_start: (seq, 'ok') or (None, refusal name)."""
        if not self.dictionary.has('ace2k_feed_start'):
            return None, 'no_feed_start'
        seq = self._next_seq(lane)
        ans = self._call('ace2k_feed_start', 'ace2k_feed_start_response',
                         lane=lane, mode=mode,
                         length_um=int(round(max(0.0, length_mm) * 1000.0)),
                         speed_um_s=self._speed_um(speed_mm_s), seq=seq,
                         free_tip=1 if free_tip else 0)
        if ans is None:
            return None, 'unanswered'
        if ans.get('accepted'):
            self._lane_seq[lane] = seq
            return seq, 'ok'
        reason = int(ans.get('reason', -1))
        return None, REFUSAL_NAMES.get(reason, 'reason_%d' % reason)

    def _stop_lane(self, lane):
        if self.dictionary.has('ace2k_feed_stop'):
            self._send('ace2k_feed_stop', lane=lane)

    def _clear_lane(self, lane, why):
        if self.dictionary.has('ace2k_feed_clear'):
            self._send('ace2k_feed_clear', lane=lane)
        logging.info(self._lp + 'lane %d cleared from error (%s)'
                     % (lane, why))

    def _start_follow(self, lane):
        seq, why = self._start(lane, FEED_MODES['follow'], 0.0,
                               FOLLOW_SPEED_MM_S)
        if seq is None and why == 'in_error':
            self._clear_lane(lane, 'follow start')
            seq, why = self._start(lane, FEED_MODES['follow'], 0.0,
                                   FOLLOW_SPEED_MM_S)
        if seq is not None:
            self._follow[lane] = seq
            self._follow_t0[lane] = self._now()
            self._follow_lost.pop(lane, None)
        return seq, why

    def _note_follow_lost(self, lane, why):
        """The follow on a lane the host still wants is not running and
        was not started again."""
        if lane in self._follow or lane not in self._follow_want:
            return
        self._follow_lost_n += 1
        self._follow_lost[lane] = (str(why), self._follow_lost_n)

    def _stop_follow(self, lane):
        """Stop the running follow on the lane and work through its end
        event, so the lane is idle for the next start."""
        if lane not in self._follow:
            return
        self._stop_lane(lane)
        self._pump(STOP_EVENT_WAIT_S)
        self._work_events()
        self._follow.pop(lane, None)

    def _start_piece(self, job):
        piece = min(job['target'] - job['done'], self._move_max_mm())
        if job.get('stop_when') and not self._arm_stop(job['slot']):
            return 'stop_not_armed'
        seq, why = self._start(job['slot'], FEED_MODES[job['kind']], piece,
                               job['speed'], job['free_tip'])
        if seq is None and why == 'in_error':
            self._clear_lane(job['slot'], '%s start' % job['kind'])
            if job.get('stop_when') and not self._arm_stop(job['slot']):
                return 'stop_not_armed'
            seq, why = self._start(job['slot'], FEED_MODES[job['kind']],
                                   piece, job['speed'], job['free_tip'])
        if seq is None:
            if job.get('stop_when'):
                self._arm_stop(job['slot'], disarm=True)
            return why
        now = self._now()
        job['seq'] = seq
        job['piece'] = piece
        job['t0'] = now
        job['enc0'] = (self._enc[1][job['slot']]
                       if self._fresh(self._enc) else None)
        job['deadline'] = (now + 1.5 * piece / max(job['speed'], 1.0)
                           + DEADLINE_EXTRA_S)
        return 'ok'

    def _arm_stop(self, lane, disarm=False):
        """Arm the at-rest and held stops for the lane's next start
        (ace2k_feed_stop_when), or disarm them. True when the unit took it."""
        flag = 0 if disarm else 1
        ans = self._call('ace2k_feed_stop_when',
                         'ace2k_feed_stop_when_response', lane=lane,
                         held=flag, at_rest=flag, held_ms=0)
        return bool(ans is not None and ans.get('accepted'))

    def _lane_full(self, lane):
        """This lane holds the shared pulled reading: its plunger neither
        at rest nor taut while the pulled input is set (the firmware's own
        definition of a lane's full)."""
        if not self._fresh(self._sensors):
            return None
        sw = self._sensors[1]
        if not sw & (1 << SW_PULLED_BIT):
            return False
        return not (sw & (1 << (SW_REST_SHIFT + lane))
                    or sw & (1 << (SW_PUSHED_SHIFT + lane)))

    def _lane_buffer_text(self, lane):
        """The lane's plunger and insert switches from the newest sensor
        state (report plus the edges since), for the log of a move that
        ended in an error."""
        if self._sensors is None:
            return 'switch state unknown'
        sw = self._sensors[1]
        if sw & (1 << (SW_PUSHED_SHIFT + lane)):
            plunger = 'taut'
        elif sw & (1 << (SW_REST_SHIFT + lane)):
            plunger = 'at rest'
        elif sw & (1 << SW_PULLED_BIT):
            plunger = 'full'
        else:
            plunger = 'between switches'
        return ('plunger %s, insert %s (report %.1fs old)'
                % (plunger, 'present' if sw & (1 << lane) else 'empty',
                   self._now() - self._sensors[0]))

    def _relax_buffer(self, params, rid):
        """Take the slack out of a lane whose buffer stands full: one
        bounded rollback that the unit ends at rest (or held). Result
        started False when there is nothing to do (plunger not full)."""
        def reply(code, msg='success', result=None):
            return {'id': rid, 'code': code, 'msg': msg,
                    'result': result or {}}
        try:
            lane = int(params.get('index', -1))
            length = float(params.get('length', 0))
            speed = float(params.get('speed', 0) or 0)
        except (TypeError, ValueError):
            return reply(FAILED, 'bad params')
        if not 0 <= lane <= 3 or length <= 0:
            return reply(FAILED, 'bad params')
        if not self.dictionary.has('ace2k_feed_stop_when'):
            return reply(FAILED, 'no_stop_when')
        self._yield_auto(lane)
        if lane in self._jobs or lane in self._follow \
                or lane in self._tag_reads:
            return reply(1, 'FORBIDDEN')
        full = self._lane_full(lane)
        if not full:
            return reply(0, result={'started': False,
                                    'full': full is not None and full})
        self._motion_seq += 1
        job = {'kind': 'retract', 'slot': lane, 'target': length,
               'done': 0.0, 'speed': speed if speed > 0 else SPEED_MAX_MM_S,
               'free_tip': False, 'stop_when': True, 'relax': True,
               'mseq': self._motion_seq}
        res = self._start_piece(job)
        if res != 'ok':
            job['stop'] = res
            self._last_motion = self._motion_of(job, False)
            self._lane_motion[lane] = self._last_motion
            logging.info(self._lp + 'buffer relax lane %d refused '
                         '(%s)' % (lane, res))
            if res == 'busy':
                return reply(1, 'FORBIDDEN')
            return reply(FAILED, res)
        self._jobs[lane] = job
        return reply(0, result={'started': True, 'seq': job['mseq']})

    def _note_inserts(self, old, new):
        """An insert switch changed (filament put in or taken out): that
        lane's motion since the insert starts over."""
        changed = (old ^ new) & 0xF
        for lane in range(4):
            if changed & (1 << lane):
                self._moved_mm[lane] = 0.0

    def _end_job(self, lane, why, stop=None):
        job = self._jobs.pop(lane, None)
        if job is None:
            return
        done = float(job.get('done', 0.0))
        self._moved_mm[lane] += -done if job['kind'] == 'retract' else done
        if stop is not None:
            job['stop'] = stop
        job['why'] = why
        if job.get('auto'):
            # The link's own buffer relax: not a move the host asked for,
            # so the motion report keeps the host's last move.
            logging.info(self._lp + 'full-buffer watch: lane %d rolled '
                         'back %.0f mm (%s)' % (lane, job['done'], why))
        else:
            self._last_motion = self._motion_of(job, False)
            self._lane_motion[lane] = self._last_motion
            logging.info(self._lp + '%s lane %d ended (%s) after %.0f mm'
                         % (job['kind'], lane, why, job['done']))
        if lane in self._follow_want and lane not in self._follow:
            # The follow this move took over (a feed), or one the host
            # armed while the move ran, starts now.
            seq, res = self._start_follow(lane)
            if seq is None:
                logging.info(self._lp + 'follow on lane %d not '
                             're-armed after the move (%s)' % (lane, res))
                self._note_follow_lost(lane, res)

    @staticmethod
    def _motion_of(job, running):
        out = {'seq': int(job['mseq']), 'slot': int(job['slot']),
               'kind': str(job['kind']),
               'mm': round(float(job['done']), 1), 'running': bool(running)}
        if job.get('stop'):
            out['stop'] = str(job['stop'])
        if job.get('why'):
            out['why'] = str(job['why'])
        if job['kind'] == 'retract':
            out['target'] = round(float(job['target']), 1)
            if not running:
                short = max(0.0, job['target'] - job['done'])
                if (job.get('stopped') or job.get('unmeasured')
                        or short <= RETRACT_SHORT_TOL_MM):
                    short = 0.0
                out['short'] = round(short, 1)
            if job.get('unmeasured'):
                out['unmeasured'] = True
        return out

    def _work_events(self):
        events, self._events = self._events, []
        for ev in events:
            self._on_event(ev)

    def _on_event(self, ev):
        lane = int(ev.get('lane', 255))
        kind = int(ev.get('kind', -1))
        name = KIND_NAMES.get(kind, 'kind_%d' % kind)
        seq = int(ev.get('seq', 0))
        moved = abs(int(ev.get('filament_um', 0))) / 1000.0
        if kind in NOTICE_KINDS:
            logging.info(self._lp + 'lane %d notice %s' % (lane, name))
            return
        read = self._tag_reads.get(lane)
        if read is not None and (
                (seq == 0 and int(ev.get('mode', -1)) == 4)
                or (seq and read.get('seq') == seq)):
            # The terminal event of the unit's load that carried the read:
            # the read with motion (seq 0, mode loading) or the load we
            # started on an insert (its own seq).
            self._end_tag_read(lane, name, moved)
            return
        if self._follow.get(lane) == seq and seq:
            # The follow ended.
            self._follow.pop(lane, None)
            if kind in ERROR_KINDS:
                self._clear_lane(lane, 'follow %s' % name)
            if name != 'stopped':
                logging.warning(self._lp + 'follow on lane %d ended '
                                '(%s)' % (lane, name))
            if name == 'stopped_link' and lane in self._follow_want:
                self._start_follow(lane)
            if name != 'stopped':
                self._note_follow_lost(lane, name)
            return
        job = self._jobs.get(lane)
        if job is None or job.get('seq') != seq or not seq:
            if seq and self._lost_seq.get(lane) == seq:
                self._lost_seq.pop(lane, None)
                logging.info(self._lp + 'lane %d end event %s (%.0f mm) '
                             'came after the move was taken as lost'
                             % (lane, name, moved))
            return
        job['done'] += moved
        if name == 'done' and not job.get('stopping'):
            if job['target'] - job['done'] > REMAIN_MIN_MM:
                res = self._start_piece(job)
                if res != 'ok':
                    self._end_job(lane, 'next piece refused (%s)' % res,
                                  stop=res)
                return
            self._end_job(lane, 'done', stop='distance')
            return
        if kind in ERROR_KINDS or name == 'blocked':
            # Where the strand was held: taut = toward the head, full = slack
            # in the unit (an obstacle ahead), at rest = nothing pulling.
            logging.info(self._lp + 'lane %d %s %s: %s'
                         % (lane, job['kind'], name,
                            self._lane_buffer_text(lane)))
        if kind in ERROR_KINDS:
            self._clear_lane(lane, name)
        stop = name
        if name == 'stopped' or name in RELAX_END_KINDS:
            job['stopped'] = True
        elif job['kind'] == 'feed' and (
                kind in ERROR_KINDS or name == 'blocked') \
                and moved < SLIP_MOVED_MM:
            # The filament did not come out of the unit: slip / tangle at
            # the MMU (the load-fail wording reads 'slip').
            stop = 'slip'
        self._end_job(lane, name, stop=stop)

    def _tick(self):
        """Work through the feed events, then catch a piece whose end
        event was lost (the lane idle in the report, or the deadline)."""
        self._work_events()
        now = self._now()
        self._flog_advance(now)
        fresh = self._fresh(self._feed)
        modes = bytearray(self._feed[1].get('mode', b'')) if fresh else b''
        for lane in list(self._jobs):
            job = self._jobs[lane]
            if job.get('waiting'):
                # A start the unit refused as busy (its tie-break).
                limit = job['deadline']
                res = self._start_piece(job)
                if res == 'ok':
                    job.pop('waiting', None)
                    logging.info(self._lp + '%s lane %d started after '
                                 'the unit freed the lane'
                                 % (job['kind'], lane))
                elif res != 'busy' or now >= limit:
                    job.pop('waiting', None)
                    self._end_job(lane, 'start refused (%s)' % res,
                                  stop=res)
                else:
                    job['t0'], job['deadline'] = now, limit
                continue
            idle = (fresh and lane < len(modes)
                    and modes[lane] not in MOVING_MODES
                    and self._feed[0] >= job['t0'] + LOST_EVENT_S)
            if idle or now >= job['deadline']:
                if not idle:
                    self._stop_lane(lane)
                # The end event was lost: the lane encoder says how far
                # the piece ran. Without a reading nothing is booked, so
                # a retract reads short instead of done.
                ran = self._lost_piece_mm(lane, job) if idle else None
                if (idle and ran is None
                        and now < job.setdefault('lost_t', now)
                        + LOST_ENCODER_WAIT_S):
                    # The end event, or the counters report after the idle
                    # one, can still follow.
                    continue
                if ran is not None:
                    job['done'] += ran
                else:
                    # Nothing measured: the motion report must not claim
                    # the move came back short.
                    job['unmeasured'] = True
                self._lost_seq[lane] = job.get('seq')
                self._end_job(lane, 'no end event (%s%s)'
                              % ('lane idle' if idle else 'deadline',
                                 '' if ran is None else ', encoder'),
                              stop='lost_event' if idle else 'timeout')
        self._relax_watch(now)
        self._follow_full_watch(now)
        self._lanes_tick(now)
        for lane in list(self._follow):
            if (fresh and lane < len(modes)
                    and modes[lane] not in FOLLOW_MODES
                    and self._feed[0] >= self._follow_t0.get(lane, 0.0)
                    + LOST_EVENT_S):
                # The follow ended without an event we saw: the lane would
                # starve the extruder (no freewheel). Armed again while the
                # host still wants it.
                self._follow.pop(lane, None)
                logging.warning(self._lp + 'follow on lane %d no '
                                'longer running (report mode %d)'
                                % (lane, modes[lane]))
                if lane in self._follow_want and lane not in self._jobs:
                    self._start_follow(lane)
                    self._note_follow_lost(lane, 'mode %d' % modes[lane])

    def _lost_piece_mm(self, lane, job):
        """Filament travel of a piece whose end event was lost, from the
        lane encoder report: the reading taken at the start against one
        taken after the lane went idle. None without both."""
        enc0 = job.get('enc0')
        if enc0 is None or not self._fresh(self._enc):
            return None
        if self._enc[0] < job['t0'] + LOST_EVENT_S:
            return None
        if self._fresh(self._feed) and self._enc[0] < self._feed[0]:
            # The counters were read before the report that showed the
            # lane idle: the piece may still have been running.
            return None
        ran = abs(self._enc[1][lane] - enc0) / 1000.0
        return min(ran, float(job.get('piece', ran)))

    def _relax_watch(self, now):
        """Roll back a resting lane that holds the shared pulled reading
        while another lane of the unit follows (its assist is paused as
        long as the reading stands). Once per full episode."""
        if not self.dictionary.has('ace2k_feed_stop_when') \
                or not self._fresh(self._sensors):
            self._full_since.clear()
            return
        sw = self._sensors[1]
        for lane in range(4):
            full = self._lane_full(lane)
            if not full:
                self._full_since.pop(lane, None)
                self._full_relaxed.discard(lane)
                continue
            resting = (lane not in self._jobs and lane not in self._follow
                       and lane not in self._tag_reads
                       and lane not in self._sessions)
            harmed = any(ln != lane for ln in self._follow)
            if not (resting and harmed and sw & (1 << lane)):
                self._full_since.pop(lane, None)
                continue
            if lane in self._full_relaxed:
                continue
            if now - self._full_since.setdefault(lane, now) < RELAX_WATCH_S:
                continue
            self._full_relaxed.add(lane)
            self._full_since.pop(lane, None)
            job = {'kind': 'retract', 'slot': lane,
                   'target': RELAX_WATCH_MM, 'done': 0.0,
                   'speed': RELAX_WATCH_SPEED_MM_S, 'free_tip': False,
                   'stop_when': True, 'relax': True, 'auto': True,
                   'mseq': 0}
            res = self._start_piece(job)
            if res != 'ok':
                logging.info(self._lp + 'full-buffer watch: lane %d '
                             'not rolled back (%s)' % (lane, res))
                continue
            self._jobs[lane] = job
            logging.info(self._lp + 'full-buffer watch: resting lane %d '
                         'held the buffer full for %.0f s - rolling back up '
                         'to %.0f mm' % (lane, RELAX_WATCH_S,
                                         RELAX_WATCH_MM))

    def _lane_report(self):
        """The image takes the lanes in use and the lanes set by hand
        (v0.15.0)."""
        d = getattr(self, 'dictionary', None)
        return bool(d is not None and d.has('ace2k_rfid_in_use')
                    and d.has('ace2k_rfid_by_hand'))

    def _set_lanes(self, params):
        """The host's lists: lanes whose tip is in a head (in_use) and
        lanes whose material the user named or whose read the host kept
        (by_hand), one bit per lane."""
        if not self._lane_report():
            return {'lane_report': False}
        try:
            in_use = int(params.get('in_use', 0)) & 0x0F
            by_hand = int(params.get('by_hand', 0)) & 0x0F
        except (TypeError, ValueError):
            return {'lane_report': True}
        now = self._now()
        self._in_use = in_use
        for lane in range(4):
            if by_hand & (1 << lane):
                self._by_hand_want.setdefault(lane, now)
            else:
                self._by_hand_want.pop(lane, None)
        if self._lanes_first:
            # The first list of this connection: a lane the unit may hold
            # as read without a record on the host is forgotten, so its
            # next move (or the unit's tie-break) reads it again. A lane
            # set by hand keeps its mark.
            self._lanes_first = False
            marked = self._by_hand_unit or 0
            for lane in range(4):
                if by_hand & (1 << lane) or marked & (1 << lane):
                    continue
                self._send('ace2k_rfid_forget', lane=lane)
        self._lanes_tick(now)
        return {'lane_report': True}

    def _lanes_tick(self, now):
        """Keep the unit's in-use mask current and its by-hand marks in
        line with what the host wants."""
        if not self._lane_report() or self._in_use is None:
            return
        mask = self._in_use
        for lane in self._follow:
            mask |= 1 << lane
        for lane, job in self._jobs.items():
            if not job.get('auto'):
                mask |= 1 << lane
        t_sent, m_sent = self._in_use_sent
        if mask != m_sent or now - t_sent >= IN_USE_PERIOD_S:
            self._send('ace2k_rfid_in_use', mask=mask)
            self._in_use_sent = (now, mask)
        sw = self._sensors[1] if self._sensors is not None else 0
        marked = self._by_hand_unit or 0
        for lane, t0 in list(self._by_hand_want.items()):
            if marked & (1 << lane):
                continue
            if lane in self._tag_reads or lane in self._sessions:
                continue
            if not sw & (1 << lane):
                if now - t0 > BY_HAND_EMPTY_DROP_S:
                    self._by_hand_want.pop(lane, None)
                continue
            if now - self._by_hand_sent.get(lane, 0.0) < IN_USE_PERIOD_S:
                continue
            self._by_hand_sent[lane] = now
            self._send('ace2k_rfid_by_hand', lane=lane)

    def _follow_full_watch(self, now):
        """Stop a follow whose own lane stays full although it keeps
        taking filament back: the plunger hangs at its full end, and the
        take-backs would pull the strand out of the toolhead. Reported
        to the host as a lost follow (buffer_full)."""
        bursts = self._bursts
        for lane in list(self._follow_full):
            if lane not in self._follow:
                self._follow_full.pop(lane, None)
        for lane in list(self._follow):
            count = (bursts[lane] if bursts and lane < len(bursts)
                     else None)
            if (lane in self._jobs or count is None
                    or not self._lane_full(lane)):
                self._follow_full.pop(lane, None)
                continue
            t0, b0 = self._follow_full.setdefault(lane, (now, count))
            if count < b0:
                # the counter starts over with each mode entry
                self._follow_full[lane] = (now, count)
                continue
            if now - t0 < FOLLOW_FULL_S or count - b0 < FOLLOW_FULL_BURSTS:
                continue
            self._follow_full.pop(lane, None)
            logging.warning(self._lp + 'lane %d stayed full for %.1f s '
                            'through %d take-backs - plunger stuck at the '
                            'full end, follow stopped'
                            % (lane, now - t0, count - b0))
            self._stop_follow(lane)
            self._note_follow_lost(lane, 'buffer_full')

    def _yield_auto(self, lane):
        """A host request on a lane the full-buffer watch is rolling
        back: the watch gives way."""
        job = self._jobs.get(lane)
        if job is None or not job.get('auto'):
            return
        job['stopping'] = True
        self._stop_lane(lane)
        self._pump(STOP_EVENT_WAIT_S)
        self._work_events()
        if lane in self._jobs:
            self._end_job(lane, 'gave way to the host')

    def _motion(self, method, params, rid):
        def reply(code, msg='success'):
            return {'id': rid, 'code': code, 'msg': msg, 'result': {}}
        try:
            lane = int(params.get('index', -1))
        except (TypeError, ValueError):
            lane = -1
        if 0 <= lane <= 3:
            self._yield_auto(lane)
        if method in ('update_feeding_speed', 'update_unwinding_speed'):
            return reply(0)
        if method in ('stop_feed_filament', 'stop_unwind_filament'):
            lanes = [lane] if 0 <= lane <= 3 else list(self._jobs)
            for ln in lanes:
                job = self._jobs.get(ln)
                if job is None:
                    continue
                job['stopping'] = True
                self._stop_lane(ln)
            if any(ln in self._jobs for ln in lanes):
                self._pump(STOP_EVENT_WAIT_S)
                self._work_events()
            return reply(0)
        if method == 'stop_feed_assist':
            lanes = [lane] if 0 <= lane <= 3 else list(self._follow)
            for ln in lanes:
                self._follow_want.discard(ln)
                self._follow_lost.pop(ln, None)
                self._stop_follow(ln)
            return reply(0)
        if not 0 <= lane <= 3:
            return reply(FAILED, 'bad index')
        if method == 'start_feed_assist':
            self._follow_want.add(lane)
            if lane in self._follow:
                return reply(0)
            if lane in self._jobs:
                # Follows when the move ends.
                return reply(0)
            seq, why = self._start_follow(lane)
            if seq is None:
                if why == 'busy':
                    return reply(1, 'FORBIDDEN')
                self._follow_want.discard(lane)
                return reply(FAILED, why)
            return reply(0)
        if lane in self._jobs:
            return reply(1, 'FORBIDDEN')
        try:
            length = float(params.get('length', 0))
        except (TypeError, ValueError):
            length = 0.0
        try:
            speed = float(params.get('speed', 0) or 0)
        except (TypeError, ValueError):
            speed = 0.0
        if length <= 0:
            return reply(FAILED, 'bad length')
        kind = 'retract' if method == 'unwind_filament' else 'feed'
        if lane in self._follow:
            # A move on a following lane: the follow gives way (back after
            # a feed when still wanted, not after a retract).
            self._stop_follow(lane)
            if kind == 'retract':
                self._follow_want.discard(lane)
                self._follow_lost.pop(lane, None)
        self._motion_seq += 1
        job = {'kind': kind, 'slot': lane, 'target': length, 'done': 0.0,
               'speed': speed if speed > 0 else SPEED_MAX_MM_S,
               'free_tip': kind == 'feed' and length > FREE_TIP_MIN_MM,
               'mseq': self._motion_seq}
        res = self._start_piece(job)
        if res != 'ok':
            job['stop'] = res
            self._last_motion = self._motion_of(job, False)
            self._lane_motion[lane] = self._last_motion
            logging.info(self._lp + '%s lane %d refused (%s)'
                         % (kind, lane, res))
            if res == 'busy' and self._lane_report():
                # The unit's own tie-break holds the lane: it goes back
                # now, the start is sent again until the lane is free.
                now = self._now()
                job.pop('stop', None)
                job.update(waiting=True, t0=now,
                           deadline=now + START_BUSY_RETRY_S)
                self._jobs[lane] = job
                return reply(0)
            if res == 'busy':
                return reply(1, 'FORBIDDEN')
            return reply(FAILED, res)
        self._jobs[lane] = job
        return reply(0)

    # -- tags (M6d) -------------------------------------------------------

    def _forget_lane_tag(self, lane):
        """The lane lost its filament: its UID, record and session go."""
        self._tag_uid[lane] = b''
        self._tag_rec[lane] = None
        sess = self._sessions.get(lane)
        if sess is not None:
            sess['closed'] = True

    def _serve_sessions(self):
        """Answer every open tag session (one reader handles one session at
        a time; the unit queues the others)."""
        if not self._ready or self.dictionary is None:
            return
        # A session the last exchange's tail announced is still unread.
        self._pump()
        for _ in range(4):
            lanes = [lane for lane, sess in self._sessions.items()
                     if not sess.get('served')]
            if not lanes:
                return
            for lane in lanes:
                sess = self._sessions[lane]
                sess['served'] = True
                try:
                    self._run_session(lane, sess)
                except (IOError, OSError):
                    raise
                except Exception:
                    # A decoding bug must not take the link down.
                    logging.exception(self._lp + 'tag session on '
                                      'lane %d failed' % lane)
                finally:
                    if self._sessions.get(lane) is sess:
                        self._sessions.pop(lane, None)

    def _tag_step(self, lane, sess, step):
        """Send one step and collect its frames: the list, 'closed' (the
        unit ended the session) or None (no answer in time)."""
        op, arg, blocks, key = step
        del sess['frames'][:]
        if self._send('ace2k_rfid_step', lane=lane, session=sess['session'],
                      op=op, arg=arg, blocks=blocks, key=key) is None:
            return None
        want = blocks if op == 0 else bin(blocks).count('1')
        deadline = self._now() + TAG_STEP_TIMEOUT_S
        misses = 0
        max_misses = int(TAG_STEP_TIMEOUT_S / max(READ_MISS_S, 0.001)) + 1
        while True:
            frames = sess['frames']
            if any(f[1] != 0 for f in frames) or len(frames) >= want:
                return list(frames)
            if sess.get('closed'):
                return 'closed'
            if self._now() >= deadline:
                return None
            chunk = self._read_some()
            if not chunk:
                misses += 1
                if misses >= max_misses:
                    return None
                continue
            for _seq, body in self._feed_rx(chunk):
                if body:
                    self._handle_body(body, None)

    def _run_session(self, lane, sess):
        """One tag session: the plan's steps, then the record (or the UID
        alone), then ace2k_rfid_done. The rules are ace2k's own: a refused
        key moves to the next brand, an error re-sends the step once, a
        step without an answer gives up, a lost tag ends it."""
        plan = TagPlan(sess['uid'], sess['sak'])
        uid_text = sess['uid'].hex().upper()
        outcome = None
        errors = {}
        for _ in range(TAG_SESSION_MAX_STEPS):
            step = plan.next_step()
            if step is None:
                break
            got = self._tag_step(lane, sess, step)
            if got == 'closed':
                outcome = 'closed'
                break
            if got is None:
                outcome = 'timeout'
                break
            res = plan.feed(step, got)
            if res == 'gone':
                outcome = 'gone'
                break
            if res == 'error':
                k = (step[0], step[1])
                errors[k] = errors.get(k, 0) + 1
                if errors[k] >= 2:
                    outcome = 'error'
                    break
        rec = plan.result() if outcome in (None, 'error') else None
        if outcome in ('closed', 'gone'):
            logging.info(self._lp + 'tag lane %d session ended by the '
                         'unit (%s, UID %s)' % (lane, outcome, uid_text))
            return
        if rec is None and (outcome == 'timeout' or (
                outcome == 'error' and plan.kind != 'ntag')):
            self._send('ace2k_rfid_done', lane=lane,
                       session=sess['session'], result=0)
            logging.info(self._lp + 'tag lane %d not read (%s, UID '
                         '%s) - the next move reads it again'
                         % (lane, outcome, uid_text))
            return
        if rec is None:
            rec = {'fmt': plan.kind if plan.kind != 'other' else ''}
        rec['uid'] = uid_text
        self._send('ace2k_rfid_done', lane=lane, session=sess['session'],
                   result=1)
        self._tag_rec_n += 1
        rec['n'] = self._tag_rec_n
        self._tag_rec[lane] = rec
        job = self._tag_reads.get(lane)
        if job is not None:
            job['got'] = 'read' if rec.get('type') else 'uid'
        logging.info(self._lp + 'tag lane %d read: %s %s %s (UID %s)'
                     % (lane, rec.get('fmt') or '?', rec.get('type') or '-',
                        rec.get('brand') or '', uid_text))

    def _tag_read_motion(self, params, rid):
        """The unit's read with motion on one lane (insert, ACE_TAG_READ):
        accepted -> code 0 and a job until the unit's load ends."""
        def reply(code, msg='success'):
            return {'id': rid, 'code': code, 'msg': msg, 'result': {}}
        try:
            lane = int(params.get('index', -1))
        except (TypeError, ValueError):
            lane = -1
        if not 0 <= lane < 4:
            return reply(FAILED, 'bad lane')
        d = self.dictionary
        if d is None or not d.has('ace2k_rfid_read'):
            return reply(UNSUPPORTED, 'unsupported:tag_read_motion')
        if lane in self._tag_reads:
            return reply(0)
        self._yield_auto(lane)
        if lane in self._jobs:
            return reply(1, 'FORBIDDEN')
        if self._lane_report() and (
                lane in self._by_hand_want
                or (self._by_hand_unit or 0) & (1 << lane)):
            # Read tag on a lane set by hand: the mark goes first, or the
            # reader gives this lane no tag.
            self._by_hand_want.pop(lane, None)
            self._send('ace2k_rfid_forget', lane=lane)
        values = {'lane': lane, 'move': 1}
        if 'seq' in [k for k, _t in d.commands['ace2k_rfid_read'][1]]:
            seq = self._tag_read_seq.get(lane, 0) % 255 + 1
            self._tag_read_seq[lane] = seq
            values['seq'] = seq
        ans = self._call('ace2k_rfid_read', 'ace2k_rfid_read_response',
                         **values)
        if ans is None:
            return reply(FAILED, 'no answer')
        if not ans.get('accepted'):
            why = {1: 'busy', 2: 'no_filament', 3: 'no_link',
                   4: 'other_lane_moving', 5: 'unsupported'}.get(
                       int(ans.get('reason', 0)),
                       'reason_%d' % int(ans.get('reason', 0)))
            logging.info(self._lp + 'tag read lane %d refused (%s)'
                         % (lane, why))
            if why in ('busy', 'other_lane_moving'):
                return reply(1, 'FORBIDDEN')
            return reply(FAILED, why)
        now = self._now()
        speed = LOAD_SPEED_UM_S / 1000.0
        self._tag_reads[lane] = {
            't0': now, 'got': None,
            'deadline': now + 2.0 * (TAG_SEARCH_UM / 1000.0) / speed
            + TAG_SEARCH_EXTRA_S}
        logging.info(self._lp + 'tag read with motion on lane %d '
                     'started' % lane)
        return reply(0)

    def _tag_read_load(self, params, rid):
        """The unit's own load on a freshly inserted lane (ace2k_feed_start
        mode 5): settle while the hand lets go, pull to the parking
        distance, search past it for a tag not read yet, back to the
        parking distance. Its return ends there, never at the insert
        sensor, where the read with motion from a bare insert came back to
        (the tip a few mm before the gear: one count of overshoot and the
        sensor fell, the unit ended runout and the filament dropped out).
        accepted -> code 0 and a read job until the load's terminal event."""
        def reply(code, msg='success'):
            return {'id': rid, 'code': code, 'msg': msg, 'result': {}}
        try:
            lane = int(params.get('index', -1))
        except (TypeError, ValueError):
            lane = -1
        if not 0 <= lane < 4:
            return reply(FAILED, 'bad lane')
        d = self.dictionary
        if d is None or not (d.has('ace2k_rfid_read')
                             and d.has('ace2k_feed_start')):
            return reply(UNSUPPORTED, 'unsupported:tag_read_load')
        if lane in self._tag_reads:
            return reply(0)
        self._yield_auto(lane)
        if lane in self._jobs or lane in self._follow:
            return reply(1, 'FORBIDDEN')
        park_mm = self._load_park_um / 1000.0
        speed = self._load_speed_um_s / 1000.0
        seq, why = self._start(lane, FEED_MODES['load'], park_mm, speed)
        if seq is None and why == 'in_error':
            self._clear_lane(lane, 'load start')
            seq, why = self._start(lane, FEED_MODES['load'], park_mm, speed)
        if seq is None:
            logging.info(self._lp + 'load on lane %d refused (%s)'
                         % (lane, why))
            if why in ('busy', 'other_lane'):
                return reply(1, 'FORBIDDEN')
            return reply(FAILED, why)
        now = self._now()
        self._tag_reads[lane] = {
            't0': now, 'got': None, 'seq': seq, 'load': True,
            'deadline': now + 2.0 * (TAG_SEARCH_UM / 1000.0) / speed
            + TAG_SEARCH_EXTRA_S}
        logging.info(self._lp + 'load on lane %d started (park %.0f '
                     'mm, tag search past it)' % (lane, park_mm))
        return reply(0)

    def _end_tag_read(self, lane, why, moved=0.0):
        job = self._tag_reads.pop(lane, None)
        if job is None:
            return
        end = job.get('got')
        if end is None:
            state = None
            ans = self._call('ace2k_rfid_lane_query', 'ace2k_rfid_lane_state',
                             lane=lane)
            if ans is not None:
                state = int(ans.get('state', -1))
            end = {5: 'no_tag', 0: 'no_filament'}.get(
                state, 'unread' if why in ('loaded', 'done') else why)
        self._tag_search_n[lane] += 1
        self._tag_search_end[lane] = end
        logging.info(self._lp + '%s on lane %d ended (%s): %s'
                     % ('load' if job.get('load') else 'tag read with motion',
                        lane, why, end))

    def _tag_tick(self):
        """End a read with motion whose terminal event was lost."""
        now = self._now()
        fresh = self._fresh(self._feed)
        modes = bytearray(self._feed[1].get('mode', b'')) if fresh else b''
        for lane in list(self._tag_reads):
            job = self._tag_reads[lane]
            idle = (fresh and lane < len(modes) and modes[lane] != 4
                    and self._feed[0] >= job['t0'] + LOST_EVENT_S)
            if idle or now >= job['deadline']:
                if not idle:
                    self._stop_lane(lane)
                self._end_tag_read(lane, 'lost_event' if idle else 'timeout')

    # -- follow instrument -------------------------------------------------
    #
    # The follow pushes on taut and pauses while the shared pulled reading
    # is set, whichever lane's plunger sets it (ace2k feed_assist.c). One
    # line per window answers whether the follow kept feeding while another
    # lane moved: the bursts per lane, the time the shared full was set,
    # the time each lane held it (its plunger neither at rest nor taut while
    # it was set), and the time a following lane stood taut under it.

    def _flog_open(self, now):
        return {'t0': now, 'last': now, 'full': 0.0, 'full_edges': 0,
                'taut': [0.0] * 4, 'rest': [0.0] * 4, 'held': [0.0] * 4,
                'starved': [0.0] * 4, 'follow': set(self._follow),
                'moves': {},
                'b0': list(self._bursts) if self._bursts else None}

    def _flog_add(self, fw, now):
        dt = now - fw['last']
        fw['last'] = now
        if dt <= 0 or self._sensors is None:
            return
        sw = self._sensors[1]
        pulled = bool(sw & (1 << SW_PULLED_BIT))
        if pulled:
            fw['full'] += dt
        for lane in range(4):
            rest = bool(sw & (1 << (SW_REST_SHIFT + lane)))
            taut = bool(sw & (1 << (SW_PUSHED_SHIFT + lane)))
            if taut:
                fw['taut'][lane] += dt
            if rest:
                fw['rest'][lane] += dt
            if pulled and not rest and not taut:
                fw['held'][lane] += dt
            if lane in self._follow:
                fw['follow'].add(lane)
                if taut and pulled:
                    fw['starved'][lane] += dt
        for lane, job in self._jobs.items():
            fw['moves'].setdefault(lane, set()).add(job.get('kind', 'move'))
        for lane in self._tag_reads:
            fw['moves'].setdefault(lane, set()).add('tag read')

    def _flog_close(self, fw, now):
        dur = now - fw['t0']
        if dur < 1.0:
            return
        b0, b1 = fw['b0'], self._bursts
        parts = ['full %.1fs' % fw['full']]
        if fw['full_edges']:
            parts[0] += ' (%d edge%s)' % (
                fw['full_edges'], '' if fw['full_edges'] == 1 else 's')
        for lane in range(4):
            follow = lane in fw['follow']
            moves = sorted(fw['moves'].get(lane, ()))
            held = fw['held'][lane]
            if not (follow or moves or held >= 0.05):
                continue
            what = ['follow'] if follow else []
            what += moves
            text = 'lane %d %s:' % (lane, '+'.join(what) or 'idle')
            if follow:
                if b0 is not None and b1 is not None and lane < len(b0) \
                        and lane < len(b1):
                    # a counter below its start was cleared by a new mode
                    d = b1[lane] - b0[lane] if b1[lane] >= b0[lane] \
                        else b1[lane]
                    text += ' bursts +%d' % d
                else:
                    text += ' bursts ?'
            text += ' taut %.1fs rest %.1fs' % (fw['taut'][lane],
                                                fw['rest'][lane])
            if follow:
                text += ' starved %.1fs' % fw['starved'][lane]
            if held >= 0.05:
                text += ' held full %.1fs' % held
            parts.append(text)
        logging.info('[multiACE] ace2k follow %s %.1fs: %s'
                     % (self.label or 'unit', dur, ' | '.join(parts)))

    def _flog_advance(self, now):
        """Sum the readings up to `now` into the open window (with the
        state before the change the caller is about to apply), close a
        window that is due, open one while a follow runs."""
        fw = self._fw
        if fw is not None:
            self._flog_add(fw, now)
            if not self._follow:
                self._flog_close(fw, now)
                self._fw = None
                return
            if now - fw['t0'] < FOLLOW_LOG_WINDOW_S:
                return
            self._flog_close(fw, now)
            self._fw = None
        if self._follow:
            self._fw = self._flog_open(now)

    def _fresh(self, entry):
        return entry is not None and self._now() - entry[0] <= REPORT_FRESH_S

    def _status(self, ok):
        if not self._fresh(self._sensors):
            # The first reports arrive within their period after the query.
            self._pump(1.2)
        if not self._fresh(self._sensors):
            # No fresh presence = no answer (contract 5.3): the watchdog
            # stays the only judge of a silent unit.
            self._log_once('stale', 'no sensor report - status withheld')
            return None
        out = make_status(
            self._sensors[1],
            self._feed[1] if self._fresh(self._feed) else None,
            self._env[1] if self._fresh(self._env) else None,
            self._rfid[1] if self._fresh(self._rfid) else None,
            self._tag_uid)
        if self._lane_report():
            # The host sends its lanes in use / set by hand (set_lanes).
            out['lane_report'] = True
        dry = self._dryer_status()
        if dry is not None:
            out['dryer_status'] = dry
        for lane, slot in enumerate(out.get('slots') or []):
            if self._tag_rec[lane] is not None:
                slot['tag_read'] = dict(self._tag_rec[lane])
            if lane in self._tag_reads:
                slot['tag_search'] = True
            if self._tag_search_n[lane]:
                slot['tag_search_n'] = self._tag_search_n[lane]
                slot['tag_search_end'] = self._tag_search_end[lane]
            if abs(self._moved_mm[lane]) >= 0.5:
                slot['moved_mm'] = round(self._moved_mm[lane], 1)
            lost = self._follow_lost.get(lane)
            if (lost is not None and lane in self._follow_want
                    and lane not in self._follow):
                slot['follow_lost'] = {'why': lost[0], 'n': lost[1]}
            # The move of THIS lane: two lanes can move at once (a bg op
            # next to a load on the same unit), the unit 'motion' below
            # only shows the newest one.
            job = self._jobs.get(lane)
            if job is not None and not job.get('auto'):
                slot['motion'] = self._motion_of(job, True)
            elif self._lane_motion[lane] is not None:
                slot['motion'] = dict(self._lane_motion[lane])
        if self._tag_reads:
            out['status'] = 'busy'
        # Distance of the running (or last) move, read by the load loop and
        # the calibration (like the CFS one).
        host_jobs = [j for j in self._jobs.values() if not j.get('auto')]
        if host_jobs:
            out['status'] = 'busy'
            job = max(host_jobs, key=lambda j: j['mseq'])
            out['motion'] = self._motion_of(job, True)
        elif self._last_motion is not None:
            out['motion'] = dict(self._last_motion)
        if out['status'] == 'busy':
            # Which lanes hold the unit busy: the lanes move independently
            # (lanes_concurrent), so ace.py can let the host act on another
            # lane while the bg engine drives these.
            busy = set(ln for ln, j in self._jobs.items()
                       if not j.get('auto')) | set(self._tag_reads)
            for lane, slot in enumerate(out.get('slots') or []):
                if slot.get('lane_mode') in MOVING_MODE_NAMES:
                    busy.add(lane)
            out['busy_lanes'] = sorted(busy)
        return ok(out)


def make_status(switches, feed=None, env=None, tags=None, uids=None):
    """Host status dict from the insert switches (bit n = lane n), the
    feed state, the environment report and the tag states."""
    slots = []
    modes = bytearray((feed or {}).get('mode', b''))
    for i in range(4):
        s = {'index': i,
             'status': 'ready' if switches & (1 << i) else 'empty',
             'sku': '', 'type': '', 'rfid': 0, 'brand': '',
             'color': [0, 0, 0]}
        if tags is not None and i < len(tags):
            s['tag_state'] = TAG_STATES.get(tags[i], 'state_%d' % tags[i])
            if tags[i] in (2, 3):
                s['rfid'] = 3
        if uids is not None and uids[i]:
            s['tag_uid'] = uids[i].hex().upper()
        if i < len(modes):
            s['lane_mode'] = MODE_NAMES.get(modes[i], 'mode_%d' % modes[i])
        slots.append(s)
    out = {'status': 'ready', 'slots': slots}
    if any(m in MOVING_MODES for m in modes[:4]):
        out['status'] = 'busy'
    if env is not None:
        valid = int(env.get('valid', 0))
        if valid & 4:
            out['temp'] = round(int(env.get('chamber_mc', 0)) / 1000.0, 1)
            out['humidity'] = round(
                int(env.get('chamber_rh_pct10', 0)) / 10.0, 1)
    return out


class AceProtocolAce2k(AceProtocol):
    NAME = 'ace2k'
    # A move ends when the host goes away (keepalive of the unit).
    MOTION_STOPS_WITH_HOST = True
    DEFAULT_BAUD = ACE2K_BAUD
    # Set from ace.py at init from [ace] ace2k_ports: only these ports are
    # ever opened as ace2k units. The ACE 2's USB id is the same with
    # every firmware, so the port is named, never guessed.
    PORTS = ()
    # The key comes from a probe before the claim (ace.py _probe_claim):
    # once per port (the port is declared ace2k, the probe only reads the
    # key), idle only, the reactor runs meanwhile.
    PROBE_BEFORE_CLAIM = True
    PROBE_ONCE_PER_PATH = True
    PROBE_DEADLINE_S = 4.0
    CAPS_STATIC = {
        'transport_engine': 'threaded',
        'blocking_write_on_reactor': False,
        'reconnectable': True,
        'presence_sensor': True,
        'empty_token': 'empty',
        # The per-lane mode is in the status (lane_mode); the device-truth
        # FA monitor and the slot_status verdict paths stay off.
        'has_slot_status_truth': False,
        # A 2 m move at the unit's speed runs ~30-60 s and reports busy.
        'busy_wait_floor': True,
        'fa_monitor': False,
        # The follow gives way to a retract on its lane; ace.py stops it
        # first so its FA cache knows (else a later arm would be skipped
        # as 'already running' on a lane that no longer follows).
        'needs_fa_prestop': True,
        'has_rollback_assist': False,
        'no_freewheel': True,
        'self_stops_on_resistance': True,
        # The lane encoder exists (ace2k_lane_counters); it is wired with
        # the motion phase.
        'has_decoder': False,
        'v1_fa_health_log': False,
        # No tag query per slot: the host reads the tags itself
        # (host_tag_session) and the reads arrive in the status.
        'tag_identity_in_status': True,
        'fw_tag_pair_dedupe': False,
        'has_humidity': True,
        # The dryer runs and protects itself in the unit, vents included.
        'has_dryer': True,
        'has_exhaust_valve': False,
        'has_feed_check': False,
        # tobecwb's flasher (web backend vendor/ace2k_flash.pyz): a new
        # ace2k image, or the factory image back.
        'flash_engine': 'ace2k',
        'v2_command_set': False,
        'lanes_concurrent': True,
        'speed_unit': 'mm_s',
        'busy_reply': 'code1_forbidden',
        # The 96-bit device id, read by the pre-claim probe ('uid:...',
        # the stock ACE 2 key of the same unit).
        'has_stable_id': True,
        'freewheel_print_mode': False,
        'locate_method': 'led',
        'calibration_feed': 'chunked',
        'host_tag_session': True,
        'buffer_relax': True,
        # stop_feed_assist ends the follow; the ACE Pro's 5 mm rollback
        # would move a strand the extruder (or nothing) holds.
        'fa_stop_rollback': False,
    }
    SERIAL_KWARGS = {
        'timeout': 0.05,
        'write_timeout': 0.25,
        'exclusive': True,
    }

    @classmethod
    def discover(cls):
        return [p for p in cls.PORTS if os.path.exists(p)]

    @classmethod
    def open_raw(cls, path, baud):
        import serial
        return serial.Serial(port=path, baudrate=cls.DEFAULT_BAUD,
                             **cls.SERIAL_KWARGS)

    @classmethod
    def open_transport(cls, path, baud, **kwargs):
        return Ace2kLink(cls.open_raw(path, cls.DEFAULT_BAUD),
                         label=port_label(path))

    @staticmethod
    def uid_key(words):
        return uid_key(words)

    @classmethod
    def probe_device(cls, path, query_timeout=ASK_TIMEOUT):
        """Bounded probe before a claim: {'ok', 'category', 'error',
        'uid' (three words), 'firmware'}. Opens the port exclusively and
        closes it again."""
        try:
            raw = cls.open_raw(path, cls.DEFAULT_BAUD)
        except Exception as e:
            msg = str(e).lower()
            cat = ('port_in_use' if 'busy' in msg or 'lock' in msg else
                   'permission_denied' if 'permission' in msg else
                   'serial_error')
            return {'ok': False, 'category': cat, 'error': str(e)}
        try:
            return Ace2kLink(raw).probe()
        except Exception as e:
            return {'ok': False, 'category': 'serial_error', 'error': str(e)}
        finally:
            try:
                raw.close()
            except Exception:
                pass

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
                logging.info('[multiACE] ace2k link: bad envelope dropped')
        return results

    def initial_handshake_requests(self):
        return [{'method': 'get_info'}]

    def make_default_info(self):
        return make_status(0)
