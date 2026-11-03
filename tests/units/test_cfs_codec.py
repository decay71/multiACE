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


def test_link_unsupported_off_the_wire():
    dev = fake_cfs.FakeCFS(H.VirtualClock(), 'cfs', slots=(0,))
    C, raw, link = _link(dev)
    for m in ('drying', 'drying_stop', 'get_feed_info', 'get_filament_info'):
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


# -- motion jobs (phase 2) ---------------------------------------------------

def _mlink(**kw):
    clock = H.VirtualClock()
    kw.setdefault('slots', (0,))
    dev = fake_cfs.FakeCFS(clock, 'cfs', **kw)
    C = _C()
    raw = _Raw(dev)
    link = C.CfsLink(raw, clock=clock.monotonic)
    proto = C.AceProtocolCfs()

    def ask(method, **params):
        link.write(proto.encode_request({'id': 1, 'method': method,
                                         'params': params}))
        buf = bytearray()
        while link.in_waiting:
            buf.extend(link.read(link.in_waiting))
        (r,) = proto.decode_frames(buf)
        return r
    ask('discover_device')
    return C, clock, dev, raw, link, ask


def _run_until_ready(clock, ask, limit=200):
    for _ in range(limit):
        clock.now += 1.0
        r = ask('get_status')
        if r['result']['status'] == 'ready':
            return r
    raise AssertionError('job did not end')


def _sent(raw, func_hex):
    return [t for t in raw.tx if t[8:10] == func_hex]


def test_feed_resumes_after_early_stop_and_stops_at_length():
    C, clock, dev, raw, link, ask = _mlink(init_limit=800)
    assert ask('feed_filament', index=0, length=2000, speed=80)['code'] == 0
    assert ask('get_status')['result']['status'] == 'busy'
    _run_until_ready(clock, ask)
    assert 1990 <= dev.wheel[0] <= 2050
    inits = [t for t in _sent(raw, '10') if t[10:14] == '0100']
    assert len(inits) == 3                       # start + two resumes
    assert any(t[10:14] == '0102' for t in _sent(raw, '10'))


def test_feed_resumes_when_only_the_wheel_stands():
    C, clock, dev, raw, link, ask = _mlink(init_limit=800, silent_stop=True)
    ask('feed_filament', index=0, length=2000, speed=80)
    _run_until_ready(clock, ask, limit=120)
    assert 1990 <= dev.wheel[0] <= 2050
    inits = [t for t in _sent(raw, '10') if t[10:14] == '0100']
    assert len(inits) == 3


def test_feed_waits_for_a_slow_restart():
    C, clock, dev, raw, link, ask = _mlink(init_limit=800, silent_stop=True,
                                           restart_delay=3.0)
    ask('feed_filament', index=0, length=2000, speed=80)
    _run_until_ready(clock, ask, limit=120)
    assert 1990 <= dev.wheel[0] <= 2050
    inits = [t for t in _sent(raw, '10') if t[10:14] == '0100']
    assert len(inits) == 3


def test_feed_waits_while_the_box_pulls_in_an_inserted_spool():
    C, clock, dev, raw, link, ask = _mlink(init_limit=800, pull_in_s=12.0)
    ask('feed_filament', index=0, length=2000, speed=80)
    clock.now += 5.0
    dev.insert(3)                       # pulled in once the motor is free
    _run_until_ready(clock, ask, limit=150)
    assert 1990 <= dev.wheel[0] <= 2050


def test_retract_waits_when_a_piece_is_rejected():
    C, clock, dev, raw, link, ask = _mlink(pull_in_s=10.0)
    ask('unwind_filament', index=0, length=600, speed=80)
    clock.now += 2.0
    dev.insert(3)                       # pulled in after the running piece
    _run_until_ready(clock, ask, limit=80)
    assert abs(dev.wheel[0] + 600.0) < 0.01


def test_motion_request_waits_for_a_busy_box():
    C, clock, dev, raw, link, ask = _mlink(pull_in_s=8.0)
    dev.insert(3)
    assert ask('unwind_filament', index=0, length=50, speed=80)['code'] == 0
    assert ask('get_status')['result']['status'] == 'busy'
    _run_until_ready(clock, ask, limit=40)
    assert abs(dev.wheel[0] + 50.0) < 0.01


def test_busy_box_gives_up_after_the_cap():
    C, clock, dev, raw, link, ask = _mlink(pull_in_s=500.0)
    dev.insert(3)
    ask('unwind_filament', index=0, length=50, speed=80)
    _run_until_ready(clock, ask, limit=150)
    assert dev.wheel[0] == 0.0


def test_feed_ends_at_silent_resistance():
    C, clock, dev, raw, link, ask = _mlink(block_at={0: 500.0},
                                           silent_stop=True)
    ask('feed_filament', index=0, length=2000, speed=80)
    r = _run_until_ready(clock, ask, limit=40)
    assert dev.wheel[0] == 500.0
    assert r['result']['motion']['stop'] == 'slip'


def test_feed_ends_at_resistance():
    C, clock, dev, raw, link, ask = _mlink(block_at={0: 500.0})
    ask('feed_filament', index=0, length=2000, speed=80)
    r = _run_until_ready(clock, ask)
    assert dev.wheel[0] == 500.0
    assert r['result']['motion']['stop'] == 'slip'   # status 09
    inits = [t for t in _sent(raw, '10') if t[10:14] == '0100']
    assert len(inits) == 2                       # one resume, no progress
    assert dev.state == 0x00                     # unlocked once idle


def test_host_stop_locks_then_status_unlocks():
    C, clock, dev, raw, link, ask = _mlink()
    ask('feed_filament', index=0, length=2000, speed=80)
    clock.now += 5.0
    r = ask('stop_feed_filament', index=0)
    assert r['code'] == 0 and dev.state == 0x03 and dev.motion is None
    assert 200 < dev.wheel[0] < 240
    clock.now += 1.0
    assert ask('get_status')['result']['status'] == 'ready'
    assert dev.state == 0x00
    assert _sent(raw, '04')[-1][10:14] == '0001'


def test_short_feed_is_dosed():
    C, clock, dev, raw, link, ask = _mlink()
    ask('feed_filament', index=0, length=30, speed=20)
    assert _sent(raw, '10')[-1][10:16] == '01051e'
    clock.now += 5.0
    assert ask('get_status')['result']['status'] == 'busy'
    _run_until_ready(clock, ask)
    assert abs(dev.wheel[0] - 30.0) < 0.01


def test_retract_runs_in_pieces():
    C, clock, dev, raw, link, ask = _mlink()
    ask('unwind_filament', index=0, length=600, speed=80)
    _run_until_ready(clock, ask)
    assert [t[10:14] for t in _sent(raw, '11')] == ['01ff', '01ff', '015a']
    assert abs(dev.wheel[0] + 600.0) < 0.01


class _TimedRaw(_Raw):
    """A port where an empty read waits one serial timeout, so the box
    can finish a motion and send its unasked reply while the link waits."""
    def __init__(self, dev, clock, C):
        _Raw.__init__(self, dev)
        self.clock, self.C = clock, C

    def read(self, n=1):
        if not self.rx:
            self.clock.now += 0.05
            self.dev.advance(self.C)
            for late in self.dev._late:
                self.rx.extend(late)
            self.dev._late = []
        return _Raw.read(self, n)


def test_retract_pieces_follow_without_a_gap():
    clock = H.VirtualClock()
    dev = fake_cfs.FakeCFS(clock, 'cfs', slots=(0,))
    C = _C()
    raw = _TimedRaw(dev, clock, C)
    link = C.CfsLink(raw, clock=clock.monotonic)
    proto = C.AceProtocolCfs()

    def ask(method, **params):
        link.write(proto.encode_request({'id': 1, 'method': method,
                                         'params': params}))
        buf = bytearray()
        while link.in_waiting:
            buf.extend(link.read(link.in_waiting))
        (r,) = proto.decode_frames(buf)
        return r
    ask('discover_device')
    t0 = clock.now
    ask('unwind_filament', index=0, length=765, speed=80)
    _run_until_ready(clock, ask)
    # three 255 mm pieces at 44 mm/s = 17.4 s; one step of slack for the
    # status that sees the job end, none per piece
    assert clock.now - t0 <= 765 / 44.0 + 2.5, clock.now - t0
    assert abs(dev.wheel[0] + 765.0) < 0.01


def test_stop_unwind_lets_the_piece_end():
    C, clock, dev, raw, link, ask = _mlink()
    ask('unwind_filament', index=0, length=600, speed=80)
    clock.now += 2.0
    assert ask('stop_unwind_filament', index=0)['code'] == 0
    _run_until_ready(clock, ask)
    assert len(_sent(raw, '11')) == 1 and abs(dev.wheel[0] + 255.0) < 0.01


def test_feed_assist_is_print_mode_and_waits_for_a_job():
    C, clock, dev, raw, link, ask = _mlink()
    ask('unwind_filament', index=0, length=100, speed=80)
    assert ask('start_feed_assist', index=0)['code'] == 0
    assert dev.state != 0x02                     # deferred while moving
    assert ask('feed_filament', index=0, length=100)['msg'] == 'FORBIDDEN'
    _run_until_ready(clock, ask)
    assert dev.state == 0x02
    assert _sent(raw, '04')[-1][10:14] == '0100'
    # print mode answers presence empty on some queries: the mask stays
    dev.print_presence_empty = True
    r = ask('get_status')
    assert [s['status'] for s in r['result']['slots']][0] == 'ready'
    # the next motion unlocks first
    ask('unwind_filament', index=0, length=50)
    assert dev.state in (0x00,) and dev.motion is not None


def test_locate_unlock_sends_04_00_01_even_when_idle():
    C, clock, dev, raw, link, ask = _mlink()
    n = len(_sent(raw, '04'))
    assert ask('locate_unlock')['code'] == 0
    assert ask('locate_unlock')['code'] == 0
    sent = _sent(raw, '04')[n:]
    assert len(sent) == 2 and all(t[10:14] == '0001' for t in sent)
    assert dev.wheel[0] == 0.0
    # from print mode it unlocks as well, and refuses while a job runs
    ask('start_feed_assist', index=0)
    assert dev.state == 0x02
    assert ask('locate_unlock')['code'] == 0
    assert dev.state == 0x00
    ask('unwind_filament', index=0, length=100, speed=80)
    assert ask('locate_unlock')['msg'] == 'FORBIDDEN'


def test_retract_is_measured_and_reports_full_distance():
    C, clock, dev, raw, link, ask = _mlink()
    ask('unwind_filament', index=0, length=600, speed=80)
    m = _run_until_ready(clock, ask, limit=80)['result']['motion']
    assert m['kind'] == 'retract' and not m['running']
    assert abs(m['mm'] - 600.0) < 1.0
    assert m['target'] == 600.0 and m['short'] == 0.0


def test_short_retract_tops_up_then_reports_short():
    # the strand hangs after 400 mm: the box still says each piece is done
    C, clock, dev, raw, link, ask = _mlink(retract_floor={0: -400.0})
    ask('unwind_filament', index=0, length=600, speed=80)
    m = _run_until_ready(clock, ask, limit=120)['result']['motion']
    assert abs(m['mm'] - 400.0) < 1.0
    assert abs(m['short'] - 200.0) < 1.0
    # 255 + 255 (short) + the rest with the missing part, retried twice
    pieces = _sent(raw, '11')
    assert len(pieces) == 4


def test_short_piece_recovered_by_the_top_up():
    C, clock, dev, raw, link, ask = _mlink(retract_floor={0: -100.0})
    ask('unwind_filament', index=0, length=150, speed=80)
    clock.now += 2.0
    dev.retract_floor.clear()          # the strand comes free again
    m = _run_until_ready(clock, ask, limit=80)['result']['motion']
    assert abs(m['mm'] - 150.0) < 1.0 and m['short'] == 0.0


def test_stopped_retract_is_not_short():
    C, clock, dev, raw, link, ask = _mlink()
    ask('unwind_filament', index=0, length=600, speed=80)
    clock.now += 1.0
    ask('stop_unwind_filament', index=0)
    m = _run_until_ready(clock, ask, limit=40)['result']['motion']
    assert m['short'] == 0.0


def test_feed_out_of_distance_reports_distance():
    # every init runs out (status 0A) and the restarts are used up
    C, clock, dev, raw, link, ask = _mlink(init_limit=200)
    ask('feed_filament', index=0, length=5000, speed=80)
    r = _run_until_ready(clock, ask)
    assert r['result']['motion']['stop'] == 'distance'


def test_feed_stopped_at_length_carries_no_stop_reason():
    C, clock, dev, raw, link, ask = _mlink(init_limit=800)
    ask('feed_filament', index=0, length=2000, speed=80)
    r = _run_until_ready(clock, ask)
    assert 'stop' not in r['result']['motion']
