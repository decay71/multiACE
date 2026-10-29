"""CFS codec on its own: frames against bytes measured on the CFS Nano
(CLAUDE.md section 80), the status synthesis, the uid key, and the link
against a scripted raw port (one transaction per write, silence bounded,
motor methods answered unsupported without touching the wire)."""
import json

from . import harness as H
from . import fake_cfs


def _C():
    return fake_cfs.codec()


def test_frames_match_wire_test():
    C = _C()
    # GET_SLAVE_INFO broadcast as the probe script sent it
    assert C.build_frame(0xFF, 0x00, 0xA1, b'\xfe\xfe').hex() == 'f7ff0500a1fefef8'
    # empty measuring-wheel reply seen on the Nano: F7 35 03 00 0E 97
    buf = bytearray(bytes.fromhex('f73503000e97'))
    frames, bad = C.parse_frames(buf)
    assert bad == 0 and frames[0]['func'] == 0x0E and frames[0]['addr'] == 0x35
    # a corrupt byte is dropped, the next good frame still parses
    buf = bytearray(bytes.fromhex('f73503000e98') + C.build_frame(0x35, 0, 0x08, b'\x03\0\0\0\0'))
    frames, bad = C.parse_frames(buf)
    assert bad == 1 and [f['func'] for f in frames] == [0x08]


def test_status_from_mask_and_box_state():
    C = _C()
    st = C.make_status(0b1010, {'status': 0, 'data': b'\0\0\0\x04'})
    assert [s['status'] for s in st['slots']] == ['empty', 'ready', 'empty', 'ready']
    assert st['box_state'] == 'locked_after_feed' and st['status'] == 'ready'
    ev = C.make_status(0b0001, {'status': 0x30, 'data': b'\x03\0\0\0'})
    assert ev['box_event'] == [3, 0, 0, 0] and 'box_state' not in ev
    assert 'temp' not in st and 'dryer_status' not in st


def test_uid_key():
    C = _C()
    w = C.uid_words(fake_cfs.UID)
    assert C.uid_key(w) == 'uid:56703531-35330d00-52373132'
    assert C.uid_key(C.uid_words(b'\0' * 12)) == ''


class _Raw:
    """Scripted raw port: a FakeCFS answers at once, read() never blocks."""
    def __init__(self, dev):
        self.dev = dev
        self.rx = bytearray()
        self.tx = []
        self.is_open = True

    def write(self, data):
        self.tx.append(bytes(data).hex())
        for _lat, reply in self.dev.on_bytes(data):
            self.rx.extend(reply)
        return len(data)

    def read(self, n=1):
        d = bytes(self.rx[:n])
        del self.rx[:n]
        return d

    @property
    def in_waiting(self):
        return len(self.rx)

    def close(self):
        self.is_open = False


def _link(dev):
    C = _C()
    raw = _Raw(dev)
    return C, raw, C.CfsLink(raw)


def _ask(C, link, method, rid=7):
    proto = C.AceProtocolCfs()
    link.write(proto.encode_request({'id': rid, 'method': method, 'params': {}}))
    buf = bytearray()
    while link.in_waiting:
        buf.extend(link.read(link.in_waiting))
    return proto.decode_frames(buf)


def test_link_status_and_identity():
    dev = fake_cfs.FakeCFS(H.VirtualClock(), 'cfs', slots=(0, 2))
    C, raw, link = _link(dev)
    (r,) = _ask(C, link, 'discover_device')
    assert r['id'] == 7 and r['code'] == 0 and link.addr == 0x35
    assert C.uid_key((r['result']['uid1'], r['result']['uid2'],
                      r['result']['uid3'])) == 'uid:56703531-35330d00-52373132'
    (r,) = _ask(C, link, 'get_info')
    assert r['result']['firmware'] == '13680000140289S226GSHT'
    (r,) = _ask(C, link, 'get_status')
    assert [s['status'] for s in r['result']['slots']] == ['ready', 'empty', 'ready', 'empty']


def test_link_motion_unsupported_off_the_wire():
    dev = fake_cfs.FakeCFS(H.VirtualClock(), 'cfs', slots=(0,))
    C, raw, link = _link(dev)
    for m in ('feed_filament', 'unwind_filament', 'start_feed_assist',
              'stop_feed_filament', 'drying'):
        (r,) = _ask(C, link, m)
        assert r['code'] == C.UNSUPPORTED and r['msg'] == 'unsupported:' + m
    assert raw.tx == []


def test_link_silent_box_gives_no_answer():
    clock = H.VirtualClock()
    dev = fake_cfs.FakeCFS(clock, 'cfs', slots=(0,), mute_until=clock.now + 30)
    C, raw, link = _link(dev)
    assert _ask(C, link, 'get_status') == []
    assert len(raw.tx) == 1          # one A1 try, then nothing


def test_link_dead_port_raises_on_both_sides():
    C = _C()

    class Dead(_Raw):
        def write(self, data):
            raise IOError('[Errno 5] Input/output error')
    link = C.CfsLink(Dead(None))
    proto = C.AceProtocolCfs()
    try:
        link.write(proto.encode_request({'id': 1, 'method': 'get_status'}))
        raise AssertionError('write did not raise')
    except IOError:
        pass
    try:
        link.read(1)
        raise AssertionError('read did not raise')
    except IOError:
        pass
