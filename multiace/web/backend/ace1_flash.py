"""Gen 1 (ACE Pro) IAP firmware flasher - library form for the web backend.

NOT a variant of ace2_ota.py: the two generations share the framing bytes
(FF AA ... FE) and the IAP idea, but nothing on the wire. A Gen 1 unit has
no OTA updater and speaks JSON-RPC; the ACE 2 speaks protobuf-style
commands 2/3/4. Same tested-images idea, separate transport, separate
allowlist, separate release gate - so nothing done to the Gen 2 path can
move the Gen 1 wire.

Protocol (reverse-engineered from V1.3.863; flasher and findings:
github.com/Godless50/ACE-PRO-v1.-NFC-UID, GPL-3.0):
  frame:   FF AA | len(u16 LE) | payload | crc16(payload) | FE
    crc16 = CRC-16/MCRF4XX (poly 0x8408, init 0xFFFF) - the same
    polynomial ace_protocol_v1.calc_crc uses, restated here so this
    module does not import a Klipper module.
  payload: JSON-RPC request     -> get_info / get_status / iap_version /
                                   iap_upgrade{size,crc,version} /
                                   iap_upgrade_finish
           or 0x55 | addr(u32 LE) | n(u8) | data[n] -> IAP data chunk
  staging base 0x08024000, 64 bytes per chunk (the unit's own limit).
  The unit resets itself after ~3.3 s without traffic, so the link is
  kept busy: the chunk stream during the write, get_status pings while
  waiting (prime / the post-finish poll).

The IAP sequence is the one the reporter's flasher runs on hardware:
  1. get_info / iap_version         - prove the link, read the version
  2. iap_upgrade{size, crc, version} - announce the image
  3. 0x55 chunks from 0x08024000     - write it (64 B, pace-limited)
  4. iap_upgrade_finish              - commit; the ACE reboots
  5. poll get_info                   - the unit must come back

HARD TRAP, in every direction: never flash through
/etc/init.d/S60klipper start|restart. That path runs the printer's own
update check and re-flashes the stock ACE firmware over a modified image.
Use this module / the web card, and a Moonraker firmware restart to bring
Klipper back. The full trap list is docs/GEN1_FLASH.md.
"""

import hashlib
import json
import struct
import time

# serial is imported LAZILY (in Gen1Transport) so a backend without
# pyserial can still parse an image and report a clean "pyserial
# missing" instead of failing the whole module import - and load_image /
# check_known need no serial at all (a dry run's file half must work
# even where the port half cannot).

BAUD              = 115200
PREAMBLE          = b'\xff\xaa'
END_MARKER        = 0xFE
CHUNK_MARK        = 0x55
MAX_CHUNK         = 64
GEN1_FLASH_BASE   = 0x08024000
MAX_PAYLOAD_LEN   = 2048
SERIAL_TIMEOUT    = 0.3
CHUNK_PACE        = 0.01
OPEN_TRIES        = 20
OPEN_RETRY_IVL    = 0.5

T_RPC             = 4.0
T_UPGRADE         = 6.0
T_FINISH          = 10.0
BOOT_WAIT         = 30.0

# Version announced when no tested entry is selected (dry runs only -
# a real flash always announces the entry's own value).
DEFAULT_ANNOUNCE  = "1.3.863"

# Known-good Gen-1 images - the release gate. Gen-1 version strings do NOT
# identify an image: stock and the OpenCubic CFW both report 1.3.863, so
# the entries are keyed by IMAGE, not by version. The md5 is what the gate
# compares byte-exactly before a byte goes to the wire, and the dropdown
# value is the entry id.
#
# A "source" line is required per entry: whose release asset this exact
# binary is. multiACE ships none of these and makes no licence claim about
# them; the user supplies the image. To add an image: dry-run it, read
# size/CRC/MD5 off the result, verify on hardware, then add the entry.
# Keep "tested" empty until a multiACE bench flash has actually run.
KNOWN_FIRMWARE = {
    "1.3.871-tunnel": {
        "version": "1.3.871",
        # Reports CV1.3.871; the bootloader is announced the 1.3.863 base
        # like every other Gen-1 image (the bench flasher always did).
        "announce": "1.3.863",
        "label": "CV1.3.871 - CFW + UID stub + RC522 tunnel",
        "file": "ACE_V1.3.863_tunnel_ops.bin",
        "size": 114632,
        "crc": 0x1AC9,
        "md5": "219df3df77f7c7e1e15a79d580a2379e",
        "source": "Godless50/ACE-PRO-v1.-NFC-UID (OpenCubic CFW base + UID "
                  "stub + RC522 tunnel), asset ACE_V1.3.863_tunnel_ops.bin, "
                  "md5 219df3df77f7c7e1e15a79d580a2379e",
        "tested": "2026-09-29 HW: ACE Pro V1.3.856 -> CV1.3.871 verified",
    },
    "1.3.863-stock": {
        "version": "1.3.863",
        "announce": "1.3.863",
        "label": "1.3.863 - stock (rollback)",
        "file": "ACE_V1.3.863_20250518.bin",
        "size": 105652,
        "crc": 0xDEFB,
        "md5": "dcd04589dcadd5b4feab66d33e772531",
        "source": "clean stock image ACE_V1.3.863_20250518.bin "
                  "(OpenCubic originalFirmware), "
                  "md5 dcd04589dcadd5b4feab66d33e772531",
        "tested": "2026-10-03 HW: ACE Pro CV1.3.871 -> V1.3.863 verified",
    },
}


class FlashError(Exception):
    """Anything that ends the update - message is user-facing."""


def crc16_mcrf4xx(data: bytes) -> int:
    """CRC-16/MCRF4XX (poly 0x8408, init 0xFFFF) - check value of
    b'123456789' is 0x6F91. Identical to ace_protocol_v1.calc_crc."""
    crc = 0xFFFF
    for byte in data:
        data_byte = byte
        data_byte ^= crc & 0xFF
        data_byte ^= (data_byte & 0x0F) << 4
        crc = (((data_byte << 8) | (crc >> 8)) ^ (data_byte >> 4)
               ^ (data_byte << 3))
    return crc & 0xFFFF


def frame(payload: bytes) -> bytes:
    """Wrap one payload in FF AA | len(u16 LE) | payload | crc16 | FE."""
    return (PREAMBLE + struct.pack('<H', len(payload)) + payload
            + struct.pack('<H', crc16_mcrf4xx(payload))
            + bytes([END_MARKER]))


def extract_frames(buf: bytearray):
    """Split complete frames out of buf IN PLACE. Returns (payloads, rest)
    - the payloads in order, and the tail that is not a complete frame
    yet (partial header, partial payload, or noise after a bad length)."""
    out = []
    while True:
        i = buf.find(PREAMBLE)
        if i < 0:
            return out, b''
        if i:
            del buf[:i]
        if len(buf) < 7:
            return out, bytes(buf)
        ln = struct.unpack_from('<H', buf, 2)[0]
        if ln > MAX_PAYLOAD_LEN:
            # Not one of our frames - drop the false preamble and resync.
            del buf[:2]
            continue
        total = 4 + ln + 3
        if len(buf) < total:
            return out, bytes(buf)
        out.append(bytes(buf[4:4 + ln]))
        del buf[:total]


def chunk_payload(addr: int, data: bytes) -> bytes:
    """One IAP data chunk: 55 | addr(u32 LE) | n(u8) | data[n]."""
    if not 0 < len(data) <= MAX_CHUNK:
        raise FlashError("chunk must be 1..%d bytes, got %d"
                         % (MAX_CHUNK, len(data)))
    return (bytes([CHUNK_MARK]) + struct.pack('<I', addr)
            + bytes([len(data)]) + data)


class Gen1Transport:
    """One JSON-RPC link to an ACE Pro over the FF AA frame protocol.

    The caller must have released the port first (ACE_FW_RELEASE in
    Klipper); this class only opens and talks. rpc() drops stale input
    before every request and matches the reply by the JSON-RPC id -
    Gen 1 has no sequence field, unlike the ACE 2 packets.
    """

    def __init__(self, port: str):
        self.port = port
        self.ser = self._open_serial()
        self.buf = bytearray()
        self._id = 0

    def _open_serial(self, tries=OPEN_TRIES):
        """Open the released port, retrying a transient EBUSY. Returns the
        serial object; raises FlashError (same message as before) when the
        port stays shut for every try."""
        try:
            import serial
        except ImportError:
            raise FlashError(
                "pyserial is not installed in the web backend - run the "
                "multiACE installer again (it now pulls pyserial) or "
                "'pip3 install --user pyserial'")
        # The port may still be closing in Klipper for a moment after
        # ACE_FW_RELEASE (and the ACE re-enumerates while idle), so retry
        # the open like the bench flasher does instead of failing the
        # flash on a transient EBUSY.
        last = None
        ser = None
        for _ in range(tries):
            try:
                ser = serial.Serial(self.port, BAUD,
                                    timeout=SERIAL_TIMEOUT,
                                    rtscts=True, exclusive=True)
                break
            except Exception as e:
                last = e
                time.sleep(OPEN_RETRY_IVL)
        if ser is None:
            raise FlashError(
                "cannot open %s after %d tries (%s) - is the unit powered "
                "and the port released (ACE_FW_RELEASE)?" % (self.port,
                                                             tries, last))
        return ser

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def reopen(self):
        """Drop the dead fd and open the port again. An ACE Pro resets and
        re-enumerates on USB after `iap_upgrade_finish`, and also while it
        sits idle (~every 3.5 s), so the fd held before the reset is gone;
        polling it would report 'not heard' for a unit that is fine. No
        stale bytes may survive from the old fd."""
        self.close()
        self.ser = self._open_serial()
        self.buf = bytearray()

    def pump(self, dur: float):
        """Read for up to dur seconds, return every complete frame's
        payload. The serial read timeout makes this overshoot by up to
        SERIAL_TIMEOUT - callers treat it as a slice, not a deadline."""
        out = []
        deadline = time.time() + dur
        while time.time() < deadline:
            data = self.ser.read(4096)
            if not data:
                continue
            self.buf += data
            frames, rest = extract_frames(self.buf)
            out.extend(frames)
            self.buf = bytearray(rest)
        return out

    def rpc(self, method: str, params=None, timeout: float = T_RPC):
        """Send one JSON-RPC request, return the reply dict or None."""
        self._id += 1
        rid = self._id
        req = {"id": rid, "method": method}
        if params is not None:
            req["params"] = params
        payload = json.dumps(req, separators=(",", ":")).encode("utf-8")
        self.buf = bytearray()
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write(frame(payload))
        self.ser.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            slice_dur = min(0.2, max(0.01, deadline - time.time()))
            for p in self.pump(slice_dur):
                try:
                    obj = json.loads(p.decode("utf-8"))
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("id") == rid:
                    return obj
        return None

    def chunk(self, addr: int, data: bytes):
        """One fire-and-forget IAP write; the ACK (if any) is dropped by
        the next rpc()'s input flush, exactly like the bench flasher."""
        self.ser.write(frame(chunk_payload(addr, data)))
        self.ser.flush()

    def prime(self, tries: int = 40, ivl: float = 0.3) -> bool:
        """Wait for the unit to answer on the released port. The unit
        resets itself while idle (~3.3 s), so the first requests may time
        out - keep asking; get_status is read-only and cheap. A probe with
        no answer (None or an error) may mean the fd points at a unit that
        just re-enumerated, so reopen before the next try; a reopen that
        fails must not abort the wait, the loop tries again later."""
        for _ in range(tries):
            try:
                if self.rpc("get_status", timeout=1.0) is not None:
                    return True
            except Exception:
                pass
            try:
                self.reopen()
            except Exception:
                pass
            time.sleep(ivl)
        return False


class FirmwareImage:
    def __init__(self, data: bytes, version: str, image_crc: int,
                 source: str = '', entry_id: str = ''):
        self.data = data
        self.version = version
        self.image_crc = image_crc
        self.image_md5 = hashlib.md5(data).hexdigest()
        self.source = source
        self.entry_id = entry_id


def load_image(path: str, entry_id: str = '',
               expected_md5=None) -> FirmwareImage:
    """Load a raw Gen-1 .bin. No archive extraction: Gen 1 has no .swu
    packaging, so an uploaded ZIP/CPIO is refused with a clear message
    instead of a flash of the wrong bytes. MD5 is checked over the raw
    file when the caller supplies one (the tested-list gate is md5-based
    and sits in check_known)."""
    with open(path, 'rb') as f:
        raw = f.read()
    if expected_md5:
        actual = hashlib.md5(raw).hexdigest()
        if actual.lower() != expected_md5.strip().lower():
            raise FlashError("MD5 mismatch: expected %s, got %s"
                             % (expected_md5.strip().lower(), actual))
    if not raw:
        raise FlashError("the uploaded image is empty")
    if raw[:2] == b'PK' or raw[:6] in (b'070701', b'070702'):
        raise FlashError(
            "Gen-1 (ACE Pro) images are raw .bin files - this looks like "
            "an .swu package, which is ACE 2 only")
    entry = KNOWN_FIRMWARE.get(entry_id) or {}
    announce = entry.get("announce") or entry.get("version") \
        or DEFAULT_ANNOUNCE
    return FirmwareImage(raw, announce, crc16_mcrf4xx(raw), "raw binary",
                         entry_id)


def check_known(fw) -> None:
    """Refuse anything that is not byte-exactly a tested image of the
    selected entry. Raises FlashError; the dry run never calls this."""
    entry = KNOWN_FIRMWARE.get(fw.entry_id)
    if entry is None:
        raise FlashError("no tested Gen-1 image selected (got %r)"
                         % (fw.entry_id or '?'))
    if len(fw.data) != entry["size"] or fw.image_crc != entry["crc"]:
        raise FlashError(
            "image does not match the tested %s build (got %d bytes / "
            "CRC 0x%04X, expected %d / 0x%04X) - wrong file?"
            % (fw.entry_id, len(fw.data), fw.image_crc,
               entry["size"], entry["crc"]))
    if entry.get("md5") and fw.image_md5 != entry["md5"]:
        raise FlashError(
            "image MD5 does not match the tested %s build (got %s, "
            "expected %s) - wrong file?"
            % (fw.entry_id, fw.image_md5, entry["md5"]))


def _result_of(resp):
    if isinstance(resp, dict):
        r = resp.get("result")
        if isinstance(r, dict):
            return r
    return None


def _fw_version(get_info_resp):
    r = _result_of(get_info_resp)
    v = (r or {}).get("firmware")
    return str(v) if v else None


def flash(port: str, fw, progress,
          dry_run: bool = False, force: bool = False,
          image_error: str = '') -> dict:
    """Run the Gen-1 IAP sequence. `progress(pct, msg)` is called
    throughout (pct None = indeterminate). Returns a result dict; raises
    FlashError on anything that ends the update.

    A DRY RUN tests the CONNECTION first and treats the image as optional
    (`fw` may be None, `image_error` carries why it could not be parsed),
    exactly like ace2_ota.flash - the port + current version are worth
    confirming before fighting with an image.

    `force` is accepted for signature parity with ace2_ota.flash and is
    UNUSED: a Gen-1 version string does not identify an image, so there
    is no reliable same-version skip to bypass - every confirmed flash
    writes the whole image."""
    def _p(pct, msg):
        try:
            progress(pct, msg)
        except Exception:
            pass

    _p(None, "opening %s" % port)
    transport = Gen1Transport(port)
    try:
        _p(None, "waking the link (the unit resets when idle)")
        if not transport.prime():
            raise FlashError(
                "no response from the ACE on %s - is it powered, and was "
                "the port released (ACE_FW_RELEASE)?" % port)
        info = transport.rpc("get_info", timeout=T_RPC)
        cur_ver = _fw_version(info)
        if cur_ver:
            _p(None, "ACE reports version %s" % cur_ver)
        else:
            _p(None, "the ACE answered, but reports no version")
        iap = _result_of(transport.rpc("iap_version", timeout=T_RPC)) or {}

        if dry_run:
            out = {"ok": True, "dry_run": True, "current": cur_ver,
                   "connected": info is not None, "iap": iap}
            if fw is not None:
                out.update({"size": len(fw.data),
                            "crc": "0x%04X" % fw.image_crc,
                            "md5": fw.image_md5,
                            "source": fw.source, "image_ok": True})
            else:
                out.update({"image_ok": False,
                            "image_error": image_error})
            return out

        # From here it is a real flash - the image and its version are
        # required (a dry run never reaches this).
        if fw is None:
            raise FlashError(image_error or "no firmware image")
        # The release gate sits IN the flash path so no caller can forget
        # it: only byte-exactly tested images ever reach the wire.
        check_known(fw)
        if info is None:
            raise FlashError("ACE did not answer the version query - "
                             "not flashing blind")

        total = len(fw.data)
        n_chunks = (total + MAX_CHUNK - 1) // MAX_CHUNK

        _p(0.0, "announcing upgrade (size=%d crc=0x%04X version=%s)"
           % (total, fw.image_crc, fw.version))
        up = transport.rpc("iap_upgrade",
                           {"size": total, "crc": fw.image_crc,
                            "version": fw.version}, timeout=T_UPGRADE)
        if up is None:
            raise FlashError("no response to iap_upgrade - is the ACE "
                             "powered and on this port?")
        code = up.get("code")
        if code not in (None, 0):
            raise FlashError("iap_upgrade rejected: %s"
                             % (up.get("msg") or ("code=%s" % code)))

        t0 = time.time()
        for i in range(n_chunks):
            offset = i * MAX_CHUNK
            # The chunk stream itself is the keepalive: no idle gap long
            # enough for the ~3.3 s self-reset.
            transport.chunk(GEN1_FLASH_BASE + offset,
                            fw.data[offset:offset + MAX_CHUNK])
            time.sleep(CHUNK_PACE)
            if i % 16 == 0 or i + 1 == n_chunks:
                _p((i + 1) / n_chunks * 100.0,
                   "flashing chunk %d/%d" % (i + 1, n_chunks))
        _p(100.0, "image written in %.1fs" % (time.time() - t0))

        _p(100.0, "committing (iap_upgrade_finish) - ACE reboots")
        # The reply is optional: the commit reboots the unit, which can
        # cut the answer off - the bench flasher expects exactly that. The
        # cut can also surface as a serial error (the USB device vanishes
        # mid-read); that is the reboot, not a failure - the wait below
        # reopens the port and proves the result.
        try:
            transport.rpc("iap_upgrade_finish", {}, timeout=T_FINISH)
        except Exception as e:
            _p(100.0, "link dropped at commit (%s) - unit rebooting" % e)

        _p(None, "waiting for the ACE to come back")
        deadline = time.time() + BOOT_WAIT
        new_ver = None
        while time.time() < deadline:
            try:
                r = transport.rpc("get_info", timeout=2.0)
            except Exception:
                r = None
            if r is not None:
                new_ver = _fw_version(r)
                break
            # No answer: the commit rebooted the unit and an ACE Pro
            # re-enumerates on USB, so the fd polled before the commit is
            # dead. Reopen it before the next probe (a failed reopen must
            # not abort the wait) so a unit that is fine is actually heard.
            try:
                transport.reopen()
            except Exception:
                pass
            time.sleep(0.5)

        # get_info alone is not proof of life (a unit sitting in the
        # bootloader may answer it); get_status is the application probe.
        # Gen 1 has no documented bootloader/app split for these two, so
        # "no answer" is reported as unknown, never as healthy.
        alive = None
        if new_ver is not None:
            try:
                alive = transport.rpc("get_status", timeout=2.0) is not None
            except Exception:
                alive = None

        out = {"ok": True, "current": cur_ver, "new": new_ver,
               "verified": new_ver is not None, "app_alive": alive}
        if new_ver is None:
            out["note"] = ("flash finished but the unit was not heard "
                           "within %.0f s - if it does not come up, "
                           "re-flash a known image" % BOOT_WAIT)
            _p(None, "flashed, but the unit was not heard after the reboot")
        elif alive is False:
            out["note"] = (
                "%s is flashed and answers get_info but not get_status - "
                "it may be sitting in the bootloader; re-flash a known "
                "image." % new_ver)
            _p(100.0, "flashed, but no application response")
        return out
    finally:
        transport.close()
