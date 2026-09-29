#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[2] / "web" / "backend"
sys.path.insert(0, str(BACKEND_DIR))
from i18n_path import resolve_i18n_dir  # noqa: E402


class I18nPathTests(unittest.TestCase):
    @staticmethod
    def _module_file(root: Path) -> Path:
        path = root / "multiace" / "web" / "backend" / "main.py"
        path.parent.mkdir(parents=True)
        path.touch()
        return path

    def test_managed_package_root_layout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiace-i18n-root-") as tmp:
            root = Path(tmp) / "multiace"
            catalog = root / "i18n"
            catalog.mkdir(parents=True)
            module_file = self._module_file(Path(tmp))

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MULTIACE_I18N_DIR", None)
                self.assertEqual(resolve_i18n_dir(module_file), catalog)

    def test_standalone_web_layout_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiace-i18n-web-") as tmp:
            root = Path(tmp) / "multiace"
            catalog = root / "web" / "i18n"
            catalog.mkdir(parents=True)
            module_file = self._module_file(Path(tmp))

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MULTIACE_I18N_DIR", None)
                self.assertEqual(resolve_i18n_dir(module_file), catalog)

    def test_explicit_override_wins(self) -> None:
        with tempfile.TemporaryDirectory(prefix="multiace-i18n-override-") as tmp:
            root = Path(tmp) / "multiace"
            root_catalog = root / "i18n"
            root_catalog.mkdir(parents=True)
            module_file = self._module_file(Path(tmp))
            override = Path(tmp) / "external-catalogs"

            with patch.dict(os.environ, {"MULTIACE_I18N_DIR": str(override)}):
                self.assertEqual(resolve_i18n_dir(module_file), override)


if __name__ == "__main__":
    unittest.main()
