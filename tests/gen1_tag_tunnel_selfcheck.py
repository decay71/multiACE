#!/usr/bin/env python3
"""Self-check for the Gen-1 (ACE Pro) tag tunnel.

The repository has no CI beyond the release tarball, so this is the
test-in-a-script - like tools/gen1_flasher_selfcheck.py and
tools/ace_set_humidity_selfcheck.py - and lives under tests/ now. It
imports the REAL modules (klipper/extras/ace.py,
klipper/extras/ace_gen1_tunnel.py and klipper/extras/ace_rc522.py, from
the repo checkout or a printer install) and drives the real code on
hand-built fakes: no Klipper, no hardware, no serial ports.

Covered, as required for the PR:
  (a) the packed index, the SIGNED 32-bit wire form (pinned against the
      tunnel notes' own vectors), the slot -> reader-CHANNEL bit-swap map
      (0,1,2,3 -> 0,2,1,3) and the exact op sequence of a read -
      acquire -> SELECT -> TXMODE/RXMODE/BitFraming -> FIFO writes ->
      TRANSCEIVE -> RX bits -> FIFO reads -> release;
  (b) reply parsing: only `result.code` counts (a stock top-level `code` is
      NOT a tunnel answer), masked to 8 bits;
  (c) graceful degradation: stock/older firmware sends NO tunnel op at all,
      a firmware that matches but does not answer the probe is dropped
      after one command, and a dead link times out bounded - all as "no
      tunnel", never an exception;
  (d) a successful read yields the expected bytes/UID for BOTH genuine live
      captures, and an OpenSpool NTAG decode produces the identity (the
      reused ace_rc522 decoder);
  (e) the ace.py wiring: the internal enable flag gates only the automatic
      fallback (always on in production), ACE_TAG_READ works with it off, one attempt per occupancy, the
      shared-antenna bind gate (bind only when the partner slot reads
      empty), the own store never writes _info_per_ace, get_status surfaces
      uid/tag_format/tag_tunnel, and the V2 path is unchanged.

Run it after touching any of the modules:

    python3 tests/gen1_tag_tunnel_selfcheck.py

Exit codes:
    0  every check passed
    1  a check FAILED
    2  usage/setup error (modules not found or not importable)
"""

from __future__ import annotations

import copy
import importlib
import json
import logging
import os
import sys
import types

# The INFO trails of ace.py are not the report - the checks are.
logging.disable(logging.INFO)

# --- locate the real modules (repo checkout OR printer install) ------------


def find_extras():
    here = os.path.dirname(os.path.abspath(__file__))          # <repo>/tests
    candidates = [
        os.environ.get("MULTIACE_KLIPPY_EXTRAS"),
        os.path.join(os.path.dirname(here),                       # repo
                     "multiace", "klipper", "extras"),
        "/home/lava/klipper/klippy/extras",                       # printer
        os.path.join(os.path.expanduser("~"), "klipper", "klippy", "extras"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "ace.py")) \
                and os.path.isfile(os.path.join(c, "ace_gen1_tunnel.py")):
            return c
    return None


EXTRAS = find_extras()
if EXTRAS is None:
    print("[FAIL] ace.py / ace_gen1_tunnel.py / ace_rc522.py not found - set "
          "MULTIACE_KLIPPY_EXTRAS to the klippy/extras directory")
    sys.exit(2)

# Import through a synthetic package whose __path__ is the extras directory:
# ace.py's relative imports resolve, and no real 'extras' package can shadow
# it (an installed Klipper keeps its own; a dev box may have anything).
_pkg = types.ModuleType("multiace_tagtunnel_selfcheck_extras")
_pkg.__path__ = [EXTRAS]
sys.modules[_pkg.__name__] = _pkg
try:
    A = importlib.import_module(_pkg.__name__ + ".ace")
    T = importlib.import_module(_pkg.__name__ + ".ace_gen1_tunnel")
except Exception as e:                                          # pragma: no cover
    print("[FAIL] cannot import from %s: %s" % (EXTRAS, e))
    sys.exit(2)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("[ok]   %s" % name)
    else:
        print("[FAIL] %s %s" % (name, detail))
        FAILED.append(name)


# --- the two genuine live captures (REPORT-RC522-TUNNEL-EN.md) -------------

CAP_A = bytes.fromhex('04 22 52 FC 51 C8 2A 81 32 48 00 00 E1 10 6D 00')
CAP_B = bytes.fromhex('53 42 70 E9 D1 B5 00 01 65 48 00 00 E1 10 12 00')
# ISO14443-3 UID = page0 bytes 0..2 + page1 bytes 0..3 (BCC0/BCC1 verified,
# the layout ace_rc522 and the phone-confirmed card_uids use). NOTE: the
# tunnel report's "UID" column prints raw page bytes 0..6 (BCC0 instead of
# UID6) - the validated ISO UID is what binds spools.
UID_A = '04225251C82A81'
UID_B = '534270D1B50001'


# --- fakes ----------------------------------------------------------------

class FakeReactor:
    NEVER = 1e18

    def __init__(self):
        self.now = 1000.0
        self.async_cbs = []
        self.timers = []

    def monotonic(self):
        return self.now

    def pause(self, seconds):
        # Advance time like the real reactor does while the greenlet yields.
        self.now += float(seconds)

    def register_async_callback(self, cb):
        self.async_cbs.append(cb)

    def register_timer(self, cb, when):
        self.timers.append((cb, when))
        return len(self.timers)


def r(code):
    """A tunnel stub reply: the byte lands in result.code."""
    return {'id': 1, 'result': {'code': code}, 'msg': 'ok'}


NO_TUNNEL = {'id': 1, 'result': {}, 'code': 0, 'msg': 'success'}
INVALID = {'id': 1, 'code': 400, 'result': {}, 'msg': 'InvalidCommand'}


class FakeAce:
    """Just enough of a MultiAce for the tunnel client: records every
    request and answers from a canned reply script (None = no reply)."""

    def __init__(self, replies=None, fw='CV1.3.871', deliver=True):
        self.reactor = FakeReactor()
        self._ace_models = {0: ('Anycubic Color Engine Pro', fw)}
        self.sent = []              # (idx, method, signed index)
        self.replies = list(replies or [])
        self.deliver = deliver
        self.default = None

    def send_request_to(self, idx, request, callback):
        self.sent.append((idx, request.get('method'),
                          request.get('params', {}).get('index')))
        if not self.deliver:
            return
        reply = self.replies.pop(0) if self.replies else self.default
        if callback is not None:
            callback(self=self, response=reply)


def unpack(signed):
    """Independent decoder (never uses the module under test)."""
    v = signed & 0xFFFFFFFF
    return ((v >> 24) & 0x3, (v >> 16) & 0xFF, (v >> 8) & 0x3F, v & 0xFF)


def read_script(pages, saved=7, release_code=0):
    """Replies for acquire + select + one read_page per (page, data) in
    order + release. 25 replies per page read (9 setup/transceive + 16
    FIFO bytes)."""
    reps = [r(saved), r(0)]
    for _page, data in pages:
        reps += [r(0), r(0), r(0), r(0), r(0), r(0), r(0), r(0), r(0x80)]
        reps += [r(b) for b in data]
    reps.append(r(release_code))
    return reps


def openspool_ndef(material='PETG', color='DE3530', brand='Creality'):
    """The NDEF-message TLV an OpenSpool tag carries (same layout the V2
    decoder/encoder use)."""
    payload = json.dumps({'protocol': 'openspool', 'version': '1.0',
                          'type': material, 'color_hex': color,
                          'brand': brand},
                         separators=(',', ':')).encode('utf-8')
    typ = b'application/json'
    rec = bytes([0xD2, len(typ), len(payload)]) + typ + payload
    tlv = bytes([0x03, len(rec)]) + rec + bytes([0xFE])
    while len(tlv) % 4:
        tlv += b'\x00'
    return tlv


# ==========================================================================
# 1. (a) packing, signed form, op constants
# ==========================================================================

print("--- 1. pack_index / as_signed32 / slot_channel / op constants ---")

check("magic is 0x80000000", T.TUNNEL_MAGIC == 0x80000000)
check("op 0 reg 0x37 packs to 0x80003700",
      T.pack_index(0, 0x37) == 0x80003700)
check("... and goes out SIGNED (R10) -2147469568",
      T.as_signed32(T.pack_index(0, 0x37)) == -2147469568)
check("op 7 acquire packs to 0x80070000 / -2147024896",
      T.pack_index(7) == 0x80070000
      and T.as_signed32(T.pack_index(7)) == -2147024896)
check("op 1 write 0x0D = 0x00 packs to 0x80010D00 / -2147414784",
      T.pack_index(1, 0x0D, 0x00) == 0x80010D00
      and T.as_signed32(T.pack_index(1, 0x0D, 0x00)) == -2147414784)
check("op 0 read 0x0D packs to 0x80000D00 / -2147480320",
      T.pack_index(0, 0x0D) == 0x80000D00
      and T.as_signed32(T.pack_index(0, 0x0D)) == -2147480320)
check("op 2 FIFO byte 0x5A packs to 0x8002005A / -2147352486",
      T.pack_index(2, 0, 0x5A) == 0x8002005A
      and T.as_signed32(T.pack_index(2, 0, 0x5A)) == -2147352486)
check("op 4 FIFO read packs to 0x80040000 / -2147221504",
      T.pack_index(4) == 0x80040000
      and T.as_signed32(T.pack_index(4)) == -2147221504)
check("op 6 SELECT reader 2 packs to 0x82060000 / -2113536000",
      T.pack_index(6, reader=2) == 0x82060000
      and T.as_signed32(T.pack_index(6, reader=2)) == -2113536000)
check("reader occupies bits 24..25 (0..3)",
      all(unpack(T.as_signed32(T.pack_index(6, reader=k)))[0] == k
          for k in range(4)))
check("a1 is masked to 6 bits, a2 to 8",
      unpack(T.pack_index(0, 0x7F, 0xFF)) == (0, 0, 0x3F, 0xFF))
check("values without bit 31 are not changed by as_signed32",
      T.as_signed32(0x00001234) == 0x1234
      and T.as_signed32(0) == 0)
check("firmware gate: CV1.3.871 is a tunnel build",
      T.firmware_supports_tunnel('CV1.3.871') is True)
check("firmware gate: CV1.3.870 (same family) accepted",
      T.firmware_supports_tunnel('CV1.3.870') is True)
check("firmware gate: stock V1.3.863 rejected",
      T.firmware_supports_tunnel('V1.3.863') is False)
check("firmware gate: UID-only community CV1.3.863 rejected",
      T.firmware_supports_tunnel('CV1.3.863') is False)
check("firmware gate: empty string rejected",
      T.firmware_supports_tunnel('') is False)
check("slot_channel maps bays to reader channels 0,2,1,3 (bit-swap)",
      [T.slot_channel(s) for s in range(4)] == [0, 2, 1, 3])
check("... the same-antenna partner slot ^ 1 sits on channel ^ 2",
      all(T.slot_channel(s ^ 1) == (T.slot_channel(s) ^ 2)
          for s in range(4)))
check("... out-of-range slots wrap into bay order",
      T.slot_channel(4) == T.slot_channel(0)
      and T.slot_channel(-1) == T.slot_channel(3))

# ==========================================================================
# 2. (b) reply parsing
# ==========================================================================

print("--- 2. parse_code ---")

check("result.code 161 -> 161", T.parse_code(r(161)) == 161)
check("result.code 0 -> 0 (not falsy-dropped)", T.parse_code(r(0)) == 0)
check("result.code is masked to 8 bits",
      T.parse_code({'result': {'code': 0x19A}}) == 0x9A)
check("a NEGATIVE result.code masks like a byte",
      T.parse_code({'result': {'code': -1}}) == 0xFF)
check("top-level stock code is NOT a tunnel answer",
      T.parse_code(NO_TUNNEL) is None)
check("InvalidCommand reply -> None", T.parse_code(INVALID) is None)
check("missing/None reply -> None",
      T.parse_code(None) is None and T.parse_code({}) is None)
check("non-numeric result.code -> None",
      T.parse_code({'result': {'code': 'abc'}}) is None)
check("numeric string result.code still parses",
      T.parse_code({'result': {'code': '161'}}) == 161)

# ==========================================================================
# 3. (a) the read op sequence + (d) both live captures
# ==========================================================================

print("--- 3. read sequence and live captures ---")

client_ace = FakeAce(read_script([(0, CAP_A)]))
client = T.Gen1TagTunnel(client_ace, 0)
res = client.read_slot(0, userdata=False)
seq = [unpack(s[2]) for s in client_ace.sent]

EXPECTED = [(0, 7, 0, 0),           # acquire
            (0, 6, 0, 0),           # SELECT on slot 0's antenna
            (0, 0, 0x12, 0),        # read TXMODE
            (0, 1, 0x12, 0x80),     # write TXMODE |= 0x80
            (0, 0, 0x13, 0),        # read RXMODE
            (0, 1, 0x13, 0x80),     # write RXMODE |= 0x80
            (0, 1, 0x0D, 0x00),     # write BitFraming = 0
            (0, 2, 0, 0x30),        # FIFO: 0x30
            (0, 2, 1, 0x00),        # FIFO: page 0
            (0, 3, 2, 0x0C),        # TRANSCEIVE, 2 TX bytes
            (0, 5, 0, 0)]           # RX bits
EXPECTED += [(0, 4, i, 0) for i in range(16)]       # FIFO reads
EXPECTED += [(0, 8, 0, 7)]          # release, saved state 7

check("the exact op sequence (acquire -> SELECT -> host order -> release)",
      seq == EXPECTED, "got %s" % (seq,))
check("all requests go to filament_recognition on the unit",
      all(s[0] == 0 and s[1] == 'filament_recognition'
          for s in client_ace.sent))
check("read returns the captured page-0 bytes",
      res is not None and res['data'] == CAP_A)
check("read returns UID 042252FC51C82A (BCCs verified)",
      res is not None and res['uid'] == UID_A)
check("read classifies the capture as an NTAG (CC e1 10 6d 00)",
      res is not None and res['format'] == 'ntag')

client_ace = FakeAce(read_script([(0, CAP_B)]))
client = T.Gen1TagTunnel(client_ace, 0)
res = client.read_slot(2, userdata=False)
seq = [unpack(s[2]) for s in client_ace.sent]
check("slot 2 selects reader channel 1 (bit-swap, NOT reader = slot)",
      seq[1] == (1, 6, 0, 0) and seq[0] == (0, 7, 0, 0)
      and seq[-1][1] == 8)
check("... and reads all 16 bytes on reader channel 1",
      all(op[0] == 1 for op in seq[2:-1] if op[1] in (2, 3, 4, 5)))
check("... the returned reader field is the channel (1), not the slot (2)",
      res is not None and res['reader'] == 1)
check("second capture yields UID 534270E9D1B500",
      res is not None and res['uid'] == UID_B
      and res['data'] == CAP_B)

# Slot 1 is the other half of the swap: it must address channel 2.
client_ace = FakeAce(read_script([(0, CAP_B)]))
client = T.Gen1TagTunnel(client_ace, 0)
res = client.read_slot(1, userdata=False)
seq = [unpack(s[2]) for s in client_ace.sent]
check("slot 1 selects reader channel 2 (the swapped partner of slot 2)",
      seq[1] == (2, 6, 0, 0) and res['reader'] == 2
      and all(op[0] == 2 for op in seq[2:-1] if op[1] in (2, 3, 4, 5)))

# Bad BCC must refuse the UID (field-edge corruption is not an identity).
bad = bytearray(CAP_A)
bad[6] ^= 0x01
check("a flipped UID byte fails the BCC check -> no UID",
      T.uid_from_page0(bytes(bad)) == '')
check("uid_from_page0 on a short read -> ''",
      T.uid_from_page0(b'\x04\x22') == '')
check("uid_from_page0 is the shared ace_rc522 BCC check (same result)",
      T.uid_from_page0(CAP_A) == UID_A
      and T.uid_from_page0(CAP_A) == T.AceTagReader.uid_from_page0(CAP_A))
check("... and the shared check rejects empty / all-zero data",
      T.AceTagReader.uid_from_page0(b'') == ''
      and T.AceTagReader.uid_from_page0(bytes(16)) == '')

# ==========================================================================
# 4. OpenSpool decode
# ==========================================================================

print("--- 4. OpenSpool decode ---")

tlv = openspool_ndef()
tlv += bytes((-len(tlv)) % 16)          # page-align the NDEF record
user_chunks = []
for _i in range((T.OPENSPOOL_LAST_PAGE - T.OPENSPOOL_FIRST_PAGE) // 4 + 1):
    _page = T.OPENSPOOL_FIRST_PAGE + 4 * _i
    user_chunks.append((_page,
                        tlv[_i * 16:(_i + 1) * 16].ljust(16, b'\x00')))
client_ace = FakeAce(read_script([(0, CAP_A)] + user_chunks))
client = T.Gen1TagTunnel(client_ace, 0)
res = client.read_slot(0)                     # userdata defaults on
check("OpenSpool tag: format upgraded to openspool",
      res is not None and res['format'] == 'openspool')
check("OpenSpool identity decoded by the REUSED ace_rc522 decoder "
      "(material/colour/vendor + temps)",
      res is not None and res['openspool'] == {
          'material': 'PETG', 'color': 'DE3530', 'vendor': 'Creality',
          'min_temp': None, 'max_temp': None},
      "%s" % (res and res.get('openspool'),))
check("the user-page reads stay inside the same acquire/release hold",
      unpack(client_ace.sent[0][2])[1] == 7
      and unpack(client_ace.sent[-1][2])[1] == 8)
check("a plain capture without NDEF decodes to None",
      T.AceTagReader._openspool_decode(CAP_A) is None)
check("garbage NDEF does not raise",
      T.AceTagReader._openspool_decode(b'\x03\xff\xff\xffzz') is None)
check("the tunnel module no longer carries its own decoder copy",
      not hasattr(T, 'decode_openspool'))

# ==========================================================================
# 5. (c) graceful degradation
# ==========================================================================

print("--- 5. graceful degradation ---")

# Stock firmware: the version gate stops every tunnel op BEFORE the wire.
stock = FakeAce([], fw='V1.3.863')
c = T.Gen1TagTunnel(stock, 0)
check("stock V1.3.863: tunnel_available() is False",
      c.tunnel_available() is False)
check("... and NOT ONE byte of tunnel traffic was sent",
      stock.sent == [], "sent=%s" % (stock.sent,))
check("... support_state is (fw, False)",
      c.support_state() == ('V1.3.863', False))
check("... uids/reads never raised", c.read_slot(0) is None)

# UID-only community image: same (its string has no tunnel prefix).
cfw_uid = FakeAce([], fw='CV1.3.863')
c = T.Gen1TagTunnel(cfw_uid, 0)
check("UID-only community CV1.3.863: no tunnel traffic either",
      c.tunnel_available() is False and cfw_uid.sent == [])

# Firmware matches but the stub does not answer: ONE probe, then dropped.
fake_fw = FakeAce([NO_TUNNEL], fw='CV1.3.871')
c = T.Gen1TagTunnel(fake_fw, 0)
check("matching fw + stock-style reply: available=False",
      c.tunnel_available() is False)
check("... exactly one probe command was sent",
      len(fake_fw.sent) == 1
      and unpack(fake_fw.sent[0][2]) == (0, 0, 0x37, 0))
check("... InvalidCommand degrades the same way (cached, no repeat)",
      c.tunnel_available() is False and len(fake_fw.sent) == 1)
check("... read_slot after a failed probe returns None, no crash",
      c.read_slot(0) is None)

invalid = FakeAce([INVALID], fw='CV1.3.871')
c = T.Gen1TagTunnel(invalid, 0)
check("InvalidCommand on the probe -> available=False, one command",
      c.tunnel_available() is False and len(invalid.sent) == 1)

# A dead link: bounded timeout, None result, no exception.
dead = FakeAce(deliver=False, fw='CV1.3.871')
dead.reactor = FakeReactor()
c = T.Gen1TagTunnel(dead, 0, timeout=0.05)
t0 = dead.reactor.now
def _raises_noreply(fn):
    try:
        fn()
    except T.NoReply:
        return True
    return False


check("a lost reply times out bounded -> NoReply ends the session",
      _raises_noreply(lambda: c.select(0))
      and _raises_noreply(lambda: c._op(T.OP_ACQUIRE))
      and c.no_reply is True)
check("... the timeout actually elapsed (bounded poll, no spin)",
      dead.reactor.now - t0 >= 0.099
      and dead.reactor.now - t0 <= 0.25, dead.reactor.now - t0)
check("... exactly the two timed-out commands were sent",
      len(dead.sent) == 2, dead.sent)
check("... and read_slot returns None without a release (no hold taken)",
      c.read_slot(0) is None)

# Successful probe is cached; a firmware change re-probes.
ok_ace = FakeAce([r(0xA1)], fw='CV1.3.871')
c = T.Gen1TagTunnel(ok_ace, 0)
check("probe answers -> available=True", c.tunnel_available() is True)
check("... VersionReg read comes from reader 0",
      unpack(ok_ace.sent[0][2]) == (0, 0, 0x37, 0))
check("... the answer is cached (no second probe)",
      c.tunnel_available() is True and len(ok_ace.sent) == 1)
ok_ace._ace_models[0] = ('Anycubic Color Engine Pro', 'CV1.3.872')
c._support = ('CV1.3.871', True)   # simulate the previous session value
ok_ace.replies.append(r(0xA1))
c.tunnel_available()
check("... a firmware change re-probes", len(ok_ace.sent) == 2)

# Release failure: logged once, read result kept.
fail_release = FakeAce([r(7), r(0)] + read_script([(0, CAP_A)])[2:-1]
                       + [NO_TUNNEL])
c = T.Gen1TagTunnel(fail_release, 0)
res = c.read_slot(0, userdata=False)
check("a failed release still returns the read data",
      res is not None and res['uid'] == UID_A)
check("... release failure is flagged once",
      c._release_fail_said is True)
c.release(7)
check("... and not flagged/said again per call",
      c._release_fail_said is True)

# ==========================================================================
# 6. (e) ace.py wiring
# ==========================================================================

print("--- 6. ace.py wiring ---")


class FakeProto:
    def __init__(self, name):
        self.NAME = name


class FakeError(Exception):
    pass


class FakeGcmd:
    def __init__(self, **params):
        self.params = {k: str(v) for k, v in params.items()}
        self.info = []
        self.errors = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        v = self.params.get(name, default)
        if v is None:
            return None
        v = int(v)
        if minval is not None and v < minval:
            raise FakeError('%s below minval' % name)
        if maxval is not None and v > maxval:
            raise FakeError('%s above maxval' % name)
        return v

    def error(self, message=None, **kw):
        return FakeError(message if message is not None else str(kw))

    def respond_info(self, msg):
        self.info.append(msg)


class FakePrintStats:
    state = 'idle'


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == 'print_stats':
            return FakePrintStats()
        return default


def make_ace(protocols=('v1',), fw='CV1.3.871', flag=False):
    inst = A.MultiAce.__new__(A.MultiAce)
    inst.printer = FakePrinter()
    inst.reactor = inst.printer.reactor
    inst.gcode = types.SimpleNamespace(respond_raw=lambda m: None)
    inst.responses = []
    inst.errors = []
    inst.log_always = lambda msg, color=False: inst.responses.append(msg)
    inst.log_error = lambda msg: inst.errors.append(msg)
    inst._ace_devices = ['/dev/fake-ace%d' % i for i in range(len(protocols))]
    inst._protocols = {i: FakeProto(n) for i, n in enumerate(protocols)}
    inst._ace_models = {i: ('Anycubic Color Engine Pro', fw)
                        for i in range(len(protocols))}
    inst._info_per_ace = {}
    for i in range(len(protocols)):
        inst._info_per_ace[i] = {
            'status': 'ready', 'temp': 25,
            'dryer_status': {'status': 'stop'},
            'slots': [{'index': s,
                       'status': 'ready' if s == 0 else 'empty1',
                       'sku': '', 'rfid': 0,
                       'type': '', 'subtype': '', 'brand': '',
                       'color': [0, 0, 0]} for s in range(4)],
        }
    inst._connected_per_ace = {i: True for i in range(len(protocols))}
    inst._gate_status_per_ace = {i: [1, 1, 1, 1]
                                 for i in range(len(protocols))}
    inst._v1_tag_seen = {}
    inst._v2_filament_info_per_ace = {}
    inst._gen1_tunnel_clients = {}
    inst._gen1_tunnel_reads = {}
    inst._gen1_tunnel_tried = {}
    inst._gen1_tunnel_busy = set()
    inst._gen1_seen_empty = {}
    inst._gen1_vet_said = {}
    inst._gen1_tunnel_busy_slot = {}
    inst._gen1_gate_gen = {}
    inst._gen1_own_lanes = {}
    inst._gen1_tunnel_aborted = {}
    inst.gen1_tag_tunnel = flag
    inst._gen1_tag_tunnel_cfg = flag
    inst._display_index_base = 0
    inst._spools = {}
    inst._spool_binding = {}
    inst.spool_mode = 'local'
    inst.spoolman_url = ''
    # get_status companions
    inst._info = {'status': 'ready', 'temp': 25, 'dryer_status': {}}
    inst.gate_status = [1, 1, 1, 1]
    inst._active_device_index = 0
    inst._ace_mode = 'multi'
    inst._ace_head = 3
    inst._pickup_cleaning = False
    inst.preflight_max_copies = 1
    inst.preflight_copies_strict = False
    inst._confirm_commands = False
    inst.spoolman_auto = False
    inst.resistance_pause = False
    inst.quad_replenish = False
    inst.purge_matrix = True
    inst.pa_sync = True
    inst.tag_write_format = 'openspool'
    inst.tag_write_uid_sku = True
    inst._feed_assist_per_ace = {i: -1 for i in range(len(protocols))}
    inst._auto_dry_started = set()
    inst._fw_update_hold = set()
    inst._auto_dry_cfg = {}
    inst.auto_dry_default = {'enabled': False, 'rh_start': 45., 'rh_end': 35.,
                             'temp': 50, 'master': -1, 'add_time': 60}
    inst.head_uses_ace = lambda h: True
    inst._is_v2 = lambda i: (inst._protocols.get(i) is not None
                             and inst._protocols[i].NAME == 'v2')
    inst._any_open_fw = lambda: False
    inst._nozzle_keys_status = lambda: []
    inst._spoollink_active = lambda: False
    inst._spoollink_agent_present = lambda: False
    inst._swap_phase = 'idle'
    inst._last_swap_result = None
    inst._event_seq = 0
    inst._head_source = {}
    inst.head_manual = {}
    inst.head_feeder = {}
    inst.head_ace = {h: h for h in range(4)}
    inst._ptc_spool_id_for = lambda h: 0
    inst._head_tag_seen = {}
    inst._swap_in_progress = False
    inst._unload_all_active = False
    inst._calibration = None
    inst._calibration_unload = None
    inst.bind_calls = []
    inst._spool_bind_by_tag = (lambda ace_idx, slot, sku, unbind=True:
                               inst.bind_calls.append(
                                   (ace_idx, slot, sku, unbind)))
    inst.cfg_writes = []
    inst._cfg_write_ace_option = (lambda option, value, section='ace':
                                  inst.cfg_writes.append(
                                      (option, value, section)))
    inst.save_variables = None
    return inst


def run_async(inst, n=1):
    for _ in range(n):
        if not inst.reactor.async_cbs:
            return 0
        cb = inst.reactor.async_cbs.pop(0)
        cb(inst.reactor.now)
    return 1


def status_slot(inst, slot):
    st = inst.get_status()
    return st['aces'][0]['slots'][slot]


# 6a. the flag gates the automatic fallback only.
inst = make_ace(flag=False)
before = copy.deepcopy(inst._info_per_ace)
inst._gen1_tunnel_status_tick(0, inst._info_per_ace[0])
check("flag OFF: no automatic read scheduled",
      inst.reactor.async_cbs == [] and inst._gen1_tunnel_reads == {})
check("flag OFF: _info_per_ace untouched", inst._info_per_ace == before)


# A fake client for the scheduled greenlet (injected before the tick so the
# scheduled closure captures it instead of building a real one).
class FakeClient:
    def __init__(self, result=None, available=True):
        self.result = result
        self._available = available
        self.calls = []

    def tunnel_available(self):
        return self._available

    def support_state(self):
        return ('CV1.3.871', self._available)

    def read_slot(self, slot, page=0, userdata=True, skip_uids=()):
        self.calls.append((slot, page))
        return self.result


fake_cli = FakeClient(result={'slot': 0, 'reader': 0, 'page': 0,
                              'data': CAP_A, 'uid': UID_A, 'format': 'ntag',
                              'openspool': None, 'saved': 7})
# No automatic read at all: neither at connect nor after an insert.
inst = make_ace(flag=True)
inst._gen1_tunnel_clients[0] = fake_cli
inst._gen1_tunnel_client = lambda idx: fake_cli
before = copy.deepcopy(inst._info_per_ace)
inst._gen1_tunnel_status_tick(0, inst._info_per_ace[0])
check("occupied no-tag slot at connect: NO automatic read",
      inst.reactor.async_cbs == [] and fake_cli.calls == [])
inst._info_per_ace[0]['slots'][0]['status'] = 'empty1'
inst._gen1_tunnel_status_tick(0, inst._info_per_ace[0])
inst._info_per_ace[0]['slots'][0]['status'] = 'ready'
inst._gen1_tunnel_status_tick(0, inst._info_per_ace[0])
check("... and none after an insert either",
      inst.reactor.async_cbs == [] and fake_cli.calls == [])
check("... the tick never touches _info_per_ace", inst._info_per_ace == before)

# The read itself (as ACE_TAG_READ runs it) stores, binds and surfaces.
inst._gen1_tunnel_schedule(0, 0, bind=True)
check("a scheduled read marks the unit busy", 0 in inst._gen1_tunnel_busy)
run_async(inst, 1)
check("the read stores the UID in the own store",
      inst._gen1_tunnel_reads.get(0, {}).get(0, {}).get('uid') == UID_A)
check("... and offers the UID to the shared bind with unbind=False",
      inst.bind_calls == [(0, 0, UID_A, False)], inst.bind_calls)
check("... busy released after the run", inst._gen1_tunnel_busy == set())
check("... _info_per_ace still untouched", inst._info_per_ace == before)
check("... the raw page is kept for status/log",
      inst._gen1_tunnel_reads[0][0]['page0'] ==
      ' '.join('%02X' % b for b in CAP_A))

slot0 = status_slot(inst, 0)
check("get_status slot carries the tunnel UID where the device had none",
      slot0['uid'] == UID_A and slot0['tag_format'] == 'ntag')
check("get_status has the additive tag_tunnel block",
      inst.get_status()['aces'][0]['tag_tunnel']['reads']['0']['uid'] == UID_A)
inst._info_per_ace[0]['slots'][0]['uid'] = 'DEVICEUID'
inst._info_per_ace[0]['slots'][0]['rfid'] = 2
slot0 = status_slot(inst, 0)
check("a device UID/tag_format always beats the tunnel copy",
      slot0['uid'] == 'DEVICEUID' and slot0['tag_format'] == 'anycubic')
inst._info_per_ace[0]['slots'][0]['uid'] = ''
inst._info_per_ace[0]['slots'][0]['rfid'] = 0

inst._info_per_ace[0]['slots'][0]['status'] = 'empty1'
inst._gen1_tunnel_status_tick(0, inst._info_per_ace[0])
check("occupancy ended: the tunnel read is dropped",
      0 not in inst._gen1_tunnel_reads.get(0, {}))
inst._info_per_ace[0]['slots'][0]['status'] = 'ready'

# V2 is never touched.
inst4 = make_ace(protocols=('v2',), flag=True)
inst4._gen1_tunnel_status_tick(0, inst4._info_per_ace[0])
check("V2: the Gen-1 tick is a no-op",
      inst4.reactor.async_cbs == [] and inst4._gen1_tunnel_reads == {})

inst._gen1_tunnel_schedule(0, 0, bind=True)
run_async(inst, 1)
# Disconnect cleanup keeps the reads (host truth, evicted by an empty
# slot) and forgets the per-connection attempt bookkeeping.
inst._drop_device_tag_reads(0)
check("disconnect keeps the Gen-1 reads, drops the attempt bookkeeping",
      inst._gen1_tunnel_reads != {} and inst._gen1_tunnel_tried == {})

# 6b. ACE_SET_TAG_TUNNEL is obsolete: always on, no config write.
inst = make_ace(flag=True)
g = FakeGcmd(ENABLE='0')
inst.cmd_ACE_SET_TAG_TUNNEL(g)
check("ACE_SET_TAG_TUNNEL no longer switches the tunnel off",
      inst.gen1_tag_tunnel is True and not inst.cfg_writes, inst.cfg_writes)
check("... and says the switch is gone",
      any('no switch' in m for m in inst.responses), inst.responses)

# 6c. ACE_TAG_READ on Gen 1: explicit command works with the flag OFF.
inst = make_ace(flag=False)
fake_cli = FakeClient(result={'slot': 1, 'reader': 2, 'page': 0,
                              'data': CAP_B, 'uid': UID_B, 'format': 'ntag',
                              'openspool': None, 'saved': 7})
inst._gen1_tunnel_client = lambda idx: fake_cli
inst._info_per_ace[0]['slots'][1]['status'] = 'ready'
g = FakeGcmd(ACE='0', SLOT='1', PAGE='0')
inst._cmd_ace_tag_read_gen1(g, 0, 1)
check("ACE_TAG_READ V1 schedules even with the flag OFF",
      len(inst.reactor.async_cbs) == 1)
check("... it acknowledges in the console",
      any('Gen-1 tag read started' in m for m in g.info), g.info)
run_async(inst, 1)
check("... prints the raw page bytes",
      any(CAP_B.hex(' ').upper() in m for m in inst.responses),
      inst.responses)
check("... prints the UID and the third-party line",
      any('UID %s' % UID_B in m and 'third-party' in m
          for m in inst.responses), inst.responses)
check("... stores the manual read and releases busy",
      inst._gen1_tunnel_reads.get(0, {}).get(1, {}).get('uid') == UID_B
      and inst._gen1_tunnel_busy == set())
check("... the manual read still BINDS (operator chose the slot, partner "
      "slot 0 is occupied)",
      inst.bind_calls == [(0, 1, UID_B, False)]
      and inst._gen1_tunnel_reads[0][1]['bound'] is True,
      inst.bind_calls)
check("... reports the outcome through the tag_op status contract",
      inst._tag_op_result == {'ok': True, 'kind': 'read',
                              'seq': inst._tag_op_seq,
                              'msg': 'UID %s (ntag)' % UID_B},
      inst._tag_op_result)

# No tunnel on the unit: one clear console line, no read.
inst = make_ace(flag=False)
inst._gen1_tunnel_client = lambda idx: FakeClient(available=False)
g = FakeGcmd(ACE='0', SLOT='1')
inst._cmd_ace_tag_read_gen1(g, 0, 1)
run_async(inst, 1)
check("no tunnel: the console names the reason, no read",
      any('no tag tunnel' in m for m in inst.responses)
      and inst._gen1_tunnel_reads == {}, inst.responses)
check("... and the tag_op result says why",
      inst._tag_op_result.get('ok') is False
      and 'no tag tunnel' in inst._tag_op_result.get('msg', ''),
      inst._tag_op_result)

# Busy: the command is queued behind the running read, never refused.
inst = make_ace(flag=False)
inst._gen1_tunnel_client = lambda idx: FakeClient(result={
    'slot': 0, 'reader': 0, 'page': 0, 'data': CAP_B, 'uid': UID_B,
    'format': 'ntag', 'openspool': None, 'saved': 7})
inst._gen1_tunnel_busy.add(0)
g = FakeGcmd(ACE='0', SLOT='0')
try:
    inst._cmd_ace_tag_read_gen1(g, 0, 0)
    busy_refused = False
except FakeError:
    busy_refused = True
check("a concurrent Gen-1 read is queued, not refused",
      not busy_refused and len(inst.reactor.async_cbs) == 1
      and any('queued behind a running read' in m for m in g.info), g.info)
run_async(inst, 1)
check("... a read that never finishes times out with a console line and "
      "leaves the other read's busy flag alone",
      any('another tag read is still running' in m
          for m in inst.responses)
      and 0 in inst._gen1_tunnel_busy
      and inst._tag_op_result.get('ok') is False, inst.responses)
inst = make_ace(flag=False)
inst._gen1_tunnel_client = lambda idx: FakeClient(result={
    'slot': 0, 'reader': 0, 'page': 0, 'data': CAP_B, 'uid': UID_B,
    'format': 'ntag', 'openspool': None, 'saved': 7})
inst._gen1_tunnel_busy.add(0)
inst._cmd_ace_tag_read_gen1(FakeGcmd(ACE='0', SLOT='0'), 0, 0)
_orig_pause = inst.reactor.pause


def _pause_release(t, _r=inst):
    _r._gen1_tunnel_busy.discard(0)     # the running read finishes
    _orig_pause(t)


inst.reactor.pause = _pause_release
run_async(inst, 1)
inst.reactor.pause = _orig_pause
check("... once the running read ends, the queued read runs",
      inst._gen1_tunnel_reads.get(0, {}).get(0, {}).get('uid') == UID_B
      and inst._gen1_tunnel_busy == set(), inst.responses)
inst = make_ace(flag=False)
inst._connected_per_ace[0] = False
try:
    inst._cmd_ace_tag_read_gen1(FakeGcmd(ACE='0', SLOT='0'), 0, 0)
    disc_refused = False
except FakeError:
    disc_refused = True
check("a disconnected unit is refused", disc_refused)

# 6d. The V2 command path is unchanged (still refuses non-Open firmware).
inst = make_ace(protocols=('v2',), fw='V1.1.31')
try:
    inst.cmd_ACE_TAG_READ(FakeGcmd(ACE='0', SLOT='0'))
    v2_refused = False
except FakeError as e:
    v2_refused = 'ACE2-Open' in str(e)
check("ACE_TAG_READ on a stock ACE 2 still refuses (V2 path unchanged)",
      v2_refused)

# 6e. The command is registered.
src = open(os.path.join(EXTRAS, 'ace.py'), encoding='utf-8').read()
check("the tunnel is always on, the old option only read",
      "self.gen1_tag_tunnel = True" in src
      and "config.get('gen1_tag_tunnel', None)" in src)
check("ACE_SET_TAG_TUNNEL is registered by __init__ (source scan)",
      "'ACE_SET_TAG_TUNNEL'," in src)
check("the Gen-1 helper is imported lazily, never at module level",
      'from .ace_gen1_tunnel import' in src
      and '\nfrom .ace_gen1_tunnel' not in src
      and '\nimport ace_gen1_tunnel' not in src)
gsrc = open(os.path.join(EXTRAS, 'ace_gen1_tunnel.py'),
            encoding='utf-8').read()
check("the tunnel module reuses the ace_rc522 readers (relative import)",
      'from .ace_rc522 import AceTagReader' in gsrc
      and 'def decode_openspool' not in gsrc)
check("... and no longer carries its own BCC UID copy",
      'bcc0 = 0x88 ^ data[0]' not in gsrc
      and 'def uid_from_page0' in gsrc)

# --- shared antenna: halt the known partner card -------------------------


def _page0(uid_hex):
    """A 16-byte NTAG page-0 block for a 7-byte UID with valid BCCs."""
    u = bytes.fromhex(uid_hex)
    bcc0 = 0x88 ^ u[0] ^ u[1] ^ u[2]
    bcc1 = u[3] ^ u[4] ^ u[5] ^ u[6]
    return bytes([u[0], u[1], u[2], bcc0, u[3], u[4], u[5], u[6], bcc1,
                  0x48, 0x00, 0x00, T.NTAG_CC_MAGIC, 0x10, 0x00, 0x00])


def halt_scenario(cards, halt_holds=True, skip=()):
    """Run read_slot with a scripted field. `cards` = UIDs answering SELECT
    in anticollision order; a halted card drops out unless halt_holds is
    False. Returns (result, halted list, last_reason)."""
    cli = T.Gen1TagTunnel(FakeAce(), 0)
    field = list(cards)
    state = {'sel': None, 'halted': []}
    cli.acquire = lambda: 7
    cli.release = lambda saved: True

    def select(reader):
        live = [c for c in field if c not in state['halted']]
        state['sel'] = live[0] if live else None
        return state['sel'] is not None

    def read_page(page, reader=0, count=16, timeout=None):
        return _page0(state['sel']) if state['sel'] else None

    def halt(reader=0, timeout=None):
        if halt_holds:
            state['halted'].append(state['sel'])
        state['sel'] = None

    cli.select, cli.read_page, cli.halt = select, read_page, halt
    res = cli.read_slot(1, page=0, userdata=False, skip_uids=skip)
    return res, state['halted'], cli.last_reason


A_UID = '04A1B2C3D4E5F6'
B_UID = '04112233445566'
res, halted, why = halt_scenario([A_UID, B_UID], skip={A_UID})
check("known partner card is halted, the other card is read",
      res is not None and res['uid'] == B_UID and halted == [A_UID],
      "res=%r halted=%r why=%r" % (res and res.get('uid'), halted, why))
res, halted, why = halt_scenario([A_UID], skip={A_UID})
check("only the partner card in the field -> no read, clear reason",
      res is None and 'partner' in why, why)
res, halted, why = halt_scenario([A_UID, B_UID], halt_holds=False,
                                 skip={A_UID})
check("a halt that does not hold never returns the partner's UID",
      res is None or res.get('uid') != A_UID, why)
res, halted, why = halt_scenario([B_UID, A_UID], skip={A_UID})
check("no halt when the first card is not the partner's",
      res is not None and res['uid'] == B_UID and halted == [])
res, halted, why = halt_scenario([A_UID, B_UID], skip=())
check("without a known partner UID the first card is taken as before",
      res is not None and res['uid'] == A_UID and halted == [])


# --- op 5 without a code: trust a full, error-free FIFO ------------------


def page_read_with(op5, err, irq, lvl):
    cli = T.Gen1TagTunnel(FakeAce(), 0)
    payload = list(range(16))

    def fake_op(op, a1=0, a2=0, reader=0, timeout=None):
        if op == T.OP_RX_BITS:
            return op5
        if op == T.OP_READ_REG:
            return {T.REG_ERROR: err, T.REG_COMIRQ: irq,
                    T.REG_FIFOLEVEL: lvl}.get(a1, 0)
        if op == T.OP_FIFO_READ:
            return payload[a1]
        return 0

    cli._op = fake_op
    return cli.read_page(0)


check("op 5 lost, but 16 error-free bytes received -> page accepted (HW)",
      page_read_with(None, 0x00, 0x64, 0x10) == bytes(range(16)))
check("op 5 lost and a short FIFO -> no page",
      page_read_with(None, 0x00, 0x64, 0x04) is None)
check("op 5 lost and an error bit -> no page",
      page_read_with(None, 0x08, 0x64, 0x10) is None)
check("op 5 lost and no reception IRQ -> no page",
      page_read_with(None, 0x00, 0x01, 0x10) is None)
check("op 5 answers a short frame -> no page (fallback only on a lost op 5)",
      page_read_with(0x04, 0x00, 0x64, 0x10) is None)


# --- Gen-1 search: rotate until a card answers, then pull back -----------


def search_scenario(hit_after=None, unreadable=False, movable=True,
                    status='ready'):
    inst = make_ace(flag=True)
    moves = []

    def fake_move(idx, slot, mm, method, patience=None, gen=None):
        moves.append((method, mm, patience))
        return True

    inst._gen1_move = fake_move
    inst._gen1_lane_movable = lambda idx, slot: movable
    inst._gen1_slot_status = lambda idx, slot: status
    inst.GEN1_INSERT_READY_WAIT = 0.0

    class Cli:
        last_reason = ''

        def read_slot(self, slot, page=0, userdata=True, skip_uids=()):
            fed = sum(mm for m, mm, _p in moves if m == 'feed_filament')
            if hit_after is not None and fed >= hit_after:
                if unreadable:
                    self.last_reason = 'card selected, but the page 0 read failed'
                    return None
                return {'uid': B_UID, 'slot': slot}
            self.last_reason = 'no card answered SELECT on channel 0'
            return None

    res = inst._gen1_search_read(Cli(), 0, 1)
    fed = sum(mm for m, mm, _p in moves if m == 'feed_filament')
    back = sum(mm for m, mm, _p in moves if m == 'unwind_filament')
    return res, fed, back


res, fed, back = search_scenario(hit_after=60)
check("search stops at the first card and pulls the lane back exactly",
      res is not None and fed == 60 and back == 60, (res, fed, back))
# The pull-back waits out a busy unit (FORBIDDEN while a neighbour preloads).
inst = make_ace(flag=True)
_calls = []


def _fake_send(idx, req, cb):
    _calls.append(req['method'])
    busy = len(_calls) <= 6
    cb(response={'code': 0, 'msg': 'FORBIDDEN' if busy else 'ok'})


inst.send_request_to = _fake_send
# The real reactor.pause takes an absolute wake time.
inst.reactor.pause = lambda t, _r=inst.reactor: setattr(
    _r, 'now', max(_r.now + 0.01, float(t)))
check("the pull-back keeps retrying a busy unit until accepted",
      inst._gen1_move(0, 1, 300, 'unwind_filament',
                      patience=inst.GEN1_PULLBACK_PATIENCE) is True
      and len(_calls) == 7, _calls)
_calls.clear()
check("... a plain search step still gives up after 4 tries",
      inst._gen1_move(0, 1, 20, 'feed_filament') is False
      and len(_calls) == 4, _calls)

res, fed, back = search_scenario(hit_after=None)
check("no card: search is bounded and the lane comes back",
      res is None and fed == A.MultiAce.GEN1_SEARCH_MAX_MM and back == fed,
      (fed, back))
res, fed, back = search_scenario(hit_after=40, unreadable=True)
check("an unreadable card is re-read around the hit (+20, +40, -20), "
      "then the lane comes back exactly",
      res is None and fed == 80 and back == 80, (fed, back))
res, fed, back = search_scenario(hit_after=40, movable=False)
check("a lane that may not move is never rotated",
      res is None and fed == 0 and back == 0, (fed, back))
res, fed, back = search_scenario(hit_after=40, status='preloading')
check("a lane that never gets ready is not rotated",
      res is None and fed == 0, (fed, back))


# --- whose card: rotate the partner lane --------------------------------
def whose_scenario(leaves_after=None):
    inst = make_ace(flag=True)
    moves = []

    def fake_move(idx, slot, mm, method, patience=None, gen=None):
        moves.append((slot, method, mm))
        return True

    inst._gen1_move = fake_move
    inst._gen1_lane_movable = lambda idx, slot: True
    inst._gen1_slot_status = lambda idx, slot: 'ready'

    class Cli:
        last_reason = ''

        def read_slot(self, slot, page=0, userdata=True, skip_uids=()):
            fed = sum(mm for s_, m, mm in moves
                      if s_ == 0 and m == 'feed_filament')
            if leaves_after is not None and fed >= leaves_after:
                self.last_reason = 'no card answered SELECT on channel 2'
                return None
            return {'uid': B_UID, 'slot': slot}

    v = inst._gen1_whose_card(Cli(), 0, 1, B_UID)
    fed = sum(mm for s_, m, mm in moves if s_ == 0 and m == 'feed_filament')
    back = sum(mm for s_, m, mm in moves
               if s_ == 0 and m == 'unwind_filament')
    own = [m for m in moves if m[0] == 1]
    return v, fed, back, own


v, fed, back, own = whose_scenario(leaves_after=60)
check("partner rotation: the card leaves with the partner -> partner's",
      v == 'partner' and fed == 60 and back == 60 and own == [],
      (v, fed, back, own))
v, fed, back, own = whose_scenario(leaves_after=None)
check("partner rotation: the card stays after 200mm -> ours",
      v == 'ours' and fed == 200 and back == 200 and own == [],
      (v, fed, back, own))
inst = make_ace(flag=True)
inst._info_per_ace[0]['slots'][0]['status'] = 'preload'
check("a unit with a preloading slot is not idle",
      not inst._gen1_unit_idle(0))
inst._info_per_ace[0]['slots'][0]['status'] = 'ready'
check("... and idle once every slot is ready or empty",
      inst._gen1_unit_idle(0))

# --- an insert waits for the firmware's preload to end ----------------
inst = make_ace(flag=False)
inst._info_per_ace[0]['slots'][1]['status'] = 'preload'
_n = {'k': 0}
_p0 = inst.reactor.pause


def _pause_preload(t, _r=inst):
    _n['k'] += 1
    if _n['k'] == 3:
        _r._info_per_ace[0]['slots'][1]['status'] = 'ready'
    _r.reactor.now += 0.5


inst.reactor.pause = _pause_preload
check("an insert read waits for the preload to end",
      inst._gen1_wait_ready(0, 1, 120.0) is True and _n['k'] == 3)
inst._info_per_ace[0]['slots'][1]['status'] = 'preload'
_n['k'] = -10**6
check("... and reads anyway when preload never ends (bounded)",
      inst._gen1_wait_ready(0, 1, 5.0) is False)
inst._info_per_ace[0]['slots'][1]['status'] = 'empty'
check("... and gives up when the slot empties",
      inst._gen1_wait_ready(0, 1, 120.0) is False)
inst.reactor.pause = _p0

# --- the automatic read never searches ------------------------------------
inst = make_ace(flag=True)
sched = []
inst._gen1_tunnel_schedule = (
    lambda idx, slot, bind=True, skip_uids=(), search=False, **kw:
    sched.append((slot, search)))
inst._gen1_tunnel_status_tick(0, inst._info_per_ace[0])
check("a spool already in the slot at connect is not read automatically",
      sched == [], sched)
st = copy.deepcopy(inst._info_per_ace[0])
st['slots'][0]['status'] = 'empty1'
inst._gen1_tunnel_status_tick(0, st)
st['slots'][0]['status'] = 'ready'
del sched[:]
inst._gen1_tunnel_status_tick(0, st)
check("... and after a real insert no automatic session at all",
      not any(x[0] == 0 for x in sched), sched)


# --- partner UID counts only when attributed -----------------------------
inst = make_ace(flag=True)
inst._spool_binding = {}
inst._spools = {}
inst._gen1_tunnel_reads = {0: {1: {'uid': A_UID, 'bound': False,
                                   'why': 'auto'}}}
check("an unbound automatic read does not make the partner card known",
      inst._gen1_partner_uids(0, 1) == set())
inst._gen1_tunnel_reads[0][1]['bound'] = True
check("a bound read does", inst._gen1_partner_uids(0, 1) == {A_UID})
inst._gen1_tunnel_reads[0][1].update(bound=False, why='manual')
check("an operator read on that slot does",
      inst._gen1_partner_uids(0, 1) == {A_UID})


class _Why:
    def __init__(self, why):
        self.last_reason = why


check("search when only the partner's known card answered",
      A.MultiAce._gen1_should_search(_Why(
          "only the partner slot's known card X answered")))
check("an unreadable card in the field is re-read (search path)",
      A.MultiAce._gen1_should_search(_Why(
          "card selected, but the page 0 read failed")))


# --- a tunnel-read UID reaches the spool table ---------------------------
inst = make_ace(flag=True)
inst._gen1_tunnel_reads = {0: {1: {'uid': UID_B, 'bound': False}}}
check("the slot's read UID comes from the Gen-1 tunnel read",
      inst._slot_read_uid(0, 1) == UID_B and inst._slot_read_uid(0, 2) == '')

# --- tunnel reads survive a restart ----------------------------------------
class _SV:
    def __init__(self):
        self.allVariables = {}


inst = make_ace(flag=True)
inst.save_variables = _SV()
inst.save_variable = (lambda k, v, write=False, _i=inst:
                      _i.save_variables.allVariables.__setitem__(k, v))
inst._gen1_tunnel_store(0, 1, {'uid': UID_B, 'format': 'ntag',
                                'data': CAP_B}, why='manual')
saved = inst.save_variables.allVariables.get('ace__gen1_tag_reads') or {}
check("a tunnel read is persisted", saved.get('0_1', {}).get('uid') == UID_B,
      saved)
inst2 = make_ace(flag=True)
inst2.save_variables = inst.save_variables
inst2._gen1_tunnel_reads = {}
inst2._restore_gen1_tag_reads()
check("... and restored after a restart",
      inst2._slot_read_uid(0, 1) == UID_B
      and inst2._gen1_tunnel_reads[0][1].get('why') == 'manual')
inst2._drop_device_tag_reads(0)
check("... a reconnect keeps it", inst2._slot_read_uid(0, 1) == UID_B)

# --- a codeless bound spool learns the code read at its slot -------------
inst = make_ace(flag=True)
inst._spools = {'183': {'id': '183', 'sku': '', 'material': 'PLA'},
                '5': {'id': '5', 'sku': 'Sm8', 'material': 'PLA'}}
inst._spool_binding = {'2_1': '183', '2_2': '5'}
inst.spool_mode = 'local'
inst._save_spool_db = lambda **k: None
inst._spool_conflict_said = {}
A.MultiAce._spool_bind_by_tag(inst, 2, 1, UID_B, unbind=False)
A.MultiAce._spool_bind_by_tag(inst, 2, 2, UID_A, unbind=False)
check("a bound spool without any code learns the code read at its slot",
      inst._spools['183']['sku'] == UID_B)
check("... a spool that already has a code is left alone",
      inst._spools['5']['sku'] == 'Sm8')

# --- a spool without any code at its slot: log only, no amber notice ------
inst = make_ace(flag=True)
warns = []
inst.log_warn = warns.append
inst._spools = {'7': {'id': '7', 'sku': ''}}
inst._tag_read_capable = lambda idx: True
inst._warn_no_tag_code(0, 1, '7')
check("no code and no read: no amber notice (the web asks at '+')",
      warns == [], warns)

# --- firmware identity vetting (partner occupied) -------------------------
class _Sup:
    def support_state(self):
        return ('CV1.3.871', True)


def vet(reads=None, tried=None, busy=False, partner='ready', fw_fmt='openspool'):
    inst = make_ace(flag=True)
    inst._gen1_tunnel_clients = {0: _Sup()}
    inst._gen1_tunnel_reads = {0: reads or {}}
    inst._gen1_tunnel_tried = {0: tried or {}}
    inst._gen1_tunnel_busy = {0} if busy else set()
    inst._gen1_tunnel_busy_slot = {0: 0} if busy else {}
    res = {'slots': [
        {'index': 0, 'status': 'ready', 'rfid': 2, 'type': 'PLA',
         'color': [122, 74, 30], 'sku': '', 'tag_format': fw_fmt},
        {'index': 1, 'status': partner, 'rfid': 0, 'type': '',
         'color': [0, 0, 0], 'sku': ''},
        {'index': 2, 'status': 'empty1', 'rfid': 0},
        {'index': 3, 'status': 'empty1', 'rfid': 0}]}
    inst._gen1_vet_firmware_ids(0, res)
    return res['slots'][0]


s0 = vet()
check("partner occupied, our read pending -> firmware identity kept",
      s0['rfid'] == 2 and s0['type'] == 'PLA', s0)
s0 = vet(busy=True)
check("... our read running -> firmware identity kept",
      s0['rfid'] == 2 and s0['type'] == 'PLA', s0)
s0 = vet(partner='empty1')
check("... partner empty -> firmware identity kept",
      s0['rfid'] == 2 and s0['color'] == [122, 74, 30], s0)
s0 = vet(reads={0: {'uid': UID_A, 'format': 'ntag',
                    'userdata_complete': True}}, tried={0: True})
check("... our full read found a non-OpenSpool card -> firmware OpenSpool "
      "hidden", s0['rfid'] == 1, s0)
s0 = vet(reads={0: {'uid': UID_A, 'format': 'ntag'}}, tried={0: True})
check("... a UID-only read (user pages not read) -> firmware kept",
      s0['rfid'] == 2 and s0['type'] == 'PLA', s0)
s0 = vet(reads={0: {'uid': UID_A, 'format': 'openspool', 'material': 'PETG',
                    'color': '1188FF', 'brand': 'X'}}, tried={0: True})
check("... our OpenSpool read replaces the firmware identity",
      s0['rfid'] == 2 and s0['type'] == 'PETG'
      and s0['color'] == [0x11, 0x88, 0xFF], s0)
s0 = vet(tried={0: True})
check("... our attempt found nothing -> firmware identity kept",
      s0['rfid'] == 2 and s0['type'] == 'PLA', s0)
s0 = vet(reads={0: {'uid': UID_A, 'format': 'anycubic'}}, tried={0: True},
         fw_fmt='')
check("... both non-OpenSpool (Anycubic) -> firmware identity kept",
      s0['rfid'] == 2 and s0['type'] == 'PLA', s0)

# --- an insert must never meet a tunnel session -----------------------------
# A refused move ("empty") is no move.
inst = make_ace(flag=True)
_calls = []


def _send_empty(idx, req, cb):
    _calls.append(req['method'])
    cb(response={'code': 0, 'msg': 'empty'})


inst.send_request_to = _send_empty
inst.reactor.pause = lambda t, _r=inst.reactor: setattr(
    _r, 'now', max(_r.now + 0.01, float(t)))
check("msg=empty is not a move and ends the move at once",
      inst._gen1_move(0, 0, 20, 'feed_filament') is False
      and len(_calls) == 1, _calls)
_calls.clear()


def _send_ok(idx, req, cb):
    _calls.append(req['method'])
    cb(response={'code': 0, 'msg': 'success'})


inst.send_request_to = _send_ok
inst._gen1_gate_gen[(0, 0)] = 3
check("a gate flip since the search started sends no move",
      inst._gen1_move(0, 0, 20, 'feed_filament', gen=2) is False
      and _calls == [], _calls)

# The search stops when the spool is pulled / re-inserted, and never pulls
# back a different spool.
inst = make_ace(flag=True)
_moves = []


def _mv(idx, slot, mm, method, patience=None, gen=None):
    if gen is not None and inst._gen1_gate_gen.get((idx, slot), 0) != gen:
        return False
    _moves.append((method, mm))
    if len(_moves) == 3:
        inst._gen1_gate_gen[(0, 0)] = \
            inst._gen1_gate_gen.get((0, 0), 0) + 2     # pulled + re-inserted
    return True


inst._gen1_move = _mv
inst._gen1_lane_movable = lambda idx, slot: True
inst._gen1_slot_status = lambda idx, slot: 'ready'
inst.GEN1_INSERT_READY_WAIT = 0.0


class _NoCard:
    last_reason = 'no card answered SELECT on channel 0'
    aborted = ''

    def read_slot(self, slot, page=0, userdata=True, skip_uids=()):
        return None


inst._gen1_search_read(_NoCard(), 0, 0)
check("search ends at a gate flip and does not pull the new spool back",
      [m for m, _ in _moves] == ['feed_filament'] * 3, _moves)

# The client stops a session the moment the abort check fires, and the
# reader is released anyway.
fa = FakeAce(replies=[r(7), r(0)] + [r(0)] * 40)
cli = T.Gen1TagTunnel(fa, 0)
_n = {'ops': 0}


def _abort():
    _n['ops'] += 1
    return 'insert running on slot 1' if _n['ops'] > 2 else ''


cli.abort_check = _abort
out = cli.read_slot(0)
check("an abort mid-session returns no read and names the reason",
      out is None and cli.aborted == 'insert running on slot 1'
      and cli.last_reason.startswith('aborted'), cli.last_reason)
check("... and the release op (8) still went out",
      unpack(fa.sent[-1][2])[1] == 8, [unpack(x[2]) for x in fa.sent][-2:])

# No session starts while the firmware inserts a spool on the unit.
inst = make_ace(flag=True)
inst.gen1_tag_tunnel = True
_sched = []
inst._gen1_tunnel_schedule = lambda *a, **k: _sched.append((a, k))
inst._gen1_partner_uids = lambda idx, slot: set()
inst._spool_by_sku = lambda sku: (None, None)
res = {'slots': [
    {'index': 0, 'status': 'ready', 'rfid': 0, 'sku': ''},
    {'index': 1, 'status': 'preload', 'rfid': 3, 'sku': ''},
    {'index': 2, 'status': 'empty1', 'rfid': 0},
    {'index': 3, 'status': 'empty1', 'rfid': 0}]}
inst._gen1_tunnel_status_tick(0, res)
check("no tunnel read is scheduled while a slot of the unit preloads",
      _sched == [] and not inst._gen1_tunnel_tried.get(0), _sched)
res['slots'][1]['status'] = 'ready'
inst._gen1_tunnel_status_tick(0, res)
check("... nor once the unit is quiet", _sched == [], _sched)

# An aborted automatic read does not use up the slot's attempt.
inst = make_ace(flag=True)
inst._gen1_tunnel_tried = {0: {1: True}}
inst._gen1_read_aborted(0, 1, types.SimpleNamespace(
    aborted='insert running on slot 0'), True)
check("an aborted read is pending again and keeps its search",
      1 not in inst._gen1_tunnel_tried[0]
      and 1 in inst._gen1_seen_empty.get(0, set()))

# The vet hides only the slot whose read is running.
inst = make_ace(flag=True)
inst._gen1_tunnel_clients = {0: _Sup()}
inst._gen1_tunnel_tried = {0: {0: True, 1: True}}
inst._gen1_tunnel_busy = {0}
inst._gen1_tunnel_busy_slot = {0: 2}
res = {'slots': [
    {'index': 0, 'status': 'ready', 'rfid': 2, 'type': 'PLA',
     'color': [122, 74, 30], 'sku': '', 'tag_format': 'openspool'},
    {'index': 1, 'status': 'ready', 'rfid': 0, 'sku': ''},
    {'index': 2, 'status': 'ready', 'rfid': 0, 'sku': ''},
    {'index': 3, 'status': 'empty1', 'rfid': 0}]}
inst._gen1_vet_firmware_ids(0, res)
check("a read on another slot does not hide this slot's identity",
      res['slots'][0]['rfid'] == 2 and res['slots'][0]['type'] == 'PLA',
      res['slots'][0])


# Our own search move reads as 'shifting' on the ACE Pro: not an insert.
inst = make_ace(flag=True)
inst._info_per_ace = {0: {'slots': [
    {'index': 0, 'status': 'ready'}, {'index': 1, 'status': 'ready'},
    {'index': 2, 'status': 'empty1'}, {'index': 3, 'status': 'empty1'}]}}
inst._gen1_session_begin(0)
_calls = []
inst.send_request_to = _send_ok
inst._gen1_move(0, 0, 20, 'feed_filament')
inst._info_per_ace[0]['slots'][0]['status'] = 'shifting'
check("our own move on the read slot does not abort the session",
      inst._gen1_session_abort(0) == '', inst._gen1_session_abort(0))
inst._info_per_ace[0]['slots'][1]['status'] = 'preload'
check("... an insert on a lane we did not move still aborts it",
      inst._gen1_session_abort(0) == 'insert running on slot 1',
      inst._gen1_session_abort(0))
inst._info_per_ace[0]['slots'][1]['status'] = 'ready'
inst._gen1_gate_gen[(0, 0)] = 2
check("... a real re-insert on our own lane aborts via its gate",
      inst._gen1_session_abort(0) == 'a gate of the unit changed',
      inst._gen1_session_abort(0))
inst._gen1_session_begin(0)
inst._info_per_ace[0]['slots'][0]['status'] = 'shifting'
check("... a new session forgets the lanes of the last one",
      inst._gen1_session_abort(0) == 'insert running on slot 0',
      inst._gen1_session_abort(0))

# A stopped read keeps the identity that was shown before it.
inst = make_ace(flag=True)
inst._gen1_tunnel_clients = {0: _Sup()}
inst._gen1_tunnel_tried = {0: {0: True}}


def _vet_res():
    return {'slots': [
        {'index': 0, 'status': 'ready', 'rfid': 2, 'type': 'PLA',
         'color': [122, 74, 30], 'sku': '', 'tag_format': 'openspool'},
        {'index': 1, 'status': 'ready', 'rfid': 0, 'sku': ''},
        {'index': 2, 'status': 'empty1', 'rfid': 0},
        {'index': 3, 'status': 'empty1', 'rfid': 0}]}


inst._gen1_vet_firmware_ids(0, _vet_res())        # settled: keep
inst._gen1_tunnel_busy_slot = {0: 0}
inst._gen1_vet_firmware_ids(0, _vet_res())        # running: hide
inst._gen1_tunnel_busy_slot = {}
inst._gen1_read_aborted(0, 0, types.SimpleNamespace(
    aborted='a gate of the unit changed'), False)
r0 = _vet_res()
inst._gen1_vet_firmware_ids(0, r0)
check("a stopped read keeps the identity shown before it",
      r0['slots'][0]['rfid'] == 2 and r0['slots'][0]['type'] == 'PLA',
      r0['slots'][0])
inst = make_ace(flag=True)
inst._gen1_tunnel_clients = {0: _Sup()}
inst._gen1_read_aborted(0, 0, types.SimpleNamespace(aborted='x'), False)
r0 = _vet_res()
inst._gen1_vet_firmware_ids(0, r0)
check("... a stopped first read keeps the firmware identity",
      r0['slots'][0]['rfid'] == 2, r0['slots'][0])


# --- summary ---------------------------------------------------------------

print("")
if FAILED:
    print("FAILED: %d check(s): %s" % (len(FAILED), ', '.join(FAILED)))
    sys.exit(1)
print("all checks passed")
sys.exit(0)
