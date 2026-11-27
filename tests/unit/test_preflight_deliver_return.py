"""deliver=return on /api/preflight/print: the rewritten file is kept for one
GET /api/preflight/result instead of going to Moonraker (slicer plugins send
it themselves)."""
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
    uploads = []

    async def live():
        return [dict(s) for s in SLOTS]

    async def head():
        return {"mode": "multi"}

    async def state():
        return {}

    class NoMoonraker:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            uploads.append(url)
            raise main.httpx.HTTPError("no moonraker in this test")

    monkeypatch.setattr(main, "_live_slots_async", live)
    monkeypatch.setattr(main, "_head_mode_context", head)
    monkeypatch.setattr(main, "_query_state_gated", state)
    monkeypatch.setattr(main.httpx, "AsyncClient", NoMoonraker)
    with TestClient(main.app) as c:
        yield main, c, uploads


def _token(c):
    with open(SAMPLE, "rb") as fh:
        r = c.post("/api/preflight",
                   files={"file": ("sample.gcode", fh,
                                   "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _wait(c, job_id):
    for _ in range(400):
        st = c.get("/api/preflight/print/status",
                   params={"job_id": job_id}).json()
        if st["done"]:
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _start(c, token, **extra):
    body = {"token": token, "mode": "slicer"}
    body.update(extra)
    return c.post("/api/preflight/print", json=body)


def test_return_keeps_the_file_and_uploads_nothing(pf):
    main, c, uploads = pf
    r = _start(c, _token(c), deliver="return")
    assert r.status_code == 200, r.text
    assert r.json()["deliver"] == "return"
    job = r.json()["job_id"]
    st = _wait(c, job)
    assert st["error"] is None
    assert st["stage"] == "done" and st["result"] is True and st["size"] > 0
    assert uploads == []

    got = c.get("/api/preflight/result", params={"job_id": job})
    assert got.status_code == 200
    text = got.text
    assert len(got.content) == st["size"]
    assert "; multiACE processed:" in text
    assert "ACE_SWAP_HEAD" in text
    assert "sample.gcode" in got.headers["content-disposition"]

    # Served once: the file is gone, the working files too.
    again = c.get("/api/preflight/result", params={"job_id": job})
    assert again.status_code == 410
    assert not list(main._PREFLIGHT_DIR.glob(job + "*"))
    assert c.get("/api/preflight/print/status",
                 params={"job_id": job}).json()["result"] is False


def test_default_still_uploads_to_moonraker(pf):
    main, c, uploads = pf
    r = _start(c, _token(c))
    assert r.json()["deliver"] == "print"
    job = r.json()["job_id"]
    st = _wait(c, job)
    assert uploads and uploads[0].endswith("/server/files/upload")
    assert "moonraker" in (st["error"] or "")
    assert st["result"] is False
    assert c.get("/api/preflight/result",
                 params={"job_id": job}).status_code == 409


def test_result_refusals(pf):
    main, c, _ = pf
    assert c.get("/api/preflight/result",
                 params={"job_id": "nope"}).status_code == 404
    token = _token(c)
    assert _start(c, token, deliver="keep").status_code == 400
    assert _start(c, token, deliver="return", queue=True).status_code == 400


def test_a_failed_return_job_reports_its_error(pf, monkeypatch):
    main, c, _ = pf

    def boom(*a, **k):
        raise RuntimeError("material not loaded")

    monkeypatch.setattr(main.preflight_core, "rewrite_pipeline", boom)
    job = _start(c, _token(c), deliver="return").json()["job_id"]
    st = _wait(c, job)
    assert st["error"] == "material not loaded" and st["result"] is False
    r = c.get("/api/preflight/result", params={"job_id": job})
    assert r.status_code == 409 and "material not loaded" in r.text


def test_an_unfetched_result_goes_with_the_job(pf, monkeypatch):
    main, c, _ = pf
    job = _start(c, _token(c), deliver="return").json()["job_id"]
    _wait(c, job)
    out = main._PREFLIGHT_DIR / (job + ".out.gcode")
    assert out.is_file()
    main._PREFLIGHT_JOBS[job]["ts"] -= main._PREFLIGHT_JOB_TTL + 1
    main._prune_old_jobs()
    assert not out.exists() and job not in main._PREFLIGHT_JOBS


def test_assignment_print_in_multi_keeps_slot_equals_head(pf, monkeypatch):
    """Multi with colour copies prints through the assignment pipeline. The
    server path must keep the multi topology: slot N feeds head N."""
    main, c, _ = pf

    async def head():
        # what the printer reports in multi: the head-mode wiring is still
        # in the context (it is persisted), only the mode says multi
        return {"mode": "multi", "max_copies": 2, "ace_head": 3,
                "ace_heads": [0, 1, 2, 3],
                "head_ace": {0: 0, 1: 1, 2: 2, 3: 3}}

    monkeypatch.setattr(main, "_head_mode_context", head)
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
        "deliver": "return", "head_assignment": choice})
    assert r.status_code == 200, r.text
    st = _wait(c, r.json()["job_id"])
    assert not st.get("error"), st
    out = c.get("/api/preflight/result",
                params={"job_id": r.json()["job_id"]}).text
    swaps = [l for l in out.splitlines() if l.startswith("ACE_SWAP_HEAD")]
    assert swaps
    for line in swaps:
        f = dict(p.split("=") for p in line.split()[1:] if "=" in p)
        assert f["HEAD"] == f["SLOT"], line
