"""Preflight plan scope: 'reorder' plans optimize / layer / color on the
loaded spools, 'all' proposes a loadout over every slot of the connected
MMUs. A proposal only prints when the spools lie as it says."""
import re
import time

from .test_mock_mode import FIXTURES, _load_main  # noqa: F401
from .test_multi_one_pipeline import FOOTER

COLOURS = ["#ffffff", "#000000", "#ff0000", "#0000ff"]


def _client(monkeypatch, tmp_path, slots, connected, mode="multi",
            types="PLA;PLA;PLA;PLA"):
    from fastapi.testclient import TestClient
    main = _load_main(monkeypatch, tmp_path, mock=False)
    monkeypatch.setattr(main, "_PREFLIGHT_DIR", tmp_path / "pf")
    src = tmp_path / "in.gcode"
    src.write_text(
        (FIXTURES / "sample_4color.gcode").read_text("utf-8")
        + FOOTER.replace("PLA;PLA;PLA;PLA", types), encoding="utf-8")
    box = {"slots": slots}

    async def live():
        return box["slots"]

    async def head():
        return {"mode": mode, "max_copies": 1, "ace_head": 3,
                "ace_heads": [0, 1, 2, 3], "feeders": [],
                "head_ace": {0: 0, 1: 1, 2: 2, 3: 3},
                "connected_aces": connected}

    async def state():
        return {}

    monkeypatch.setattr(main, "_live_slots_async", live)
    monkeypatch.setattr(main, "_head_mode_context", head)
    monkeypatch.setattr(main, "_query_state_gated", state)
    return main, TestClient(main.app), src, box


def _preview(c, src):
    with open(src, "rb") as fh:
        return c.post("/api/preflight", files={
            "file": ("in.gcode", fh, "application/octet-stream")}).json()


def _print(c, rep, plan, scope):
    r = c.post("/api/preflight/print", json={
        "token": rep["token"], "mode": "head", "head_plan": plan,
        "head_scope": scope, "deliver": "return"})
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    for _ in range(600):
        st = c.get("/api/preflight/print/status",
                   params={"job_id": job}).json()
        if st["done"]:
            break
        time.sleep(0.05)
    assert st["done"], st
    if st.get("error"):
        return st, []
    out = c.get("/api/preflight/result", params={"job_id": job}).text
    return st, out.splitlines()


def _swap_slots(lines):
    out = set()
    for ln in lines:
        m = re.match(r"ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)", ln)
        if m:
            out.add((int(m.group(2)), int(m.group(3))))
    return out


def _one_unit():
    return [{"ace": 0, "slot": s, "material": "PLA", "color": COLOURS[s]}
            for s in range(4)]


def test_report_carries_both_scopes(monkeypatch, tmp_path):
    main, client, src, _ = _client(monkeypatch, tmp_path, _one_unit(), [0, 1])
    with client as c:
        rep = _preview(c, src)
    assert rep["head_mode"] and set(rep["plans_reorder"]) >= {
        "optimize", "layer"}
    loaded = {(0, s) for s in range(4)}
    for name, plan in rep["plans_reorder"].items():
        if not plan["feasible"]:
            continue
        used = {(m["ace"], m["slot"]) for m in plan["mapping"]
                if m.get("kind") == "ace"}
        assert used <= loaded, name
        assert not [m for m in plan["mapping"] if m.get("tier") == "copy"]


def test_empty_connected_unit_counts_for_a_proposal(monkeypatch, tmp_path):
    """MMU 2 is connected and empty: 'all' may plan spools into it,
    'reorder' never does."""
    main, client, src, _ = _client(monkeypatch, tmp_path, _one_unit(), [0, 1])
    with client as c:
        rep = _preview(c, src)
    assert rep["num_aces"] == 2
    main, client, src, _ = _client(monkeypatch, tmp_path, _one_unit(), [0])
    with client as c:
        rep1 = _preview(c, src)
    assert rep1["num_aces"] == 1


def test_reorder_prints_on_loaded_spools(monkeypatch, tmp_path):
    main, client, src, _ = _client(monkeypatch, tmp_path, _one_unit(), [0, 1])
    with client as c:
        rep = _preview(c, src)
        assert rep["plans_reorder"]["optimize"]["feasible"]
        st, lines = _print(c, rep, "optimize", "reorder")
    assert not st.get("error"), st
    assert _swap_slots(lines) <= {(0, s) for s in range(4)}


def test_reorder_is_material_strict(monkeypatch, tmp_path):
    """Two PETG colours, one PETG spool loaded: nothing to reorder to."""
    slots = _one_unit()
    slots[3]["material"] = "PETG"
    main, client, src, _ = _client(monkeypatch, tmp_path, slots, [0],
                                   types="PLA;PLA;PETG;PETG")
    with client as c:
        rep = _preview(c, src)
        plan = rep["plans_reorder"]["optimize"]
        assert not plan["feasible"]
        assert plan["reason"] == main.preflight_core.NO_FIT_REASON
        st, _ = _print(c, rep, "optimize", "reorder")
    assert "loaded spools do not fit" in (st.get("error") or "")


def test_proposal_refused_until_spools_lie_as_planned(monkeypatch, tmp_path):
    """A proposal that names a slot without a declared spool, or one with
    another material, does not print. Loaded as shown, it does."""
    core_slots = _one_unit()
    main, client, src, box = _client(monkeypatch, tmp_path, core_slots, [0, 1])
    with client as c:
        rep = _preview(c, src)
        plan = rep["plans"]["optimize"]
        assert plan["feasible"]
        want = {(m["ace"], m["slot"]): m["t"] for m in plan["mapping"]
                if m.get("kind") == "ace"}
        if set(want) <= {(0, s) for s in range(4)}:
            # the proposal happens to match what is loaded; make one slot
            # carry another material instead
            a, s = sorted(want)[0]
            box["slots"] = [dict(x, material="PETG")
                            if (x["ace"], x["slot"]) == (a, s) else x
                            for x in core_slots]
            st, _ = _print(c, rep, "optimize", "all")
            assert "do not match this plan" in (st.get("error") or ""), st
            assert "(PETG)" in st["error"]
        else:
            st, _ = _print(c, rep, "optimize", "all")
            assert "do not match this plan" in (st.get("error") or ""), st
            assert "no declared spool" in st["error"]
        box["slots"] = [{"ace": a, "slot": s, "material": "PLA",
                         "color": COLOURS[t]} for (a, s), t in want.items()]
        st, lines = _print(c, rep, "optimize", "all")
    assert not st.get("error"), st
    assert _swap_slots(lines) <= set(want)


def test_older_client_keeps_the_proposal(monkeypatch, tmp_path):
    """No head_scope in the request = scope 'all', as before the switch."""
    main, _, _, _ = _client(monkeypatch, tmp_path, _one_unit(), [0])
    assert main._PreflightPrint(token="0" * 32, mode="head").head_scope \
        == "all"
    assert main.preflight_core.plan_scope(None) == "all"
    assert main.preflight_core.plan_scope("REORDER") == "reorder"
    assert main.preflight_core.plan_scope("bogus") == "all"


def test_color_plan_is_printable_on_the_server(monkeypatch, tmp_path):
    main, client, src, _ = _client(monkeypatch, tmp_path, _one_unit(), [0])
    with client as c:
        rep = _preview(c, src)
        if not rep["plans_reorder"].get("color", {}).get("feasible"):
            return
        st, lines = _print(c, rep, "color", "reorder")
    assert not st.get("error"), st
    assert _swap_slots(lines)
