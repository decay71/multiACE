"""The ace2k flasher in the web: tobecwb's ace2k_flash.pyz, vendored and run
unchanged, behind the firmware card.

The wrapper (ace2k_flasher.py) tells an ace2k image from a factory image,
gates the released versions and reads the tool's output; the flash route
releases the port, runs the tool and hands the port back (resume, or the
driver change + FIRMWARE_RESTART when the unit changes between stock and
ace2k). The tool itself is replaced by a stub here, except for the check
that the shipped file is the documented build and starts.
"""
import asyncio
import hashlib
import json
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

from .test_mock_mode import BACKEND, _load_main

sys.path.insert(0, str(BACKEND))
import ace2k_flasher as F  # noqa: E402


def _ace2k_image(version="0.14.0"):
    """A minimal ace2k image: vector table, a compressed Klipper
    dictionary, the crc32 of the body and the bootloader trailer."""
    head = struct.pack("<II", 0x20002000, 0x08008101)
    dictionary = {"config": {"ACE2K_VERSION": version}, "version": version}
    body = head + b"\0" * 24 + zlib.compress(json.dumps(dictionary).encode())
    return body + struct.pack("<I", zlib.crc32(body)) + F.TRAILER


def _factory_image():
    body = struct.pack("<II", 0x20002000, 0x08024101) + b"\x55" * 64
    return body + b"\0\0\0\0" + F.TRAILER


# ---- wrapper -----------------------------------------------------------

def test_classify_ace2k_factory_and_other(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(_ace2k_image("0.14.0"))
    got = F.classify_image(str(a))
    assert got["kind"] == "ace2k" and got["version"] == "0.14.0"
    assert got["md5"] == hashlib.md5(a.read_bytes()).hexdigest()
    f = tmp_path / "f.bin"
    f.write_bytes(_factory_image())
    assert F.classify_image(str(f))["kind"] == "factory"
    s = tmp_path / "x.swu"
    s.write_bytes(b"PK\x03\x04 not an image")
    assert F.classify_image(str(s))["kind"] == ""


def test_classify_ace2k_without_version(tmp_path):
    body = struct.pack("<II", 0x20002000, 0x08008101) + b"\0" * 40
    p = tmp_path / "old.bin"
    p.write_bytes(body + struct.pack("<I", zlib.crc32(body)) + F.TRAILER)
    got = F.classify_image(str(p))
    assert got["kind"] == "ace2k" and got["version"] == ""
    assert "no version" in got["error"]


def test_check_known(monkeypatch):
    monkeypatch.setitem(F.KNOWN_ACE2K, "0.14.0",
                        {"md5": "", "tested": ""})
    F.check_known({"version": "0.14.0", "md5": "x"})
    with pytest.raises(F.FlasherError):
        F.check_known({"version": "0.9.0", "md5": "x"})
    monkeypatch.setitem(F.KNOWN_ACE2K, "0.14.0",
                        {"md5": "abc", "tested": ""})
    with pytest.raises(F.FlasherError):
        F.check_known({"version": "0.14.0", "md5": "x"})
    F.check_known({"version": "0.14.0", "md5": "abc"})


def test_listed_releases_are_pinned():
    for ver, e in F.KNOWN_ACE2K.items():
        assert len(e["md5"]) == 32 and len(e["sha256"]) == 64, ver
        assert F.downloadable(ver)
    assert F.release_url("0.14.0") == ("https://github.com/tobecwb/ace2k/"
                                       "releases/download/v0.14.0/"
                                       "ace2k-0.14.0.bin")


def test_check_download(monkeypatch):
    data = b"image bytes"
    monkeypatch.setitem(F.KNOWN_ACE2K, "0.14.0", {
        "md5": "", "tested": "",
        "sha256": hashlib.sha256(data).hexdigest()})
    F.check_download("0.14.0", data)
    with pytest.raises(F.FlasherError, match="not the ace2k 0.14.0"):
        F.check_download("0.14.0", data + b"x")
    with pytest.raises(F.FlasherError, match="not offered"):
        F.check_download("0.9.0", data)
    monkeypatch.setitem(F.KNOWN_ACE2K, "0.13.0", {"md5": "", "tested": ""})
    assert not F.downloadable("0.13.0")


def test_calibration_files(tmp_path):
    name = "ace-calibration-8F3B521807B091083131334A-20261109-061557.txt"
    older = "ace-calibration-8F3B938707B091083131334A-20261108-101500.txt"
    (tmp_path / name).write_text("pairs")
    (tmp_path / older).write_text("pairs")
    (tmp_path / "notes.txt").write_text("x")
    files = F.calibration_files(str(tmp_path))
    assert [f["name"] for f in files] == [name, older]
    assert files[0]["uid"] == "8F3B521807B091083131334A"
    assert F.calibration_path(str(tmp_path), name) == str(tmp_path / name)
    for bad in ("notes.txt", "../" + name, "/etc/passwd", ""):
        assert F.calibration_path(str(tmp_path), bad) is None
    assert F.saved_calibration(
        ["x", "Calibration saved: /some/dir/" + name, "done"]) == name
    assert F.saved_calibration(["Calibration saved: /x/evil.sh"]) == ""
    # The unit key carries the same device id as three words.
    assert F.uid_of_key("uid:8f3b5218-07b09108-3131334a") == \
        "8F3B521807B091083131334A"
    assert F.uid_of_key("port:/dev/x") == ""


def test_command_lines():
    cmd = F.command("/i.bin", "/dev/p", "/cal", "http://m", version="1.1.31")
    assert cmd[:3] == [sys.executable, "-u", F.PYZ]
    assert cmd[3:] == ["/i.bin", "--port", "/dev/p", "--yes",
                       "--config-follows", "--calibration-dir", "/cal",
                       "--moonraker", "http://m", "--version", "1.1.31"]
    assert F.command("/i.bin", "/dev/p", "/cal", "http://m",
                     identify=True)[3:] == [
        "--identify", "--port", "/dev/p", "--moonraker", "http://m"]


def test_verdicts():
    ok = F.verdict(F.EXIT_OK, ["Klipper reports ace2k 0.14.0: done."])
    assert ok["verified"] and ok["proven"]
    un = F.verdict(F.EXIT_UNVERIFIED,
                   ["ace2k 0.14.0 answers and stored its proof: it stays.",
                    "The bootloader let go of the unit; ..."])
    assert un["verified"] is False and un["proven"] is True
    for code in (F.EXIT_FAILED, F.EXIT_UNCHANGED, F.EXIT_NO_ANSWER, 7):
        with pytest.raises(F.FlasherError):
            F.verdict(code, ["boom"])
    assert F.verdict(F.EXIT_OK, ["blinks"], identify=True)["connected"]
    assert F.verdict(F.EXIT_NO_BLINK, ["x"], identify=True)["connected"]
    assert not F.verdict(F.EXIT_NO_ANSWER, ["x"],
                         identify=True)["connected"]


def test_run_reads_progress_and_status(tmp_path):
    script = ("import sys\n"
              "print('Writing ace2k 0.14.0 ...')\n"
              "print('  64/128 chunks')\n"
              "print('  128/128 chunks', flush=True)\n"
              "sys.exit(2)\n")
    seen = []
    code, lines = asyncio.run(F.run(
        [sys.executable, "-c", script], str(tmp_path),
        lambda pct, line: seen.append((pct, line))))
    assert code == 2
    assert lines[-1].strip() == "128/128 chunks"
    assert seen[1][0] == 50.0 and seen[2][0] == 100.0


def test_shipped_tool_is_the_documented_build():
    assert F.available()
    assert F.pyz_sha256() == F.FLASHER_SHA256
    readme = (Path(F.PYZ).parent / "README.md").read_text(encoding="utf-8")
    assert F.FLASHER_SHA256 in readme and F.FLASHER_VERSION in readme
    out = subprocess.run([sys.executable, F.PYZ, "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "--config-follows" in out.stdout


# ---- the flash route ---------------------------------------------------

@pytest.fixture
def rig(monkeypatch, tmp_path):
    main = _load_main(monkeypatch, tmp_path, mock=False)
    monkeypatch.setattr(main, "_ACEFW_DIR", tmp_path / "acefw")
    monkeypatch.setattr(main, "MULTIACE_DATA_DIR", str(tmp_path / "data"))
    # The stub images are not the release files: no md5 pin here.
    monkeypatch.setattr(F, "KNOWN_ACE2K", {
        v: dict(e, md5="") for v, e in F.KNOWN_ACE2K.items()})

    class R:
        engine = "ota_v2"
        scripts = []
        posts = []
        code = 2
        lines = ["ace2k 0.14.0 answers and stored its proof: it stays."]
        cmds = []
    R.main = main

    async def fake_post(path, body=None, timeout=30.0):
        R.posts.append(path)
        if path == "/printer/gcode/script":
            R.scripts.append(body["script"])
        return {"result": "ok"}

    async def fake_state():
        return {}

    def fake_parse(_raw):
        held = any(s.startswith("ACE_FW_RELEASE") for s in R.scripts)
        return {"aces": [{"idx": 0, "flash_engine": R.engine,
                          "serial_path": "/dev/serial/by-path/u0",
                          "fw_hold": held}]}

    async def fake_run(cmd, cwd, progress, timeout=0, log=None):
        R.cmds.append(cmd)
        progress(None, R.lines[-1])
        return R.code, list(R.lines)

    monkeypatch.setattr(main, "_mr_post", fake_post)
    monkeypatch.setattr(main, "_query_state_gated", fake_state)
    monkeypatch.setattr(main, "_parse_state", fake_parse)
    monkeypatch.setattr(F, "run", fake_run)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        R.client = c
        yield R


def _upload(R, data, name="ace2k-0.14.0.bin"):
    r = R.client.post("/api/acefw/upload", files={"file": (name, data)})
    assert r.status_code == 200, r.text
    return r.json()


def _wait_done(R):
    for _ in range(200):
        st = R.client.get("/api/acefw/status").json()
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.02)
    raise AssertionError("flash did not finish")


def test_upload_reports_the_ace2k_image(rig):
    body = _upload(rig, _ace2k_image("0.14.0"))
    assert body["ace2k_image"] == {
        "version": "0.14.0", "known": True,
        "tested": F.KNOWN_ACE2K["0.14.0"]["tested"], "error": ""}
    assert _upload(rig, _factory_image(), "stock.bin")["ace2k_image"] is None


def test_versions_lists_ace2k(rig):
    v = rig.client.get("/api/acefw/versions").json()
    assert v["ace2k_flasher"]["version"] == F.FLASHER_VERSION
    assert v["ace2k_flasher"]["available"] is True
    assert {e["version"] for e in v["ace2k_versions"]} >= {"0.14.0"}


def test_stock_unit_to_ace2k_sets_driver_and_restarts(rig):
    _upload(rig, _ace2k_image("0.14.0"))
    r = rig.client.post("/api/acefw/flash", json={"ace": 0})
    assert r.status_code == 200, r.text
    assert r.json()["target"] == "ace2k"
    st = _wait_done(rig)
    assert st["state"] == "done", st
    assert st["result"]["engine"] == "ace2k"
    assert st["result"]["proven"] and st["result"]["restart"]
    assert rig.scripts == ["ACE_FW_RELEASE ACE=0",
                           "ACE_FW_SET_DRIVER ACE=0 DRIVER=ace2k"]
    assert rig.posts[-1] == "/printer/firmware_restart"
    cmd = rig.cmds[0]
    assert cmd[2] == F.PYZ and "--yes" in cmd and "--config-follows" in cmd
    assert cmd[cmd.index("--port") + 1] == "/dev/serial/by-path/u0"
    assert "--version" not in cmd


def test_ace2k_unit_update_resumes(rig):
    rig.engine = "ace2k"
    rig.code = 0
    rig.lines = ["Klipper reports ace2k 0.14.0: done."]
    _upload(rig, _ace2k_image("0.14.0"))
    assert rig.client.post("/api/acefw/flash",
                           json={"ace": 0}).status_code == 200
    st = _wait_done(rig)
    assert st["state"] == "done"
    assert rig.scripts == ["ACE_FW_RELEASE ACE=0", "ACE_FW_RESUME ACE=0"]
    assert "/printer/firmware_restart" not in rig.posts


def test_failed_flash_resumes_and_reports(rig):
    rig.code = 3
    rig.lines = ["A print is running or paused; nothing changed"]
    _upload(rig, _ace2k_image("0.14.0"))
    rig.client.post("/api/acefw/flash", json={"ace": 0})
    st = _wait_done(rig)
    assert st["state"] == "error"
    assert "nothing changed" in st["error"]
    assert rig.scripts[-1] == "ACE_FW_RESUME ACE=0"


def test_dry_run_identifies(rig):
    rig.code = 0
    rig.lines = ["The unit on /dev/x blinks its four LEDs (ace2k 0.13.0)."]
    rig.engine = "ace2k"
    _upload(rig, _ace2k_image("0.14.0"))
    rig.client.post("/api/acefw/flash", json={"ace": 0, "dry_run": True})
    st = _wait_done(rig)
    assert st["result"]["dry_run"] and st["result"]["connected"]
    assert "--identify" in rig.cmds[0]
    assert rig.scripts[-1] == "ACE_FW_RESUME ACE=0"


def test_refusals(rig):
    _upload(rig, _ace2k_image("0.9.0"))
    r = rig.client.post("/api/acefw/flash", json={"ace": 0})
    assert r.status_code == 400 and "released" in r.json()["detail"]
    rig.engine = "iap_v1"
    r = rig.client.post("/api/acefw/flash", json={"ace": 0})
    assert r.status_code == 400 and "ACE Pro" in r.json()["detail"]
    # A unit that runs ace2k only takes the factory firmware back.
    rig.engine = "ace2k"
    _upload(rig, _factory_image(), "ACE2_V1.1.31.bin")
    r = rig.client.post("/api/acefw/flash",
                        json={"ace": 0, "version": "1.1.60O"})
    assert r.status_code == 400 and "1.1.31" in r.json()["detail"]
    r = rig.client.post("/api/acefw/flash",
                        json={"ace": 0, "version": "1.1.31",
                              "patch_to_open": True})
    assert r.status_code == 400 and "ACE2-Open" in r.json()["detail"]
    assert rig.scripts == []


def test_route_choice(rig):
    route = rig.main._acefw_ace2k_route
    ace2k = {"kind": "ace2k"}
    factory = {"kind": "factory"}
    assert route("ota_v2", ace2k, False) == "ace2k"
    assert route("ace2k", ace2k, False) == "ace2k"
    assert route("ace2k", factory, False) == "factory"
    assert route("ace2k", {"kind": ""}, False) == "factory"
    assert route("ota_v2", factory, False) is None
    assert route("iap_v1", factory, False) is None
    assert route("none", ace2k, False) is None


# ---- download from GitHub and the calibration files ---------------------

class _FakeResponse:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def _fake_github(monkeypatch, main, content, urls):
    class Client:
        def __init__(self, *a, **kw):
            assert kw.get("follow_redirects") is True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            urls.append(url)
            return _FakeResponse(content)
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)


def test_download_stages_the_release(rig, monkeypatch):
    img = _ace2k_image("0.14.0")
    monkeypatch.setitem(F.KNOWN_ACE2K, "0.14.0", {
        "md5": "", "tested": "t", "sha256": hashlib.sha256(img).hexdigest()})
    urls = []
    _fake_github(monkeypatch, rig.main, img, urls)
    r = rig.client.post("/api/acefw/download_ace2k",
                        json={"version": "0.14.0"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert urls == [F.release_url("0.14.0")]
    assert body["name"] == "ace2k-0.14.0.bin" and body["downloaded"]
    assert body["ace2k_image"]["version"] == "0.14.0"
    assert body["ace2k_image"]["known"]
    assert (rig.main._ACEFW_DIR / "upload.bin").read_bytes() == img
    # The staged download flashes like an upload.
    assert rig.client.post("/api/acefw/flash",
                           json={"ace": 0}).status_code == 200
    assert _wait_done(rig)["state"] == "done"


def test_download_refusals(rig, monkeypatch):
    urls = []
    _fake_github(monkeypatch, rig.main, b"not the release", urls)
    r = rig.client.post("/api/acefw/download_ace2k",
                        json={"version": "0.9.0"})
    assert r.status_code == 400 and urls == []
    r = rig.client.post("/api/acefw/download_ace2k",
                        json={"version": "0.14.0"})
    assert r.status_code == 502 and "sha256" in r.json()["detail"]
    assert not (rig.main._ACEFW_DIR / "upload.bin").exists()


def test_versions_flag_downloadable(rig):
    v = rig.client.get("/api/acefw/versions").json()
    assert all(e["download"] for e in v["ace2k_versions"])


def test_calibration_routes(rig):
    name = "ace-calibration-8F3B521807B091083131334A-20261109-061557.txt"
    d = Path(rig.main.MULTIACE_DATA_DIR) / "ace2k-calibration"
    d.mkdir(parents=True)
    (d / name).write_text("calibration pairs")
    (d / "other.txt").write_text("secret")
    files = rig.client.get("/api/acefw/calibrations").json()["files"]
    assert [f["name"] for f in files] == [name]
    r = rig.client.get("/api/acefw/calibration/" + name)
    assert r.status_code == 200 and r.text == "calibration pairs"
    assert "attachment" in r.headers.get("content-disposition", "")
    assert rig.client.get(
        "/api/acefw/calibration/other.txt").status_code == 404


def test_flash_reports_the_saved_calibration(rig):
    name = "ace-calibration-8F3B521807B091083131334A-20261109-061557.txt"
    rig.lines = ["Calibration saved: /x/" + name,
                 "ace2k 0.14.0 answers and stored its proof: it stays."]
    _upload(rig, _ace2k_image("0.14.0"))
    rig.client.post("/api/acefw/flash", json={"ace": 0})
    st = _wait_done(rig)
    assert st["state"] == "done" and st["calibration"] == name
    # A new staging clears it.
    _upload(rig, _ace2k_image("0.14.0"))
    assert rig.client.get("/api/acefw/status").json()["calibration"] == ""
