"""ACE 2 Pro OTA firmware flasher - library form for the multiACE backend.

Based on hakimio's ACE 2 Pro OTA updater
(https://gist.github.com/hakimio/39c71fa7174e699c6470b7c79323b189),
reverse-engineered by him from the Kobra S1 gklib binary (v2.7.0.9).
Used as the basis with attribution, with hakimio's explicit permission
(2026-08-09: "Sure, feel free to use it in your project.").

Changes vs. the gist: the CLI layer (argparse, input() confirm, print
UI) is gone - this is a library the web backend drives. Progress goes
through a callback, errors are exceptions, the IAP sequence / framing /
CRC / protobuf and the .swu extraction logic are kept 1:1.

IAP sequence (hakimio):
  1. IAP_UPGRADE (cmd 2)  - announce size + CRC-16/Kermit + version
  2. IAP_FIRMWARE (cmd 3) - 64-byte chunks, each with its flash address
  3. IAP_FINISH (cmd 4)   - commit, ACE reboots into the new firmware
Flash base 0x08024000; the MCU verifies the CRC over the region.
"""

import logging
import hashlib
import io
import json
import os
import re
import struct
import tarfile
import time
import zipfile

# serial is imported LAZILY (in ACE2Transport) so a backend without
# pyserial can still parse a firmware file and report a clean "pyserial
# missing" instead of failing the whole module import - and load_image /
# guess_version need no serial at all (a dry run's file half must work
# even where the port half cannot).

BAUD              = 230400
PREAMBLE          = b'\xff\xaa'
END_MARKER        = 0xFE
FLAG_REQUEST      = 0x00

CMD_IAP_UPGRADE   = 2
CMD_IAP_FIRMWARE  = 3
CMD_IAP_FINISH    = 4
CMD_GET_INFO      = 7
# Application-only, read-only - the post-flash liveness probe.
CMD_GET_STATUS    = 6

ACE2_FLASH_BASE   = 0x08024000
MAX_FRAME_PAYLOAD = 100
CHUNK_SIZE        = 64

T_START   = 2.0
T_CHUNK   = 2.0
T_FINISH  = 5.0

# Known-good firmware images - the release gate, with no override in the
# web. Version string -> CRC-16/Kermit + size of the EXTRACTED binary. The
# web offers exactly these versions, and a flash is refused unless the
# uploaded file extracts to the EXACT tested bytes - a wrong file, foreign
# image or mangled extraction dies before a byte goes to the unit.
# Releasing a NEW version: run a DRY RUN with the new package (dry runs
# accept any file and report size + CRC + inner name), verify on hardware,
# then add the pair here. The concrete .swu name lives per entry ('swu')
# and the UI shows it next to the selected version, so the user has a
# searchable filename and nothing in the UI text can go stale.
KNOWN_FIRMWARE = {
    "1.1.31": {
        "crc": 0x91A8, "size": 71592,
        "swu": "ACE2_V1.1.31_20260306.swu",
        # MD5 of the EXTRACTED binary (not the .swu wrapper - the same
        # firmware can ship in different .swu packagings, the binary is
        # the constant and the thing that actually goes to the unit).
        "md5": "79fb22e7914bae1dc75ac91b30739c19",
        "source": "ACE2_V1.1.31_20260306.bin",
        "tested": "2026-08-09 dry-run vs a live V1.1.31 unit",
    },
    # ACE2-Open (Simon-CR), patched onto the tested stock V1.1.31 base by
    # apply_open_patch. 60O is the Open build to run: 61O does not come up,
    # 62O/63O left the presence word hanging after an aborted insert, older
    # builds lack the ghost-tag read gate and the dual-grab NOPs. The gate key reports
    # V1.1.60O while the unit is told 1.1.31 ("announce"), the base the
    # bootloader expects.
    # CRC is OURS (init 0xFFFF, what the ACE verifies), derived from Simon's
    # init-0 value: the CRC register is linear, so ours = his XOR
    # crc16(init=0xFFFF, zeros(result_size)).
    "1.1.60O": {
        "crc": 0xFB3A, "size": 79582,
        "announce": "1.1.31",
        "swu": "ACE2-Open.bin (backend patch on the V1.1.31 base)",
        "md5": "0426c316ab6cac90bc2b334293c4bcba",
        "source": "ACE2-Open.bin (patch.json commit 9fb5dbe)",
        "tested": "2026-09-19 flashed and running",
    },
}

# Open-firmware patch specs. Every target patches the SAME stock base.
PATCH_SPECS = {
    "1.1.60O": "ace2_open_60_patch.json",
}
# The PRESELECTION in the firmware tab. Set by hand, so adding a spec file
# never moves everyone's flash target by itself.
PATCH_TARGET = "1.1.60O"


def check_known(fw) -> None:
    """Refuse anything that is not byte-exactly a tested image of the
    chosen version. Raises FlashError; the dry run never calls this."""
    entry = KNOWN_FIRMWARE.get(_norm_ver(fw.version))
    if entry is None:
        raise FlashError("version %s is not on the tested-versions list"
                         % (fw.version or '?'))
    if len(fw.data) != entry["size"] or fw.image_crc != entry["crc"]:
        raise FlashError(
            "image does not match the tested %s build (got %d bytes / "
            "CRC 0x%04X, expected %d / 0x%04X) - wrong file?"
            % (fw.version, len(fw.data), fw.image_crc,
               entry["size"], entry["crc"]))
    if entry.get("md5") and fw.image_md5 != entry["md5"]:
        raise FlashError(
            "image MD5 does not match the tested %s build (got %s, "
            "expected %s) - wrong file?"
            % (fw.version, fw.image_md5, entry["md5"]))


_UID_PATCH_MAGIC = bytes([0x61, 0xA5, 0x63, 0x5A, 0x65, 0xA5, 0x32, 0x5A])


def apply_open_patch(base_data: bytes,
                     target: str = None) -> "FirmwareImage":
    """Turn a stock V1.1.31 image into ACE2-Open in pure Python - no ARM
    toolchain. Faithful port of firmware/apply_patch.py from
    github.com/Simon-CR/ace2-pro-firmware-research (MIT, (c) 2026
    Simon-CR); the spec files hold ONLY Simon's assembled bytes + offsets,
    no Anycubic firmware - the base image is supplied by the user's own
    upload.

    Deterministic and self-verifying: the base md5/size and every hook site
    are checked, so a wrong or already-patched base raises rather than
    producing a bad image. The returned FirmwareImage carries the target
    version so check_known gates the result byte-exact.

    The spec's own `version_string` (e.g. 'V1.1.60O') is what the unit will
    report, and every target keeps the trailing letter O: that is what
    ace._is_open_fw_idx and the web's open_fw flag key on to decide whether
    the RC522 tag features exist. A future numeric-only build would silently
    lose them, which is why the naming was worth agreeing with Simon."""
    target = target or PATCH_TARGET
    fname = PATCH_SPECS.get(target)
    if fname is None:
        raise FlashError("no patch spec for target %s" % target)
    spec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             fname)
    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            spec = json.load(f)
    except OSError as e:
        raise FlashError("patch spec missing (%s) - reinstall the web "
                         "backend" % e)
    img = bytearray(base_data)
    md5 = hashlib.md5(img).hexdigest()
    if md5 != spec["base_md5"] or len(img) != spec["base_size"]:
        raise FlashError(
            "the uploaded image is not the exact V1.1.31 base this patch "
            "targets (got %d bytes md5 %s, expected %d md5 %s). Upload the "
            "stock ACE2 V1.1.31 package."
            % (len(img), md5, spec["base_size"], spec["base_md5"]))
    if bytes(img[-8:]) != _UID_PATCH_MAGIC:
        raise FlashError("base image has no IAP magic trailer - not a "
                         "flashable ACE2 image")
    body = img[:-8]
    body += bytes.fromhex(spec["appended_hex"])
    for h in spec["hooks"]:
        off = h["file_offset"]
        expect = bytes.fromhex(h["expect_hex"])
        if bytes(body[off:off + len(expect)]) != expect:
            raise FlashError("patch hook site 0x%X unexpected (already "
                             "patched, or wrong base)" % off)
        body[off:off + len(expect)] = bytes.fromhex(h["replace_hex"])
    for p in spec["pokes"]:
        off = p["file_offset"]
        pk = bytes.fromhex(p["bytes_hex"])
        body[off:off + len(pk)] = pk
    out = bytes(body) + _UID_PATCH_MAGIC
    if len(out) != spec.get("result_size", len(out)):
        raise FlashError("patched image is %d bytes, the spec expects %d"
                         % (len(out), spec["result_size"]))
    out_md5 = hashlib.md5(out).hexdigest()
    # The md5 of a target whose KNOWN_FIRMWARE entry does not carry one yet is
    # only obtainable by building it once - log it so the entry can be filled
    # from a real build instead of a guess.
    logging.info("ace2 patch %s: %d bytes crc 0x%04X md5 %s",
                 target, len(out), crc16_kermit(out), out_md5)
    return FirmwareImage(out, target, crc16_kermit(out),
                         "ACE2-Open (patched)")


# Old name, kept so a stale caller does not break; always builds the current
# target.
apply_uid_patch = apply_open_patch


class FlashError(Exception):
    """Anything that ends the update - message is user-facing."""


def crc16_kermit(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def _varint(v: int) -> bytes:
    r = bytearray()
    while v > 0x7F:
        r.append((v & 0x7F) | 0x80)
        v >>= 7
    r.append(v & 0x7F)
    return bytes(r)


def _field_uint32(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


def _field_bytes(field: int, data: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(data)) + data


def _field_string(field: int, text: str) -> bytes:
    return _field_bytes(field, text.encode())


def encode_upgrade_request(size: int, image_crc: int, version: str) -> bytes:
    return _field_uint32(1, size) + _field_uint32(2, image_crc) \
        + _field_string(3, version)


def encode_firmware_request(address: int, chunk: bytes) -> bytes:
    return _field_uint32(1, address) + _field_bytes(2, chunk)


def build_packet(cmd: int, payload: bytes, seq: int) -> bytes:
    plen = len(payload)
    if plen > MAX_FRAME_PAYLOAD:
        raise FlashError(f"payload {plen} bytes exceeds frame limit")
    inner = bytearray([FLAG_REQUEST, seq & 0xFF, (seq >> 8) & 0xFF,
                       cmd & 0xFF, plen & 0xFF])
    inner.extend(payload)
    crc = crc16_kermit(bytes(inner))
    return bytes(PREAMBLE + inner + bytes([crc & 0xFF, (crc >> 8) & 0xFF,
                                           END_MARKER]))


def parse_response(buf: bytearray):
    while len(buf) >= 2:
        idx = buf.find(PREAMBLE)
        if idx < 0:
            return None, max(0, len(buf) - 1)
        if idx > 0:
            return None, idx
        if len(buf) < 10:
            return None, 0
        for end in range(9, min(len(buf), 270)):
            if buf[end] != END_MARKER:
                continue
            flags, seq = buf[2], buf[3] | (buf[4] << 8)
            cmd, plen = buf[5], buf[6]
            exp = 7 + plen + 2
            if end != exp:
                continue
            inner = bytes(buf[2:7 + plen])
            crc_recv = buf[7 + plen] | (buf[8 + plen] << 8)
            if crc_recv != crc16_kermit(inner):
                return None, end + 1
            return {
                "cmd": cmd,
                "is_resp": bool(flags & 0x80),
                "seq": seq,
                "payload": bytes(buf[7:7 + plen]),
            }, end + 1
        return None, 2 if len(buf) > 270 else 0
    return None, 0


def decode_varint(data: bytes, pos: int):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def pb_decode(data: bytes) -> dict:
    fields, pos = {}, 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        fnum, wtype = tag >> 3, tag & 7
        if wtype == 0:
            val, pos = decode_varint(data, pos)
        elif wtype == 2:
            ln, pos = decode_varint(data, pos)
            val = data[pos:pos + ln]; pos += ln
        elif wtype == 5:
            val = struct.unpack_from('<f', data, pos)[0]; pos += 4
        elif wtype == 1:
            val = struct.unpack_from('<d', data, pos)[0]; pos += 8
        else:
            break
        fields.setdefault(fnum, []).append((wtype, val))
    return fields


def get_field(fields: dict, num: int, default=0):
    return fields.get(num, [(0, default)])[0][1]


class ACE2Transport:
    def __init__(self, port: str):
        try:
            import serial
        except ImportError:
            raise FlashError(
                "pyserial is not installed in the web backend - run the "
                "multiACE installer again (it now pulls pyserial) or "
                "'pip3 install --user pyserial'")
        self.ser = serial.Serial(port, BAUD, timeout=0.1)
        self._seq = 0

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def _next_seq(self) -> int:
        self._seq = (self._seq % 0xFFFF) + 1
        return self._seq

    def send_recv(self, cmd: int, payload: bytes, timeout: float) -> list:
        seq = self._next_seq()
        pkt = build_packet(cmd, payload, seq)
        self.ser.reset_input_buffer()
        self.ser.write(pkt)
        self.ser.flush()
        buf = bytearray()
        results = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting:
                buf.extend(self.ser.read(self.ser.in_waiting))
                while len(buf) > 4:
                    p, n = parse_response(buf)
                    if n > 0:
                        buf = buf[n:]
                    else:
                        break
                    # Match on the command ID, not the 0x80 response
                    # flag: the BOOTLOADER answers with flags=1 instead of
                    # 0x81 (gist comment Simon-CR), so the flag
                    # filter made a healthy ACE in recovery mode look
                    # unresponsive. App-mode replies (flags=0x81) match
                    # unchanged; nothing echoes our own packets back, so
                    # the cmd match alone is unambiguous.
                    if p and p["cmd"] == cmd:
                        results.append(p)
                # EARLY RETURN - deviation from hakimio's original, which
                # collects until the FULL timeout although every caller
                # only ever uses the first matching response. Per 64-byte
                # chunk that meant waiting 2 s for a ~10 ms answer: 1119
                # chunks x 2 s = ~37 min for a flash that fits in under a
                # minute. A command that gets no
                # response (IAP_FINISH while the ACE reboots) still runs
                # to its timeout exactly as before.
                if results:
                    return results
            else:
                time.sleep(0.005)
        return results


def get_ace_version(transport: ACE2Transport):
    """(version, boot_version) or None."""
    results = transport.send_recv(CMD_GET_INFO, b'', timeout=2.0)
    for r in results:
        f = pb_decode(r["payload"])
        if 1 in f:
            version = get_field(f, 1, b'').decode(errors='replace')
            boot = get_field(f, 2, b'').decode(errors='replace')
            return version, boot
    return None


def ace_app_alive(transport: ACE2Transport):
    """True when the APPLICATION answers, False when nothing does, None
    when the probe itself failed.

    Why this exists: GET_INFO is the ONLY command the bootloader and the
    application both answer (Simon-CR, 2026-09-19), so a version string
    that matches after a flash proves the image is there and nothing
    about whether it runs. The 1.1.61O fault verified as OK three times
    in a row while the unit was mute for everything else. GET_STATUS is
    application-only and read-only, so it separates the two.

    TRAP, and it would be a destructive one: Simon numbers these from the
    firmware's own dispatch table (info 0x40, status 0x41, temp 0x42).
    Those are NOT the wire opcodes. On the wire 0x40 is GET_TEMP and
    0x41/0x42 are SET_DRY_POWER and SET_VALVE - WRITES. Always use our
    own Cmd numbering (ace_protocol_v2.Cmd), never his, when turning one
    of his command names into a frame.
    """
    try:
        return bool(transport.send_recv(CMD_GET_STATUS, b'', timeout=2.0))
    except Exception:
        return None


# --- firmware file parsing (hakimio, 1:1) ------------------------------

_ACE_KEYWORDS = ('ace', 'filament_hub', 'filament-hub')
_CPIO_MAGIC = (b'070701', b'070702')
_CPIO_HDR_LEN = 110


def _find_in_tar(tar_bytes: bytes, mode: str):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode=mode) as tf:
        for member in tf.getmembers():
            if member.isfile() and any(kw in member.name.lower()
                                       for kw in _ACE_KEYWORDS):
                f = tf.extractfile(member)
                if f:
                    return f.read(), member.name
    return None


def _extract_from_zip_swu(data: bytes, password):
    pwd_bytes = password.encode() if password else None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        tar_name = next(
            (n for n in zf.namelist()
             if n.endswith(('setup.tar.gz', 'setup.tar'))), None)
        if tar_name is None:
            return None
        tar_bytes = zf.read(tar_name, pwd=pwd_bytes)
        mode = 'r:gz' if tar_name.endswith('.gz') else 'r:'
        return _find_in_tar(tar_bytes, mode)


def _extract_from_cpio_swu(data: bytes):
    pos = 0
    while pos + _CPIO_HDR_LEN <= len(data):
        hdr = data[pos:pos + _CPIO_HDR_LEN]
        if hdr[:6] not in _CPIO_MAGIC:
            return None
        filesize = int(hdr[54:62], 16)
        namesize = int(hdr[94:102], 16)
        pos += _CPIO_HDR_LEN
        name = data[pos:pos + namesize - 1].decode(errors='replace')
        pos = (pos + namesize + 3) & ~3
        if name == 'TRAILER!!!':
            break
        file_data = data[pos:pos + filesize]
        pos = (pos + filesize + 3) & ~3
        if any(kw in name.lower() for kw in _ACE_KEYWORDS):
            return file_data, name
    return None


class FirmwareImage:
    def __init__(self, data: bytes, version: str, image_crc: int,
                 source: str = ''):
        self.data = data
        self.version = version
        self.image_crc = image_crc
        self.image_md5 = hashlib.md5(data).hexdigest()
        self.source = source


def load_image(path: str, version: str, expected_md5=None,
               swu_password=None) -> FirmwareImage:
    """Load + auto-extract the firmware (raw .bin / ZIP .swu / CPIO .swu).
    MD5 is checked over the RAW input file, matching gkapi."""
    with open(path, 'rb') as f:
        raw_bytes = f.read()
    if expected_md5:
        actual = hashlib.md5(raw_bytes).hexdigest()
        if actual.lower() != expected_md5.strip().lower():
            raise FlashError(f"MD5 mismatch: expected "
                             f"{expected_md5.strip().lower()}, got {actual}")
    image_data, source = raw_bytes, 'raw binary'
    if raw_bytes[:2] == b'PK':
        try:
            result = _extract_from_zip_swu(raw_bytes, swu_password)
        except RuntimeError as e:
            # zipfile raises RuntimeError on a wrong/missing ZIP password.
            raise FlashError(f"cannot decrypt .swu: {e}")
        if result is None:
            raise FlashError(
                "ZIP archive contains no ACE firmware (check the .swu "
                "password and that it is an ACE update package)")
        image_data, source = result
    elif raw_bytes[:6] in _CPIO_MAGIC:
        result = _extract_from_cpio_swu(raw_bytes)
        if result is None:
            raise FlashError("CPIO archive contains no ACE firmware entry")
        image_data, source = result
    return FirmwareImage(image_data, version, crc16_kermit(image_data),
                         source)


def _norm_ver(v: str) -> str:
    return (v or '').lstrip('Vv')


_VER_RE = re.compile(r'[vV]?(\d+\.\d+(?:\.\d+)?)')


def guess_version(*names) -> str:
    """Pull an X.Y[.Z] version out of a file name - the raw .bin carries
    no version the PROTOCOL reads (it is only the string announced to the
    ACE), but the name usually spells it: 'ACE2_V1.1.31.bin' -> the
    outer upload name, the inner archive entry, whatever is given. Empty
    when nothing matches (a nameless raw .bin still needs a typed value)."""
    for n in names:
        m = _VER_RE.search(str(n or ''))
        if m:
            return m.group(1)
    return ''


def probe_image(path: str, swu_password=None) -> dict:
    """Parse the firmware WITHOUT touching a serial port (the file half of
    a dry run): size, CRC, source, a version guessed from the names.
    Never raises - a locked .swu comes back as {ok: False, reason}."""
    try:
        fw = load_image(path, '', swu_password=swu_password)
    except FlashError as e:
        return {"ok": False, "reason": str(e),
                "version_guess": guess_version(path)}
    return {"ok": True, "size": len(fw.data),
            "crc": "0x%04X" % fw.image_crc, "source": fw.source,
            "version_guess": guess_version(fw.source, path)}


def flash(port: str, fw, progress,
          dry_run: bool = False, force: bool = False,
          image_error: str = '') -> dict:
    """Run the IAP sequence. `progress(pct, msg)` is called throughout
    (pct None = indeterminate). Returns a result dict; raises FlashError
    on anything that ends the update.

    A DRY RUN tests the CONNECTION first and treats the image as optional
    (`fw` may be None, `image_error` carries why it could not be parsed):
    the whole point of a dry run is to confirm the port + current version
    BEFORE fighting with a .swu password, so a locked archive must not
    stop it."""
    def _p(pct, msg):
        try:
            progress(pct, msg)
        except Exception:
            pass

    _p(None, "opening %s" % port)
    transport = ACE2Transport(port)
    try:
        _p(None, "querying current version")
        current = get_ace_version(transport)
        cur_ver = current[0] if current else None
        if current:
            _p(None, "ACE reports version %s (boot %s)" % current)
        else:
            _p(None, "no version response from the ACE")

        if dry_run:
            out = {"ok": True, "dry_run": True, "current": cur_ver,
                   "connected": current is not None}
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
        if not (fw.version or '').strip():
            raise FlashError("a target version is required to flash "
                             "(it is announced to the ACE)")
        # The release gate sits IN the flash path so no caller can forget
        # it: only byte-exactly tested images ever reach the wire.
        check_known(fw)
        if current is None:
            raise FlashError("ACE did not answer the version query - "
                             "not flashing blind")
        if _norm_ver(cur_ver) == _norm_ver(fw.version) and not force:
            return {"ok": True, "skipped": True, "current": cur_ver,
                    "target": fw.version}

        total = len(fw.data)
        n_chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE

        # The announced version can differ from the gate key: a modded
        # image (e.g. ACE2-Open, gate key V1.1.60O) announces the stock
        # 1.1.31 the bootloader expects. Stock entries have no "announce"
        # and fall back to fw.version, unchanged.
        _entry = KNOWN_FIRMWARE.get(_norm_ver(fw.version)) or {}
        announce_ver = _entry.get("announce", fw.version)
        _p(0.0, "announcing upgrade (size=%d crc=0x%04X version=%s)"
           % (total, fw.image_crc, announce_ver))
        payload = encode_upgrade_request(total, fw.image_crc, announce_ver)
        results = transport.send_recv(CMD_IAP_UPGRADE, payload,
                                      timeout=T_START)
        if not results:
            raise FlashError("no response to IAP_UPGRADE - is the ACE "
                             "powered and on this port?")
        code = get_field(pb_decode(results[0]["payload"]), 1, 0)
        if code != 0:
            raise FlashError("IAP_UPGRADE rejected: code=%s" % code)

        for i in range(n_chunks):
            offset = i * CHUNK_SIZE
            chunk = fw.data[offset:offset + CHUNK_SIZE]
            addr = ACE2_FLASH_BASE + offset
            results = transport.send_recv(
                CMD_IAP_FIRMWARE, encode_firmware_request(addr, chunk),
                timeout=T_CHUNK)
            if not results:
                raise FlashError("no response for chunk %d/%d "
                                 "(addr=0x%08X)" % (i + 1, n_chunks, addr))
            code = get_field(pb_decode(results[0]["payload"]), 1, 0)
            if code != 0:
                raise FlashError("chunk %d/%d rejected (addr=0x%08X, "
                                 "code=%s)" % (i + 1, n_chunks, addr, code))
            if i % 16 == 0 or i + 1 == n_chunks:
                _p((i + 1) / n_chunks * 100.0,
                   "flashing chunk %d/%d" % (i + 1, n_chunks))

        _p(100.0, "committing (IAP_FINISH) - ACE reboots")
        transport.send_recv(CMD_IAP_FINISH, b'', timeout=T_FINISH)

        # A test build may report another version than its key names
        # ("reports"), so the entry says what the unit will answer.
        expect_ver = _entry.get("reports", fw.version)
        _p(None, "waiting for the ACE to report %s" % expect_ver)
        deadline = time.time() + 30.0
        new_ver = None
        while time.time() < deadline:
            time.sleep(2.0)
            result = get_ace_version(transport)
            if result:
                new_ver = result[0]
                if _norm_ver(new_ver) == _norm_ver(expect_ver):
                    # The version alone is not proof of life - see
                    # ace_app_alive. Never downgrade "verified" over it:
                    # the image IS on the device either way, and the way
                    # out is another flash, not a power cycle.
                    alive = ace_app_alive(transport)
                    out = {"ok": True, "current": cur_ver, "new": new_ver,
                           "verified": True, "app_alive": alive}
                    if alive is False:
                        out["note"] = (
                            "%s is flashed and reports its version, but the "
                            "unit answers no application command - it is "
                            "mute. Flash another target (the picker offers "
                            "them); no power cycle is needed."
                            % (new_ver or fw.version))
                        _p(100.0, "flashed, but the unit answers no "
                                  "application command")
                    return out
        return {"ok": True, "current": cur_ver, "new": new_ver,
                "verified": False,
                "note": "flash completed but the version was not "
                        "confirmed within 30 s - check the unit"}
    finally:
        transport.close()
