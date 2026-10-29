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
    assert lines[1] == ("SET_PRINT_PREFERENCES BED_LEVEL=1 FLOW_CALIBRATE=0 "
                        "TIME_LAPSE_CAMERA=0 FORCE=1")
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
