#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


PAXX_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PAXX_DIR))
import build_package  # noqa: E402


class PackageTests(unittest.TestCase):
    def test_manifest_is_self_consistent(self) -> None:
        manifest = json.loads(
            (PAXX_DIR / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["release"]["asset_prefix"], "multiace-paxx-")
        self.assertIn("{version}", manifest["runtime"]["versioned_app_root"])
        for relative in manifest["payload"]:
            self.assertTrue(
                (build_package.ROOT / relative.rstrip("/")).exists(), relative)
        self.assertIn("install_multiace.sh", manifest["excluded_from_package"])
        self.assertIn("uninstall_multiace.sh", manifest["excluded_from_package"])

    def test_mount_sources_are_allowlisted(self) -> None:
        manifest = json.loads(
            (PAXX_DIR / "manifest.json").read_text(encoding="utf-8"))
        payload = set(manifest["payload"])
        for mount in manifest["klipper_mounts"]:
            source = mount["source"]
            self.assertTrue(
                source in payload or any(
                    item.rstrip("/") == source
                    or source.startswith(item.rstrip("/") + "/")
                    for item in payload if item.endswith("/")
                ), source)

    def test_package_contains_only_managed_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiace-paxx-test-") as tmp:
            output = Path(tmp) / "multiace.tar.gz"
            archive_path, digest = build_package.build(output)
            checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            self.assertEqual(checksum, digest)
            with tarfile.open(archive_path, "r:gz") as archive:
                names = {member.name for member in archive.getmembers()}
            self.assertTrue(any(name.endswith("/paxx/manifest.json") for name in names))
            self.assertFalse(any(name.endswith("/install_multiace.sh") for name in names))
            self.assertFalse(any(name.endswith("/uninstall_multiace.sh") for name in names))
            self.assertFalse(any("/tools/" in name for name in names))
            self.assertFalse(any("/deploy/" in name for name in names))

    def test_package_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiace-paxx-deterministic-") as tmp:
            first = Path(tmp) / "first.tar.gz"
            second = Path(tmp) / "second.tar.gz"
            _, first_digest = build_package.build(first)
            _, second_digest = build_package.build(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_digest, second_digest)


if __name__ == "__main__":
    unittest.main()
