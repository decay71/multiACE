"""The OrcaSlicer plugin (multiace/web/backend/orca_plugin/multiace_orca.py) against the real
web backend: only the transport and the Orca window are stand-ins."""
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from .test_mock_mode import FIXTURES
from .test_preflight_deliver_return import pf, _wait  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "multiace" / "web" / "backend" / "orca_plugin" / "multiace_orca.py"
SAMPLE = FIXTURES / "sample_4color.gcode"

HEADER = ("; filament_colour = #FFFFFF;#000000;#FF0000;#0000FF\n"
          "; filament_type = PLA;PLA;PLA;PLA\n")


def _load(name="multiace_orca_test"):
    spec = importlib.util.spec_from_file_location(name, SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plugin = _load()


class ClientPrinter(plugin.Printer):
    """The plugin's Printer with the HTTP layer swapped for the TestClient."""

    def __init__(self, client, has=True):
        super().__init__("http://u1/multiace")
        self.c = client
        self.has = has
        self.inbox = []
        self.grants = 0

    def grant(self):
        self.grants += 1

    def _check(self, r):
        if r.status_code >= 400:
            err = plugin.PrinterError(plugin._detail(r.content))
            err.status = r.status_code
            raise err
        return r

    def get_json(self, path, timeout=None):
        return self._check(self.c.get(path)).json()

    def post_json(self, path, body, timeout=None):
        return self._check(self.c.post(path, json=body)).json()

    def stage_in_queue(self, file_path, staged_name):
        self.staged = getattr(self, "staged", [])
        self.staged.append((staged_name, Path(file_path).read_bytes()))
        return {"ok": True}

    def unstage(self, staged_name):
        self.unstaged = getattr(self, "unstaged", [])
        self.unstaged.append(staged_name)

    def upload(self, path, file_path, name, fields=None, base=None):
        if path.endswith("/inbox"):
            self.inbox.append(name)
            return {"ok": True}
        with open(file_path, "rb") as fh:
            return self._check(self.c.post(path, files={
                "file": (name, fh, "application/octet-stream")})).json()

    def download(self, path, dest):
        Path(dest).write_bytes(self._check(self.c.get(path)).content)

    def has_multiace(self):
        return self.has


class Ctx:
    def __init__(self, path, host="Moonraker", print_host="192.168.1.50"):
        self.gcode_path = str(path)
        self.host = host
        self.output_name = "benchy.gcode"
        self._ph = print_host

    def config_value(self, key):
        return self._ph if key == "print_host" else None

    def cancelled(self):
        return False


class FakeUi:
    """orca.host.ui stand-in: the 'page' answers each view with script[i]."""
    WINDOW_MODAL = 1

    def __init__(self, script):
        self.script = list(script)
        self.posts = []
        self.open = False

    def create_window(self, html, title, width, height, on_message,
                      on_close, style):
        assert "window.orca.postMessage" in html
        self.on_message = on_message
        self.open = True
        on_message({"action": "ready"})
        return self

    def post(self, data):
        self.posts.append(data)
        if data.get("type") in ("view", "error") and self.script:
            step = self.script.pop(0)
            self.on_message(step(data) if callable(step) else step)

    def close(self):
        self.open = False

    def is_open(self):
        return self.open


@pytest.fixture
def job(tmp_path):
    """A slicer export with a colour header, as Orca hands it over."""
    path = tmp_path / "job" / "plate.gcode.pp"
    path.parent.mkdir()
    path.write_text(HEADER + SAMPLE.read_text(encoding="utf-8"),
                    encoding="utf-8")
    return path


class TestAddress:
    @pytest.mark.parametrize("raw, want", [
        ("192.168.1.50", "http://192.168.1.50/multiace"),
        ("192.168.1.50:7125", "http://192.168.1.50/multiace"),
        ("http://u1.local/", "http://u1.local/multiace"),
        ("https://u1.local:8443/x", "https://u1.local:8443/multiace"),
        ('"u1.local:80"', "http://u1.local/multiace"),
        ("", ""), (None, ""),
    ])
    def test_printer_base(self, raw, want):
        assert plugin.printer_base(raw) == want


class TestView:
    MULTI = {
        "token": "t", "filename": "a.gcode",
        "slicer_colors": [{"t": 0, "hex": "#fff", "name": "White",
                           "material": "PLA"},
                          {"t": 1, "hex": "#000", "name": "Black",
                           "material": "PETG"}],
        "live_slots": [{"ace": 0, "slot": 0, "material": "PLA",
                        "color": "#ffffff", "name": "White"},
                       {"ace": 1, "slot": 2, "material": "PLA",
                        "color": "#ff0000", "name": "Red"},
                       {"ace": 1, "slot": 1, "material": "PETG",
                        "color": "#000000", "name": "Black"}],
        "plans": {"slicer": {"mapping": [
            {"t": 0, "slot": {"ace": 0, "slot": 0}},
            {"t": 1, "slot": {"ace": 1, "slot": 1}}]}},
    }

    def test_multi_rows_are_material_strict_with_the_proposal(self):
        v = plugin.build_view(self.MULTI)
        assert v["head_mode"] is False
        assert v["rows"][0]["options"] == ["slot-0-0", "slot-1-2"]
        assert v["rows"][0]["default"] == "slot-0-0"
        assert v["rows"][1]["options"] == ["slot-1-1"]

    def test_multi_body_is_a_remap_of_the_changed_colours(self):
        v = plugin.build_view(self.MULTI)
        body = plugin.print_body(self.MULTI, v,
                                 {"0": "slot-1-2", "1": "slot-1-1"})
        assert body == {"token": "t", "deliver": "return", "mode": "slicer",
                        "remap": {"0": 6, "1": 5}}

    def test_a_colour_without_a_choice_is_refused(self):
        v = plugin.build_view(self.MULTI)
        with pytest.raises(plugin.PrinterError):
            plugin.print_body(self.MULTI, v, {"0": "slot-0-0"})
        with pytest.raises(plugin.PrinterError):      # wrong material
            plugin.print_body(self.MULTI, v,
                              {"0": "slot-1-1", "1": "slot-1-1"})

    def test_head_mode_body_is_the_target_assignment(self):
        rep = {"token": "t", "head_mode": True,
               "slicer_colors": [{"t": 0, "hex": "#fff", "material": "PLA"}],
               "targets": [{"id": "feeder-1", "kind": "pin", "head": 1,
                            "material": "PLA", "color": "#fff"},
                           {"id": "slot-0-2", "kind": "ace", "head": 3,
                            "ace": 0, "slot": 2, "material": "PLA",
                            "color": "#eee"}],
               "plans": {"loadout": {"mapping": [
                   {"t": 0, "target_id": "slot-0-2"}]}}}
        v = plugin.build_view(rep)
        assert v["rows"][0]["default"] == "slot-0-2"
        assert plugin.print_body(rep, v, {"0": "feeder-1"}) == {
            "token": "t", "deliver": "return", "mode": "head",
            "head_plan": "loadout", "head_assignment": {"0": "feeder-1"}}


class TestSendStep:
    def test_file_export_and_processed_files_are_left_alone(self, job):
        assert plugin.send_step(Ctx(job, host="File"))[0] == "skip"
        job.write_text("; multiACE processed: format=13\n" + HEADER)
        assert plugin.send_step(Ctx(job))[0] == "skip"

    def test_a_printer_without_multiace_is_skipped(self, pf, job):
        _, c, _ = pf
        out = plugin.send_step(Ctx(job),
                               make_printer=lambda b: ClientPrinter(c, False))
        assert out[0] == "skip"

    def test_apply_puts_the_rewritten_file_in_place(self, pf, job):
        main, c, uploads = pf
        before = job.read_bytes()
        ui = FakeUi([lambda m: {"action": "send", "choice": {
            str(r["t"]): r["default"] or r["options"][0]
            for r in m["view"]["rows"]}}])
        kind, text = plugin.send_step(
            Ctx(job), make_printer=lambda b: ClientPrinter(c), ui=ui)
        assert kind == "ok", text
        after = job.read_bytes()
        assert after != before and plugin.PROCESSED_MARKER in after
        assert b"ACE_SWAP_HEAD" in after
        assert uploads == []                       # nothing went to Moonraker
        assert not ui.open
        assert os.listdir(job.parent) == [job.name]     # no temp left
        view = next(p for p in ui.posts if p["type"] == "view")["view"]
        assert [r["t"] for r in view["rows"]] == [0, 1, 2, 3]

    def test_closing_the_window_stops_the_send(self, pf, job):
        _, c, _ = pf
        before = job.read_bytes()
        ui = FakeUi([{"action": "cancel"}])
        kind, _ = plugin.send_step(
            Ctx(job), make_printer=lambda b: ClientPrinter(c), ui=ui)
        assert kind == "stop" and job.read_bytes() == before

    def test_open_in_preflight_hands_the_original_to_the_inbox(self, pf, job):
        _, c, _ = pf
        before = job.read_bytes()
        printers = []

        def make(base):
            printers.append(ClientPrinter(c))
            return printers[-1]

        ui = FakeUi([{"action": "preflight"}])
        kind, text = plugin.send_step(Ctx(job), make_printer=make, ui=ui)
        assert kind == "stop" and "preflight" in text
        assert printers[0].inbox == ["benchy.gcode"]
        assert job.read_bytes() == before

    def test_a_printer_error_stays_in_the_window_for_another_try(
            self, pf, job, monkeypatch):
        main, c, _ = pf
        real = main.preflight_core.rewrite_pipeline
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("material not loaded")
            return real(*a, **k)

        monkeypatch.setattr(main.preflight_core, "rewrite_pipeline", flaky)
        pick = lambda m: {"action": "send", "choice": {  # noqa: E731
            str(r["t"]): r["options"][0] for r in view["rows"]}}
        view = {}

        def first(m):
            view.update(m["view"])
            return pick(m)

        ui = FakeUi([first, pick])
        kind, _ = plugin.send_step(
            Ctx(job), make_printer=lambda b: Counting(c, pysrc=False),
            ui=ui)
        assert kind == "ok"
        assert [p["text"] for p in ui.posts if p["type"] == "error"] == [
            "material not loaded"]

    def test_an_unreachable_printer_says_so_in_the_window(self, job):
        class Down(plugin.Printer):
            def grant(self):
                pass

            def has_multiace(self):
                err = plugin.PrinterError("printer not reachable: refused")
                err.status = None
                raise err
        before = job.read_bytes()
        ui = FakeUi([{"action": "cancel"}])
        kind, _ = plugin.send_step(Ctx(job), make_printer=Down, ui=ui)
        assert kind == "stop" and job.read_bytes() == before
        assert "not reachable" in ui.posts[0]["text"]

    def test_the_host_permission_is_asked_from_the_window(self, pf, job):
        """A printer that only answers once the user allowed the host: the
        first check fails, the window's first message grants, then it
        loads."""
        _, c, _ = pf

        class Gated(ClientPrinter):
            def has_multiace(self):
                if not self.grants:
                    err = plugin.PrinterError("not permitted")
                    err.status = None
                    raise err
                return True

        made = []

        def make(base):
            made.append(Gated(c))
            return made[-1]

        ui = FakeUi([{"action": "cancel"}])
        kind, _ = plugin.send_step(Ctx(job), make_printer=make, ui=ui)
        assert kind == "stop" and made[0].grants == 1
        assert ui.posts[0]["type"] == "view"

    def test_no_multiace_found_after_the_permission_skips(self, job):
        class Other(plugin.Printer):
            calls = 0

            def grant(self):
                pass

            def has_multiace(self):
                Other.calls += 1
                if Other.calls == 1:
                    err = plugin.PrinterError("not permitted")
                    err.status = None
                    raise err
                return False
        ui = FakeUi([])
        assert plugin.send_step(Ctx(job), make_printer=Other, ui=ui)[0] == "skip"
        assert not ui.open


def test_the_header_is_what_orca_parses():
    head = SRC.read_text(encoding="utf-8").split("# ///")[1]
    assert "[tool.orcaslicer.plugin]" in head
    assert 'version = "%s"' % plugin.VERSION in head


def test_real_http_transport(tmp_path):
    """Printer over a real socket: multipart upload, JSON, download, errors."""
    import http.server
    import json
    import threading
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/multiace/api/version":
                self._send(200, b'{"web": "x"}')
            elif self.path.startswith("/multiace/api/preflight/result"):
                self._send(200, b"; multiACE processed: x\nG1\n", "text/plain")
            else:
                self._send(404, b'{"detail": "Not Found"}')

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            seen[self.path] = (self.headers["Content-Type"], data)
            if self.path.endswith("/print"):
                self._send(409, b'{"detail": "nope"}')
            else:
                self._send(200, b'{"token": "abc"}')

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        p = plugin.Printer(base + "/multiace")
        assert p.has_multiace() is True
        assert plugin.Printer(base + "/other").has_multiace() is False
        src = tmp_path / "a.gcode"
        src.write_bytes(b"G28\n" * 1000)
        assert p.preflight(str(src), 'we"ird.gcode') == {"token": "abc"}
        ctype, data = seen["/multiace/api/preflight"]
        assert ctype.startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="weird.gcode"' in data
        assert b"G28\n" * 1000 in data
        with pytest.raises(plugin.PrinterError) as e:
            p.start({"token": "abc"})
        assert str(e.value) == "nope" and e.value.status == 409
        dest = tmp_path / "out.gcode"
        p.fetch("job1", str(dest))
        assert dest.read_bytes().startswith(b"; multiACE processed:")
    finally:
        srv.shutdown()
    with pytest.raises(plugin.PrinterError) as e:
        plugin.Printer(base + "/multiace").has_multiace()
    assert e.value.status is None


class TestSwitchOn:
    def test_adds_the_send_step_to_an_empty_option(self):
        assert plugin.with_send_step("") == '"multiACE+ - assign and send"'
        assert plugin.with_send_step(None) == '"multiACE+ - assign and send"'

    def test_keeps_what_is_already_chosen(self):
        assert plugin.with_send_step('"Other step"') == \
            '"Other step";"multiACE+ - assign and send"'

    def test_the_old_name_is_renamed_in_place(self):
        assert plugin.with_send_step(
            'x;"multiACE - assign and send"') == \
            'x;"multiACE+ - assign and send"'

    def test_nothing_to_do_when_it_is_there(self):
        assert plugin.with_send_step(
            'x;"multiACE+ - assign and send"') is None


def test_backend_serves_the_plugin_file():
    import main
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/api/orca/plugin")
    assert r.status_code == 200
    assert "multiace_orca.py" in r.headers["content-disposition"]
    assert r.text == SRC.read_text(encoding="utf-8")


class TestSendOptions:
    NOW = {"airprint": False, "quad": True, "noodle": True,
           "clean_bed": False, "led_off": False, "_defect_main": True}

    def test_only_changed_settings_become_commands(self):
        import main
        cmds = main._send_option_commands(self.NOW, {
            "airprint": True, "quad": True, "noodle": False,
            "clean_bed": None, "led_off": True})
        assert cmds == [
            "ACE_SET_AIRPRINT_DETECTION ENABLE=1",
            "DEFECT_DETECTION_CONFIG NOODLE_ENABLE=0",
            "SET_PRINT_PREFERENCES END_LED_TURN_OFF=1"]

    def test_nothing_changed_sends_nothing(self):
        import main
        want = {k: v for k, v in self.NOW.items() if not k.startswith("_")}
        assert main._send_option_commands(self.NOW, want) == []

    def test_a_setting_the_printer_does_not_report_is_left_alone(self):
        import main
        assert main._send_option_commands({"quad": False},
                                          {"airprint": True}) == []

    def test_detection_switched_on_turns_the_main_switch_on(self):
        import main
        now = dict(self.NOW, noodle=False, _defect_main=False)
        assert main._send_option_commands(now, {"noodle": True}) == [
            "DEFECT_DETECTION_CONFIG MAIN_ENABLE=1 NOODLE_ENABLE=1"]

    def test_prefs_go_into_the_print_body(self):
        report = {"token": "t", "slicer_colors": [
            {"t": 0, "hex": "#fff", "material": "PLA"}],
            "live_slots": [{"ace": 0, "slot": 0, "material": "PLA",
                            "color": "#fff"}],
            "plans": {"slicer": {"mapping": [
                {"t": 0, "slot": {"ace": 0, "slot": 0}}]}}}
        view = plugin.build_view(report)
        body = plugin.print_body(report, view, {"0": "slot-0-0"},
                                 {"bed_mesh": True, "camera": False})
        assert body.get("bed_mesh") is True and "camera" not in body

    def test_the_window_choice_reaches_the_printer(self, pf, job):
        _, c, _ = pf
        seen = {}

        class P(ClientPrinter):
            def get_json(self, path, timeout=None):
                if path == "/api/preflight/pysrc":      # printer path
                    err = plugin.PrinterError("not found")
                    err.status = 404
                    raise err
                return super().get_json(path, timeout)

            def options(self):
                return {"airprint": False, "quad": False}

            def set_options(self, options):
                seen["options"] = dict(options)
                return {"ok": True}

            def start(self, body):
                seen["body"] = dict(body)
                return super().start(body)

        ui = FakeUi([lambda m: {
            "action": "send",
            "choice": {str(r["t"]): r["default"] or r["options"][0]
                       for r in m["view"]["rows"]},
            "prefs": {"bed_mesh": True, "camera": False, "flow_cal": False},
            "options": {"airprint": True, "quad": False, "bogus": True}}])
        kind, text = plugin.send_step(
            Ctx(job), make_printer=lambda b: P(c), ui=ui)
        assert kind == "ok", text
        assert seen["options"] == {"airprint": True, "quad": False}
        assert seen["body"].get("bed_mesh") is True
        view = next(p for p in ui.posts if p["type"] == "view")["view"]
        assert view["options"] == {"airprint": False, "quad": False}


class Counting(ClientPrinter):
    """Counts what goes to the printer's own preflight."""

    def __init__(self, client, pysrc=True):
        super().__init__(client)
        self.preflights = 0
        self.starts = 0
        self.pysrc = pysrc
        self.prefs = []

    def get_json(self, path, timeout=None):
        if path == "/api/preflight/pysrc" and not self.pysrc:
            err = plugin.PrinterError("not found")
            err.status = 404
            raise err
        return super().get_json(path, timeout)

    def post_json(self, path, body, timeout=None):
        if path == "/api/preflight/prefs":
            self.prefs.append(dict(body))
            return {"ok": True}
        return super().post_json(path, body, timeout)

    def preflight(self, file_path, name):
        self.preflights += 1
        return super().preflight(file_path, name)

    def start(self, body):
        self.starts += 1
        return super().start(body)


def _pick_all(m, **extra):
    msg = {"action": "send", "choice": {
        str(r["t"]): r["default"] or r["options"][0]
        for r in m["view"]["rows"]}}
    msg.update(extra)
    return msg


class TestLocalEngine:
    def test_the_file_is_rewritten_here_not_on_the_printer(self, pf, job):
        _, c, uploads = pf
        p = Counting(c)
        ui = FakeUi([_pick_all])
        kind, text = plugin.send_step(Ctx(job), make_printer=lambda b: p,
                                      ui=ui)
        assert kind == "ok", text
        assert p.preflights == 0 and p.starts == 0 and uploads == []
        after = job.read_bytes()
        assert plugin.PROCESSED_MARKER in after and b"ACE_SWAP_HEAD" in after
        assert os.listdir(job.parent) == [job.name]

    def test_same_result_as_the_printer(self, pf, job, tmp_path):
        _, c, _ = pf
        other = tmp_path / "copy" / job.name
        other.parent.mkdir()
        other.write_bytes(job.read_bytes())
        plugin.send_step(Ctx(job), make_printer=lambda b: Counting(c),
                         ui=FakeUi([_pick_all]))
        plugin.send_step(Ctx(other),
                         make_printer=lambda b: Counting(c, pysrc=False),
                         ui=FakeUi([_pick_all]))
        assert job.read_bytes() == other.read_bytes()

    def test_a_printer_without_the_code_does_the_work_itself(self, pf, job):
        _, c, _ = pf
        p = Counting(c, pysrc=False)
        kind, text = plugin.send_step(Ctx(job), make_printer=lambda b: p,
                                      ui=FakeUi([_pick_all]))
        assert kind == "ok", text
        assert p.preflights == 1 and p.starts == 1
        assert plugin.PROCESSED_MARKER in job.read_bytes()

    def test_print_switches_go_to_the_printer_after_the_rewrite(self, pf, job):
        _, c, _ = pf
        p = Counting(c)
        ui = FakeUi([lambda m: _pick_all(m, prefs={"bed_mesh": True})])
        kind, text = plugin.send_step(Ctx(job), make_printer=lambda b: p,
                                      ui=ui)
        assert kind == "ok", text
        assert p.prefs == [{"bed_mesh": True}]

    def test_the_engine_modules_do_not_stay_importable(self, pf):
        import sys
        _, c, _ = pf
        before = {n: sys.modules.get(n) for n, _ in plugin.ENGINE_MODULES}
        plugin.LocalEngine(Counting(c))
        assert {n: sys.modules.get(n)
                for n, _ in plugin.ENGINE_MODULES} == before


class TestCancelWhileBusy:
    def test_cancel_during_the_rewrite_leaves_the_file(self, pf, job):
        import threading
        _, c, _ = pf
        before = job.read_bytes()
        started, release = threading.Event(), threading.Event()

        class Slow(Counting):
            pass

        p = Slow(c)
        ui = FakeUi([_pick_all])
        real = plugin.LocalEngine.rewrite

        def slow(self, body, gcode_path, progress=None, cancelled=None):
            started.set()
            release.wait(5)
            return real(self, body, gcode_path, progress, cancelled)

        def click_cancel():
            started.wait(5)
            ui.on_message({"action": "cancel"})
            release.set()

        plugin.LocalEngine.rewrite = slow
        t = threading.Thread(target=click_cancel)
        t.start()
        try:
            kind, _ = plugin.send_step(Ctx(job), make_printer=lambda b: p,
                                       ui=ui)
        finally:
            plugin.LocalEngine.rewrite = real
            t.join()
        assert kind == "stop"
        assert job.read_bytes() == before
        assert os.listdir(job.parent) == [job.name]


class TestQueueAndWeights:
    def test_queue_stages_the_rewritten_file_without_the_printer_computing(
            self, pf, job, monkeypatch):
        main, c, uploads = pf
        seen = {}

        async def enqueue(filename, path, mode, used_slots, snapshot,
                          replaces=None):
            seen.update(filename=filename, path=path, mode=mode,
                        used=used_slots)
            return {"id": "q1"}

        monkeypatch.setattr(main, "_queue_enqueue", enqueue)
        p = Counting(c)
        ui = FakeUi([lambda m: dict(_pick_all(m), action="queue")])
        kind, text = plugin.send_step(Ctx(job), make_printer=lambda b: p,
                                      ui=ui)
        assert kind == "stop" and "print queue" in text
        assert p.preflights == 0 and p.starts == 0
        (staged, data), = p.staged
        assert plugin.PROCESSED_MARKER in data
        assert seen["path"] == plugin.QUEUE_DIR + "/" + staged
        assert staged.endswith("_" + seen["filename"])
        assert seen["used"] and all(len(x) == 2 for x in seen["used"])
        view = next(x for x in ui.posts if x["type"] == "view")["view"]
        assert view["can_queue"] is True

    def test_a_refused_queue_entry_removes_the_staged_file(
            self, pf, job, monkeypatch):
        main, c, _ = pf

        async def enqueue(*a, **k):
            raise main.HTTPException(status_code=507, detail="no room")

        monkeypatch.setattr(main, "_queue_enqueue", enqueue)
        p = Counting(c)
        ui = FakeUi([lambda m: dict(_pick_all(m), action="queue"),
                     {"action": "cancel"}])
        kind, _ = plugin.send_step(Ctx(job), make_printer=lambda b: p, ui=ui)
        assert kind == "stop"
        assert [x[0] for x in p.staged] == p.unstaged
        assert any(x["type"] == "error" and "no room" in x["text"]
                   for x in ui.posts)

    def test_rows_carry_what_each_colour_needs(self):
        report = {"token": "t", "slicer_colors": [
            {"t": 0, "hex": "#fff", "material": "PLA"}],
            "live_slots": [{"ace": 0, "slot": 0, "material": "PLA",
                            "color": "#fff"}],
            "plans": {"slicer": {"swaps": 3, "mapping": [
                {"t": 0, "slot": {"ace": 0, "slot": 0}}],
                "estimate": {"total_s": 600, "per_color": [
                    {"t": 0, "print_g": 10.0, "purge_g": 2.5}]}}}}
        view = plugin.build_view(report)
        assert view["rows"][0]["need_g"] == 12.5
        assert view["summary"] == {"swaps": 3, "bg_ok": None, "bg_of": None,
                                   "total_s": 600, "grams": 12.5}
        report["plans"]["slicer"]["bg"] = {"bg_ok": 2, "unloads": 3}
        assert plugin.build_view(report)["summary"]["bg_ok"] == 2
        assert plugin.build_view(report)["summary"]["bg_of"] == 3

    def test_spool_weights_by_target(self, pf, monkeypatch):
        main, c, _ = pf
        monkeypatch.setattr(main, "_parse_state", lambda st: {
            "spools": {"7": {"weight_g": 98.2}, "9": {"weight_g": 1500},
                       "3": {}},
            "spool_binding": {"0_1": "7", "h3": "9", "1_0": "3"},
            "aces": [{"idx": 0, "slots": [
                {"idx": 0, "subtype": "Support"}, {"idx": 1, "subtype": ""},
                {"idx": 2, "subtype": "NONE"}]}],
            "toolheads": [{"idx": 3, "manual": True, "subtype": "Silk"},
                          {"idx": 0, "subtype": "Matte"}]})
        got = c.get("/api/orca/spools").json()["spools"]
        assert got == {"slot-0-1": {"weight_g": 98.2, "pct": 10},
                       "slot-0-0": {"sub": "Support"},
                       "feeder-3": {"weight_g": 1500.0, "pct": 100,
                                    "sub": "Silk"}}

    def test_stock_replenish_is_one_preferences_line_with_the_light(self):
        import main
        now = {"led_off": False, "replenish": True}
        assert main._send_option_commands(
            now, {"led_off": True, "replenish": False}) == [
            "SET_PRINT_PREFERENCES END_LED_TURN_OFF=1 "
            "AUTO_REPLENISH_FILAMENT=0"]


class TestThumbnail:
    def test_the_largest_embedded_png_is_handed_to_the_window(self):
        url = plugin.extract_thumbnail(str(SAMPLE))
        assert url.startswith("data:image/png;base64,iVBOR")
        import base64
        png = base64.b64decode(url.split(",", 1)[1])
        # IHDR: width, height
        assert int.from_bytes(png[16:20], "big") == 300
        assert int.from_bytes(png[20:24], "big") == 300

    def test_a_file_without_a_preview_gives_none(self, tmp_path):
        f = tmp_path / "plain.gcode"
        f.write_text("G28\nG1 X1 Y1 E1\n")
        assert plugin.extract_thumbnail(str(f)) == ""
        assert plugin.extract_thumbnail(str(tmp_path / "missing")) == ""


def test_hub_upload_copies_match_the_plugin():
    """orca_hub/ holds one copy per upload target; refresh them with
    scripts/orca_hub_files.py after changing the plugin."""
    hub = ROOT / "orca_hub"
    if not hub.is_dir():
        pytest.skip("no orca_hub folder in this tree")
    copies = sorted(hub.glob("multiace_plus_*.py"))
    assert copies
    for c in copies:
        assert c.read_bytes() == SRC.read_bytes(), c.name


def test_engine_modules_stay_private_and_find_the_estimate_model():
    import sys
    before = set(sys.modules)
    mods = plugin.load_engine_modules({
        "swap_cost": "X = 1\n",
        "postprocess": "Y = 2\n",
        "core": ("try:\n    import no_such_module_xyz as _swap_cost\n"
                 "except ImportError:\n    _swap_cost = None\n")})
    assert mods["preflight_core"]._swap_cost.X == 1
    assert set(sys.modules) == before
    assert "sys.modules" not in SRC.read_text(encoding="utf-8")


class TestPlanButtons:
    REP = {"token": "t", "head_mode": True,
           "slicer_colors": [{"t": 0, "hex": "#fff", "material": "PLA"},
                             {"t": 1, "hex": "#000", "material": "PLA"}],
           "targets": [{"id": "slot-0-0", "kind": "ace", "head": 0, "ace": 0,
                        "slot": 0, "material": "PLA", "color": "#fff"},
                       {"id": "slot-1-0", "kind": "ace", "head": 0, "ace": 1,
                        "slot": 0, "material": "PLA", "color": "#000"},
                       {"id": "slot-0-1", "kind": "ace", "head": 1, "ace": 0,
                        "slot": 1, "material": "PLA", "color": "#111"},
                       {"id": "slot-1-1", "kind": "ace", "head": 1, "ace": 1,
                        "slot": 1, "material": "PETG", "color": "#222"}],
           "plans": {"loadout": {"swaps": 9, "mapping": [
               {"t": 0, "target_id": "slot-0-0"},
               {"t": 1, "target_id": "slot-1-0"}]}},
           "plans_reorder": {
               "optimize": {"feasible": True, "swaps": 0,
                            "estimate": {"total_s": 600, "per_color": [
                                {"t": 0, "print_g": 5, "purge_g": 1}]},
                            "mapping": [
                   {"t": 0, "kind": "ace", "ace": 0, "slot": 0, "head": 0},
                   {"t": 1, "kind": "ace", "ace": 0, "slot": 1, "head": 1,
                    "tier": "fallback"}]},
               "layer": {"feasible": False, "swaps": 0, "mapping": []},
               "color": {"feasible": True, "swaps": 1, "mapping": [
                   {"t": 0, "kind": "ace", "ace": 0, "slot": 0, "head": 0},
                   {"t": 1, "kind": "ace", "ace": 1, "slot": 1, "head": 1}]}}}

    def test_printable_plans_become_buttons(self):
        v = plugin.build_view(self.REP)
        # layer is not feasible, color would put PLA on a PETG spool
        assert [p["key"] for p in v["plans"]] == ["loaded", "optimize"]
        opt = v["plans"][1]
        assert opt["choice"] == {"0": "slot-0-0", "1": "slot-0-1"}
        assert opt["swaps"] == 0 and opt["total_s"] == 600
        assert opt["need"] == {"0": 6.0} and opt["grams"] == 6.0

    def test_a_plan_choice_prints_as_an_ordinary_assignment(self):
        v = plugin.build_view(self.REP)
        pick = v["plans"][1]["choice"]
        body = plugin.print_body(self.REP, v, pick)
        assert body["head_plan"] == "loadout"
        assert body["head_assignment"] == pick
        assert plugin.plan_for_choice(v, pick)["key"] == "optimize"
        assert plugin.plan_for_choice(v, v["plans"][0]["choice"])["key"] \
            == "loaded"
        assert plugin.plan_for_choice(
            v, {"0": "slot-0-1", "1": "slot-0-0"}) is None

    def test_a_plan_names_the_spools_to_move_first(self):
        v = plugin.build_view(self.REP)
        # colour 2 goes to a slot that holds another colour
        assert v["plans"][0]["moves"] == []
        assert v["plans"][1]["moves"] == ["1"]

    def test_a_near_colour_where_it_prints_anyway_is_no_move(self):
        rep = json.loads(json.dumps(self.REP))
        rep["plans"]["loadout"]["mapping"][1]["target_id"] = "slot-0-1"
        v = plugin.build_view(rep)
        assert v["plans"][1]["moves"] == []

    def test_no_buttons_without_a_second_plan(self):
        rep = dict(self.REP)
        rep.pop("plans_reorder")
        assert plugin.build_view(rep)["plans"] == []


def test_a_real_report_offers_the_plans_on_the_loaded_spools(pf, job):
    _, c, _ = pf
    seen = {}

    def pick(m, **_):
        seen["view"] = m["view"]
        plans = m["view"]["plans"]
        return {"action": "send", "choice": dict((plans[-1] if plans else
                {"choice": _pick_all(m)["choice"]})["choice"])}

    kind, text = plugin.send_step(Ctx(job), make_printer=lambda b: Counting(c),
                                  ui=FakeUi([pick]))
    assert kind == "ok", text
    plans = seen["view"]["plans"]
    assert [p["key"] for p in plans][:2] == ["loaded", "optimize"]
    ids = {x["id"] for x in seen["view"]["targets"]}
    for p in plans:
        assert set(p["choice"].values()) <= ids
    assert plugin.PROCESSED_MARKER in job.read_bytes()


class TestHandMadeChoiceNumbers:
    def _setup(self, pf, job):
        _, c, _ = pf
        engine = plugin.LocalEngine(Counting(c))
        report = engine.report(str(job), job.name)
        return engine, plugin.build_view(report)

    def test_the_loaded_choice_counts_like_the_loaded_plan(self, pf, job):
        engine, view = self._setup(pf, job)
        assert engine.can_count()
        loaded = view["plans"][0]
        card = engine.numbers(view, loaded["choice"])
        assert card["key"] == "custom"
        assert card["swaps"] == loaded["swaps"]
        assert card["total_s"] == loaded["total_s"]
        assert card["grams"] == loaded["grams"]

    def test_another_choice_gets_its_own_numbers(self, pf, job):
        engine, view = self._setup(pf, job)
        pick = dict(view["plans"][0]["choice"])
        rows = [r for r in view["rows"] if len(r["options"]) > 1]
        assert rows, "the sample needs a colour with a second spool"
        r = rows[0]
        pick[str(r["t"])] = next(o for o in r["options"]
                                 if o != pick[str(r["t"])])
        card = engine.numbers(view, pick)
        assert card is not None and isinstance(card["swaps"], int)
        assert card["choice"] == pick

    def test_an_unknown_target_gives_no_numbers(self, pf, job):
        engine, view = self._setup(pf, job)
        pick = dict(view["plans"][0]["choice"])
        pick[next(iter(pick))] = "slot-9-9"
        assert engine.numbers(view, pick) is None


class TestLookAtThePrinter:
    class P:
        base = "http://u1/multiace"

        def __init__(self):
            self.calls = []

        def screen_frame(self, etag=None):
            self.calls.append(("screen", etag))
            if etag == '"a"':
                return None, None, etag
            return b"PNG", "image/png", '"a"'

        def cam_frame(self, name):
            self.calls.append(("cam", name))
            return b"JPG", "image/jpeg", None

    def test_a_screen_frame_goes_out_once_until_it_changes(self):
        p = self.P()
        feed = plugin.LiveFeed(p, lambda m: None)
        msg, etag = feed.frame(("screen", ""), None)
        assert msg == {"type": "frame", "kind": "screen", "cam": "",
                       "data": "data:image/png;base64,UE5H"}
        again, etag2 = feed.frame(("screen", ""), etag)
        assert again is None and etag2 == etag

    def test_a_camera_frame_names_its_camera(self):
        feed = plugin.LiveFeed(self.P(), lambda m: None)
        msg, _ = feed.frame(("cam", "case"), None)
        assert msg["kind"] == "cam" and msg["cam"] == "case"
        assert msg["data"].startswith("data:image/jpeg;base64,")

    def test_the_feed_runs_only_while_asked(self):
        p, got = self.P(), []
        feed = plugin.LiveFeed(p, got.append)
        feed.PERIOD = {"screen": 0.01, "cam": 0.01}
        feed.show("cam", "case")
        deadline = time.time() + 2
        while not got and time.time() < deadline:
            time.sleep(0.01)
        feed.show("")
        feed.thread.join(1)
        assert got and not feed.thread.is_alive()
        feed.close()

    def test_sources_skip_embedded_pages(self):
        class Q(plugin.Printer):
            def get_json(self, path, timeout=None):
                if path == "/api/screen-available":
                    return {"available": True}
                if path == "/api/print-control":
                    return {"light": 0.0}
                return {"cameras": [{"name": "gui", "kind": "iframe"},
                                    {"name": "case", "kind": "webrtc"}]}
        src = Q("http://u1/multiace").live_sources()
        assert src == {"screen": True, "cams": ["case"], "light": 0.0,
                       "panel": "http://u1/multiace/?panel=1"}


def test_the_light_goes_through_the_print_control():
    calls = []

    class Q(plugin.Printer):
        def get_json(self, path, timeout=None):
            calls.append(("get", path))
            return {"light": 100.0}

        def post_json(self, path, body, timeout=None):
            calls.append(("post", path, body))
            return {"ok": True}
    q = Q("http://u1/multiace")
    assert q.set_light(True) == 100.0
    assert calls[0] == ("post", "/api/print-control",
                        {"verb": "light", "value": 100})
    q.set_light(False)
    assert calls[2][2] == {"verb": "light", "value": 0}


class TestSpoolsChangeWhileTheWindowIsOpen:
    def test_the_engine_notices_a_changed_spool(self, pf, job, monkeypatch):
        from . import test_preflight_deliver_return as base
        _, c, _ = pf
        p = Counting(c)
        engine = plugin.LocalEngine(p)
        assert engine.changed(p) is False
        monkeypatch.setitem(base.SLOTS[2], "color", "#00ff00")
        assert engine.changed(p) is True
        assert engine.live_slots[2]["color"] == "#00ff00"
        assert engine.changed(p) is False

    def test_the_window_gets_the_new_spools(self, pf, job, monkeypatch):
        from . import test_preflight_deliver_return as base
        _, c, _ = pf
        monkeypatch.setattr(plugin, "SPOOL_WATCH_S", 0.0)
        seen = []

        def first(m):
            monkeypatch.setitem(base.SLOTS[2], "color", "#00ff00")
            return {"action": "noop"}

        def second(m):
            seen.append(m)
            return _pick_all(m)

        ui = FakeUi([first, second])
        kind, text = plugin.send_step(Ctx(job),
                                      make_printer=lambda b: Counting(c),
                                      ui=ui)
        assert kind == "ok", text
        assert seen and seen[0].get("keep") is True
        cols = {x["id"]: x["color"] for x in seen[0]["view"]["targets"]}
        assert cols["slot-0-2"] == "#00ff00"


class TestUseAllSlots:
    REP = json.loads(json.dumps(TestPlanButtons.REP))
    REP["plans"]["optimize"] = {
        "feasible": True, "swaps": 2, "mapping": [
            {"t": 0, "kind": "ace", "ace": 0, "slot": 0, "head": 0},
            {"t": 1, "kind": "ace", "ace": 1, "slot": 3, "head": 3},
            {"t": 1, "kind": "ace", "ace": 1, "slot": 2, "head": 2,
             "tier": "copy"}]}
    REP["plans"]["layer"] = {"feasible": False, "swaps": 0, "mapping": []}

    def test_an_all_slots_plan_names_the_spools_to_load(self):
        v = plugin.build_view(json.loads(json.dumps(self.REP)))
        card = next(p for p in v["plans"] if p["id"] == "all-optimize")
        assert card["scope"] == "all"
        assert card["choice"] == {"0": "slot-0-0", "1": "slot-1-3"}
        assert card["extra"] == [{"t": 1, "tid": "slot-1-2"}]
        assert card["todo"] == [{"tid": "slot-1-3", "t": 1, "why": "load"},
                                {"tid": "slot-1-2", "t": 1, "why": "load"}]
        ghost = next(x for x in v["targets"] if x["id"] == "slot-1-3")
        assert ghost["ghost"] is True and ghost["head"] == 3
        assert "all-layer" not in {p["id"] for p in v["plans"]}

    def test_it_prints_as_the_printers_plan(self):
        rep = json.loads(json.dumps(self.REP))
        v = plugin.build_view(rep)
        card = next(p for p in v["plans"] if p["id"] == "all-optimize")
        body = plugin.print_body(rep, v, card["choice"], plan="all-optimize")
        assert body["head_plan"] == "optimize"
        assert body["head_scope"] == "all"

    def test_an_edited_proposal_cannot_print_on_an_empty_slot(self):
        rep = json.loads(json.dumps(self.REP))
        v = plugin.build_view(rep)
        pick = {"0": "slot-1-0", "1": "slot-1-3"}
        with pytest.raises(plugin.PrinterError, match="empty slot"):
            plugin.print_body(rep, v, pick, plan="all-optimize")

    def test_a_loaded_matching_spool_needs_nothing(self):
        rep = json.loads(json.dumps(self.REP))
        rep["targets"].append({"id": "slot-1-3", "kind": "ace", "head": 3,
                               "ace": 1, "slot": 3, "material": "PLA",
                               "color": "#000"})
        rep["targets"].append({"id": "slot-1-2", "kind": "ace", "head": 2,
                               "ace": 1, "slot": 2, "material": "PLA",
                               "color": "#ff0000"})
        v = plugin.build_view(rep)
        card = next(p for p in v["plans"] if p["id"] == "all-optimize")
        assert card["todo"] == [{"tid": "slot-1-2", "t": 1, "why": "swap"}]


def test_a_real_all_slots_plan_prints_or_is_refused_with_a_reason(pf, job):
    _, c, _ = pf
    p = Counting(c)
    engine = plugin.LocalEngine(p)
    report = engine.report(str(job), job.name)
    view = plugin.build_view(report)
    cards = [x for x in view["plans"] if x.get("scope") == "all"]
    assert cards, "the sample report should carry a load proposal"
    card = cards[0]
    body = plugin.print_body(report, view, card["choice"], plan=card["id"])
    assert body["head_scope"] == "all" and body["head_plan"] == card["key"]
    if card["todo"]:
        with pytest.raises(Exception, match="load them as shown"):
            engine.rewrite(body, str(job))
    else:
        engine.rewrite(body, str(job))
        assert plugin.PROCESSED_MARKER in job.read_bytes()
