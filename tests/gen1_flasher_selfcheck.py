#!/usr/bin/env python3
"""Self-check for the Gen 1 (ACE Pro) flasher - web/backend/ace1_flash.py.

The repository has no test runner and no CI beyond the release tarball:
tests/ holds the test-in-a-script files, run directly. This is the one
for the Gen-1 flasher: the pure protocol parts plus a FULL simulated flash
against an in-process fake transport. No hardware, no pyserial, no serial
port needed.

Run it after touching ace1_flash.py:

    python3 tests/gen1_flasher_selfcheck.py

On the printer (or a dev box) with real images, also verify the shipped
tested-images entries byte-for-byte:

    python3 tests/gen1_flasher_selfcheck.py \
        --image ACE_V1.3.863_tunnel_ops.bin --entry 1.3.871-tunnel

Exit codes:
    0  every check passed
    1  a check FAILED
    2  usage/setup error (flasher module or image not found)
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import tempfile

# --- locate the backend module (repo checkout OR installed web head) ------

def find_backend():
    candidates = [
        os.environ.get("MULTIACE_BACKEND"),
        # repo checkout: this file lives in <repo>/tests/
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "multiace", "web", "backend"),
        "/home/lava/multiace_web/backend",                       # printer
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "backend"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "ace1_flash.py")):
            return c
    return None


BACKEND = find_backend()
if BACKEND is None:
    print("[FAIL] ace1_flash.py not found - set MULTIACE_BACKEND to the "
          "web/backend directory")
    sys.exit(2)
sys.path.insert(0, BACKEND)
try:
    ACE1 = importlib.import_module("ace1_flash")
except Exception as e:                                   # pragma: no cover
    print("[FAIL] cannot import ace1_flash from %s: %s" % (BACKEND, e))
    sys.exit(2)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("[ok]   %s" % name)
    else:
        print("[FAIL] %s %s" % (name, detail))
        FAILED.append(name)


def expect_flash_error(name, fn):
    try:
        fn()
    except ACE1.FlashError as e:
        check(name, True, "(%s)" % str(e)[:60])
    else:
        check(name, False, "- no FlashError raised")


# --- 1. pure protocol ------------------------------------------------------

check("CRC-16/MCRF4XX check value (123456789 -> 0x6F91)",
      ACE1.crc16_mcrf4xx(b"123456789") == 0x6F91)

payload = b'{"id":1,"method":"get_info"}'
f = ACE1.frame(payload)
check("frame layout FF AA | len LE | payload | crc LE | FE",
      f[:2] == b"\xff\xaa" and f[2:4] == bytes([len(payload), 0])
      and f[-3:] == bytes([ACE1.crc16_mcrf4xx(payload) & 0xFF,
                           (ACE1.crc16_mcrf4xx(payload) >> 8) & 0xFF,
                           ACE1.END_MARKER]))

frames, rest = ACE1.extract_frames(bytearray(b"\x00\xff\xaa" + f + f[:3]))
check("extract_frames skips noise, keeps a partial frame as rest",
      frames == [payload] and rest == f[:3], "(frames=%r rest=%r)"
      % (frames, rest))

chunk = ACE1.chunk_payload(0x08024000, b"\x01\x02")
check("chunk payload 55 | addr LE | n | data",
      chunk == b"\x55\x00\x40\x02\x08\x02\x01\x02")

expect_flash_error("chunk_payload refuses empty/oversized data",
                   lambda: ACE1.chunk_payload(0, b"\x00" * 65))

# --- 2. image gate ---------------------------------------------------------

tmp = tempfile.mkdtemp(prefix="ace1-selfcheck-")
img_path = os.path.join(tmp, "synthetic.bin")
img = bytes((i * 7 + 3) & 0xFF for i in range(200))
with open(img_path, "wb") as fh:
    fh.write(img)

ENTRY = "__selftest__"          # synthetic entry; never a shipped image
ACE1.KNOWN_FIRMWARE[ENTRY] = {
    "version": "1.3.863", "announce": "1.3.863", "label": "selftest",
    "file": "synthetic.bin", "size": len(img),
    "crc": ACE1.crc16_mcrf4xx(img),
    "md5": hashlib.md5(img).hexdigest(), "source": "synthetic",
    "tested": "",
}
fw = ACE1.load_image(img_path, ENTRY)
check("load_image + check_known accept the exact tested bytes",
      fw.image_md5 == hashlib.md5(img).hexdigest()
      and fw.version == "1.3.863" and len(fw.data) == len(img))

bad = ACE1.FirmwareImage(img[:-1], "1.3.863", ACE1.crc16_mcrf4xx(img[:-1]),
                         "raw binary", ENTRY)
expect_flash_error("check_known refuses a size/CRC mismatch",
                   lambda: ACE1.check_known(bad))
expect_flash_error("check_known refuses an unknown entry id",
                   lambda: ACE1.check_known(
                       ACE1.FirmwareImage(img, "1.3.863",
                                          ACE1.crc16_mcrf4xx(img),
                                          "raw binary", "nope")))
with open(os.path.join(tmp, "fake.swu"), "wb") as fh:
    fh.write(b"PK\x03\x04not really a package")
expect_flash_error("load_image refuses .swu/ZIP uploads (Gen 1 = raw .bin)",
                   lambda: ACE1.load_image(os.path.join(tmp, "fake.swu"),
                                           ENTRY))

# --- 3. simulated flash over a fake transport ------------------------------

class FakeTransport:
    last = None

    def __init__(self, port):
        self.port = port
        self.calls = []
        self.chunks = []
        self.finished = False
        self.polls_after_finish = 0
        self.closed = False
        self.reopens = 0
        FakeTransport.last = self

    def prime(self, tries=40, ivl=0.3):
        self.calls.append(("prime",))
        return True

    def rpc(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == "get_status":
            return {"id": 1, "result": {}}
        if method == "get_info":
            if self.finished and self.polls_after_finish < 1:
                # First poll after the commit: still rebooting.
                self.polls_after_finish += 1
                return None
            ver = "CV1.3.863" if self.finished else "V1.3.863"
            return {"id": 2, "code": 0,
                    "result": {"firmware": ver, "model": "Color Engine Pro"}}
        if method == "iap_version":
            return {"id": 3, "code": 0, "result": {"version": "1.0.1"}}
        if method == "iap_upgrade":
            return {"id": 4, "code": 0, "msg": "success"}
        if method == "iap_upgrade_finish":
            self.finished = True
            return None
        return None

    def chunk(self, addr, data):
        self.chunks.append((addr, data))

    def close(self):
        self.closed = True

    def reopen(self):
        # The real transport drops the fd from before the commit reboot
        # and opens the port again when a post-finish probe finds nothing.
        self.reopens += 1
        self.closed = False


real_transport = ACE1.Gen1Transport
ACE1.Gen1Transport = FakeTransport
progress = []
try:
    res = ACE1.flash("/dev/fake", fw, lambda p, m: progress.append((p, m)))
finally:
    ACE1.Gen1Transport = real_transport

t = FakeTransport.last
names = [c[0] for c in t.calls]
check("flash: get_info/iap_version before iap_upgrade, finish after",
      names.index("get_info") < names.index("iap_upgrade")
      < names.index("iap_upgrade_finish"))
up = dict((c[0], c[1]) for c in t.calls if c[0] == "iap_upgrade")
check("flash: iap_upgrade announces size/crc/version",
      up["iap_upgrade"] == {"size": len(img),
                            "crc": ACE1.crc16_mcrf4xx(img),
                            "version": "1.3.863"},
      str(up))
base = ACE1.GEN1_FLASH_BASE
addrs = [a for a, _ in t.chunks]
check("flash: chunks cover the image from the staging base, 64 B max",
      len(t.chunks) == (len(img) + 63) // 64
      and all(a == base + i * 64 for i, (a, _) in enumerate(t.chunks))
      and all(len(d) <= 64 for _, d in t.chunks)
      and b"".join(d for _, d in t.chunks) == img)
check("flash: result verified, unit heard after reboot, app answers",
      res.get("verified") is True and res.get("new") == "CV1.3.863"
      and res.get("app_alive") is True, str(res))
check("flash: transport closed", t.closed)
check("flash: reopens the port after the commit reboot", t.reopens == 1)
check("flash: progress reached 100 with messages",
      any(p == 100.0 for p, _ in progress) and all(m for _, m in progress))

# An untested entry must die before any wire traffic.
ACE1.Gen1Transport = FakeTransport
try:
    expect_flash_error(
        "flash: an untested entry never reaches the wire",
        lambda: ACE1.flash("/dev/fake",
                           ACE1.FirmwareImage(img, "1.3.863",
                                              ACE1.crc16_mcrf4xx(img),
                                              "raw binary", "nope"),
                           lambda p, m: None))
    t2 = FakeTransport.last
    check("flash: no iap_upgrade sent for the refused image",
          "iap_upgrade" not in [c[0] for c in t2.calls])
finally:
    ACE1.Gen1Transport = real_transport

# --- 4. optional: verify a real image against a shipped entry -------------

if "--image" in sys.argv:
    try:
        img_arg = sys.argv[sys.argv.index("--image") + 1]
        entry_arg = sys.argv[sys.argv.index("--entry") + 1]
    except (IndexError, ValueError):
        print("[FAIL] --image needs --entry <id>")
        sys.exit(2)
    real = ACE1.load_image(img_arg, entry_arg)
    ACE1.check_known(real)
    e = ACE1.KNOWN_FIRMWARE[entry_arg]
    print("[ok]   %s matches %s: %d bytes, CRC 0x%04X, md5 %s"
          % (os.path.basename(img_arg), entry_arg, len(real.data),
             real.image_crc, real.image_md5))
    check("real image announce version", real.version == e["announce"])

# --- summary ----------------------------------------------------------------

if FAILED:
    print("\n%d check(s) FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("\nAll Gen-1 flasher self-checks passed (%s)" % BACKEND)
