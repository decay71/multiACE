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
UID_C = (777777777, 888888, 999999999)
UID_D = (121212121, 343434, 565656565)
UID_E = (787878787, 909090, 101010101)

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

    def test_dropped_address_is_restored_before_loss(self):
        # Seen on hardware: when one chained unit loses power, the other one
        # can drop back to address 0 on its own. A missed reply must trigger
        # a scan at address 0 straight away, so the unit gets its address
        # back before it is reported lost (mid-print that would pause).
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        bus = self.make_bus(FakeChain(a, b))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        lost = []
        real = bus._mark_lost
        bus._mark_lost = lambda unit: (lost.append(unit.uid), real(unit))
        before = a.addr
        with mock.patch.object(B, 'RESCAN_S', 30.0):   # no scheduled rescue
            a.reboot()
            self.assertTrue(self.wait_for(lambda: a.addr == before,
                                          timeout=2.0))
            time.sleep(0.5)
        self.assertEqual(lost, [])
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

    def test_presence_check_follows_the_bus(self):
        # multiACE's background reconnect waits for the device to be there
        # before reopening it. A virtual path is no file, so it must ask the
        # bus (found on hardware: a unit off for more than ~10 s never came
        # back without a Klipper restart).
        from extras.ace_protocol_v2 import AceProtocolV2
        cable = '/dev/serial/by-path/fake-cable-presence'
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        chain = FakeChain(a, b)
        bus = B.bus_for(cable, serial_factory=lambda path: chain)
        self.buses.append(bus)
        self.addCleanup(B._buses.pop, cable, None)
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 2))
        path_b = bus.virtual_path(bus.unit_for(B.uid_hex(UID_B)))
        with mock.patch.object(AceProtocolV2, 'CHAIN', True):
            self.assertTrue(AceProtocolV2.device_present(path_b))
            b.powered = False
            self.assertTrue(self.wait_for(
                lambda: not AceProtocolV2.device_present(path_b)))
            b.powered = True
            self.assertTrue(self.wait_for(
                lambda: AceProtocolV2.device_present(path_b)))
            self.assertFalse(AceProtocolV2.device_present(
                '/dev/serial/by-path/no-such-cable@00000000-00000000-00000000'))

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



class ManyUnitsAndCablesTest(unittest.TestCase):
    """More than two units on one cable, and more than one cable."""

    def setUp(self):
        patcher = mock.patch.multiple(B, **FAST)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.buses = []

    def tearDown(self):
        for bus in self.buses:
            bus.stop()

    def make_bus(self, chain, cable='/dev/serial/by-path/fake-cable'):
        bus = B.V2Bus(cable, serial_factory=lambda path: chain)
        self.buses.append(bus)
        return bus

    def register_bus(self, chain, cable):
        """A bus in the module registry, as discover_units() finds it."""
        bus = B.bus_for(cable, serial_factory=lambda path: chain)
        self.buses.append(bus)
        self.addCleanup(B._buses.pop, cable, None)
        return bus

    def wait_for(self, pred, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def assert_routes(self, bus, uids, seq0):
        """Each unit's port reaches that unit and no other."""
        ports = {uid: bus.open_unit(B.uid_hex(uid)) for uid in uids}
        for round_ in range(3):
            for i, uid in enumerate(uids):
                seq = seq0 + round_ * 10 + i
                reply = ask(ports[uid], seq, B.CMD_DISCOVER)
                self.assertIsNotNone(reply, 'no reply for %r' % (uid,))
                self.assertEqual(B.uid_of(reply['payload']), uid)
                self.assertEqual(reply['addr'], 0x80)
        return ports

    # ---- one cable, three and four units --------------------------------
    def test_three_units_get_distinct_addresses_and_route(self):
        units = [FakeUnit(u, 'x') for u in (UID_A, UID_B, UID_C)]
        bus = self.make_bus(FakeChain(*units))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 3))
        self.assertEqual(sorted(u.addr for u in units), [1, 2, 3])
        self.assert_routes(bus, [UID_A, UID_B, UID_C], 600)

    def test_four_units_fill_the_bus_and_a_fifth_is_ignored(self):
        units = [FakeUnit(u, 'x')
                 for u in (UID_A, UID_B, UID_C, UID_D, UID_E)]
        bus = self.make_bus(FakeChain(*units))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 4))
        time.sleep(B.RESCAN_S * 2)           # later scans change nothing
        self.assertEqual(len(bus.present_paths()), 4)
        addressed = sorted(u.addr for u in units if u.addr)
        self.assertEqual(addressed, [1, 2, 3, 4])
        ignored = [u for u in units if not u.addr]
        self.assertEqual(len(ignored), 1)
        known = [u.uid for u in units if u.addr]
        self.assert_routes(bus, known, 700)

    def test_four_units_keep_their_addresses_while_idle(self):
        units = [FakeUnit(u, 'x') for u in (UID_A, UID_B, UID_C, UID_D)]
        bus = self.make_bus(FakeChain(*units))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 4))
        addrs = [u.addr for u in units]
        time.sleep(EXPIRY * 3)
        self.assertEqual([u.addr for u in units], addrs)
        self.assertEqual(len(bus.present_paths()), 4)

    def test_middle_unit_off_leaves_the_others_working(self):
        a, b, c = (FakeUnit(u, 'x') for u in (UID_A, UID_B, UID_C))
        bus = self.make_bus(FakeChain(a, b, c))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 3))
        ports = self.assert_routes(bus, [UID_A, UID_B, UID_C], 800)
        b.powered = False
        self.assertTrue(self.wait_for(
            lambda: _raises_eio(lambda: ports[UID_B].read(1))))
        for i, uid in enumerate((UID_A, UID_C)):
            reply = ask(ports[uid], 850 + i, B.CMD_DISCOVER)
            self.assertEqual(B.uid_of(reply['payload']), uid)
        self.assertEqual(len(bus.present_paths()), 2)
        b.powered = True
        b.reboot()                           # a power cycle loses the address
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 3))
        self.assertEqual(sorted(u.addr for u in (a, b, c)), [1, 2, 3])
        self.assert_routes(bus, [UID_A, UID_B, UID_C], 870)

    def test_two_units_drop_to_address_0_together(self):
        # The hardware case with more units: one unit unpowered, the others
        # fall back to address 0. Both get their address back by uid.
        units = [FakeUnit(u, 'x') for u in (UID_A, UID_B, UID_C)]
        bus = self.make_bus(FakeChain(*units))
        self.assertTrue(self.wait_for(lambda: len(bus.present_paths()) == 3))
        before = {u.uid: u.addr for u in units}
        units[0].reboot()
        units[2].reboot()
        self.assertTrue(self.wait_for(
            lambda: {u.uid: u.addr for u in units} == before))
        self.assertEqual(len(bus.present_paths()), 3)
        self.assert_routes(bus, [UID_A, UID_B, UID_C], 900)

    # ---- two cables -----------------------------------------------------
    def test_two_cables_are_separate_buses(self):
        cable1 = '/dev/serial/by-path/fake-usb-1'
        cable2 = '/dev/serial/by-path/fake-usb-2'
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        c, d = FakeUnit(UID_C, 'x'), FakeUnit(UID_D, 'y')
        bus1 = self.register_bus(FakeChain(a, b), cable1)
        bus2 = self.register_bus(FakeChain(c, d), cable2)
        self.assertTrue(self.wait_for(
            lambda: len(B.discover_units([cable1, cable2])) == 4))
        paths = B.discover_units([cable1, cable2])
        self.assertEqual(sorted(p.rpartition(B.SEP)[0] for p in paths),
                         [cable1, cable1, cable2, cable2])
        # addresses are per cable: each bus numbers its own units from 1
        self.assertEqual(sorted((a.addr, b.addr)), [1, 2])
        self.assertEqual(sorted((c.addr, d.addr)), [1, 2])
        for path in paths:
            uid_hex = B.split_path(path)[1]
            port = B.open_unit(path)
            reply = ask(port, 1000 + paths.index(path), B.CMD_DISCOVER)
            self.assertEqual(B.uid_hex(B.uid_of(reply['payload'])), uid_hex)
        self.assertIsNot(bus1, bus2)

    def test_cable_failure_stays_on_its_cable(self):
        cable1 = '/dev/serial/by-path/fake-usb-1'
        cable2 = '/dev/serial/by-path/fake-usb-2'
        chain1 = FakeChain(FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y'))
        chain2 = FakeChain(FakeUnit(UID_C, 'x'), FakeUnit(UID_D, 'y'))
        bus1 = self.register_bus(chain1, cable1)
        bus2 = self.register_bus(chain2, cable2)
        self.assertTrue(self.wait_for(
            lambda: len(B.discover_units([cable1, cable2])) == 4))
        p1 = [bus1.open_unit(B.uid_hex(u)) for u in (UID_A, UID_B)]
        p2 = self.assert_routes(bus2, [UID_C, UID_D], 1100)
        chain1.fail = True
        self.assertTrue(self.wait_for(lambda: not bus1.alive))
        for port in p1:
            self.assertTrue(_raises_eio(lambda: port.read(1)))
        # the other cable does not notice
        for i, uid in enumerate((UID_C, UID_D)):
            reply = ask(p2[uid], 1150 + i, B.CMD_DISCOVER)
            self.assertEqual(B.uid_of(reply['payload']), uid)
        self.assertEqual(len(B.discover_units([cable2])), 2)
        # and the failed cable comes back on its own
        chain1.fail = False
        self.assertTrue(self.wait_for(
            lambda: len(B.discover_units([cable1])) == 2))

    def test_unit_lost_on_one_cable_only(self):
        cable1 = '/dev/serial/by-path/fake-usb-1'
        cable2 = '/dev/serial/by-path/fake-usb-2'
        a, b = FakeUnit(UID_A, 'x'), FakeUnit(UID_B, 'y')
        c, d = FakeUnit(UID_C, 'x'), FakeUnit(UID_D, 'y')
        bus1 = self.register_bus(FakeChain(a, b), cable1)
        self.register_bus(FakeChain(c, d), cable2)
        self.assertTrue(self.wait_for(
            lambda: len(B.discover_units([cable1, cable2])) == 4))
        b.powered = False
        self.assertTrue(self.wait_for(
            lambda: len(B.discover_units([cable1])) == 1))
        self.assertEqual(len(B.discover_units([cable2])), 2)
        self.assertFalse(B.unit_present(
            bus1.virtual_path(bus1.unit_for(B.uid_hex(UID_B)))))
        b.powered = True
        b.reboot()
        self.assertTrue(self.wait_for(
            lambda: len(B.discover_units([cable1, cable2])) == 4))


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
