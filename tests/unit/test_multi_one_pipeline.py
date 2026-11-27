"""Multi prints take the assignment pipeline. The classic synthetic-T
pipeline (MULTI_CLASSIC_PIPELINE) must give the same print: same tool and
temperature lines, same real swaps, same purge stamps."""
import time

import pytest

from .test_mock_mode import FIXTURES, _load_main  # noqa: F401

FOOTER = (
    "; filament_colour = #FFFFFF;#000000;#FF0000;#0000FF\n"
    "; filament_type = PLA;PLA;PLA;PLA\n"
    "; flush_multiplier = 1\n"
    "; flush_volumes_matrix = 0,280,300,290,450,0,400,380,330,250,0,270,"
    "340,260,280,0\n"
    "; nozzle_diameter = 0.4,0.4,0.4,0.4\n")
COLOURS = ["#ffffff", "#000000", "#ff0000", "#0000ff"]
# colour t -> (mmu, slot); in multi the slot is the head
PLANS = {
    "one_unit": {0: (0, 0), 1: (0, 1), 2: (0, 2), 3: (0, 3)},
    "two_per_head": {0: (0, 0), 1: (1, 0), 2: (0, 2), 3: (1, 2)},
    "mixed": {0: (0, 0), 1: (0, 1), 2: (1, 1), 3: (0, 3)},
}


def _slots(plan):
    slots = [{"ace": a, "slot": s, "material": "PLA",
              "color": "#%02x5a%02x" % (40 * a + 10 * s, 200 - 30 * s)}
             for a in (0, 1) for s in range(4)]
    for t, (a, s) in plan.items():
        for x in slots:
            if (x["ace"], x["slot"]) == (a, s):
                x["color"] = COLOURS[t]
    return slots


def _run(monkeypatch, tmp_path, plan, classic):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=False)
    monkeypatch.setattr(main, "_PREFLIGHT_DIR", tmp_path / "pf")
    monkeypatch.setattr(main.preflight_core, "MULTI_CLASSIC_PIPELINE",
                        classic)
    src = tmp_path / "in.gcode"
    src.write_text((FIXTURES / "sample_4color.gcode").read_text("utf-8")
                   + FOOTER, encoding="utf-8")

    async def live():
        return _slots(plan)

    async def head():
        return {"mode": "multi", "max_copies": 1, "ace_head": 3,
                "ace_heads": [0, 1, 2, 3],
                "head_ace": {0: 0, 1: 1, 2: 2, 3: 3},
                "connected_aces": [0, 1]}

    async def state():
        return {}

    monkeypatch.setattr(main, "_live_slots_async", live)
    monkeypatch.setattr(main, "_head_mode_context", head)
    monkeypatch.setattr(main, "_query_state_gated", state)
    with TestClient(main.app) as c:
        with open(src, "rb") as fh:
            rep = c.post("/api/preflight", files={
                "file": ("in.gcode", fh, "application/octet-stream")}).json()
        if classic:
            assert not rep.get("head_mode")
            shown = rep["plans"]["slicer"]["swaps"]
            body = {"token": rep["token"], "mode": "slicer",
                    "remap": {str(t): a * 4 + s
                              for t, (a, s) in plan.items()}}
        else:
            assert rep.get("head_mode") and rep.get("topology") == "multi"
            shown = rep["plans"]["loadout"]["swaps"]
            body = {"token": rep["token"], "mode": "head",
                    "head_plan": "loadout",
                    "head_assignment": {str(t): "slot-%d-%d" % (a, s)
                                        for t, (a, s) in plan.items()}}
        body["deliver"] = "return"
        r = c.post("/api/preflight/print", json=body)
        assert r.status_code == 200, r.text
        job = r.json()["job_id"]
        for _ in range(600):
            st = c.get("/api/preflight/print/status",
                       params={"job_id": job}).json()
            if st["done"]:
                break
            time.sleep(0.05)
        assert st["done"] and not st.get("error"), st
        out = c.get("/api/preflight/result", params={"job_id": job}).text
    return shown, out.splitlines()


def _body(lines):
    """Lines behind the auto-load block."""
    end = lines.index("; multiACE auto-load: end")
    return lines[end + 1:]


def _keep(lines, *prefixes):
    return [ln for ln in lines if ln.startswith(prefixes)]


@pytest.mark.parametrize("name", sorted(PLANS))
def test_both_pipelines_print_the_same(monkeypatch, tmp_path, name):
    plan = PLANS[name]
    shown_c, old = _run(monkeypatch, tmp_path, plan, True)
    shown_a, new = _run(monkeypatch, tmp_path, plan, False)
    assert shown_a == shown_c
    for prefixes in (("T0", "T1", "T2", "T3"), ("M104", "M109"),
                     ("ACE_SET_PURGE",)):
        assert _keep(new, *prefixes) == _keep(old, *prefixes), prefixes
    # the auto-load block loads the same heads from the same slots
    block = lambda ls: _keep(ls[:ls.index("; multiACE auto-load: end")],
                             "ACE_SWAP_HEAD")
    assert block(new) == block(old)
    # every swap of the classic file is in the new one, in order; the new
    # one adds a swap line at a head's first use (a no-op on a loaded head)
    strip = lambda ls: [ln.split(" ANTI_OOZE")[0]
                        for ln in _keep(_body(ls), "ACE_SWAP_HEAD")]
    old_s, new_s = strip(old), strip(new)
    loaded = {ln for ln in (x.replace(" INITIAL=1", "") for x in block(new))}
    it = iter(new_s)
    for want in old_s:
        for got in it:
            if got == want:
                break
            assert got in loaded, (got, want)
        else:
            raise AssertionError("missing swap %s" % want)
    assert all(x in loaded for x in it)
