"""FakeAce2k: an ACE 2 Pro on the community firmware ace2k (a Klipper MCU).

Models what the link needs and what the wire test showed (CLAUDE.md
section 85):
- Klipper's MCU protocol: blocks with a 4-bit sequence; a block with the
  expected sequence is processed, answered (one message per block) and
  acknowledged (an empty block naming the next sequence); any other
  sequence is refused with the same empty block;
- identify: the data dictionary (zlib JSON) in pieces;
- get_config (is_shutdown), get_clock, reset (restarts: sequence 0,
  reports off, shutdown cleared);
- ace2k_version (the link proof), ace2k_linkproof_state, ace2k_uid_query,
  ace2k_feed_load_set (auto_load is kept so a test can read it);
- the periodic reports a *_query rest_ticks starts (sensors, feed, env,
  rfid), sent from the next exchange on, one report per exchange;
- an insert edge as ace2k_sensors_event;
- motion (M6b): ace2k_feed_start (feed, rollback, follow) with the
  refusals busy / in_error / no_filament / bounds and the same-seq retry
  rule, a move that runs at its speed in virtual time and ends with one
  ace2k_feed_event (done, or blocked / stuck by a knob), ace2k_feed_stop
  (stopped), ace2k_feed_clear (error -> idle); the state report carries
  each lane's mode and last accepted seq;
- tags (M6d): a tag put on a lane (NTAG pages or MIFARE blocks with their
  sector keys) opens a session (ace2k_rfid_tag) when the lane moves, at
  the start of the move; ace2k_rfid_step answers with ace2k_rfid_data
  (auth_failed for a wrong MIFARE key), ace2k_rfid_done ends it (read or
  pending); ace2k_rfid_read move=1 runs the read with motion as a load
  (report mode 4) that ends with ace2k_feed_event loaded (seq 0, mode 4);
  ace2k_rfid_forget, ace2k_rfid_lane_query and ace2k_rfid_search_set;
  ace2k_feed_start mode 5 (a load) runs the same search as a load with its
  own seq (report mode 4, ends loaded with that seq; self.loads records
  each accepted load).

Blocks are built and parsed with the real codec module.
"""
from __future__ import annotations

import json
import sys
import zlib

from . import harness as H

CLOCK_FREQ = 120000000
VERSION = 'v0.13.0'
# The device id as the unit sends it (HW 2026-11-08): three little-endian
# words, the stock ACE 2 key uid:8f3b9387-07b09108-3131334a of the same chip.
UID = bytes(bytearray([0x87, 0x93, 0x3B, 0x8F, 0x08, 0x91, 0xB0, 0x07,
                       0x4A, 0x33, 0x31, 0x31]))
UID_KEY = 'uid:8f3b9387-07b09108-3131334a'
UID_WORDS = (0x8f3b9387, 0x07b09108, 0x3131334a)

COMMANDS = {
    'identify offset=%u count=%c': 1,
    'get_config': 2,
    'get_clock': 4,
    'reset': 6,
    'ace2k_version': 10,
    'ace2k_linkproof_state': 12,
    'ace2k_uid_query': 14,
    'ace2k_feed_load_set park_um=%u speed_um_s=%u auto_load=%c': 16,
    'ace2k_sensors_query rest_ticks=%u': 17,
    'ace2k_feed_query rest_ticks=%u': 19,
    'ace2k_env_query rest_ticks=%u': 21,
    'ace2k_rfid_query rest_ticks=%u': 23,
    'ace2k_feed_start lane=%c mode=%c length_um=%u speed_um_s=%u seq=%c '
    'free_tip=%c': 28,
    'ace2k_feed_stop lane=%c': 30,
    'ace2k_feed_clear lane=%c': 31,
    'ace2k_rfid_read lane=%c move=%c seq=%c': 40,
    'ace2k_rfid_step lane=%c session=%c op=%c arg=%c blocks=%c key=%*s': 41,
    'ace2k_rfid_done lane=%c session=%c result=%c': 42,
    'ace2k_rfid_forget lane=%c': 43,
    'ace2k_rfid_lane_query lane=%c': 44,
    'ace2k_rfid_search_set search_um=%u': 45,
}
RESPONSES = {
    'identify_response offset=%u data=%.*s': 0,
    'config is_config=%c crc=%u is_shutdown=%c move_count=%hu': 3,
    'clock clock=%u': 5,
    'ace2k_version_response version=%s': 11,
    'ace2k_linkproof_state_response proven=%c remaining_ms=%u': 13,
    'ace2k_uid_response uid=%*s': 15,
    'ace2k_sensors_state switches=%hu insert_mv=%*s empty_mv=%*s '
    'aux_mv=%hu age_ms=%c': 18,
    'ace2k_feed_state mode=%*s error=%*s speed_um_s=%*s duty_pct=%*s '
    'bursts=%*s seq=%*s': 20,
    'ace2k_env_state ptc_left_mc=%i ptc_right_mc=%i chamber_mc=%i '
    'chamber_rh_pct10=%hu vdda_mv=%hu valid=%c': 22,
    'ace2k_rfid_state state=%*s field=%c': 24,
    'ace2k_sensors_event lane=%c kind=%c level=%c': 25,
    'shutdown clock=%u static_string_id=%hu': 26,
    'is_shutdown static_string_id=%hu': 27,
    'ace2k_feed_start_response lane=%c accepted=%c reason=%c': 29,
    'ace2k_feed_event lane=%c kind=%c mode=%c motor_um=%u filament_um=%i '
    'seq=%c': 32,
    'ace2k_rfid_read_response lane=%c accepted=%c reason=%c': 34,
    'ace2k_rfid_tag lane=%c session=%c uid=%*s atqa=%hu sak=%c': 35,
    'ace2k_rfid_data lane=%c session=%c block=%c status=%c data=%*s': 36,
    'ace2k_rfid_event lane=%c kind=%c session=%c': 37,
    'ace2k_rfid_lane_state lane=%c state=%c uid=%*s': 38,
}
# The armed stop (ace2k_feed_stop_when, v0.13.0): only in the dictionary of
# a fake built with stop_when=True, so the older scenarios keep their
# dictionary byte for byte.
STOP_WHEN_COMMAND = {
    'ace2k_feed_stop_when lane=%c held=%c at_rest=%c held_ms=%hu': 46,
}
STOP_WHEN_RESPONSE = {
    'ace2k_feed_stop_when_response lane=%c accepted=%c reason=%c': 39,
}
SPEED_MIN = 9000
SPEED_MAX = 70000
MOVE_MAX = 2000000
KIND = {'done': 0, 'stopped': 1, 'loaded': 5, 'stuck': 7, 'tangled': 8,
        'blocked': 18, 'held': 19, 'at_rest': 20}
# The read with motion: seconds from the session's end to the load's end
# (the return), and the speed of a search that finds no tag (mm/s).
SEARCH_RETURN_S = 1.0
SEARCH_SPEED = 60.0
CONFIG = {
    'CLOCK_FREQ': CLOCK_FREQ,
    'ACE2K_VERSION': VERSION,
    'ACE2K_LANE_SPEED_MIN_UM_S': 9000,
    'ACE2K_LANE_SPEED_MAX_UM_S': 70000,
    'ACE2K_LANE_MOVE_MAX_UM': 2000000,
}
QUERY_REPORT = {17: 'sensors', 19: 'feed', 21: 'env', 23: 'rfid'}


def codec():
    H.load_ace()
    return sys.modules[H.PKG + '.ace_protocol_ace2k']


def _rc522():
    import importlib
    H.load_ace()
    return importlib.import_module(H.PKG + '.ace_rc522')


def dictionary_bytes(commands=None, responses=None):
    data = {'version': VERSION, 'commands': dict(commands or COMMANDS),
            'responses': dict(responses or RESPONSES), 'config': dict(CONFIG),
            'enumerations': {'static_string_id': {
                'Timer too close': 7}}}
    return zlib.compress(json.dumps(data, sort_keys=True).encode('utf-8'))


class FakeAce2k:
    MODEL = 'ACE 2 Pro (ace2k)'

    def __init__(self, clock, label, slots=(), uid=UID, latency=0.002,
                 shutdown=False, proven=True, chamber_mc=24500,
                 rh_pct10=412, env_valid=True, commands=None,
                 block_feed_after=None, stuck_rollback_after=None,
                 stop_when=False, relax_at_mm=None):
        self.clock = clock
        self.label = label
        self.latency = latency
        self.uid = bytes(uid)
        self.switches = 0
        for s in slots:
            self.switches |= 1 << int(s)
        # rest bits (4-7) on for every lane, like the wire test's 247
        self.switches |= 0xF0
        self.shutdown = shutdown
        self.proven = proven
        self.chamber_mc = chamber_mc
        self.rh_pct10 = rh_pct10
        self.env_valid = env_valid
        cmdset = dict(commands or COMMANDS)
        respset = dict(RESPONSES)
        if stop_when:
            cmdset.update(STOP_WHEN_COMMAND)
            respset.update(STOP_WHEN_RESPONSE)
        self.dict_blob = dictionary_bytes(cmdset, respset)
        # The armed stops per lane (held, at_rest) and, for an armed
        # rollback on a full lane, the travel after which the plunger
        # reads rest (None: never - the move runs its length).
        self.armed = {}
        self.relax_at_mm = relax_at_mm
        self.stop_whens = []    # (time, lane, held, at_rest)
        self.expected = 0
        self.periods = {}       # report name -> (period s, next due)
        self.auto_load = 1
        self.events = []        # pending unsolicited messages
        self._buf = bytearray()
        self.requests = []
        self.resets = 0
        self.lane_mode = [0, 0, 0, 0]
        self.lane_seq = [0, 0, 0, 0]
        self.lane_err = [0, 0, 0, 0]
        # lane -> running move: {mode, seq, t0, mm, speed, limit, limit_kind}
        self.moves = {}
        self.last_start = {}    # lane -> (seq, mode, length, speed, t_end)
        # A feed ends blocked after this many mm (filament stops); a
        # rollback ends stuck (error) after this many mm.
        self.block_feed_after = block_feed_after
        self.stuck_rollback_after = stuck_rollback_after
        self.starts = []        # (time, lane, mode, length_um, speed, seq, free_tip)
        self.loads = []         # (time, lane, park_um, speed, seq): mode-5 loads
        # tags: lane -> {uid, sak, atqa, pages | blocks, keys}
        self.tags = {}
        self.tag_state = [1 if self.switches & (1 << i) else 0
                          for i in range(4)]
        self.session_no = 0
        self.sessions = {}      # lane -> session number
        self.search_um = 750000
        self.steps = []         # (time, lane, op, arg, blocks)
        self.dones = []         # (time, lane, result)
        self.fail_steps = 0     # answer the next n steps with status 3
        self.bursts = [0, 0, 0, 0]  # the follow's burst counter per lane

    # -- test knobs --------------------------------------------------------

    def insert(self, lane):
        self.switches |= 1 << int(lane)
        self.tag_state[int(lane)] = 1
        self.events.append(('ace2k_sensors_event',
                            {'lane': int(lane), 'kind': 0, 'level': 1}))

    def plunger(self, lane, kind, level):
        """A plunger edge: kind 'rest' or 'taut' on a lane, 'full' for the
        shared pulled reading (no lane: the four switches share one input)."""
        if kind == 'full':
            bit, ev_lane, k = 1 << 12, 255, 3
        else:
            k = 1 if kind == 'rest' else 2
            bit = 1 << ((4 if k == 1 else 8) + int(lane))
            ev_lane = int(lane)
        if level:
            self.switches |= bit
        else:
            self.switches &= ~bit
        self.events.append(('ace2k_sensors_event',
                            {'lane': ev_lane, 'kind': k,
                             'level': 1 if level else 0}))

    def burst(self, lane, n=1):
        self.bursts[int(lane)] = (self.bursts[int(lane)] + int(n)) & 0xFFFF

    def remove(self, lane):
        self.switches &= ~(1 << int(lane))
        self.tag_state[int(lane)] = 0
        self.sessions.pop(int(lane), None)
        self.events.append(('ace2k_sensors_event',
                            {'lane': int(lane), 'kind': 0, 'level': 0}))
        self.events.append(('ace2k_rfid_event',
                            {'lane': int(lane), 'kind': 0, 'session': 0}))

    def put_tag(self, lane, tag):
        """tag: {'uid': bytes, 'sak': int, 'pages': {page: 4 bytes}} or
        {'uid', 'sak': 8, 'blocks': {block: 16 bytes}, 'keys': {sector:
        6 bytes}}."""
        self.tags[int(lane)] = dict(tag)

    # -- tags --------------------------------------------------------------

    def _open_session(self, lane):
        tag = self.tags.get(lane)
        if tag is None or self.tag_state[lane] not in (1, 2):
            return
        self.session_no = self.session_no % 255 + 1
        self.sessions[lane] = self.session_no
        self.tag_state[lane] = 3
        self.events.append(('ace2k_rfid_tag', {
            'lane': lane, 'session': self.session_no,
            'uid': bytes(tag['uid']), 'atqa': int(tag.get('atqa', 0x44)),
            'sak': int(tag.get('sak', 0))}))

    def _step(self, v):
        lane, sess = v['lane'], v['session']
        self.steps.append((self.clock.now, lane, v['op'], v['arg'],
                           v['blocks']))
        if self.sessions.get(lane) != sess:
            return
        tag = self.tags.get(lane) or {}

        def frame(block, status, data=b''):
            self.events.append(('ace2k_rfid_data', {
                'lane': lane, 'session': sess, 'block': block,
                'status': status, 'data': bytes(data)}))
        if self.fail_steps:
            self.fail_steps -= 1
            frame(v['arg'], 3)
            return
        if v['op'] == 0:
            pages = tag.get('pages') or {}
            for g in range(v['blocks']):
                first = v['arg'] + 4 * g
                data = b''.join(pages.get(first + k, b'\x00' * 4)
                                for k in range(4))
                frame(first, 0, data)
            return
        sector = v['arg']
        if (tag.get('keys') or {}).get(sector) != bytes(v['key']):
            frame(sector * 4, 1)
            return
        blocks = tag.get('blocks') or {}
        for i in range(3):
            if v['blocks'] >> i & 1:
                b = sector * 4 + i
                frame(b, 0, blocks.get(b, b'\x00' * 16))

    def _done(self, v):
        lane = v['lane']
        self.dones.append((self.clock.now, lane, v['result']))
        if self.sessions.get(lane) != v['session']:
            return
        self.sessions.pop(lane, None)
        searching = lane in self.moves and self.moves[lane].get('search')
        self.tag_state[lane] = 4 if v['result'] else (2 if searching else 1)
        self.events.append(('ace2k_rfid_event', {
            'lane': lane, 'kind': self.tag_state[lane], 'session': 0}))
        if searching:
            self.moves[lane]['t_end'] = self.clock.now + SEARCH_RETURN_S

    def _rfid_read(self, C, v):
        lane = v['lane']

        def resp(acc, reason):
            return self._msg(C, 'ace2k_rfid_read_response',
                             {'lane': lane, 'accepted': acc,
                              'reason': reason})
        if lane in self.sessions:
            return resp(0, 1)
        if not self.switches & (1 << lane):
            return resp(0, 2)
        if lane in self.moves:
            return resp(0, 1)
        if any(mv['mode'] != 6 for ln, mv in self.moves.items()
               if ln != lane):
            return resp(0, 4)
        if self.tag_state[lane] in (4, 5):
            self.tag_state[lane] = 1
        self.tag_state[lane] = 2
        self.moves[lane] = {
            'mode': 4, 'seq': 0, 't0': self.clock.now, 'search': True,
            't_end': self.clock.now
            + 2.0 * (self.search_um / 1000.0) / SEARCH_SPEED}
        self.lane_mode[lane] = 4
        self._open_session(lane)
        return resp(1, 0)

    # -- wire --------------------------------------------------------------

    def _msg(self, C, name, values):
        allr = dict(RESPONSES)
        allr.update(STOP_WHEN_RESPONSE)
        for fmt, mid in allr.items():
            n, params = C.parse_format(fmt)
            if n == name:
                return C.klipper_vlq(mid) + C.encode_params(params, values)
        raise KeyError(name)

    def _block(self, C, payload=b''):
        return C.klipper_block(self.expected, payload)

    def _report(self, C, which):
        if which == 'sensors':
            return self._msg(C, 'ace2k_sensors_state', {
                'switches': self.switches, 'insert_mv': b'\x00' * 8,
                'empty_mv': b'\x00' * 8, 'aux_mv': 97, 'age_ms': 10})
        if which == 'feed':
            return self._msg(C, 'ace2k_feed_state', {
                'mode': bytes(bytearray(self.lane_mode)),
                'error': bytes(bytearray(self.lane_err)),
                'speed_um_s': b'\x00' * 16,
                'duty_pct': b'\x00' * 4,
                'bursts': bytes(bytearray(
                    b for n in self.bursts for b in (n & 0xFF, n >> 8))),
                'seq': bytes(bytearray(self.lane_seq))})
        if which == 'env':
            return self._msg(C, 'ace2k_env_state', {
                'ptc_left_mc': 23000, 'ptc_right_mc': 23100,
                'chamber_mc': self.chamber_mc,
                'chamber_rh_pct10': self.rh_pct10, 'vdda_mv': 3300,
                'valid': 15 if self.env_valid else 3})
        if which == 'rfid':
            return self._msg(C, 'ace2k_rfid_state', {
                'state': bytes(bytearray(self.tag_state)), 'field': 0})
        raise KeyError(which)

    def on_bytes(self, data):
        C = codec()
        self._buf.extend(data)
        out = []
        for seq, body in C.parse_klipper_blocks(self._buf):
            if seq != self.expected:
                self.requests.append((self.clock.now, 'nak', seq))
                out.append((self.latency, self._block(C)))
                continue
            self.expected = (seq + 1) & C.KLIPPER_SEQ_MASK
            due = self._due(C)
            replies = self.handle(C, body)
            if replies is None:
                # reset: the MCU restarts at once, nothing is sent
                return []
            for payload in due:
                out.append((self.latency, self._block(C, payload)))
            for payload in replies:
                out.append((self.latency, self._block(C, payload)))
            # An event the command itself caused (a stop) goes out at once,
            # as the unit sends it.
            for name, values in self.events:
                out.append((self.latency,
                            self._block(C, self._msg(C, name, values))))
            self.events = []
            out.append((self.latency, self._block(C)))
        return out

    def lane_full(self, lane):
        rest = self.switches & (1 << (4 + lane))
        taut = self.switches & (1 << (8 + lane))
        return bool(self.switches & (1 << 12)) and not rest and not taut

    def _end_move(self, lane, kind, mm):
        if kind == 'at_rest':
            self.switches |= 1 << (4 + lane)
            if not any(self.lane_full(ln) for ln in range(4)):
                self.switches &= ~(1 << 12)
        mv = self.moves.pop(lane)
        sign = -1 if mv['mode'] == 1 else 1
        self.lane_mode[lane] = 7 if KIND[kind] in range(7, 14) else 0
        self.lane_err[lane] = KIND[kind] if self.lane_mode[lane] == 7 else 0
        self.events.append(('ace2k_feed_event', {
            'lane': lane, 'kind': KIND[kind], 'mode': mv['mode'],
            'motor_um': int(mm * 1000), 'filament_um': int(sign * mm * 1000),
            'seq': mv['seq']}))
        last = self.last_start.get(lane, (0, 0, 0, 0, 0.0))
        self.last_start[lane] = last[:4] + (self.clock.now,)

    def _advance(self):
        now = self.clock.now
        for lane in list(self.moves):
            mv = self.moves[lane]
            if mv.get('search'):
                if now >= mv['t_end']:
                    self.moves.pop(lane)
                    self.lane_mode[lane] = 0
                    if self.tag_state[lane] == 2:
                        self.tag_state[lane] = (1 if lane in self.tags
                                                else 5)
                        self.events.append(('ace2k_rfid_event', {
                            'lane': lane, 'kind': self.tag_state[lane],
                            'session': 0}))
                    park = int(mv.get('park_mm', 0.0) * 1000)
                    self.events.append(('ace2k_feed_event', {
                        'lane': lane, 'kind': KIND['loaded'], 'mode': 4,
                        'motor_um': park, 'filament_um': park,
                        'seq': mv['seq']}))
                continue
            if mv['mode'] == 6:
                continue
            ran = (now - mv['t0']) * mv['speed']
            if mv.get('limit') is not None and ran >= mv['limit']:
                self._end_move(lane, mv['limit_kind'], mv['limit'])
            elif ran >= mv['mm']:
                self._end_move(lane, 'done', mv['mm'])

    def _due(self, C):
        self._advance()
        out = []
        for name, values in self.events:
            out.append(self._msg(C, name, values))
        self.events = []
        if self.shutdown:
            out.append(self._msg(C, 'is_shutdown', {'static_string_id': 7}))
            return out
        now = self.clock.now
        for which in ('sensors', 'feed', 'env', 'rfid'):
            entry = self.periods.get(which)
            if entry is None:
                continue
            period, due = entry
            if now >= due:
                out.append(self._report(C, which))
                self.periods[which] = (period, now + period)
        return out

    def handle(self, C, body):
        out = []
        pos = 0
        cmds = {}
        allc = dict(COMMANDS)
        allc.update(STOP_WHEN_COMMAND)
        for fmt, mid in allc.items():
            cmds[mid] = C.parse_format(fmt)
        while pos < len(body):
            mid, pos = C.parse_klipper_vlq(body, pos)
            name, params = cmds[mid]
            vals, pos = C.decode_params(params, body, pos)
            self.requests.append((self.clock.now, name, vals))
            if name == 'identify':
                off = vals['offset']
                piece = self.dict_blob[off:off + vals['count']]
                out.append(self._msg(C, 'identify_response',
                                     {'offset': off, 'data': piece}))
            elif name == 'get_config':
                out.append(self._msg(C, 'config', {
                    'is_config': 0, 'crc': 0,
                    'is_shutdown': 1 if self.shutdown else 0,
                    'move_count': 0}))
            elif name == 'get_clock':
                out.append(self._msg(C, 'clock', {
                    'clock': int(self.clock.now * CLOCK_FREQ) & 0xFFFFFFFF}))
            elif name == 'reset':
                self.resets += 1
                self.shutdown = False
                self.periods = {}
                self.expected = 0
                self.auto_load = 1
                return None
            elif name == 'ace2k_version':
                self.proven = True
                out.append(self._msg(C, 'ace2k_version_response',
                                     {'version': VERSION.encode('ascii')}))
            elif name == 'ace2k_linkproof_state':
                out.append(self._msg(C, 'ace2k_linkproof_state_response', {
                    'proven': 1 if self.proven else 0, 'remaining_ms': 0}))
            elif name == 'ace2k_uid_query':
                out.append(self._msg(C, 'ace2k_uid_response',
                                     {'uid': self.uid}))
            elif name == 'ace2k_feed_load_set':
                self.auto_load = vals['auto_load']
            elif name == 'ace2k_feed_stop_when':
                lane = vals['lane']
                self.stop_whens.append((self.clock.now, lane, vals['held'],
                                        vals['at_rest']))
                if self.lane_mode[lane] == 7:
                    acc, reason = 0, 2
                elif lane in self.moves:
                    acc, reason = 0, 1
                else:
                    acc, reason = 1, 0
                    if vals['held'] or vals['at_rest']:
                        self.armed[lane] = (vals['held'], vals['at_rest'])
                    else:
                        self.armed.pop(lane, None)
                out.append(self._msg(C, 'ace2k_feed_stop_when_response', {
                    'lane': lane, 'accepted': acc, 'reason': reason}))
            elif name == 'ace2k_feed_start':
                out.append(self._feed_start(C, vals))
            elif name == 'ace2k_feed_stop':
                self._advance()
                for lane in ([vals['lane']] if vals['lane'] != 255
                             else list(self.moves)):
                    mv = self.moves.get(lane)
                    if mv is None:
                        continue
                    ran = 0.0 if mv['mode'] == 6 else min(
                        mv['mm'], (self.clock.now - mv['t0']) * mv['speed'])
                    self._end_move(lane, 'stopped', ran)
            elif name == 'ace2k_rfid_read':
                out.append(self._rfid_read(C, vals))
            elif name == 'ace2k_rfid_step':
                self._step(vals)
            elif name == 'ace2k_rfid_done':
                self._done(vals)
            elif name == 'ace2k_rfid_forget':
                lane = vals['lane']
                if self.tag_state[lane] in (4, 5):
                    self.tag_state[lane] = 1
            elif name == 'ace2k_rfid_lane_query':
                lane = vals['lane']
                tag = self.tags.get(lane)
                out.append(self._msg(C, 'ace2k_rfid_lane_state', {
                    'lane': lane, 'state': self.tag_state[lane],
                    'uid': bytes(tag['uid']) if tag and
                    self.tag_state[lane] in (3, 4) else b''}))
            elif name == 'ace2k_rfid_search_set':
                self.search_um = vals['search_um']
            elif name == 'ace2k_feed_clear':
                lane = vals['lane']
                if self.lane_mode[lane] == 7:
                    self.lane_mode[lane] = 0
                    self.lane_err[lane] = 0
            elif mid in QUERY_REPORT:
                which = QUERY_REPORT[mid]
                ticks = vals['rest_ticks']
                if ticks:
                    period = float(ticks) / CLOCK_FREQ
                    self.periods[which] = (period, self.clock.now + period)
                else:
                    self.periods.pop(which, None)
        return out

    def _feed_start(self, C, v):
        lane, mode = v['lane'], v['mode']
        self.starts.append((self.clock.now, lane, mode, v['length_um'],
                            v['speed_um_s'], v['seq'], v['free_tip']))

        def resp(acc, reason):
            return self._msg(C, 'ace2k_feed_start_response',
                             {'lane': lane, 'accepted': acc,
                              'reason': reason})
        key = (v['seq'], mode, v['length_um'], v['speed_um_s'])
        last = self.last_start.get(lane)
        if last is not None and last[:4] == key and (
                lane in self.moves or self.clock.now - last[4] <= 1.0):
            return resp(1, 0)      # the retry of the same start
        if self.lane_mode[lane] == 7:
            return resp(0, 2)
        if lane in self.moves:
            return resp(0, 1)
        if not SPEED_MIN <= v['speed_um_s'] <= SPEED_MAX:
            return resp(0, 5)
        if mode in (0, 1) and not 1 <= v['length_um'] <= MOVE_MAX:
            return resp(0, 5)
        if mode in (0, 5, 6) and not self.switches & (1 << lane):
            return resp(0, 3)
        if mode == 5:
            # The load: settle, pull to the parking distance, search past
            # it, back. Modelled as the read with motion with its own seq.
            if self.tag_state[lane] in (4, 5):
                self.tag_state[lane] = 1
            self.tag_state[lane] = 2
            self.moves[lane] = {
                'mode': 4, 'seq': v['seq'], 't0': self.clock.now,
                'search': True, 'park_mm': v['length_um'] / 1000.0,
                't_end': self.clock.now
                + 2.0 * (self.search_um / 1000.0) / SEARCH_SPEED}
            self.lane_mode[lane] = 4
            self.lane_seq[lane] = v['seq']
            self.last_start[lane] = key + (1e18,)
            self.loads.append((self.clock.now, lane, v['length_um'],
                               v['speed_um_s'], v['seq']))
            self._open_session(lane)
            return resp(1, 0)
        mv = {'mode': mode, 'seq': v['seq'], 't0': self.clock.now,
              'mm': v['length_um'] / 1000.0,
              'speed': v['speed_um_s'] / 1000.0}
        armed = self.armed.pop(lane, None)
        if (mode == 1 and armed is not None and armed[1]
                and self.relax_at_mm is not None and self.lane_full(lane)):
            mv['limit'], mv['limit_kind'] = self.relax_at_mm, 'at_rest'
        elif mode == 0 and self.block_feed_after is not None:
            mv['limit'], mv['limit_kind'] = self.block_feed_after, 'blocked'
        if (mode == 1 and self.stuck_rollback_after is not None
                and 'limit' not in mv):
            mv['limit'], mv['limit_kind'] = self.stuck_rollback_after, 'stuck'
        self.moves[lane] = mv
        self.lane_mode[lane] = {0: 1, 1: 2, 6: 8}.get(mode, 1)
        self.lane_seq[lane] = v['seq']
        self.bursts[lane] = 0   # entering a mode clears the counters
        self.last_start[lane] = key + (1e18,)
        if mode in (0, 1):
            self._open_session(lane)
        return resp(1, 0)


# -- tag builders for the scenarios ----------------------------------------

NTAG_UID = bytes(bytearray([0x04, 0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0x80]))
MIFARE_UID = bytes(bytearray([0x11, 0x22, 0x33, 0x44]))


def _pages_from(user, cc_size):
    """NTAG pages: 0-2 the UID area, 3 the capability container, user
    data from page 4."""
    pages = {0: b'\x04\xa1\xb2\x00', 1: b'\xc3\xd4\xe5\x80',
             2: b'\x00\x48\x00\x00',
             3: bytes(bytearray([0xE1, 0x10, cc_size, 0x00]))}
    user = bytes(user)
    for i in range(0, len(user), 4):
        pages[4 + i // 4] = (user[i:i + 4] + b'\x00' * 4)[:4]
    return pages


def ntag_anycubic(sku, material, color_hex, brand='AC', uid=NTAG_UID):
    R = _rc522().AceTagReader
    user = R._anycubic_encode(sku, material, brand=brand,
                              color_hex=color_hex)
    return {'uid': uid, 'sak': 0, 'pages': _pages_from(user, 0x12)}


def ntag_openspool(material, color_hex, brand, uid=NTAG_UID):
    R = _rc522().AceTagReader
    user = R._openspool_encode(material, color_hex, brand)
    return {'uid': uid, 'sak': 0, 'pages': _pages_from(user, 0x3E)}


def _pad(text, n=16):
    b = text.encode('ascii')[:n]
    return b + b'\x00' * (n - len(b))


def mifare_bambu(material, name, rgba, uid=MIFARE_UID):
    import struct
    C = codec()
    blocks = {
        1: _pad('A00-K0', 8) + _pad('GFA01', 8),
        2: _pad(material),
        4: _pad(name),
        5: bytes(bytearray(rgba)) + struct.pack('<H', 1000) + b'\x00\x00'
        + struct.pack('<f', 1.75) + b'\x00' * 4,
        6: struct.pack('<6H', 55, 8, 1, 55, 230, 190) + b'\x00' * 4,
        12: _pad('2026_01_01_00_00'),
        14: b'\x00' * 4 + struct.pack('<H', 330) + b'\x00' * 10,
    }
    keys = dict((s, C._Bambu.key_for(uid, s)) for s in (0, 1, 3))
    return {'uid': uid, 'sak': 8, 'atqa': 4, 'blocks': blocks,
            'keys': keys}


def mifare_snapmaker(main, sub, rgb, uid=MIFARE_UID):
    import struct
    C = codec()
    img = bytearray(176)
    img[16:32] = _pad('Snapmaker')
    img[32:48] = _pad('Snapmaker')
    struct.pack_into('<H', img, 66, main)
    struct.pack_into('<H', img, 68, sub)
    img[72] = 1
    img[80:83] = bytes(bytearray(rgb))
    struct.pack_into('<I', img, 96, 900001)
    struct.pack_into('<HHH', img, 128, 175, 1000, 330)
    blocks = dict((b, bytes(img[b * 16:b * 16 + 16])) for b in range(11)
                  if b % 4 < 3)
    keys = dict((s, C._Snapmaker.key_for(uid, s)) for s in (0, 1, 2))
    return {'uid': uid, 'sak': 8, 'atqa': 4, 'blocks': blocks,
            'keys': keys}
