"""Exercise firmware inspection with synthetic images and no hardware."""
import base64
import hashlib
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

BACKEND = Path(__file__).resolve().parents[2] / "multiace" / "web" / "backend"
IMAGE = b"fake ACE firmware"
# ZipCrypto-protected setup.tar.gz containing ACE2_test.bin (IMAGE).
ENCRYPTED_SWU = base64.b64decode(
    "UEsDBBQAAQAAAO4FeF2j74AciAAAAHwAAAAMAAAAc2V0dXAudGFyLmd6zhr2gL1RoK5944Wx"
    "q1Y1HUiDTqWUiEPO4JJqGlsZXD5lm9PI5jG9C5BgYqK25lk8tpm5OVYTPs6+wumSQB4S8Qi6"
    "Hiz1nV8WfqvKyGx6nMHELdmRsYuY1GjHYpp1iVXy7m8VAd6oYnPR1K83P2T12Jt6b5uv8d6t"
    "AjyU3sTh37eSoPeCCkkmJ1BLAQI/ABQAAQAAAO4FeF2j74AciAAAAHwAAAAMACQAAAAAAAAA"
    "IAAAAAAAAABzZXR1cC50YXIuZ3oKACAAAAAAAAEAGAC6nqIM6HvdAQAAAAAAAAAAAAAAAAAA"
    "AABQSwUGAAAAAAEAAQBeAAAAsgAAAAAA"
)


class FirmwareInspectionTest(unittest.TestCase):
    def setUp(self):
        staging = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(sys, "path", [str(BACKEND), *sys.path]))
        main = importlib.import_module("main")
        self.main = main
        ota = importlib.import_module("ace2_ota")
        self.enterContext(patch.object(main, "_ACEFW_DIR", staging))
        self.enterContext(patch.object(main, "_acefw", {"state": "idle"}))
        self.enterContext(patch.dict(ota.KNOWN_FIRMWARE, {
            "1.1.31": {
                "md5": hashlib.md5(IMAGE).hexdigest(),
                "size": len(IMAGE),
                "crc": ota.crc16_kermit(IMAGE),
            },
        }, clear=True))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def upload(self, data, filename):
        response = self.client.post("/api/acefw/upload", files={"file": (filename, data)})
        self.assertEqual(response.status_code, 200, response.text)
        return {key: response.json()[key] for key in ("upload_id", "md5")}

    def inspect(self, payload):
        return self.client.post("/api/acefw/inspect", json=payload)

    def test_known_binary(self):
        response = self.inspect(self.upload(IMAGE, "renamed.bin"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"version": "1.1.31"})

    def test_unknown_binary(self):
        response = self.inspect(self.upload(b"other image", "unknown.bin"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"version": ""})

    def test_encrypted_swu_password(self):
        upload = self.upload(ENCRYPTED_SWU, "encrypted.swu")
        for password in (None, "wrong-password"):
            with self.subTest(password=password):
                response = self.inspect({**upload, "password": password})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["password_required"])

        response = self.inspect({**upload, "password": "test-password"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"version": "1.1.31"})

    def test_stale_upload(self):
        previous = self.upload(IMAGE, "first.bin")
        self.upload(b"second image", "second.bin")
        response = self.inspect(previous)
        self.assertEqual(response.status_code, 409, response.text)

    def test_open_release_download_checks_integrity_and_stages(self):
        tag = "v1.1.61O-test"
        filename = "ACE2-Open-test.bin"
        digest = hashlib.sha256(IMAGE).hexdigest()
        releases = {tag: (filename, len(IMAGE), digest)}
        async_client = httpx.AsyncClient

        def mock_client(**kwargs):
            return async_client(transport=httpx.MockTransport(download), **kwargs)

        def download(request):
            self.assertEqual(
                str(request.url),
                f"https://github.com/Simon-CR/ace2-pro-firmware-research/releases/download/{tag}/{filename}",
            )
            return httpx.Response(200, content=data)

        with patch.object(self.main, "_ACEFW_OPEN_RELEASES", releases), patch.object(
            self.main.httpx, "AsyncClient", mock_client
        ):
            invalid = self.client.post("/api/acefw/download_open", json={"version": "v0"})
            self.assertEqual(invalid.status_code, 400)
            data = b"x" * len(IMAGE)
            corrupted = self.client.post("/api/acefw/download_open", json={"version": tag})
            self.assertEqual(corrupted.status_code, 502)
            self.assertFalse((self.main._ACEFW_DIR / "upload.bin").exists())
            data = IMAGE
            staged = self.client.post("/api/acefw/download_open", json={"version": tag})
            self.assertEqual(staged.status_code, 200, staged.text)
            self.assertEqual(staged.json()["open_release"], tag)
            self.assertEqual(staged.json()["upload_id"], digest)
            flash = self.client.post("/api/acefw/flash", json={"ace": 0, "upload_id": digest})
            self.assertEqual(flash.status_code, 400)
            self.assertIn("hardware validation", flash.json()["detail"])
