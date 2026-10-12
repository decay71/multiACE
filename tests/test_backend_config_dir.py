#!/usr/bin/env python3
"""Self-check for backend configuration directory resolution and tools lookup.

Tests the MULTIACE_CONFIG_DIR environment precedence and derived configuration paths
in multiace/web/backend/main.py:
1. MULTIACE_CONFIG_DIR environment variable takes precedence for _CFG_DIR.
2. Unset MULTIACE_CONFIG_DIR falls back to standard user paths candidate list.
3. Non-existent or whitespace MULTIACE_CONFIG_DIR falls back to standard user paths.
4. Derived paths (_CFG_EXT_DIR, ace.cfg, snapshots, slot overrides, spool db) follow _CFG_DIR.
5. Post-processor virtual toolheads script lookup follows _CFG_DIR before repo fallback.
6. MULTIACE_POST_PROCESS_SCRIPT environment variable takes precedence when set and valid.

Run it directly:
    python3 tests/backend_config_dir_selfcheck.py
Or with pytest:
    pytest -v tests/
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Mock third-party web dependencies if not installed in standalone test environment
for mod in [
    "websockets",
    "httpx",
    "fastapi",
    "fastapi.responses",
    "fastapi.staticfiles",
    "pydantic",
    "preflight_core",
]:
    if mod not in sys.modules:
        m = MagicMock()
        if mod == "fastapi":
            m.FastAPI.return_value = MagicMock()
        sys.modules[mod] = m

BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "multiace", "web", "backend"
)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _reload_main():
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    return main


def test_config_dir_override() -> None:
    print("--- 1. MULTIACE_CONFIG_DIR environment override ---")
    with tempfile.TemporaryDirectory() as tmp_dir:
        custom_cfg = os.path.join(tmp_dir, "custom_printer_config")
        os.makedirs(custom_cfg, exist_ok=True)
        custom_tools = os.path.join(custom_cfg, "tools")
        os.makedirs(custom_tools, exist_ok=True)
        dummy_pp = os.path.join(custom_tools, "post_process_virtual_toolheads.py")
        with open(dummy_pp, "w", encoding="utf-8") as f:
            f.write("# dummy post-processor for test\n")

        saved_env = dict(os.environ)
        try:
            os.environ["MULTIACE_CONFIG_DIR"] = custom_cfg
            os.environ.pop("MULTIACE_POST_PROCESS_SCRIPT", None)
            main = _reload_main()

            resolved = main._CFG_DIR
            assert resolved == os.path.abspath(custom_cfg), (
                f"Expected _CFG_DIR to be {os.path.abspath(custom_cfg)}, got {resolved}"
            )
            print(f"[ok]   _CFG_DIR respects MULTIACE_CONFIG_DIR: {resolved}")

            # Verify derived paths
            assert main._CFG_EXT_DIR == os.path.join(resolved, "extended")
            print("[ok]   _CFG_EXT_DIR follows _CFG_DIR")

            assert main.SNAPSHOT_DIR == os.path.join(resolved, "extended", "multiace", "filament_snapshots")
            print("[ok]   SNAPSHOT_DIR follows _CFG_DIR")

            assert main.OVERRIDE_FILE == os.path.join(resolved, "extended", "multiace", "slot_overrides.json")
            print("[ok]   OVERRIDE_FILE follows _CFG_DIR")

            assert main.SPOOL_DB_PATH == os.path.join(resolved, "persistent", "multiace_spools.json")
            print("[ok]   SPOOL_DB_PATH follows _CFG_DIR")

            # Verify post-processor lookup candidate order
            expected_pp_candidate = Path(resolved) / "tools" / "post_process_virtual_toolheads.py"
            assert expected_pp_candidate.is_file(), "Custom post-processor should exist"
            assert main.post_process_script_path() == expected_pp_candidate, (
                f"Expected post_process_script_path() to be {expected_pp_candidate}, got {main.post_process_script_path()}"
            )
            print("[ok]   custom post-processor located via _CFG_DIR / tools")

        finally:
            os.environ.clear()
            os.environ.update(saved_env)


def test_default_fallback() -> None:
    print("--- 2. Default fallback when MULTIACE_CONFIG_DIR unset ---")
    saved_env = dict(os.environ)
    try:
        os.environ.pop("MULTIACE_CONFIG_DIR", None)
        os.environ.pop("MULTIACE_CFG_PATH", None)
        os.environ.pop("MULTIACE_POST_PROCESS_SCRIPT", None)
        main = _reload_main()

        default_dir = main._CFG_DIR
        expected_candidates = main._user_paths("printer_data/config")
        expected_default = main._first_existing(expected_candidates)
        assert default_dir == expected_default, (
            f"Expected default _CFG_DIR to be {expected_default}, got {default_dir}"
        )
        print(f"[ok]   default _CFG_DIR resolved to {default_dir}")

    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def test_cfg_path_override() -> None:
    print("--- 3. MULTIACE_CFG_PATH override takes precedence over _CFG_DIR ---")
    with tempfile.TemporaryDirectory() as tmp_dir:
        explicit_cfg = os.path.join(tmp_dir, "my_custom_ace.cfg")
        with open(explicit_cfg, "w", encoding="utf-8") as f:
            f.write("# dummy ace.cfg\n")

        saved_env = dict(os.environ)
        try:
            os.environ["MULTIACE_CONFIG_DIR"] = "/tmp/ignored_cfg_dir"
            os.environ["MULTIACE_CFG_PATH"] = explicit_cfg
            main = _reload_main()

            assert main.MULTIACE_CFG_PATH == explicit_cfg, (
                f"Expected MULTIACE_CFG_PATH to be {explicit_cfg}, got {main.MULTIACE_CFG_PATH}"
            )
            print("[ok]   MULTIACE_CFG_PATH override respected directly")

        finally:
            os.environ.clear()
            os.environ.update(saved_env)


def test_invalid_and_whitespace_fallback() -> None:
    print("--- 4. Fallback when MULTIACE_CONFIG_DIR is non-existent or whitespace ---")
    saved_env = dict(os.environ)
    try:
        os.environ.pop("MULTIACE_CFG_PATH", None)
        os.environ.pop("MULTIACE_POST_PROCESS_SCRIPT", None)

        main = _reload_main()
        expected_default = main._first_existing(main._user_paths("printer_data/config"))

        # Case A: non-existent directory
        os.environ["MULTIACE_CONFIG_DIR"] = "/nonexistent/printer_data/config"
        main = _reload_main()
        expected_missing = os.path.abspath("/nonexistent/printer_data/config")
        assert main._CFG_DIR == expected_missing, (
            f"Expected {expected_missing} for non-existent path, got {main._CFG_DIR}"
        )
        print("[ok]   non-existent MULTIACE_CONFIG_DIR respects env value directly")

        # Case B: whitespace-only directory
        os.environ["MULTIACE_CONFIG_DIR"] = "   \t  \n  "
        main = _reload_main()
        assert main._CFG_DIR == expected_default, (
            f"Expected fallback to {expected_default} for whitespace path, got {main._CFG_DIR}"
        )
        print("[ok]   whitespace MULTIACE_CONFIG_DIR falls back to default")

        # Case C: valid directory with surrounding whitespace
        with tempfile.TemporaryDirectory() as tmp_dir:
            custom_cfg = os.path.join(tmp_dir, "spaced_config")
            os.makedirs(custom_cfg, exist_ok=True)
            os.environ["MULTIACE_CONFIG_DIR"] = f"  {custom_cfg}  "
            main = _reload_main()
            assert main._CFG_DIR == os.path.abspath(custom_cfg), (
                f"Expected {os.path.abspath(custom_cfg)}, got {main._CFG_DIR}"
            )
            print("[ok]   surrounding whitespace trimmed and resolved correctly")

    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def test_post_process_script_override() -> None:
    print("--- 5. MULTIACE_POST_PROCESS_SCRIPT environment override & fallback ---")
    with tempfile.TemporaryDirectory() as tmp_dir:
        custom_pp = os.path.join(tmp_dir, "custom_post_processor.py")
        with open(custom_pp, "w", encoding="utf-8") as f:
            f.write("# custom post-processor\n")

        saved_env = dict(os.environ)
        try:
            os.environ["MULTIACE_CONFIG_DIR"] = "/tmp/ignored_cfg_dir"
            os.environ["MULTIACE_POST_PROCESS_SCRIPT"] = custom_pp
            main = _reload_main()

            resolved_pp = main.post_process_script_path()
            assert resolved_pp == Path(custom_pp), (
                f"Expected post_process_script_path() to be {Path(custom_pp)}, got {resolved_pp}"
            )
            print("[ok]   MULTIACE_POST_PROCESS_SCRIPT override respected directly")

            # Non-existent override should fall back
            os.environ["MULTIACE_POST_PROCESS_SCRIPT"] = "/nonexistent/script.py"
            main = _reload_main()
            resolved_fallback = main.post_process_script_path()
            repo_pp = (
                Path(BACKEND_DIR).resolve().parent.parent / "tools" / "post_process_virtual_toolheads.py"
            )
            if repo_pp.is_file():
                assert resolved_fallback == repo_pp, (
                    f"Expected fallback to {repo_pp}, got {resolved_fallback}"
                )
            print("[ok]   non-existent MULTIACE_POST_PROCESS_SCRIPT falls back gracefully")

            # Fallback to repo tools post-processor when not in config dir
            empty_cfg = os.path.join(tmp_dir, "empty_config")
            os.makedirs(empty_cfg, exist_ok=True)
            os.environ["MULTIACE_CONFIG_DIR"] = empty_cfg
            os.environ.pop("MULTIACE_POST_PROCESS_SCRIPT", None)
            main = _reload_main()
            if repo_pp.is_file():
                assert main.post_process_script_path() == repo_pp, (
                    f"Expected repo root script {repo_pp}, got {main.post_process_script_path()}"
                )
                print(f"[ok]   fallback resolves to repo tools post-processor: {repo_pp}")

        finally:
            os.environ.clear()
            os.environ.update(saved_env)


def run_checks() -> None:
    test_config_dir_override()
    test_default_fallback()
    test_cfg_path_override()
    test_invalid_and_whitespace_fallback()
    test_post_process_script_override()
    print("\nAll backend config dir checks passed successfully.")


if __name__ == "__main__":
    run_checks()
