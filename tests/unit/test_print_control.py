"""Live print control: whitelisted verbs, server-side clamps, state readback.

Moonraker is replaced by stubs for `_mr_get` / `_mr_post`; every G-code line
the endpoint would send is captured instead.
"""
import asyncio
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
    m._babystep_state.update(total=0.0)
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

    @pytest.mark.parametrize("material", ["PLA", "PETG", "PC", "PLA-CF", "FOO", None])
    def test_the_loaded_material_does_not_lower_the_ceiling(
            self, client, printer, material):
        # Stock loads and unloads above a material's printing range (PLA loads
        # at 250), so heating up to clear a clog must work with any spool in.
        printer.toolheads = [{"idx": 0, "filament_detected": True, "material": material}]
        assert post(client, "nozzle", 250).json()["applied"] == 250
        assert post(client, "nozzle", 999).json()["applied"] == 300

    @pytest.mark.parametrize("value", [150, 200, 100, 1])
    def test_a_target_below_any_printing_range_is_not_raised(
            self, client, printer, value):
        printer.toolheads = [{"idx": 0, "filament_detected": True, "material": "PC"}]
        assert post(client, "nozzle", value).json()["applied"] == value
        assert printer.sent[-1] == f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={value}"

    @pytest.mark.parametrize("value", [0, -10])
    def test_zero_is_off(self, client, printer, value):
        assert post(client, "nozzle", value).json()["applied"] == 0
        assert printer.sent == ["SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0"]

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

    def test_the_limits_endpoint_reports_klippers_range_only(self, client, printer):
        j = client.get("/api/print-control/limits").json()
        assert j["nozzle"] == {"min": 0.0, "max": 300.0}
        assert j["bed"] == {"min": 0.0, "max": 110.0}


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

    @staticmethod
    def _state(main, state):
        asyncio.run(main._on_status_update([{"print_stats": {"state": state}}]))

    def _use_up_the_allowance(self, client):
        for _ in range(10):
            post(client, "babystep", -0.05)

    def test_a_print_start_resets_the_total(self, main, client, printer):
        self._state(main, "printing")
        self._use_up_the_allowance(client)
        assert post(client, "babystep", -0.01).json()["ok"] is False
        self._state(main, "complete")
        self._state(main, "printing")          # the same file again
        assert post(client, "babystep", -0.05).json()["total_mm"] == -0.05

    def test_a_pause_and_resume_is_the_same_print(self, main, client, printer):
        self._state(main, "printing")
        self._use_up_the_allowance(client)
        self._state(main, "paused")
        self._state(main, "printing")
        assert post(client, "babystep", -0.01).json()["ok"] is False

    def test_a_cancelled_print_then_a_new_one_starts_over(self, main, client, printer):
        self._state(main, "printing")
        self._use_up_the_allowance(client)
        self._state(main, "cancelled")
        self._state(main, "standby")
        self._state(main, "printing")
        assert post(client, "babystep", -0.05).json()["ok"] is True

    def test_the_total_is_kept_between_prints_until_one_starts(self, main, client, printer):
        # Idle stepping accumulates too: the floor protects the plate then as well.
        self._use_up_the_allowance(client)
        assert post(client, "babystep", -0.01).json()["ok"] is False

    def test_the_filename_plays_no_part(self, main, client, printer):
        printer.status = {"print_stats": {"state": "printing", "filename": "a.gcode"}}
        self._use_up_the_allowance(client)
        printer.status = {"print_stats": {"state": "printing", "filename": "b.gcode"}}
        assert post(client, "babystep", -0.01).json()["ok"] is False


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


DD_ON = {"main_enable": True, "clean_bed": {"enable": True},
         "noodle": {"enable": True}, "residue": {"enable": False},
         "nozzle": {"enable": False}}
LED_ON = {"color_data": [[0.0, 0.0, 0.0, 1.0]]}


def light_status(state, detection=DD_ON):
    st = {"toolhead": {"extruder": "extruder"}, "led cavity_led": LED_ON,
          "print_stats": {"state": state}}
    if detection is not None:
        st["defect_detection"] = detection
    return st


class TestLightAndDefectDetection:
    """Stock's AI camera check needs the chamber light, which stock turns on
    itself when it starts, so the light stays on while it is watching."""

    @pytest.mark.parametrize("state", ["printing", "paused"])
    @pytest.mark.parametrize("value", [0, 40, 99])
    def test_dimming_or_switching_off_is_refused_during_a_print(
            self, client, printer, state, value):
        printer.status = light_status(state)
        r = post(client, "light", value)
        assert r.status_code == 409 and "defect detection" in r.json()["detail"]
        assert printer.sent == []

    @pytest.mark.parametrize("state", ["printing", "paused"])
    def test_keeping_it_fully_on_is_fine(self, client, printer, state):
        printer.status = light_status(state)
        assert post(client, "light", 100).status_code == 200
        assert printer.sent == ["SET_LED LED=cavity_led WHITE=1.00 TRANSMIT=1"]

    @pytest.mark.parametrize("state", ["standby", "complete", "cancelled", "error"])
    def test_outside_a_print_it_can_be_switched_off(self, client, printer, state):
        printer.status = light_status(state)
        assert post(client, "light", 0).status_code == 200

    @pytest.mark.parametrize("detection", [
        {**DD_ON, "main_enable": False},                       # master switch off
        {"main_enable": True, "clean_bed": {"enable": False},  # no check enabled
         "noodle": {"enable": False}, "residue": {"enable": False},
         "nozzle": {"enable": False}},
        None,                                                  # no such object
        "garbage",
    ])
    def test_without_detection_the_light_is_free_during_a_print(
            self, client, printer, detection):
        printer.status = light_status("printing", detection)
        assert post(client, "light", 0).status_code == 200

    @pytest.mark.parametrize("detection,expected", [
        (DD_ON, True),
        ({**DD_ON, "main_enable": False}, False),
        ({"main_enable": True}, False),
        (None, None),
        ("garbage", None),
    ])
    def test_state_reports_whether_detection_is_on(
            self, client, printer, detection, expected):
        printer.status = light_status("printing", detection)
        assert client.get("/api/print-control").json()["detection"] is expected


OFF_NOW = ("SET_PURIFIER_MODE MODE=0 EXHAUST_FAN_DELAY_OFF=0 "
           "INNER_FAN_DELAY_OFF=0")
PURIFIER_OBJ = {
    "power_detected": True, "mode": 0,
    "exhaust_fan": {"speed": 0.5, "delay": 180, "speed_threshold": 0},
    "inner_fan": {"speed": 0.25, "delay": 180, "speed_threshold": 0}}
CAVITY = {"temperature": 31.4, "measured_min_temp": 18.0, "measured_max_temp": 33.0}


class TestEnclosure:
    def test_attached_enclosure_reports_temperature_and_fans(self, client, printer):
        printer.status = {"purifier": PURIFIER_OBJ,
                          "temperature_sensor cavity": CAVITY}
        assert client.get("/api/print-control").json()["enclosure"] == {
            "temp": 31.4, "attached": True, "exhaust": 50.0, "inner": 25.0,
            "mode": 0, "desired_temp": None}

    def test_no_enclosure_still_reads_the_chamber_temperature(self, client, printer):
        # The cavity sensor is part of the printer; power_detected says
        # whether an enclosure is attached.
        printer.status = {
            "purifier": {**PURIFIER_OBJ, "power_detected": False,
                         "exhaust_fan": {"speed": 0.0}, "inner_fan": {"speed": 0.0}},
            "temperature_sensor cavity": CAVITY}
        assert client.get("/api/print-control").json()["enclosure"] == {
            "temp": 31.4, "attached": False, "exhaust": 0.0, "inner": 0.0,
            "mode": 0, "desired_temp": None}

    def test_a_printer_with_only_the_sensor(self, client, printer):
        printer.status = {"temperature_sensor cavity": CAVITY}
        assert client.get("/api/print-control").json()["enclosure"] == {
            "temp": 31.4, "attached": None, "exhaust": None, "inner": None,
            "mode": None, "desired_temp": None}

    def test_a_printer_with_neither_has_no_enclosure(self, client, printer):
        printer.status = {"toolhead": {"extruder": "extruder"}}
        assert client.get("/api/print-control").json()["enclosure"] is None

    def test_malformed_objects_do_not_crash_the_readback(self, client, printer):
        printer.status = {"purifier": {"exhaust_fan": None, "inner_fan": "x"},
                          "temperature_sensor cavity": {}}
        j = client.get("/api/print-control").json()["enclosure"]
        assert j["temp"] is None and j["attached"] is False
        assert j["exhaust"] == 0.0 and j["inner"] == 0.0

    def test_enclosure_off_sends_the_stock_off_command(self, client, printer):
        printer.status = {"purifier": PURIFIER_OBJ}
        j = post(client, "enclosure_off").json()
        assert printer.sent == [OFF_NOW]
        assert j["ok"] is True and j["applied"] is None

    def test_enclosure_off_ignores_any_value(self, client, printer):
        printer.status = {"purifier": PURIFIER_OBJ}
        post(client, "enclosure_off", 3)
        assert printer.sent == [OFF_NOW]

    def test_a_printer_without_the_purifier_is_refused_and_sends_nothing(
            self, client, printer):
        printer.status = {"toolhead": {"extruder": "extruder"}}
        r = post(client, "enclosure_off")
        assert r.status_code == 409 and printer.sent == []

    def test_the_mode_and_its_target_temperature_are_read_back(self, client, printer):
        printer.status = {"purifier": {**PURIFIER_OBJ, "mode": 1,
                                       "desired_chamber_temp": 42}}
        j = client.get("/api/print-control").json()["enclosure"]
        assert j["mode"] == 1 and j["desired_temp"] == 42

    def test_there_is_no_way_to_pick_a_mode_or_name_a_fan(self, client, printer):
        printer.status = {"purifier": PURIFIER_OBJ}
        for verb in ("enclosure_on", "enclosure_mode", "purifier", "exhaust_fan",
                     "inner_fan", "fan=exhaust"):
            assert post(client, verb, 100).status_code == 400
        assert printer.sent == []


class TestEnclosureFans:
    """Direct fan speed: only with an enclosure attached; outside a print also
    only while no mode runs."""
    IDLE = {"purifier": PURIFIER_OBJ, "print_stats": {"state": "standby"}}

    @pytest.mark.parametrize("fan,value,script,applied", [
        ("exhaust", 50, "SET_PURIFIER FAN=exhaust SPEED=0.50", 50),
        ("inner", 100, "SET_PURIFIER FAN=inner SPEED=1.00", 100),
        ("exhaust", 900, "SET_PURIFIER FAN=exhaust SPEED=1.00", 100),
        ("inner", -5, "SET_PURIFIER FAN=inner SPEED=0.00", 0),
        ("exhaust", 0, "SET_PURIFIER FAN=exhaust SPEED=0.00", 0),
    ])
    def test_speed_is_clamped_to_a_fraction(
            self, client, printer, fan, value, script, applied):
        printer.status = self.IDLE
        j = post(client, fan, value).json()
        assert printer.sent == [script] and j["applied"] == applied

    def test_a_value_is_required(self, client, printer):
        printer.status = self.IDLE
        assert post(client, "exhaust").status_code == 400 and printer.sent == []

    @pytest.mark.parametrize("status,why", [
        ({"print_stats": {"state": "standby"}}, "no enclosure"),
        ({"purifier": {**PURIFIER_OBJ, "power_detected": False},
          "print_stats": {"state": "standby"}}, "not attached"),
        ({"purifier": {**PURIFIER_OBJ, "mode": 1}, "print_stats": {"state": "standby"}}, "cool"),
        ({"purifier": {**PURIFIER_OBJ, "mode": 2}, "print_stats": {"state": "standby"}}, "preheat"),
        ({"purifier": {**PURIFIER_OBJ, "mode": 3}, "print_stats": {"state": "standby"}}, "hot"),
        ({"purifier": {k: v for k, v in PURIFIER_OBJ.items() if k != "mode"},
          "print_stats": {"state": "standby"}}, "mode unknown"),
    ])
    @pytest.mark.parametrize("fan", ["exhaust", "inner"])
    def test_refused_and_nothing_sent(self, client, printer, status, why, fan):
        printer.status = status
        assert post(client, fan, 50).status_code == 409, why
        assert printer.sent == []

    @pytest.mark.parametrize("state", ["printing", "paused"])
    @pytest.mark.parametrize("mode", [0, 1, 3])
    @pytest.mark.parametrize("fan", ["exhaust", "inner"])
    def test_allowed_during_a_print_whatever_the_mode(
            self, client, printer, state, mode, fan):
        printer.status = {"purifier": {**PURIFIER_OBJ, "mode": mode},
                          "print_stats": {"state": state}}
        assert post(client, fan, 50).status_code == 200
        assert printer.sent == [f"SET_PURIFIER FAN={fan} SPEED=0.50"]

    def test_a_missing_enclosure_is_still_refused_during_a_print(self, client, printer):
        printer.status = {"print_stats": {"state": "printing"}}
        assert post(client, "exhaust", 50).status_code == 409
        assert printer.sent == []

    def test_an_unreachable_moonraker_blocks_it(self, main, client, printer, monkeypatch):
        async def boom(path):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(main, "_mr_get", boom)
        assert post(client, "exhaust", 50).status_code == 502
        assert printer.sent == []

    def test_fans_off_is_not_gated_on_the_mode(self, client, printer):
        # The way out of a running mode: it must work while one is active.
        printer.status = {"purifier": {**PURIFIER_OBJ, "mode": 1}}
        assert post(client, "enclosure_off").status_code == 200
        assert printer.sent == [OFF_NOW]

    def test_limits_offer_the_fan_ranges(self, client, printer):
        r = client.get("/api/print-control/limits").json()["ranges"]
        assert r["exhaust"] == r["inner"] == {"min": 0.0, "max": 100.0}


class TestGcodeErrorText:
    def test_a_klipper_error_shows_its_message_not_a_traceback(self, client, printer):
        req = httpx.Request("POST", "http://x")
        body = {"error": {"code": 400, "message": "Unknown command:\"SET_NOPE\"",
                          "traceback": "Traceback (most recent call last): ..."}}
        printer.post_error = httpx.HTTPStatusError(
            "e", request=req, response=httpx.Response(400, json=body, request=req))
        r = post(client, "pause")
        assert r.status_code == 400
        assert r.json()["detail"] == 'moonraker: Unknown command:"SET_NOPE"'


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
            # Fresh, but safely in the past: a file mtime can read a little
            # ahead of time.time() on Windows, which would count as stale.
            flag.write_text("1")
            past = time.time() - 0.2
            os.utime(flag, (past, past))

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
                     "z_offset": -0.123, "light": 75.0, "detection": None, "enclosure": None, "nozzle": {"temp": 201.5, "target": 210},
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
