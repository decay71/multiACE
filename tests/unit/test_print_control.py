"""Live print control: whitelisted verbs, server-side clamps, state readback.

Moonraker is replaced by stubs for `_mr_get` / `_mr_post`; every G-code line
the endpoint would send is captured instead.
"""
import pytest

from .test_mock_mode import _load_main, mock_client, live_client  # noqa: F401

CFG = {"result": {"status": {"configfile": {"settings": {
    "extruder": {"min_temp": 0, "max_temp": 300},
    "extruder1": {"min_temp": 0, "max_temp": 280},
    "heater_bed": {"min_temp": 0, "max_temp": 110}}}}}}


@pytest.fixture
def main(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path, mock=False)
    m._babystep_state.update(job=None, total=0.0)
    return m


@pytest.fixture
def printer(main, monkeypatch):
    """A stub printer: records the scripts, serves a status and config."""
    class P:
        sent = []
        status = {"toolhead": {"extruder": "extruder"},
                  "print_stats": {"state": "printing", "filename": "a.gcode"}}
        toolheads = []
    P.sent = []

    async def fake_get(path):
        if "configfile" in path:
            return CFG
        return {"result": {"status": P.status}}

    async def fake_post(path, body=None, timeout=30.0):
        assert path == "/printer/gcode/script"
        P.sent.append(body["script"])
        return {"result": "ok"}

    async def fake_state():
        return {}

    monkeypatch.setattr(main, "_mr_get", fake_get)
    monkeypatch.setattr(main, "_mr_post", fake_post)
    monkeypatch.setattr(main, "_query_state_gated", fake_state)
    monkeypatch.setattr(main, "_parse_state", lambda s: {"toolheads": P.toolheads})
    return P


@pytest.fixture
def client(main):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def post(client, verb, value=None):
    body = {"verb": verb}
    if value is not None:
        body["value"] = value
    return client.post("/api/print-control", json=body)


class TestRanges:
    @pytest.mark.parametrize("verb,value,script,applied", [
        ("speed", 120, "M220 S120", 120),
        ("speed", 5000, "M220 S300", 300),
        ("speed", 1, "M220 S25", 25),
        ("flow", 400, "M221 S125", 125),
        ("flow", 0, "M221 S75", 75),
        ("fan", 50, "M106 S128", 50),
        ("fan", 900, "M106 S255", 100),
        ("fan", -5, "M106 S0", 0),
    ])
    def test_value_is_clamped_before_it_becomes_gcode(
            self, client, printer, verb, value, script, applied):
        j = post(client, verb, value).json()
        assert printer.sent == [script]
        assert j["applied"] == applied and j["ok"] is True

    @pytest.mark.parametrize("verb", ["speed", "flow", "fan", "nozzle", "bed",
                                      "babystep"])
    def test_a_value_is_required(self, client, printer, verb):
        assert post(client, verb).status_code == 400
        assert printer.sent == []


class TestWhitelist:
    @pytest.mark.parametrize("verb", ["G28", "M104 S300", "gcode", "restart",
                                      "", "speed; M104 S400"])
    def test_unknown_verbs_send_nothing(self, client, printer, verb):
        assert post(client, verb, 1).status_code == 400
        assert printer.sent == []

    def test_a_script_field_is_not_a_way_in(self, client, printer):
        r = client.post("/api/print-control",
                        json={"verb": "pause", "script": "M104 S400"})
        assert r.status_code == 200 and printer.sent == ["PAUSE"]

    def test_job_verbs(self, client, printer):
        for verb, script in (("pause", "PAUSE"), ("resume", "RESUME"),
                             ("cancel", "CANCEL_PRINT")):
            printer.sent.clear()
            assert post(client, verb).json()["applied"] is None
            assert printer.sent == [script]

    def test_verb_is_case_and_space_insensitive(self, client, printer):
        post(client, " Pause ")
        assert printer.sent == ["PAUSE"]


class TestHeaters:
    def test_nozzle_is_clamped_to_the_klipper_config(self, client, printer):
        post(client, "nozzle", 999)
        assert printer.sent == ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=300"]

    def test_bed_uses_its_own_limit(self, client, printer):
        post(client, "bed", 200)
        assert printer.sent == ["SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=110"]

    def test_loaded_material_tightens_the_nozzle_ceiling(self, client, printer):
        printer.toolheads = [{"filament_detected": True, "material": "PLA"}]
        j = post(client, "nozzle", 300).json()
        assert j["applied"] == 230
        assert post(client, "nozzle", 0).json()["applied"] == 170

    def test_unloaded_heads_do_not_count(self, client, printer):
        printer.toolheads = [{"filament_detected": False, "material": "PLA"}]
        assert post(client, "nozzle", 300).json()["applied"] == 300

    def test_follows_the_active_extruder(self, client, printer):
        printer.status = {"toolhead": {"extruder": "extruder1"}}
        post(client, "nozzle", 999)
        assert printer.sent == ["SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=280"]

    def test_an_unknown_extruder_name_falls_back(self, client, printer):
        printer.status = {"toolhead": {"extruder": "extruder; M112"}}
        post(client, "nozzle", 100)
        assert printer.sent == ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=100"]


class TestBabystep:
    def test_one_press_is_capped(self, client, printer):
        j = post(client, "babystep", -5).json()
        assert j["applied"] == -0.05
        assert printer.sent == ["SET_GCODE_OFFSET Z_ADJUST=-0.050 MOVE=1"]

    def test_total_floor_refuses_and_sends_nothing(self, client, printer):
        for _ in range(10):
            assert post(client, "babystep", -0.05).json()["ok"] is True
        printer.sent.clear()
        j = post(client, "babystep", -0.01).json()
        assert j["ok"] is False and "re-level" in j["detail"]
        assert printer.sent == []

    def test_going_back_up_frees_the_allowance(self, client, printer):
        for _ in range(10):
            post(client, "babystep", -0.05)
        assert post(client, "babystep", 0.05).json()["ok"] is True
        assert post(client, "babystep", -0.05).json()["ok"] is True

    def test_new_job_resets_the_total(self, client, printer):
        for _ in range(10):
            post(client, "babystep", -0.05)
        printer.status = {"print_stats": {"state": "printing", "filename": "b.gcode"}}
        assert post(client, "babystep", -0.05).json()["total_mm"] == -0.05


class TestState:
    def test_readback(self, client, printer):
        printer.status = {
            "toolhead": {"extruder": "extruder"},
            "gcode_move": {"speed_factor": 1.25, "extrude_factor": 0.95,
                           "homing_origin": [0, 0, -0.1234, 0]},
            "fan": {"speed": 0.5},
            "extruder": {"temperature": 201.5, "target": 210},
            "heater_bed": {"temperature": 59.9, "target": 60},
            "virtual_sdcard": {"progress": 0.4321},
            "display_status": {"message": "hi"},
            "print_stats": {"state": "printing", "filename": "a.gcode"}}
        j = client.get("/api/print-control").json()
        assert j == {"speed_factor": 125.0, "extrude_factor": 95.0, "fan": 50.0,
                     "z_offset": -0.123, "nozzle": {"temp": 201.5, "target": 210},
                     "bed": {"temp": 59.9, "target": 60}, "progress": 43.2,
                     "message": "hi", "state": "printing", "filename": "a.gcode"}

    def test_an_empty_status_gives_defaults(self, client, printer):
        printer.status = {}
        j = client.get("/api/print-control").json()
        assert j["speed_factor"] == 100.0 and j["fan"] == 0.0
        assert j["nozzle"] == {"temp": None, "target": None}

    def test_limits(self, client, printer):
        j = client.get("/api/print-control/limits").json()
        assert j["ranges"]["flow"] == {"min": 75.0, "max": 125.0}
        assert j["nozzle"]["max"] == 300 and j["bed"]["max"] == 110
        assert j["babystep"] == {"step": 0.05, "total": 0.5, "used": 0.0}

    def test_moonraker_down_is_a_502(self, main, client, monkeypatch):
        import httpx

        async def boom(path):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(main, "_mr_get", boom)
        assert client.get("/api/print-control").status_code == 502


class TestMockMode:
    def test_state_comes_from_the_fixture(self, mock_client):
        j = mock_client.get("/api/print-control").json()
        assert j["mock"] is True and j["state"] == "standby"

    def test_verbs_never_reach_moonraker(self, main, mock_client, monkeypatch):
        # The mock_client app is a different import; patch that module.
        import sys
        m = sys.modules["main"]

        async def boom(*a, **k):
            raise AssertionError("mock mode must not contact Moonraker")
        monkeypatch.setattr(m, "_mr_post", boom)
        j = post(mock_client, "speed", 150).json()
        assert j["mock"] is True and j["script"] == "M220 S150"

    def test_query_param_cannot_enable_mock_on_a_real_printer(self, live_client):
        assert live_client.get("/api/print-control?mock=1").status_code in (200, 502)
        assert live_client.get("/api/print-control?mock=1").json().get("mock") is not True
