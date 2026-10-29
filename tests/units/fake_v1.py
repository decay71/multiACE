"""FakeV1: an ACE Pro on the wire (JSON-RPC in the V1 frame).

Models what the host observes (UNIT_DRIVER_DESIGN.md section 10, H0):
- status words 'ready' / 'empty1' per slot, sku/type/brand/color in the
  status (the ACE Pro reads its own tags, V1 has no separate query);
- `code=0 msg=FORBIDDEN` while a motion is running (the V1 busy reply);
- `msg=empty` for an assist on an empty gate (paragraph 50);
- feed/unwind take length/speed seconds of virtual time, the status says
  'busy' meanwhile, assist is not busy.

Frames are decoded and encoded with the real protocol module, so the
host's framing is exercised byte for byte.
"""
from __future__ import annotations

import json

from . import harness as H


class FakeV1:
    MODEL = 'Anycubic ACE'

    def __init__(self, clock, label, firmware='V1.3.871', slots=None,
                 latency=0.02, omit=()):
        self.clock = clock
        # Scenario knob: status keys this unit never reports (a codec
        # without a dryer or a temperature probe).
        self.omit = set(omit)
        self.label = label
        self.firmware = firmware
        self.latency = latency
        P1, _ = H.protocol_modules()
        self._proto = P1.AceProtocolV1()
        self._buf = bytearray()
        self.temp = 24
        self.dryer = {'status': 'stop', 'target_temp': 0, 'duration': 0,
                      'remain_time': 0}
        self.fan_speed = 7000
        self.feed_assist_count = 0
        self.cont_assist_time = 0.0
        self.assist_slot = -1
        self.motion = None          # (slot, until, kind)
        self.stuck = False          # status 'busy' forever, no motion
        self.slots = []
        for i in range(4):
            s = {'index': i, 'status': 'empty1', 'sku': '', 'type': '',
                 'rfid': 0, 'brand': '', 'color': [0, 0, 0]}
            self.slots.append(s)
        for i, spec in (slots or {}).items():
            self.insert(i, **spec)
        self.requests = []

    # -- spool handling ---------------------------------------------------

    def insert(self, slot, sku='', type='PLA', brand='', color=(0, 0, 0),
               rfid=None):
        s = self.slots[slot]
        s['status'] = 'ready'
        s['sku'] = sku
        s['type'] = type
        s['brand'] = brand
        s['color'] = list(color)
        s['rfid'] = (2 if sku or type else 0) if rfid is None else rfid

    def remove(self, slot):
        s = self.slots[slot]
        s.update({'status': 'empty1', 'sku': '', 'type': '', 'rfid': 0,
                  'brand': '', 'color': [0, 0, 0]})

    # -- wire -------------------------------------------------------------

    def on_bytes(self, data):
        self._buf.extend(data)
        out = []
        for req in self._proto.decode_frames(self._buf):
            resp = self.handle(req)
            if resp is None:
                continue
            payload = json.dumps(resp).encode('utf-8')
            out.append((self.latency, self._frame(payload)))
        return out

    def _frame(self, payload):
        import struct
        P1, _ = H.protocol_modules()
        data = bytes(P1.FRAME_START)
        data += struct.pack('<H', len(payload))
        data += payload
        data += struct.pack('<H', P1.calc_crc(payload))
        data += bytes([P1.FRAME_END])
        return data

    # -- behaviour --------------------------------------------------------

    def busy_for(self, seconds, slot=0):
        """Scenario knob: the unit is in a motion of its own for a while
        (a neighbour's preload) and answers FORBIDDEN meanwhile."""
        self.motion = (slot, self.clock.now + seconds, 'external')

    def _busy(self):
        if self.stuck:
            return True
        if self.motion is None:
            return False
        if self.clock.now >= self.motion[1]:
            self.motion = None
            return False
        return True

    def _status(self):
        busy = self._busy()
        st = self._status_full(busy)
        for k in self.omit:
            st.pop(k, None)
        return st

    def _status_full(self, busy):
        return {
            'status': 'busy' if busy else 'ready',
            'dryer_status': dict(self.dryer),
            'temp': self.temp,
            'enable_rfid': 1,
            'fan_speed': self.fan_speed,
            'feed_assist_count': self.feed_assist_count,
            'cont_assist_time': self.cont_assist_time,
            'slots': [dict(s) for s in self.slots],
        }

    def handle(self, req):
        method = req.get('method', '')
        params = req.get('params', {}) or {}
        rid = req.get('id')
        self.requests.append((self.clock.now, method, params))
        ok = {'id': rid, 'code': 0, 'msg': 'success', 'result': {}}

        if method == 'get_info':
            ok['result'] = {'model': self.MODEL, 'firmware': self.firmware}
            return ok
        if method == 'get_status':
            ok['result'] = self._status()
            return ok
        if method in ('feed_filament', 'unwind_filament'):
            if self._busy():
                return {'id': rid, 'code': 0, 'msg': 'FORBIDDEN'}
            slot = int(params.get('index', 0))
            length = float(params.get('length', 0))
            speed = float(params.get('speed', 50)) or 50.
            self.motion = (slot, self.clock.now + length / speed, method)
            return ok
        if method in ('stop_feed_filament', 'stop_feed_assist'):
            self.motion = None
            if method == 'stop_feed_assist':
                self.assist_slot = -1
            return ok
        if method == 'start_feed_assist':
            slot = int(params.get('index', 0))
            if self._busy():
                return {'id': rid, 'code': 0, 'msg': 'FORBIDDEN'}
            if self.slots[slot]['status'].startswith('empty'):
                return {'id': rid, 'code': 0, 'msg': 'empty'}
            self.assist_slot = slot
            return ok
        if method == 'update_feeding_speed':
            return ok
        if method == 'drying':
            self.dryer = {'status': 'drying',
                          'target_temp': int(params.get('temp', 50)),
                          'duration': int(params.get('duration', 0)),
                          'remain_time': int(params.get('duration', 0))}
            return ok
        if method == 'drying_stop':
            self.dryer = {'status': 'stop', 'target_temp': 0, 'duration': 0,
                          'remain_time': 0}
            return ok
        if method == 'set_fan_speed':
            self.fan_speed = int(params.get('speed', 0))
            return ok
        return {'id': rid, 'code': 1, 'msg': 'unknown method %s' % method}
