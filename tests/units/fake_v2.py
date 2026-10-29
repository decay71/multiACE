"""FakeV2: an ACE 2 Pro on the wire (protobuf in the V2 frame).

Models what the host observes (UNIT_DRIVER_DESIGN.md section 10, H0):
- discover_device answers the unit's uid1..3 (the MCU id, D4);
- get_info answers firmware and boot version;
- get_status: slot_status transitions over virtual time (feeding/rollback
  for length/speed seconds, assisting while armed), filament state per
  slot, dryer block, temperature, humidity;
- the V2 busy reply `code=n msg=error_n` for a motion on a busy slot;
- get_filament_info / filament_identify answer sku/type/color + code;
- get_feed_info answers steps/length/decoder per slot, the decoder
  advancing with motion.

Frames are parsed and built here with the real module's helpers (varint,
crc), so the host's codec is exercised byte for byte.
"""
from __future__ import annotations

from . import harness as H


def _pb_len(field, blob):
    _, P2 = H.protocol_modules()
    return P2.pb_varint((field << 3) | 2) + P2.pb_varint(len(blob)) + blob


def _pb_str(field, text):
    return _pb_len(field, text.encode('utf-8'))


SLOT_STATE_CODE = {'ready': 0, 'feeding': 1, 'rollback': 2, 'assisting': 3,
                   'rollback_assisting': 4, 'preloading': 5, 'upgrading': 6,
                   'feed_error': 129, 'rollback_error': 130,
                   'assist_error': 131, 'preload_error': 132,
                   'stuck_error': 133, 'tangled_error': 134,
                   'motor_error': 135}
FILAMENT_STATE_CODE = {'empty': 0, 'unknown': 1, 'identified': 2,
                       'identifying': 3}
DRY_STATE_CODE = {'stop': 0, 'starting': 1, 'keeping': 2, 'stopping': 3}


class FakeV2:
    def __init__(self, clock, label, firmware='V1.1.31', boot='V1.0.2',
                 uid=(0x8f3b5218, 0x07b09108, 0x3131334a), slots=None,
                 latency=0.02, humidity=38, temp=26):
        self.clock = clock
        self.label = label
        self.firmware = firmware
        self.boot = boot
        self.uid = tuple(uid)
        self.latency = latency
        self.humidity = humidity
        self.temp = temp
        _, P2 = H.protocol_modules()
        self.P2 = P2
        self._buf = bytearray()
        self.rfid_enabled = True
        self.feed_assist_count = 0
        self.cont_assist_time = 0
        self.dryer = {'state': 'stop', 'target': 0, 'duration': 0,
                      'remain': 0}
        self.feed_check = None
        self.drop = {}              # cmd -> remaining responses to swallow
        self.ignore_next_assist = 0 # assist commands to not even execute
        self.slots = []
        for i in range(4):
            self.slots.append({
                'index': i, 'filament': 'empty', 'state': 'ready',
                'sku': '', 'type': '', 'color': (0, 0, 0),
                'motion_until': None, 'decoder': 0, 'steps': 0, 'length': 0,
            })
        for i, spec in (slots or {}).items():
            self.insert(i, **spec)
        self.requests = []

    # -- spool handling ---------------------------------------------------

    def insert(self, slot, sku='', type='PLA', color=(0, 0, 0),
               identified=True):
        s = self.slots[slot]
        s['filament'] = 'identified' if identified else 'unknown'
        s['sku'] = sku
        s['type'] = type
        s['color'] = tuple(color)

    def remove(self, slot):
        s = self.slots[slot]
        s.update({'filament': 'empty', 'sku': '', 'type': '',
                  'color': (0, 0, 0)})

    def drop_next(self, cmd, count=1):
        """Scenario knob: swallow the next `count` responses to `cmd` (the
        unit executes the command, the reply is lost, paragraph 33)."""
        self.drop[cmd] = self.drop.get(cmd, 0) + count

    def stick_slot(self, slot, state='preloading'):
        """Scenario knob: a slot reports a motion state and never leaves it
        (the device-side hang wait_ace_ready_on has to cope with)."""
        s = self.slots[slot]
        s['state'] = state
        s['motion_until'] = float('inf')

    # -- wire -------------------------------------------------------------

    def on_bytes(self, data):
        P2 = self.P2
        self._buf.extend(data)
        out = []
        buf = self._buf
        while len(buf) >= P2.MIN_FRAME_LEN:
            start = buf.find(P2.PREAMBLE)
            if start < 0:
                del buf[:]
                break
            if start:
                del buf[:start]
            if len(buf) < P2.HEADER_LEN:
                break
            plen = buf[6]
            total = P2.HEADER_LEN + plen + P2.TRAILER_LEN
            if len(buf) < total:
                break
            inner = bytes(buf[2:P2.HEADER_LEN + plen])
            crc = buf[P2.HEADER_LEN + plen] | (buf[P2.HEADER_LEN + plen + 1] << 8)
            end = buf[total - 1]
            del buf[:total]
            if end != P2.END_MARKER or crc != P2.crc16_kermit(inner):
                continue
            flags, seq, cmd = inner[0], inner[1] | (inner[2] << 8), inner[3]
            payload = inner[5:5 + plen]
            if flags & P2.FLAG_RESPONSE:
                continue
            reply = self.handle(cmd, seq, payload)
            if reply is None:
                continue
            if self.drop.get(cmd, 0) > 0:
                self.drop[cmd] -= 1
                continue
            out.append((self.latency, reply))
        return out

    def _frame(self, seq, cmd, payload):
        P2 = self.P2
        inner = bytearray([P2.FLAG_RESPONSE, seq & 0xFF, (seq >> 8) & 0xFF,
                           cmd & 0xFF, len(payload) & 0xFF])
        inner.extend(payload)
        crc = P2.crc16_kermit(bytes(inner))
        return (bytes(P2.PREAMBLE) + bytes(inner)
                + bytes([crc & 0xFF, (crc >> 8) & 0xFF, P2.END_MARKER]))

    # -- behaviour --------------------------------------------------------

    def _tick(self):
        now = self.clock.now
        for s in self.slots:
            until = s['motion_until']
            if until is not None and now >= until:
                s['motion_until'] = None
                s['state'] = 'ready'

    def _status_payload(self):
        P2 = self.P2
        self._tick()
        out = bytearray()
        out += P2.pb_uint32(1, 1)
        dry = (P2.pb_uint32(1, DRY_STATE_CODE[self.dryer['state']])
               + P2.pb_uint32(2, self.dryer['target'])
               + P2.pb_uint32(3, self.dryer['duration'])
               + P2.pb_uint32(4, self.dryer['remain']))
        out += _pb_len(2, dry)
        out += P2.pb_uint32(3, self.temp)
        out += P2.pb_uint32(4, self.humidity)
        out += P2.pb_bool(5, self.rfid_enabled)
        out += P2.pb_uint32(7, self.feed_assist_count)
        out += P2.pb_uint32(8, self.cont_assist_time)
        for s in self.slots:
            sub = (P2.pb_uint32(1, SLOT_STATE_CODE[s['state']])
                   + P2.pb_uint32(2, FILAMENT_STATE_CODE[s['filament']]))
            out += _pb_len(9, sub)
        return bytes(out)

    def _filament_payload(self, slot):
        P2 = self.P2
        s = self.slots[slot]
        out = bytearray(P2.pb_uint32(1, slot))
        if s['filament'] == 'identified':
            r, g, b = s['color']
            rgba = (r << 24) | (g << 16) | (b << 8) | 0xFF
            out += _pb_str(3, s['sku'])
            out += _pb_str(4, s['type'])
            out += _pb_len(5, P2.pb_uint32(1, rgba))
            out += P2.pb_uint32(12, 0)
        else:
            out += P2.pb_uint32(12, 3)
        return bytes(out)

    def _feed_info_payload(self):
        P2 = self.P2
        self._tick()
        out = bytearray()
        for s in self.slots:
            sub = (P2.pb_uint32(1, s['steps']) + P2.pb_uint32(2, s['length'])
                   + P2.pb_uint32(3, s['decoder'] & 0xFFFFFFFFFFFFFFFF))
            out += _pb_len(1, sub)
        return bytes(out)

    def _generic(self, seq, cmd, code=0):
        payload = b'' if code == 0 else self.P2.pb_uint32(1, code)
        return self._frame(seq, cmd, payload)

    def handle(self, cmd, seq, payload):
        P2 = self.P2
        Cmd = P2.Cmd
        fields = P2.pb_decode(payload) if payload else {}
        fv = lambda n, d=0: P2._fval(fields, n, d)
        self.requests.append((self.clock.now, cmd, dict(
            (k, [v for _, v in vs]) for k, vs in fields.items())))

        if cmd == Cmd.DISCOVER_DEVICE:
            pl = (P2.pb_uint32(1, self.uid[0]) + P2.pb_uint32(2, self.uid[1])
                  + P2.pb_uint32(3, self.uid[2]))
            return self._frame(seq, cmd, pl)
        if cmd == Cmd.GET_INFO:
            return self._frame(seq, cmd, _pb_str(1, self.firmware)
                               + _pb_str(2, self.boot))
        if cmd == Cmd.GET_STATUS:
            return self._frame(seq, cmd, self._status_payload())
        if cmd == Cmd.SET_FEED_CHECK:
            self.feed_check = (fv(1), fv(2))
            return self._generic(seq, cmd)
        if cmd in (Cmd.GET_FILAMENT_INFO, Cmd.FILAMENT_IDENTIFY):
            slot = min(max(fv(1), 0), 3)
            return self._frame(seq, cmd, self._filament_payload(slot))
        if cmd == Cmd.GET_FEED_INFO:
            return self._frame(seq, cmd, self._feed_info_payload())
        if cmd == Cmd.FEED_OR_ROLLBACK:
            self._tick()
            slot = min(max(fv(1), 0), 3)
            speed = fv(2, 50) or 50
            length = fv(3)
            mode = fv(4)
            s = self.slots[slot]
            if mode in (P2.FEED_MODE_FEED, P2.FEED_MODE_ROLLBACK):
                if s['motion_until'] is not None:
                    return self._generic(seq, cmd, code=2)
                s['state'] = 'feeding' if mode == P2.FEED_MODE_FEED else 'rollback'
                s['motion_until'] = self.clock.now + float(length) / float(speed)
                sign = 1 if mode == P2.FEED_MODE_FEED else -1
                s['decoder'] = (s['decoder'] + sign * int(length) * 10)
                s['length'] += int(length)
                s['steps'] += int(length) * 100
            elif mode == P2.FEED_MODE_ASSIST:
                if self.ignore_next_assist > 0:
                    self.ignore_next_assist -= 1
                    return self._generic(seq, cmd)
                if s['filament'] == 'empty':
                    return self._generic(seq, cmd, code=3)
                if s['motion_until'] is not None:
                    return self._generic(seq, cmd, code=2)
                s['state'] = 'assisting'
                self.feed_assist_count += 1
            elif mode == P2.FEED_MODE_ROLLBACK_ASSIST:
                s['state'] = 'rollback_assisting'
            return self._generic(seq, cmd)
        if cmd == Cmd.STOP_FEED_OR_ROLLBACK:
            slot = min(max(fv(1), 0), 3)
            s = self.slots[slot]
            s['motion_until'] = None
            s['state'] = 'ready'
            return self._generic(seq, cmd)
        if cmd == Cmd.UPDATE_SPEED:
            return self._generic(seq, cmd)
        if cmd == Cmd.DRYING:
            temp, duration = fv(1), fv(2)
            if temp == 0 and duration == 0:
                self.dryer = {'state': 'stop', 'target': 0, 'duration': 0,
                              'remain': 0}
            else:
                self.dryer = {'state': 'starting', 'target': temp,
                              'duration': duration, 'remain': duration}
            return self._generic(seq, cmd)
        if cmd == Cmd.SET_RFID_ENABLE:
            self.rfid_enabled = bool(fv(2, 1))
            return self._generic(seq, cmd)
        if cmd == Cmd.GET_TEMP:
            return self._frame(seq, cmd, P2.pb_uint32(1, self.temp)
                               + P2.pb_uint32(5, self.temp)
                               + P2.pb_uint32(6, self.humidity))
        if cmd in (Cmd.SET_DRY_TEMP, Cmd.SET_VALVE, Cmd.SET_FAN,
                   Cmd.SET_DRY_POWER, Cmd.GET_KEY_STATE, Cmd.RFID_TEST,
                   Cmd.ASSIGN_DEVICE_ID, Cmd.IAP_VERSION):
            return self._generic(seq, cmd)
        return self._generic(seq, cmd, code=1)
