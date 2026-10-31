"""ace_bus_v2: several ACE 2 Pro daisy-chained on one cable.

Runs the real V2Bus against FakeChain, an in-process model of units on one
RS-485 line: units boot at address 0, answer DISCOVER with their uid, ACK an
ASSIGN on the OLD address, drop back to address 0 after `expiry` s without a
frame addressed to them, and serialise their replies to a broadcast
(behaviour measured by tobecwb, multiACE #120). Timings are scaled down.
"""
import errno
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'multiace' / 'klipper'))

from extras import ace_bus_v2 as B                       # noqa: E402
from extras.ace_protocol_v2 import pb_decode, pb_uint32  # noqa: E402

UID_A = (111111111, 222222, 333333333)
UID_B = (444444444, 555555, 666666666)

FAST = dict(KEEPALIVE_S=0.2, REPLY_TIMEOUT_S=0.3, PROBE_TIMEOUT_S=0.1,
            DISCOVER_WAIT_S=0.05, RESCAN_S=0.5, LOST_PROBE_S=0.2,
            LOST_PROBE_SLOW_S=0.5, REOPEN_S=0.1, OPEN_WAIT_S=2.0)
EXPIRY = 0.7


class FakeUnit:
    def __init__(self, uid, fw):
        self.uid, self.fw = uid, fw
        self.addr = 0
        self.last = time.monotonic()
        self.mute_until = 0.0
        self.powered = True
        self.seen = []                      # (addr, cmd) of frames it took

    def handle(self, f):
        if not self.powered:
            return None
        if self.addr and time.monotonic() - self.last > EXPIRY:
            self.addr = 0
        if f['addr'] != self.addr:
            return None
        if time.monotonic() < self.mute_until:
            return None
        self.last = time.monotonic()
        self.seen.append((f['addr'], f['cmd']))
        reply = 0x80 | self.addr
        if f['cmd'] == B.CMD_DISCOVER:
            payload = b''.join(pb_uint32(i + 1, v)
                               for i, v in enumerate(self.uid))
        elif f['cmd'] == B.CMD_ASSIGN:
            if B.uid_of(f['payload']) != self.uid:
                return None
            ack = B.build(reply, f['seq'], f['cmd'])      # on the OLD address
            self.addr = int(pb_decode(f['payload'])[4][0][1])
            return ack
        elif f['cmd'] == B.CMD_GET_INFO:
            payload = bytes([0x0A, len(self.fw)]) + self.fw.encode()
        else:
            payload = b''
        return B.build(reply, f['seq'], f['cmd'], payload)

    def reboot(self):
        self.addr = 0


class FakeChain:
    """Looks like the pyserial object the bus opens."""

    def __init__(self, *units):
        self.units = list(units)
        self.rx = bytearray()
        self.tx = bytearray()
        self.lock = threading.Lock()
        self.fail = False
        self.is_open = True
        self.frames = []                    # every host frame on the wire

    def write(self, data):
        if self.fail:
            raise OSError(errno.EIO, 'Input/output error')
        self.tx.extend(data)
        for f in B.parse(self.tx):
            self.frames.append(f)
            for u in self.units:
                r = u.handle(f)
                if r:
                    with self.lock:
                        self.rx.extend(r)
        return len(data)

    @property
    def in_waiting(self):
        if self.fail:
            raise OSError(errno.EIO, 'Input/output error')
        return len(self.rx)

    def read(self, size=1):
        if self.fail:
            raise OSError(errno.EIO, 'Input/output error')
        with self.lock:
            data = bytes(self.rx[:size])
            del self.rx[:size]
        if not data:
            time.sleep(0.005)
        return data

    def close(self):
        self.is_open = False


def host_frame(seq, cmd, payload=b''):
    """What multiACE's V2 codec sends: always address 0x00."""
    return B.build(0x00, seq, cmd, payload)


def ask(port, seq, cmd, wait=2.0):
    port.write(host_frame(seq, cmd))
    buf, deadline = bytearray(), time.monotonic() + wait
    while time.monotonic() < deadline:
        buf.extend(port.read(64))
        for f in B.parse(buf):
            if f['seq'] == seq:
                return f
    return None


class BusTest(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.multiple(B, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.buses = []

    def tearDown(self):
        for bus in self.buses:
            bus.stop()

    def make_bus(self, chain):
        bus = B.V2Bus('/dev/serial/by-path/fake-cable',
                      serial_factory=lambda path: chain)
        self.buses.append(bus)
        return bus

    def wait_for(self, pred, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_single_unit_stays_at_address_0(self):
        a = FakeUnit(UID_A, 'V1.1.60O')
        chain = FakeChain(a)
        bus = self.make_bus(chain)
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 1))
        self.assertFalse(bus.addressed)
        port = bus.open_unit(B.uid_hex(UID_A))
        reply = ask(port, 101, B.CMD_GET_INFO)
        self.assertIsNotNone(reply)
        self.assertEqual(reply['addr'], 0x80)
        self.assertEqual(a.addr, 0)
        self.assertNotIn(B.CMD_ASSIGN, [f['cmd'] for f in chain.frames])
        # the host's frame went out byte for byte as it was written
        self.assertIn(host_frame(101, B.CMD_GET_INFO),
                      [f['raw'] for f in chain.frames])

    def test_two_units_get_addresses_and_route(self):
        a, b = FakeUnit(UID_A, 'V1.1.60O'), FakeUnit(UID_B, 'V1.1.31')
        bus = self.make_bus(FakeChain(a, b))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        self.assertTrue(bus.addressed)
        self.assertEqual(sorted((a.addr, b.addr)), [1, 2])
        pa = bus.open_unit(B.uid_hex(UID_A))
        pb = bus.open_unit(B.uid_hex(UID_B))
        for i in range(10):
            ra = ask(pa, 200 + 2 * i, B.CMD_DISCOVER)
            rb = ask(pb, 201 + 2 * i, B.CMD_DISCOVER)
            self.assertEqual(B.uid_of(ra['payload']), UID_A)
            self.assertEqual(B.uid_of(rb['payload']), UID_B)
            self.assertEqual(ra['addr'], 0x80)
            self.assertEqual(rb['addr'], 0x80)

    def test_keepalive_holds_addresses_while_idle(self):
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        bus = self.make_bus(FakeChain(a, b))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        addrs = (a.addr, b.addr)
        time.sleep(EXPIRY * 3)
        self.assertEqual((a.addr, b.addr), addrs)
        self.assertEqual(len(bus.present_paths()), 2)

    def test_reboot_is_reassigned_by_uid(self):
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        bus = self.make_bus(FakeChain(a, b))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        port = bus.open_unit(B.uid_hex(UID_B))
        before = b.addr
        b.reboot()
        # The rescan finds it at address 0 and gives it its address back.
        # Usually before three keep-alives are missed, so the host port keeps
        # working; if it was reported lost first, reopening it works.
        self.assertTrue(self.wait_for(lambda: b.addr == before))
        try:
            reply = ask(port, 300, B.CMD_DISCOVER)
        except OSError:
            port = bus.open_unit(B.uid_hex(UID_B))
            reply = ask(port, 301, B.CMD_DISCOVER)
        self.assertEqual(B.uid_of(reply['payload']), UID_B)
        self.assertEqual(len(bus.present_paths()), 2)

    def test_unit_gone_raises_on_its_port_only(self):
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        bus = self.make_bus(FakeChain(a, b))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        pa = bus.open_unit(B.uid_hex(UID_A))
        pb = bus.open_unit(B.uid_hex(UID_B))
        b.powered = False
        self.assertTrue(self.wait_for(
            lambda: _raises_eio(lambda: pb.read(1))))
        self.assertIsNotNone(ask(pa, 310, B.CMD_GET_INFO))
        self.assertEqual(bus.present_paths(), [bus.virtual_path(
            bus.unit_for(B.uid_hex(UID_A)))])
        b.powered = True
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))

    def test_slow_unit_is_not_lost(self):
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        bus = self.make_bus(FakeChain(a, b))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        port = bus.open_unit(B.uid_hex(UID_A))
        a.mute_until = time.monotonic() + 0.25   # fewer than 3 missed windows
        time.sleep(0.6)
        self.assertIsNotNone(ask(port, 400, B.CMD_GET_INFO))
        self.assertEqual(len(bus.present_paths()), 2)

    def test_assign_and_flash_refused_on_shared_bus(self):
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        chain = FakeChain(a, b)
        bus = self.make_bus(chain)
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        port = bus.open_unit(B.uid_hex(UID_A))
        for seq, cmd in ((500, B.CMD_ASSIGN), (501, B.CMD_IAP_UPGRADE)):
            self.assertIsNone(ask(port, seq, cmd, wait=0.5))
            self.assertNotIn(seq, [f['seq'] for f in chain.frames])

    def test_second_unit_joins_later(self):
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        b.powered = False
        bus = self.make_bus(FakeChain(a, b))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 1))
        self.assertFalse(bus.addressed)
        b.powered = True
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        self.assertTrue(bus.addressed)
        self.assertEqual(sorted((a.addr, b.addr)), [1, 2])

    def test_cable_failure_reaches_every_port(self):
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        chain = FakeChain(a, b)
        bus = self.make_bus(chain)
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        pa = bus.open_unit(B.uid_hex(UID_A))
        pb = bus.open_unit(B.uid_hex(UID_B))
        chain.fail = True
        self.assertTrue(self.wait_for(lambda: not bus.alive))
        for port in (pa, pb):
            with self.assertRaises(OSError):
                port.read(1)

    def test_discover_never_blocks_the_caller(self):
        # decay71 #189 point 1: discovery runs on the Klipper reactor, so
        # it must return at once even while a new bus is still enumerating.
        cable = '/dev/serial/by-path/fake-cable-new'
        chain = FakeChain(FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y'))
        bus = B.bus_for(cable, serial_factory=lambda path: chain)
        self.buses.append(bus)
        self.addCleanup(B._buses.pop, cable, None)
        t0 = time.monotonic()
        B.discover_units([cable])
        self.assertLess(time.monotonic() - t0, 0.05)
        self.assertTrue(self.wait_for(lambda: not B.first_scan_pending([cable])))
        self.assertEqual(len(B.discover_units([cable])), 2)


def _raises_eio(fn):
    try:
        fn()
    except OSError as e:
        return e.errno == errno.EIO
    return False


class PathTest(unittest.TestCase):

    def test_virtual_path_round_trip(self):
        path = '/dev/serial/by-path/cable@%s' % B.uid_hex(UID_A)
        self.assertTrue(B.is_bus_path(path))
        self.assertEqual(B.split_path(path),
                         ('/dev/serial/by-path/cable', B.uid_hex(UID_A)))
        self.assertEqual(B.uid_key_for(path), 'uid:' + B.uid_hex(UID_A))

    def test_virtual_path_survives_gcode(self):
        # multiACE saves the active device path with SAVE_VARIABLE: the path
        # must not contain a G-code comment character.
        bus_path = '/dev/serial/by-path/platform-fed00000.usb-usb-0:1.3:1.0'
        path = '%s%s%s' % (bus_path, B.SEP, B.uid_hex(UID_A))
        self.assertNotIn('#', path)
        self.assertNotIn(';', path)


if __name__ == '__main__':
    unittest.main()
