"""Stock ACE 2 firmware 1.1.34 on the tested list: flashable as stock, a
known state to flash FROM, and never a base for the ACE2-Open patch."""
import sys

import pytest

from .test_mock_mode import BACKEND

sys.path.insert(0, str(BACKEND))
import ace2_ota as O  # noqa: E402


def _image(size, version):
    data = bytes(size)
    return O.FirmwareImage(data, version, O.crc16_kermit(data), "test")


def test_entry_is_stock_only():
    e = O.KNOWN_FIRMWARE["1.1.34"]
    assert (e["size"], e["crc"]) == (72872, 0xC995)
    assert e["md5"] == "e6c8c971a1f186097ce2736a4928aa4c"
    assert "1.1.34" not in O.PATCH_SPECS
    assert "announce" not in e


def test_unit_on_1_1_34_is_a_known_flash_source():
    assert O.flash_source_known("V1.1.34")
    O.check_flash_source("V1.1.34")


def test_gate_refuses_other_bytes_under_that_version():
    with pytest.raises(O.FlashError):
        O.check_known(_image(72872, "1.1.34"))
    with pytest.raises(O.FlashError):
        O.check_known(_image(71592, "1.1.34"))


def test_open_patch_refuses_a_1_1_34_sized_base():
    with pytest.raises(O.FlashError):
        O.apply_open_patch(bytes(72872))


def test_versions_endpoint_lists_it_outside_the_patch_targets(
        monkeypatch, tmp_path):
    from .test_mock_mode import _load_main
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=False)
    v = TestClient(main.app).get("/api/acefw/versions").json()
    row = [e for e in v["versions"] if e["version"] == "1.1.34"]
    assert row and row[0]["swu"] == "ACE2_V1.1.34_20260430.bin"
    assert "1.1.34" not in {e["version"] for e in v["patch_targets"]}
