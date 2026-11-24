"""preflight_core.prepend_print_prefs: the print-preference pass shared by
the printer backend and the in-browser (Pyodide) preflight. Streams the
file (the browser runs it on files of several hundred MB)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _core():
    spec = importlib.util.spec_from_file_location(
        "preflight_core_t", ROOT / "multiace/web/backend/preflight_core.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SRC = """; header
SET_PRINT_PREFERENCES BED_LEVEL=0
T1
SM_PRINT_FLOW_CALIBRATE EXTRUDER=0
; multiACE auto-load: begin
ACE_SWAP_HEAD HEAD=1 ACE=0 SLOT=1 INITIAL=1
; multiACE auto-load: end
G1 X10
T2
G1 X20
T1
"""


def _run(tmp_path, **prefs):
    core = _core()
    src = tmp_path / "in.gcode"
    out = tmp_path / "out.gcode"
    src.write_text(SRC, encoding="utf-8")
    core.prepend_print_prefs(str(src), str(out), **prefs)
    return out.read_text(encoding="utf-8").splitlines()


def test_prefs_line_on_top_and_slicer_line_disabled(tmp_path):
    lines = _run(tmp_path, bed_mesh=True, camera=False, flow_cal=False)
    assert lines[0] == "; multiACE preflight: print preferences"
    # a marker, not a command: stock 2.0+ cancels the print on a preference
    # change from inside the file
    assert lines[1] == ("; multiACE preflight prefs: BED_LEVEL=1 "
                        "FLOW_CALIBRATE=0 TIME_LAPSE_CAMERA=0")
    assert not any(ln.startswith("SET_PRINT_PREFERENCES") for ln in lines)
    assert "; multiACE disabled: SET_PRINT_PREFERENCES BED_LEVEL=0" in lines
    # flow calibration off: the slicer's line stays where it is
    assert "SM_PRINT_FLOW_CALIBRATE EXTRUDER=0" in lines


def test_flow_calibration_moves_behind_the_auto_load_block(tmp_path):
    lines = _run(tmp_path, flow_cal=True)
    assert "; multiACE moved: SM_PRINT_FLOW_CALIBRATE EXTRUDER=0" in lines
    end = lines.index("; multiACE auto-load: end")
    # heads printed with: 1 (start tool, last) and 2
    assert lines[end + 1:end + 8] == [
        "; multiACE preflight: flow calibration (moved behind the "
        "auto-load, heads loaded)",
        "T2 A0", "FLOW_CALIBRATE EXTRUDER=2",
        "T1 A0", "FLOW_CALIBRATE EXTRUDER=1",
        "T1",
        "; multiACE preflight: flow calibration end"]


def test_flow_cal_block_takes_any_line_iterator():
    core = _core()
    idx, block = core.flow_cal_block(iter(SRC.splitlines(True)))
    assert idx == 7 and block[1] == "T2 A0\n"
    assert core.flow_cal_block(iter(["G1 X1\n", "T0\n"])) == (None, [])


def test_backend_names_point_at_the_shared_code():
    src = (ROOT / "multiace/web/backend/main.py").read_text(encoding="utf-8")
    assert "_prepend_print_prefs = preflight_core.prepend_print_prefs" in src
    assert "def _prepend_print_prefs" not in src


def test_marker_reads_back_as_the_command_to_send(tmp_path):
    core = _core()
    lines = _run(tmp_path, bed_mesh=True, camera=True, flow_cal=False)
    head = "\n".join(lines[:8])
    assert core.read_print_prefs(head) == (
        "SET_PRINT_PREFERENCES BED_LEVEL=1 FLOW_CALIBRATE=0 "
        "TIME_LAPSE_CAMERA=1")
    assert core.print_prefs_line(True, True) == core.read_print_prefs(head)
    assert "FORCE" not in core.print_prefs_line(True, True, True)


def test_file_without_marker_asks_for_nothing():
    core = _core()
    assert core.read_print_prefs(SRC) is None
    assert core.read_print_prefs("; multiACE preflight prefs: BED_LEVEL=2") is None
    assert core.read_print_prefs("") is None


def test_backend_sends_the_preferences_before_every_start():
    src = (ROOT / "multiace/web/backend/main.py").read_text(encoding="utf-8")
    # the direct print, the queue start and the history reprint
    assert src.count("await _start_print_file(") == 2
    assert "await _send_print_prefs(prefs_line)" in src
    assert '"/printer/print/start", {"filename": path}' in src
    assert src.count('"/printer/print/start"') == 1


def test_start_sends_the_marker_preferences_first(monkeypatch, tmp_path):
    import asyncio
    from .test_mock_mode import _load_main
    main = _load_main(monkeypatch, tmp_path, mock=False)
    calls = []

    async def fake_prefs(path):
        return "SET_PRINT_PREFERENCES BED_LEVEL=1 FLOW_CALIBRATE=0 TIME_LAPSE_CAMERA=0"

    async def fake_post(path, body=None, timeout=30.0):
        calls.append((path, dict(body or {})))
        return {}
    monkeypatch.setattr(main, "_file_print_prefs", fake_prefs)
    monkeypatch.setattr(main, "_mr_post", fake_post)
    asyncio.run(main._start_print_file("a.gcode"))
    assert [c[0] for c in calls] == ["/printer/gcode/script",
                                     "/printer/print/start"]
    assert calls[0][1]["script"].startswith("SET_PRINT_PREFERENCES BED_LEVEL=1")
    assert calls[1][1] == {"filename": "a.gcode"}


def test_start_without_marker_only_starts(monkeypatch, tmp_path):
    import asyncio
    from .test_mock_mode import _load_main
    main = _load_main(monkeypatch, tmp_path, mock=False)
    calls = []

    async def no_prefs(path):
        return None

    async def fake_post(path, body=None, timeout=30.0):
        calls.append(path)
        return {}
    monkeypatch.setattr(main, "_file_print_prefs", no_prefs)
    monkeypatch.setattr(main, "_mr_post", fake_post)
    asyncio.run(main._start_print_file("a.gcode"))
    assert calls == ["/printer/print/start"]
