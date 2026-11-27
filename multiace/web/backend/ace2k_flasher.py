"""Run the ace2k flasher of tobecwb (vendor/ace2k_flash.pyz) for the web.

ace2k (https://github.com/tobecwb/ace2k, GPL-3.0) is a Klipper firmware
for the ACE 2 Pro. Its flasher writes an ace2k image or the factory image,
finds out by itself what runs on the unit (factory firmware, recovery or
ace2k), saves the unit's factory calibration before the first write and
checks the new image's link proof afterwards. The web backend runs that
tool unchanged as a subprocess; vendor/README.md names the build.

This module only:
- tells an ace2k image from a raw factory image (classify_image),
- gates the ace2k versions the web offers (check_known),
- builds the command line and reads the tool's output: the chunk counter
  becomes the progress, the exit status and the last lines the result.

The port comes from Klipper as for the other flash engines: ACE_FW_RELEASE
closes it and holds every reconnect path, so the tool finds a free port
and takes its "no Klipper on this port" route (for a unit running ace2k:
ace2k_bootloader_enter over the unit's own cable).
"""

import asyncio
import hashlib
import json
import os
import re
import struct
import sys
import zlib

FLASHER_VERSION = "0.14.0"
FLASHER_SHA256 = (
    "290a47cc677b9000eaf6d7edc5bcefb8947a0659ca71b5367865560dfc91547b")
PYZ = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "vendor", "ace2k_flash.pyz")

# ace2k images the web offers. The image carries its version in its Klipper
# dictionary (ACE2K_VERSION) and its own checksum; md5, once known, pins
# the exact release file. 'tested' with a date = run on our hardware.
# sha256 is the one in the release's SHA256SUMS; it gates the download
# from GitHub (a version without one is upload-only).
KNOWN_ACE2K = {
    "0.16.0": {"md5": "8861965c0e56c76a9e5d3a165f7aaa80",
               "sha256": "fb94d890dfba34cdf7d5003f0800a3debcb9086d579190d952"
                         "aa582d9882fedd",
               "tested": ""},
    # Pre-release of tobecwb: the dryer learns its heater's rate (for units
    # whose dryer stopped with 'fault (response)' on 230 V). Flashing it
    # erases the unit's config page.
    "0.17.0-alpha.1": {"md5": "6d017f0ce98622b61a7724db7ef74f8c",
                       "sha256": "f014b23f1f12a159c1107b74c7a54dd71f52ac07c0"
                                 "19fe5c77b4b39d056caccc",
                       "tested": ""},
}

# The release assets of tobecwb's repository: one image per version under
# a fixed name. Only versions on the list above are fetched, and the bytes
# must match the listed sha256.
RELEASE_URL = ("https://github.com/tobecwb/ace2k/releases/download/"
               "v{v}/ace2k-{v}.bin")
DOWNLOAD_MAX_BYTES = 512 * 1024
DOWNLOAD_TIMEOUT_S = 60.0

# The tool names a saved calibration "ace-calibration-<UID>-<stamp>.txt";
# the UID is the unit's 12-byte device id in hex.
CALIBRATION_NAME = re.compile(
    r"^ace-calibration-([0-9A-F]{24})-(\d{8}-\d{6})\.txt$")
_CALIBRATION_SAVED = re.compile(r"Calibration saved: (\S+)")

# The only factory image the web writes back over ace2k: stock V1.1.31.
# ACE2-Open is a second step with the ACE 2 flasher once the unit runs the
# factory firmware again.
FACTORY_VERSION = "1.1.31"

# The bootloader's trailer at the end of every image for the unit
# (factory and ace2k alike, firmware/tools/ace2k_flash/image.py).
TRAILER = bytes.fromhex("61a5635a65a5325a")

# Exit statuses of the tool (its flasher.py docstring).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNVERIFIED = 2
EXIT_UNCHANGED = 3
EXIT_NO_ANSWER = 4
EXIT_NO_BLINK = 5

RUN_TIMEOUT_S = 900.0
_CHUNKS = re.compile(r"(\d+)/(\d+) chunks")
_TAIL_LINES = 12


class FlasherError(Exception):
    pass


def available() -> bool:
    return os.path.isfile(PYZ)


_sha_cache = {}


def pyz_sha256() -> str:
    """sha256 of the vendored tool, cached per (size, mtime)."""
    try:
        st = os.stat(PYZ)
    except OSError:
        return ""
    key = (st.st_size, st.st_mtime)
    if _sha_cache.get("key") != key:
        h = hashlib.sha256()
        with open(PYZ, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        _sha_cache.update({"key": key, "sha": h.hexdigest()})
    return _sha_cache["sha"]


def check_tool() -> None:
    """Refuse a missing or changed vendored tool."""
    if not available():
        raise FlasherError("the ace2k flasher is not installed (%s)" % PYZ)
    if pyz_sha256() != FLASHER_SHA256:
        raise FlasherError("the ace2k flasher %s does not match the shipped "
                           "build (sha256 %s)" % (PYZ, pyz_sha256()[:12]))


def _embedded_dictionary(body: bytes):
    """The Klipper data dictionary compiled into an ace2k image, or None."""
    for m in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", body):
        try:
            raw = zlib.decompressobj().decompress(body[m.start():])
        except zlib.error:
            continue
        if raw.startswith(b"{"):
            try:
                return json.loads(raw)
            except ValueError:
                continue
    return None


def classify_image(path: str) -> dict:
    """Tell what the staged file is: an ace2k image ('ace2k', with its
    version), a raw factory image ('factory') or neither ('', e.g. a .swu
    package, which the ACE 2 flasher unpacks at flash time). The tool
    checks the image again before it writes anything."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"kind": "", "error": str(e)}
    out = {"kind": "", "size": len(data),
           "md5": hashlib.md5(data).hexdigest()}
    if len(data) < 20 or data[-8:] != TRAILER:
        return out
    body = data[:-12]
    stored = struct.unpack_from("<I", data, len(data) - 12)[0]
    if stored != zlib.crc32(body):
        out["kind"] = "factory"
        return out
    out["kind"] = "ace2k"
    out["crc32"] = "0x%08X" % stored
    d = _embedded_dictionary(body)
    cfg = d.get("config") if isinstance(d, dict) else None
    ver = cfg.get("ACE2K_VERSION") if isinstance(cfg, dict) else None
    out["version"] = ver if isinstance(ver, str) else ""
    if not out["version"]:
        out["error"] = ("this ace2k image carries no version (built before "
                        "ace2k v0.12)")
    return out


def check_known(image: dict) -> None:
    """Refuse an ace2k image the web does not offer."""
    ver = image.get("version") or ""
    entry = KNOWN_ACE2K.get(ver)
    if entry is None:
        raise FlasherError("ace2k %s is not on the list of released "
                           "images" % (ver or "?"))
    if entry.get("md5") and image.get("md5") != entry["md5"]:
        raise FlasherError("this file is not the ace2k %s release (md5 %s)"
                           % (ver, image.get("md5", "?")))


def command(image_path, port, calibration_dir, moonraker,
            version=None, identify=False) -> list:
    """The tool's command line. -u: unbuffered, so its lines (and the
    chunk counter) reach the web while it runs, not only at its exit. No
    -I/-s: pyserial may live in the user site of the Klipper user
    (HOME=/home/lava on the U1)."""
    cmd = [sys.executable, "-u", PYZ]
    if identify:
        return cmd + ["--identify", "--port", port, "--moonraker", moonraker]
    cmd += [image_path, "--port", port, "--yes", "--config-follows",
            "--calibration-dir", calibration_dir, "--moonraker", moonraker]
    if version:
        cmd += ["--version", version]
    return cmd


def release_url(version: str) -> str:
    return RELEASE_URL.format(v=version)


def downloadable(version: str) -> bool:
    entry = KNOWN_ACE2K.get(version or "")
    return bool(entry and entry.get("sha256"))


def check_download(version: str, data: bytes) -> None:
    """The fetched bytes are the listed release of this version: right
    sha256, and an ace2k image that names this version."""
    entry = KNOWN_ACE2K.get(version or "")
    if not entry or not entry.get("sha256"):
        raise FlasherError("ace2k %s is not offered for download"
                           % (version or "?"))
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry["sha256"]:
        raise FlasherError("the downloaded file is not the ace2k %s release "
                           "(sha256 %s)" % (version, digest[:16]))


def saved_calibration(lines) -> str:
    """File name of the calibration the tool saved in this run, or ''."""
    for line in reversed(list(lines or [])):
        m = _CALIBRATION_SAVED.search(line)
        if m:
            name = os.path.basename(m.group(1))
            if CALIBRATION_NAME.match(name):
                return name
    return ""


def calibration_files(directory: str) -> list:
    """The saved calibrations, newest first: name, uid, stamp, size."""
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        m = CALIBRATION_NAME.match(name)
        if not m:
            continue
        try:
            size = os.path.getsize(os.path.join(directory, name))
        except OSError:
            continue
        out.append({"name": name, "uid": m.group(1), "stamp": m.group(2),
                    "size": size})
    out.sort(key=lambda e: e["stamp"], reverse=True)
    return out


def prune_calibrations(directory: str) -> dict:
    """Keep one calibration per unit: the OLDEST file of a UID is the
    factory backup, a later file with the same bytes is a copy of it and
    is removed. A later file that differs stays (the calibration changed).
    Returns {removed name: kept name}."""
    by_uid = {}
    for e in calibration_files(directory):
        by_uid.setdefault(e["uid"], []).append(e)
    removed = {}
    for entries in by_uid.values():
        entries.sort(key=lambda e: e["stamp"])
        keep = entries[0]
        try:
            with open(os.path.join(directory, keep["name"]), "rb") as fh:
                ref = fh.read()
        except OSError:
            continue
        for e in entries[1:]:
            path = os.path.join(directory, e["name"])
            try:
                with open(path, "rb") as fh:
                    same = fh.read() == ref
                if same:
                    os.remove(path)
                    removed[e["name"]] = keep["name"]
            except OSError:
                continue
    return removed


def calibration_path(directory: str, name: str):
    """Path of one saved calibration, or None for any other name (the
    name must be the tool's own pattern: no directories, no other
    files)."""
    if not CALIBRATION_NAME.match(name or ""):
        return None
    path = os.path.join(directory, name)
    return path if os.path.isfile(path) else None


def uid_of_key(key: str) -> str:
    """The calibration UID of a unit key 'uid:aaaaaaaa-bbbbbbbb-cccccccc'
    (the device id as three little-endian words, the tool writes the same
    words in order), or ''."""
    if not (key or "").startswith("uid:"):
        return ""
    words = key[4:].split("-")
    if len(words) != 3 or not all(len(w) == 8 for w in words):
        return ""
    return "".join(words).upper()


def calibration_dir(data_dir: str) -> str:
    """Where the factory calibration files go: persistent, not the overlay."""
    path = os.path.join(data_dir, "ace2k-calibration")
    os.makedirs(path, exist_ok=True)
    return path


async def run(cmd, cwd, progress, timeout=RUN_TIMEOUT_S, log=None) -> tuple:
    """Run the tool, feed each output line to progress(pct, line) and
    return (exit status, all lines)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines = []
    pct = None

    async def _read():
        nonlocal pct
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if not line.strip():
                continue
            lines.append(line)
            if log is not None:
                log(line)
            m = _CHUNKS.search(line)
            if m and int(m.group(2)):
                pct = 100.0 * int(m.group(1)) / int(m.group(2))
            progress(pct, line.strip())
        return await proc.wait()

    try:
        code = await asyncio.wait_for(_read(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FlasherError("the ace2k flasher did not finish within %d s"
                           % timeout)
    return code, lines


def _tail(lines) -> list:
    return [l.strip() for l in lines[-_TAIL_LINES:]]


def verdict(code, lines, identify=False) -> dict:
    """Turn the tool's exit status into the result the web shows. Raises
    FlasherError for a failure, with the tool's last lines."""
    if identify:
        if code in (EXIT_OK, EXIT_NO_BLINK):
            return {"connected": True, "current": lines[-1].strip()
                    if lines else "", "lines": _tail(lines)}
        if code == EXIT_NO_ANSWER:
            return {"connected": False, "current": "", "lines": _tail(lines)}
        raise FlasherError(" | ".join(_tail(lines)[-3:]) or
                           "ace2k flasher exit %s" % code)
    if code == EXIT_OK:
        return {"verified": True, "proven": True, "lines": _tail(lines)}
    if code == EXIT_UNVERIFIED:
        # The image is written. Proven: it answered with its version over
        # its own link and stays. Else Klipper's connect proves it, within
        # the image's window (180 s) after its start.
        proven = any("stored its proof" in l for l in lines)
        return {"verified": False, "proven": proven, "lines": _tail(lines)}
    what = {EXIT_UNCHANGED: "refused, nothing changed",
            EXIT_NO_ANSWER: "nothing answered on the port",
            EXIT_FAILED: "failed"}.get(code, "exit %s" % code)
    msg = " | ".join(_tail(lines)[-3:])
    raise FlasherError("ace2k flasher: %s%s" % (what, (": " + msg)
                                                 if msg else ""))
