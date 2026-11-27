"""ace2k codec on its own: the device id read as the stock ACE 2 key
(HW 2026-11-08), and the real pre-claim probe against FakeAce2k: it reads
the dictionary and the id and nothing else (no link proof, no reset, no
reports), answers in a Klipper shutdown, and stays bounded on a silent
port."""
from . import fake_ace2k


class _Clock:
    now = 1000.0


class _Raw:
    """Raw port in front of a FakeAce2k that answers at once; read() never
    blocks. dev None = a silent port."""
    def __init__(self, dev):
        self.dev = dev
        self.rx = bytearray()
        self.is_open = True

    def write(self, data):
        if self.dev is not None:
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


def _C():
    return fake_ace2k.codec()


def test_hw_device_id_is_the_stock_key():
    C = _C()
    raw = bytes.fromhex('87933b8f0891b0074a333131')
    assert C.chip_words(raw) == fake_ace2k.UID_WORDS
    assert C.uid_key(C.chip_words(raw)) == 'uid:8f3b9387-07b09108-3131334a'
    assert C.uid_key(C.chip_words(b'\0' * 12)) == ''
    assert C.chip_words(b'\x01\x02') == ()


def test_probe_reads_key_and_changes_nothing():
    C = _C()
    dev = fake_ace2k.FakeAce2k(_Clock(), 'k')
    res = C.Ace2kLink(_Raw(dev)).probe()
    assert res['ok'] and res['category'] == 'verified'
    assert C.uid_key(res['uid']) == fake_ace2k.UID_KEY
    assert res['firmware'] == 'ace2k v0.13.0'
    names = [r[1] for r in dev.requests]
    assert set(names) == {'identify', 'ace2k_uid_query'}
    assert dev.periods == {} and dev.resets == 0 and dev.auto_load == 1


def test_probe_in_shutdown_still_reads_the_key():
    C = _C()
    dev = fake_ace2k.FakeAce2k(_Clock(), 'k', shutdown=True)
    res = C.Ace2kLink(_Raw(dev)).probe()
    assert res['ok'] and C.uid_key(res['uid']) == fake_ace2k.UID_KEY
    assert dev.resets == 0


def test_probe_silent_port_is_bounded():
    C = _C()
    res = C.Ace2kLink(_Raw(None)).probe()
    assert not res['ok'] and res['category'] == 'no_response'


def test_probe_refuses_a_foreign_dictionary():
    C = _C()
    cmds = {k: v for k, v in fake_ace2k.COMMANDS.items()
            if not k.startswith('ace2k_uid_query')}
    dev = fake_ace2k.FakeAce2k(_Clock(), 'k', commands=cmds)
    res = C.Ace2kLink(_Raw(dev)).probe()
    assert not res['ok'] and res['category'] == 'invalid_identity'


def test_port_label_is_the_usb_tail():
    C = _C()
    assert C.port_label('/dev/serial/by-path/platform-fed00000.usb-usb-'
                        '0:1.3.3:1.0') == '0:1.3.3:1.0'
    assert C.port_label('/dev/ttyACM0') == 'ttyACM0'
    assert C.port_label('') == ''


def test_closed_port_read_is_a_dead_link():
    """pyserial raises TypeError when another thread closed the port under
    a read: the link reports a dead link instead of a traceback."""
    C = _C()

    class _Closed(_Raw):
        def read(self, n=1):
            raise TypeError("'NoneType' object cannot be interpreted")

    link = C.Ace2kLink(_Closed(None))
    try:
        link._read_some()
    except C.LinkDead:
        pass
    else:
        raise AssertionError('no LinkDead')


def _lost_rig(C, now):
    link = C.Ace2kLink(_Raw(None), clock=lambda: now[0])
    link.dictionary = type('D', (), {'has': staticmethod(lambda n: False)})()
    link._feed = (99.0, {'mode': bytes(4)})
    link._jobs[1] = {'kind': 'retract', 'slot': 1, 'target': 500.0,
                     'done': 0.0, 'speed': 60.0, 'free_tip': False,
                     'mseq': 7, 'seq': 3, 'piece': 500.0, 't0': 90.0,
                     'deadline': 200.0}
    return link


def test_lost_end_event_books_nothing_and_is_not_short():
    """A piece whose end event never came (the lane idle in the report):
    the link waits a moment for the event, then ends the move. Nothing is
    booked, and the motion report does not claim the retract came back
    short (HW 2026-11-21: a false unload pause)."""
    C = _C()
    now = [100.0]
    link = _lost_rig(C, now)
    link._tick()
    assert 1 in link._jobs                 # still waiting for the event
    now[0] += C.LOST_ENCODER_WAIT_S + 0.1
    link._tick()
    assert 1 not in link._jobs
    m = link._lane_motion[1]
    assert m['mm'] == 0.0 and m['stop'] == 'lost_event'
    assert m['short'] == 0.0 and m['unmeasured'] is True


def test_end_event_after_the_idle_report_still_counts():
    """An image before 2026-11-12 can send the idle report before the
    move's end event: an event inside the wait ends the move normally."""
    C = _C()
    now = [100.0]
    link = _lost_rig(C, now)
    link._tick()
    kind = [k for k, v in C.KIND_NAMES.items() if v == 'done'][0]
    link._on_event({'lane': 1, 'kind': kind, 'seq': 3,
                    'filament_um': 500000})
    assert 1 not in link._jobs
    m = link._lane_motion[1]
    assert m['mm'] == 500.0 and m['short'] == 0.0
    assert 'unmeasured' not in m
