"""The assignment pipeline (mode 'head' of /api/preflight/print) on a printer
in multi mode: the server path keeps the multi topology, slot N feeds head N."""
import time

import pytest

from .test_mock_mode import FIXTURES, _load_main  # noqa: F401

SAMPLE = FIXTURES / "sample_4color.gcode"
SLOTS = [
    {"ace": 0, "slot": 0, "material": "PLA", "color": "#ffffff"},
    {"ace": 0, "slot": 1, "material": "PLA", "color": "#000000"},
    {"ace": 0, "slot": 2, "material": "PLA", "color": "#ff0000"},
    {"ace": 0, "slot": 3, "material": "PLA", "color": "#0000ff"},
]


@pytest.fixture
def pf(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=False)
    monkeypatch.setattr(main, "_PREFLIGHT_DIR", tmp_path / "pf")
    uploaded = []

    async def live():
        return [dict(s) for s in SLOTS]

    async def head():
        # what the printer reports in multi: the head-mode wiring is still
        # in the context (it is persisted), only the mode says multi
        return {"mode": "multi", "max_copies": 2, "ace_head": 3,
                "ace_heads": [0, 1, 2, 3],
                "head_ace": {0: 0, 1: 1, 2: 2, 3: 3}}

    async def state():
        return {}

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class Moonraker:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            files = k.get("files")
            if files:
                uploaded.append(files["file"][1].read().decode("utf-8"))
            return Reply()

    monkeypatch.setattr(main, "_live_slots_async", live)
    monkeypatch.setattr(main, "_head_mode_context", head)
    monkeypatch.setattr(main, "_query_state_gated", state)
    monkeypatch.setattr(main.httpx, "AsyncClient", Moonraker)
    with TestClient(main.app) as c:
        yield main, c, uploaded


def _wait(c, job_id):
    for _ in range(400):
        st = c.get("/api/preflight/print/status",
                   params={"job_id": job_id}).json()
        if st["done"]:
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_assignment_print_in_multi_keeps_slot_equals_head(pf):
    main, c, uploaded = pf
    with open(SAMPLE, "rb") as fh:
        rep = c.post("/api/preflight", files={
            "file": ("sample.gcode", fh, "application/octet-stream")}).json()
    assert rep.get("head_mode") and rep.get("topology") == "multi"
    by = {x["id"]: x for x in rep["targets"]}
    assert by["slot-0-1"]["head"] == 1
    # colours 1 and 2 swap their spools: colour 1 prints from slot 2
    choice = {str(x["t"]): "slot-0-%d" % x["t"] for x in rep["slicer_colors"]}
    choice["0"], choice["1"] = "slot-0-1", "slot-0-0"
    r = c.post("/api/preflight/print", json={
        "token": rep["token"], "mode": "head", "head_plan": "loadout",
        "head_assignment": choice})
    assert r.status_code == 200, r.text
    st = _wait(c, r.json()["job_id"])
    assert not st.get("error"), st
    assert uploaded
    swaps = [l for l in uploaded[-1].splitlines()
             if l.startswith("ACE_SWAP_HEAD")]
    assert swaps
    for line in swaps:
        f = dict(p.split("=") for p in line.split()[1:] if "=" in p)
        assert f["HEAD"] == f["SLOT"], line
