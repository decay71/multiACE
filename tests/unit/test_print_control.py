"""Live print control: whitelisted verbs, server-side clamps, state readback.

Moonraker is replaced by stubs for `_mr_get` / `_mr_post`; every G-code line
the endpoint would send is captured instead.
"""
import os
import time

import httpx
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
        gets = []
        post_error = None
    P.sent = []
    P.gets = []
    P.post_error = None

    async def fake_get(path):
        P.gets.append(path)
        if "configfile" in path:
            return CFG
        return {"result": {"status": P.status}}

    async def fake_post(path, body=None, timeout=30.0):
        assert path == "/printer/gcode/script"
        if P.post_error:
            raise P.post_error
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
        printer.toolheads = [{"idx": 0, "filament_detected": True, "material": "PLA"}]
        j = post(client, "nozzle", 300).json()
        assert j["applied"] == 230
        assert post(client, "nozzle", 100).json()["applied"] == 170

    @pytest.mark.parametrize("value", [0, -10])
    def test_zero_is_off_whatever_the_material_floor(self, client, printer, value):
        printer.toolheads = [{"idx": 0, "filament_detected": True, "material": "PLA"}]
        assert post(client, "nozzle", value).json()["applied"] == 0
        assert printer.sent == ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0"]

    def test_unloaded_heads_do_not_count(self, client, printer):
        printer.toolheads = [{"idx": 0, "filament_detected": False, "material": "PLA"}]
        assert post(client, "nozzle", 300).json()["applied"] == 300

    def test_other_heads_materials_do_not_count(self, client, printer):
        # PLA on the active head 0 and PETG on head 1: the range is PLA's
        # 170-230, not the 210-230 overlap of both.
        printer.toolheads = [
            {"idx": 0, "filament_detected": True, "material": "PLA"},
            {"idx": 1, "filament_detected": True, "material": "PETG"}]
        assert post(client, "nozzle", 100).json()["applied"] == 170
        assert post(client, "nozzle", 999).json()["applied"] == 230

    def test_mixed_materials_cannot_invert_the_range(self, client, printer):
        # PLA (max 230) next to PC (min 240) used to give 240/230 and land
        # every target on 240.
        printer.toolheads = [
            {"idx": 0, "filament_detected": True, "material": "PLA"},
            {"idx": 1, "filament_detected": True, "material": "PC"}]
        assert post(client, "nozzle", 200).json()["applied"] == 200

    def test_the_active_head_decides(self, client, printer):
        printer.status = {"toolhead": {"extruder": "extruder1"}}
        printer.toolheads = [
            {"idx": 0, "filament_detected": True, "material": "PLA"},
            {"idx": 1, "filament_detected": True, "material": "PETG"}]
        assert post(client, "nozzle", 100).json()["applied"] == 210
        assert printer.sent == ["SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=210"]

    def test_klipper_config_is_read_once_not_on_every_call(self, client, printer):
        post(client, "nozzle", 100)
        post(client, "bed", 50)
        assert sum("configfile" in g for g in printer.gets) == 1

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


class TestLight:
    HAS_LED = {"toolhead": {"extruder": "extruder"},
               "led cavity_led": {"color_data": [[0.0, 0.0, 0.0, 0.0]]}}

    @pytest.mark.parametrize("value,script,applied", [
        (100, "SET_LED LED=cavity_led WHITE=1.00 TRANSMIT=1", 100),
        (0, "SET_LED LED=cavity_led WHITE=0.00 TRANSMIT=1", 0),
        (40, "SET_LED LED=cavity_led WHITE=0.40 TRANSMIT=1", 40),
        (900, "SET_LED LED=cavity_led WHITE=1.00 TRANSMIT=1", 100),
        (-5, "SET_LED LED=cavity_led WHITE=0.00 TRANSMIT=1", 0),
    ])
    def test_value_is_clamped_to_a_percentage(
            self, client, printer, value, script, applied):
        printer.status = self.HAS_LED
        j = post(client, "light", value).json()
        assert printer.sent == [script] and j["applied"] == applied

    def test_a_printer_without_the_led_is_refused_and_sends_nothing(
            self, client, printer):
        printer.status = {"toolhead": {"extruder": "extruder"}}
        r = post(client, "light", 100)
        assert r.status_code == 409 and printer.sent == []

    def test_a_value_is_required(self, client, printer):
        printer.status = self.HAS_LED
        assert post(client, "light").status_code == 400

    def test_readback_without_a_white_channel_is_unknown(self, main):
        assert main._chamber_light_pct(
            {"led cavity_led": {"color_data": [[1.0, 0.5, 0.0]]}}) is None
        assert main._chamber_light_pct({}) is None

    def test_limits_offer_the_light_range(self, client, printer):
        assert client.get("/api/print-control/limits").json()["ranges"]["light"] \
            == {"min": 0.0, "max": 100.0}


class TestQueued:
    def test_a_read_timeout_is_queued_not_an_error(self, client, printer):
        # A swap holds Klipper's queue: the command runs later, so a second
        # press must not look like a retry of a failure.
        printer.post_error = httpx.ReadTimeout("slow")
        r = post(client, "pause")
        assert r.status_code == 200
        assert r.json()["queued"] is True and r.json()["ok"] is True

    def test_a_normal_answer_is_not_queued(self, client, printer):
        assert "queued" not in post(client, "pause").json()

    def test_a_connect_timeout_is_still_a_failure(self, client, printer):
        # Never reached Moonraker: nothing was queued.
        printer.post_error = httpx.ConnectTimeout("no route")
        assert post(client, "pause").status_code == 502

    def test_other_transport_errors_are_failures(self, client, printer):
        printer.post_error = httpx.ConnectError("down")
        assert post(client, "pause").status_code == 502

    def test_a_queued_babystep_keeps_its_allowance_used(self, client, printer):
        printer.post_error = httpx.ReadTimeout("slow")
        j = post(client, "babystep", -0.05).json()
        assert j["queued"] is True and j["total_mm"] == -0.05


class TestHomingGate:
    """While ace.py's homing flag is fresh, the status poll must not reach
    Moonraker: that I/O competes with the homing probe and bed mesh."""

    @pytest.fixture
    def homing(self, main, tmp_path, monkeypatch):
        flag = tmp_path / "homing"
        monkeypatch.setattr(main, "HOMING_FLAG_PATH", str(flag))
        monkeypatch.setattr(main, "_GATE_WAIT_MAX", 0.0)

        def on():
            flag.write_text("1")
            os.utime(flag, None)

        def off():
            if flag.exists():
                flag.unlink()
        return type("H", (), {"on": staticmethod(on), "off": staticmethod(off)})

    def test_the_last_status_is_served_while_homing(self, client, printer, homing):
        printer.status = {"print_stats": {"state": "printing", "filename": "a.gcode"}}
        assert client.get("/api/print-control").json()["state"] == "printing"
        polls = len(printer.gets)
        printer.status = {"print_stats": {"state": "paused"}}
        homing.on()
        assert client.get("/api/print-control").json()["state"] == "printing"
        assert len(printer.gets) == polls

    def test_polling_resumes_when_the_flag_goes_stale(
            self, client, printer, homing):
        printer.status = {"print_stats": {"state": "printing"}}
        client.get("/api/print-control")
        printer.status = {"print_stats": {"state": "paused"}}
        homing.off()
        assert client.get("/api/print-control").json()["state"] == "paused"

    def test_klipper_config_is_not_queried_while_homing(
            self, client, printer, homing):
        homing.on()
        post(client, "bed", 200)
        assert not any("configfile" in g for g in printer.gets)

    def test_a_stale_cache_is_not_served_forever(
            self, main, client, printer, homing, monkeypatch):
        client.get("/api/print-control")
        main._LAST_PRINT_STATUS_TS = time.time() - 3600
        printer.status = {"print_stats": {"state": "paused"}}
        homing.on()
        assert client.get("/api/print-control").json()["state"] == "paused"


class TestState:
    def test_readback(self, client, printer):
        printer.status = {
            "toolhead": {"extruder": "extruder"},
            "gcode_move": {"speed_factor": 1.25, "extrude_factor": 0.95,
                           "homing_origin": [0, 0, -0.1234, 0]},
            "fan": {"speed": 0.5},
            "led cavity_led": {"color_data": [[0.0, 0.0, 0.0, 0.75]]},
            "extruder": {"temperature": 201.5, "target": 210},
            "heater_bed": {"temperature": 59.9, "target": 60},
            "virtual_sdcard": {"progress": 0.4321},
            "display_status": {"message": "hi"},
            "print_stats": {"state": "printing", "filename": "a.gcode"}}
        j = client.get("/api/print-control").json()
        assert j == {"speed_factor": 125.0, "extrude_factor": 95.0, "fan": 50.0,
                     "z_offset": -0.123, "light": 75.0, "nozzle": {"temp": 201.5, "target": 210},
                     "bed": {"temp": 59.9, "target": 60}, "progress": 43.2,
                     "message": "hi", "state": "printing", "filename": "a.gcode"}

    def test_an_empty_status_gives_defaults(self, client, printer):
        printer.status = {}
        j = client.get("/api/print-control").json()
        assert j["speed_factor"] == 100.0 and j["fan"] == 0.0
        assert j["nozzle"] == {"temp": None, "target": None}
        assert j["light"] is None

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
