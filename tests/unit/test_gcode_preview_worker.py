"""The g-code preview parser (frontend worker) reads the file in slices and
must give the same picture whatever the slice size; a file over the size
limit is refused. Runs the worker under node; skipped when node is absent."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "multiace" / "web" / "frontend" / "gcode_preview_worker.js"
DRIVER = Path(__file__).with_name("gcode_preview_driver.js")
SAMPLE = ROOT / "tests" / "fixtures" / "sample_4color.gcode"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")


def _run(gcode, chunk="", fake_size=""):
    out = subprocess.run(
        ["node", str(DRIVER), str(WORKER), str(gcode), chunk, fake_size],
        capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def test_the_sample_parses():
    r = _run(SAMPLE)
    assert r["segments"] > 1000 and r["layers"] > 1 and r["toolchanges"] > 0
    assert r["decimated"] is None


@pytest.mark.parametrize("chunk", ["4096", "1000", "37"])
def test_slice_size_does_not_change_the_result(chunk):
    assert _run(SAMPLE, chunk) == _run(SAMPLE)


def test_a_line_split_across_slices_stays_whole(tmp_path):
    # A multi-byte character and CRLF endings straddling slice edges.
    f = tmp_path / "a.gcode"
    f.write_bytes((";café → x\r\nG90\r\nM83\r\n"
                   ";LAYER_CHANGE\r\n;Z:0.2\r\nG1 Z0.2\r\n"
                   "G1 X10 Y0 E1\r\nG1 X20 Y0 E1\r\n").encode("utf-8"))
    assert _run(f, "5")["segments"] == _run(f)["segments"] >= 1


def test_a_file_over_the_limit_is_refused():
    r = _run(SAMPLE, fake_size=str(600 * 1024 * 1024))
    assert "too large" in r["error"]
