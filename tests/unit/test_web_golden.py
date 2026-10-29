"""Web golden (design section 9, M3b/M4.6): the dashboard view the web
backend derives from a real V1/V2 fleet must not change while the backend
moves from protocol flags to the engine's capability block.

The fleet comes from the H0 harness (tests/units): the real ace.py against
fake ACE Pro / ACE 2 units on a virtual clock, snapshotted after connect.
Its get_status() is fed to main._parse_state as Moonraker would. The parsed
output is frozen in tests/fixtures/web_golden_<fleet>.json; the keys that
M3b adds (listed in ADDITIVE) are stripped before the compare and asserted
present separately, so a V1/V2 fleet renders exactly as before.

Refreeze only with intent:  UPDATE_WEB_GOLDENS=1 python3 -m pytest tests/unit/test_web_golden.py
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "multiace" / "web" / "backend"
FIXTURES = ROOT / "tests" / "fixtures"
UPDATE = os.environ.get("UPDATE_WEB_GOLDENS", "").strip().lower() in ("1", "true", "yes")

# Keys M3b adds to the parsed view (per ACE); stripped before the compare.
ADDITIVE_ACE = ("kind", "caps", "presence_sensor", "flash_engine",
                "has_dryer", "has_decoder", "has_humidity")


def _main(monkeypatch, tmp_path):
    cfg = tmp_path / "ace.cfg"
    cfg.write_text("[ace]\nace_device_count: 2\n", encoding="utf-8")
    monkeypatch.setenv("MULTIACE_CFG_PATH", str(cfg))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    monkeypatch.delenv("MULTIACE_MOCK_MODE", raising=False)
    monkeypatch.syspath_prepend(str(BACKEND))
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def _fleet_status(name):
    """get_status() of a connected harness fleet, JSON-safe."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    S = importlib.import_module("tests.units.scenarios")
    H = importlib.import_module("tests.units.harness")
    if name == "connect_mixed":
        def make(clock):
            return [S.unit_v1(clock, 'v1a', S.PATH_V1_A,
                              slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA',
                                         'brand': 'Anycubic'}}),
                    S.unit_v2(clock, 'v2a', S.PATH_V2_A,
                              slots={2: {'sku': 'AHPEWH-102', 'type': 'PETG',
                                         'color': (240, 240, 240)}},
                              uid=(0x8f3b9387, 0x07b09108, 0x3131334a))]
        h = S._harness(make, 2)
    elif name == "open_fw_fleet":
        # ACE2-Open unit beside a Gen-1 community-firmware ACE Pro: the
        # open_fw / tag_read / community_fw derivations of the web.
        def make(clock):
            return [S.unit_v1(clock, 'v1a', S.PATH_V1_A, firmware='CV1.3.871'),
                    S.unit_v2(clock, 'v2a', S.PATH_V2_A, firmware='V1.1.60O')]
        h = S._harness(make, 2)
    elif name == "head_mode_connect":
        def make(clock):
            return [S.unit_v1(clock, 'v1a', S.PATH_V1_A),
                    S.unit_v2(clock, 'v2a', S.PATH_V2_A)]
        h = S._harness(make, 2, save_vars={'ace__mode': 'head'})
    else:
        raise KeyError(name)
    h.build()
    h.ready()
    h.run(5.0)
    status = h.snapshot()
    h.finish()
    return json.loads(json.dumps(status, default=str, sort_keys=True))


def _strip(parsed):
    out = json.loads(json.dumps(parsed, sort_keys=True))
    for a in out.get("aces", []) or []:
        for k in ADDITIVE_ACE:
            a.pop(k, None)
    return out


@pytest.mark.parametrize("fleet", ["connect_mixed", "head_mode_connect", "open_fw_fleet"])
def test_parsed_view_of_a_v1_v2_fleet_is_frozen(monkeypatch, tmp_path, fleet):
    main = _main(monkeypatch, tmp_path)
    parsed = main._parse_state({"ace": _fleet_status(fleet)})
    path = FIXTURES / ("web_golden_%s.json" % fleet)
    stripped = _strip(parsed)
    if UPDATE or not path.exists():
        path.write_text(json.dumps(stripped, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    golden = json.loads(path.read_text(encoding="utf-8"))
    assert stripped == golden, "web view of a V1/V2 fleet changed"


def test_additive_keys_present_and_caps_drive_a_sensorless_unit(monkeypatch, tmp_path):
    main = _main(monkeypatch, tmp_path)
    parsed = main._parse_state({"ace": _fleet_status("connect_mixed")})
    for a in parsed["aces"]:
        for k in ADDITIVE_ACE:
            assert k in a, k
        assert isinstance(a["caps"], dict) and a["kind"] == a["protocol"]
        assert a["presence_sensor"] is True and a["has_dryer"] is True
    v2 = next(a for a in parsed["aces"] if a["protocol"] == "v2")
    v1 = next(a for a in parsed["aces"] if a["protocol"] == "v1")
    assert (v2["flash_engine"], v2["has_decoder"], v2["has_humidity"]) == ("ota_v2", True, True)
    assert (v1["flash_engine"], v1["has_decoder"], v1["has_humidity"]) == ("iap_v1", False, False)
    # A third kind with the caps a Klipper-MCU unit would carry: no presence
    # sensor, no dryer, no flash engine. Its lanes say so and are not loadable.
    status = _fleet_status("connect_mixed")
    unit = json.loads(json.dumps(status["aces"][0]))
    unit.update({"idx": 2, "protocol": "kmcu", "kind": "kmcu", "connected": True,
                 "firmware": "ace2k 0.1 [klipper]",
                 "caps": {"presence_sensor": False, "has_dryer": False,
                          "has_decoder": False, "has_humidity": False,
                          "flash_engine": "none", "rc522_tunnel": False,
                          "gen1_tunnel": False, "tag_read": False,
                          "community_fw": ""}})
    status["aces"].append(unit)
    status["device_count"] = 3
    parsed = main._parse_state({"ace": status})
    k = next(a for a in parsed["aces"] if a["protocol"] == "kmcu")
    assert k["presence_sensor"] is False and k["has_dryer"] is False
    assert k["flash_engine"] == "none" and k["open_fw"] is False and k["tag_read"] is False
    assert {s["state"] for s in k["slots"]} == {"nosensor"}
    # the V1/V2 units of the same status are untouched by the neighbour
    for a in parsed["aces"]:
        if a["protocol"] != "kmcu":
            assert all(s["state"] != "nosensor" for s in a["slots"])
