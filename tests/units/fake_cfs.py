"""FakeCFS: a Creality CFS Nano on the RS485 bus (F7 frames, CRC-8).

Models what the wire test measured (CLAUDE.md section 80):
- GET_SLAVE_INFO (0xA1) answered only on the broadcast address 0xFF,
  from the fixed address 0x35, device type 8, a 12-byte unique id;
- 0x08 = presence mask (bit n = slot n), 0x0A = box state (last byte),
  with status 0x30 once after an insert or a removal (the event is
  consumed by the read), 0x14 = version string;
- motions (measured 2026-10-31): 10 <m> 00 init feed ~44 mm/s until the
  box stops by itself (max distance -> state 04 status 0A, resistance ->
  state 04 status 09); while it runs the wheel answers empty until a
  second 10 <m> 05 (rejected 03/03); 10 <m> 02 stops it at once (state
  03); 10 <m> 05 <mm> dosed feed ~3.7 mm/s; 11 <m> <mm> retract ~44 mm/s;
  the last two reply only when the motion ends (sent with the next
  answer); 04 00 01 unlocks (state 00, ~2.3 mm back from state 03),
  04 <m> 00 print mode (state 02, wheel answers empty);
- 0x0E 01 = wheel per slot, four big-endian floats;
- every other function goes unanswered (the Nano is silent on 0x02,
  0x03, 0x05, 0xF0);
- `mute` until a virtual time (the box is mute for about 30 s after
  power-on).

Frames are built and parsed with the real codec module.
"""
from __future__ import annotations

import struct
import sys

from . import harness as H

ADDR = 0x35
UID = bytes(bytearray([0x56, 0x70, 0x35, 0x31, 0x35, 0x33, 0x0D, 0x00,
                       0x52, 0x37, 0x31, 0x32]))
VERSION = b'13680000140289S226GSHT'


def codec():
    H.load_ace()
    return sys.modules[H.PKG + '.ace_protocol_cfs']


class FakeCFS:
    MODEL = 'Creality CFS Nano'

    FEED_SPEED = 44.0
    DOSED_SPEED = 3.7

    def __init__(self, clock, label, slots=(), latency=0.01, uid=UID,
                 mute_until=None, init_limit=1200.0, block_at=None,
                 print_presence_empty=False, silent_stop=False,
                 restart_delay=0.0, pull_in_s=0.0, retract_floor=None):
        self.clock = clock
        self.label = label
        self.latency = latency
        self.uid = bytes(uid)
        self.mask = 0
        for s in slots:
            self.mask |= 1 << int(s)
        self.state = 0x00
        self.event = None
        self.mute_until = mute_until
        self._buf = bytearray()
        self.requests = []
        self.wheel = [0.0, 0.0, 0.0, 0.0]
        # max distance of one init feed; block_at = {slot: wheel position}
        # where resistance stops a feed (the extruder gears, a tangle)
        self.init_limit = float(init_limit)
        self.block_at = dict(block_at or {})
        self.print_presence_empty = print_presence_empty
        # silent_stop: a self-stop leaves the box looking idle (state 00,
        # status 00), only the standing wheel tells it (seen in a bowden)
        self.silent_stop = silent_stop
        # an init after a self-stop only starts moving after this delay
        self.restart_delay = float(restart_delay)
        self._stopped_once = False
        # pull_in_s: an inserted spool is pulled in by the box itself, as
        # soon as its one motor is free; meanwhile it rejects every
        # motion and mode command with state 01
        self.pull_in_s = float(pull_in_s)
        # retract_floor = {slot: wheel position}: a retract stops moving
        # there (the strand hangs) but the box still reports the piece done
        self.retract_floor = dict(retract_floor or {})
        self._pull_pending = False
        self.busy_until = None
        self.motion = None
        self.stop_status = 0x00
        self._late = []

    def insert(self, slot):
        if self.pull_in_s > 0:
            if self.motion is not None:
                self._pull_pending = True
            else:
                self.busy_until = self.clock.now + self.pull_in_s
        self.mask |= 1 << int(slot)
        self.event = [0, 0, 0, 0]
        self.event[int(slot)] = 0x03

    def remove(self, slot):
        self.mask &= ~(1 << int(slot))
        self.event = [0, 0, 0, 0]
        self.event[int(slot)] = 0x02

    def on_bytes(self, data):
        C = codec()
        self._buf.extend(data)
        frames, _ = C.parse_frames(self._buf)
        out = []
        for fr in frames:
            self.requests.append((self.clock.now, fr['addr'], fr['func'],
                                  fr['data'].hex()))
            if self.mute_until is not None and self.clock.now < self.mute_until:
                continue
            self.advance(C)
            # a motion that ended since the last frame sends its reply now
            for late in self._late:
                out.append((self.latency, late))
            self._late = []
            reply = self.handle(C, fr)
            if reply is not None:
                out.append((self.latency, reply))
        return out

    # -- motion model ------------------------------------------------------

    def _start(self, kind, slot, rate, length=None):
        self.motion = {'kind': kind, 'slot': slot, 'rate': rate,
                       't0': self.clock.now, 'w0': self.wheel[slot],
                       'length': length, 'readable': kind != 'init',
                       'delay': (self.restart_delay
                                 if kind == 'init' and self._stopped_once
                                 else 0.0)}

    def advance(self, C):
        mo = self.motion
        if mo is None:
            return
        slot = mo['slot']
        dist = mo['rate'] * max(0.0, self.clock.now - mo['t0']
                                - mo.get('delay', 0.0))
        if mo['kind'] == 'init':
            end, status = None, None
            limit = mo['w0'] + self.init_limit
            block = self.block_at.get(slot)
            pos = mo['w0'] + dist
            if block is not None and pos >= block >= mo['w0']:
                end, status = block, 0x09
            elif pos >= limit:
                end, status = limit, 0x0A
            if end is None:
                self.wheel[slot] = pos
                return
            self.wheel[slot] = end
            self.motion = None
            self._stopped_once = True
            if self.silent_stop:
                self.state, self.stop_status = 0x00, 0x00
            else:
                self.state = 0x04
                self.stop_status = status
            return
        sign = -1.0 if mo['kind'] == 'retract' else 1.0
        floor = (self.retract_floor.get(slot)
                 if mo['kind'] == 'retract' else None)

        def _pos(d):
            p = mo['w0'] + sign * d
            return p if floor is None else max(floor, p)
        if dist < mo['length']:
            self.wheel[slot] = _pos(dist)
            return
        self.wheel[slot] = _pos(mo['length'])
        func = C.F_RETRUDE if mo['kind'] == 'retract' else C.F_EXTRUDE
        self.motion = None
        self.state = 0x00
        self._late.append(C.build_frame(ADDR, 0x00, func,
                                        bytes(bytearray([1 << slot]))))

    def _reject(self, C, func):
        return C.build_frame(ADDR, 0x03, func, bytes(bytearray([self.state])))

    def _pulling_in(self):
        # the pull-in starts once the motor is free AND the box is idle
        # (after a self-stop only once it has been unlocked)
        if self._pull_pending and self.motion is None and self.state == 0x00:
            self._pull_pending = False
            self.busy_until = self.clock.now + self.pull_in_s
        return self.busy_until is not None and self.clock.now < self.busy_until

    def _slot_of(self, mask):
        for i in range(4):
            if mask == 1 << i:
                return i
        return None

    def handle(self, C, fr):
        func, addr = fr['func'], fr['addr']
        if func == C.F_SLAVE_INFO:
            if addr != C.BROADCAST:
                return None
            return C.build_frame(ADDR, 0x00, func,
                                 bytes(bytearray([C.DEV_TYPE_NANO, 0]))
                                 + self.uid)
        if addr != ADDR:
            return None
        if func == C.F_VERSION:
            return C.build_frame(ADDR, 0x00, func, VERSION)
        d = bytearray(fr['data'])
        if func == C.F_PRESENCE:
            if self.state == 0x02 and self.print_presence_empty:
                return C.build_frame(ADDR, 0x00, func)
            return C.build_frame(ADDR, 0x00, func,
                                 bytes(bytearray([self.mask, 0, 0, 0, 0])))
        if func == C.F_BOX_STATE:
            if self.event is not None:
                ev, self.event = self.event, None
                return C.build_frame(ADDR, C.INSERT_EVENT_STATUS, func,
                                     bytes(bytearray(ev)))
            status = self.stop_status if self.state == 0x04 else 0x00
            state = 0x01 if self.motion is not None else self.state
            return C.build_frame(ADDR, status, func,
                                 bytes(bytearray([0, 0, 0, state])))
        if func == C.F_WHEEL:
            mo = self.motion
            if self.state == 0x02 or (mo is not None and not mo['readable']):
                return C.build_frame(ADDR, 0x00, func)
            return C.build_frame(ADDR, 0x00, func,
                                 b''.join(struct.pack('>f', w)
                                          for w in self.wheel))
        if func in (C.F_EXTRUDE, C.F_RETRUDE) and self._pulling_in():
            return C.build_frame(ADDR, 0x03, func, b'\x01')
        if (func == C.F_MODE and self.busy_until is not None
                and self.clock.now < self.busy_until):
            return C.build_frame(ADDR, 0x03, func, b'\x01')
        if func == C.F_MODE and len(d) >= 2:
            if self.motion is not None:
                return self._reject(C, func)
            if d[0] == 0 and d[1] == 1:
                if self.state == 0x03:
                    self.wheel = [w - 2.3 if self.mask & (1 << i) else w
                                  for i, w in enumerate(self.wheel)]
                self.state = 0x00
            else:
                self.state = 0x02
            return C.build_frame(ADDR, 0x00, func, b'')
        if func == C.F_EXTRUDE and len(d) >= 2:
            slot = self._slot_of(d[0])
            stage = d[1]
            mo = self.motion
            if stage == 0x02:
                if mo is not None and mo['kind'] in ('init', 'dosed'):
                    self.motion = None
                self.state = 0x03
                return C.build_frame(ADDR, 0x00, func, b'')
            if mo is not None:
                if stage == 0x05 and mo['kind'] == 'init':
                    mo['readable'] = True
                return self._reject(C, func)
            if self.state != 0x00 or slot is None:
                return self._reject(C, func)
            if stage == 0x00:
                self._start('init', slot, self.FEED_SPEED)
                return C.build_frame(ADDR, 0x00, func, b'')
            if stage == 0x05 and len(d) >= 3:
                self._start('dosed', slot, self.DOSED_SPEED, float(d[2]))
                return None
            return self._reject(C, func)
        if func == C.F_RETRUDE and len(d) >= 2:
            slot = self._slot_of(d[0])
            if self.motion is not None or self.state != 0x00 or slot is None:
                return self._reject(C, func)
            if d[1] >= 5:
                self._start('retract', slot, self.FEED_SPEED, float(d[1]))
                return None
            return self._reject(C, func)
        return None
