"""FakeCFS: a Creality CFS Nano on the RS485 bus (F7 frames, CRC-8).

Models what the wire test measured (CLAUDE.md section 80):
- GET_SLAVE_INFO (0xA1) answered only on the broadcast address 0xFF,
  from the fixed address 0x35, device type 8, a 12-byte unique id;
- 0x08 = presence mask (bit n = slot n), 0x0A = box state (last byte),
  with status 0x30 once after an insert or a removal (the event is
  consumed by the read), 0x14 = version string;
- every other function goes unanswered (the Nano is silent on 0x02,
  0x03, 0x05, 0xF0);
- `mute` until a virtual time (the box is mute for about 30 s after
  power-on).

Frames are built and parsed with the real codec module.
"""
from __future__ import annotations

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

    def __init__(self, clock, label, slots=(), latency=0.01, uid=UID,
                 mute_until=None):
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

    def insert(self, slot):
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
            reply = self.handle(C, fr)
            if reply is not None:
                out.append((self.latency, reply))
        return out

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
        if func == C.F_PRESENCE:
            return C.build_frame(ADDR, 0x00, func,
                                 bytes(bytearray([self.mask, 0, 0, 0, 0])))
        if func == C.F_BOX_STATE:
            if self.event is not None:
                ev, self.event = self.event, None
                return C.build_frame(ADDR, C.INSERT_EVENT_STATUS, func,
                                     bytes(bytearray(ev)))
            return C.build_frame(ADDR, 0x00, func,
                                 bytes(bytearray([0, 0, 0, self.state])))
        return None
