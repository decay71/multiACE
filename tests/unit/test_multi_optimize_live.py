"""Multi mode: optimize/layer plan on the loaded spools.

Field case (forum, 2026-11-06): two ACEs, ACE 1 slots 1-4 PLA, ACE 2
slot 2 PETG white, slot 3 PETG orange, a two-colour PETG file. The
optimize plan used to assign MMU 1 per head and show the slicer's
material, so it promised 0 swaps on PLA slots and printed from them.
"""
import pytest

from multiace.tools import post_process_virtual_toolheads as pp
from multiace.web.backend import preflight_core as pc


def _slot(ace, slot, mat, color):
    return {"ace": ace, "slot": slot, "material": mat, "color": color}


LIVE = [
    _slot(0, 0, "PLA", "#000000"), _slot(0, 1, "PLA", "#ffffff"),
    _slot(0, 2, "PLA", "#ff0000"), _slot(0, 3, "PLA", "#00ff00"),
    _slot(1, 0, "PLA", "#0000ff"), _slot(1, 1, "PETG", "#ffffff"),
    _slot(1, 2, "PETG", "#ff8000"), _slot(1, 3, "PLA", "#888888"),
]
COLORS = {0: "#ffffff", 1: "#ff8000"}
TYPES = {0: "PETG", 1: "PETG"}
EVENTS = [0, 1, 0, 1, 0, 1]


def _plan(name, live=LIVE):
    result = {"events": EVENTS, "total_changes": len(EVENTS) - 1,
              "layer_info": {"layer_color_sets": [{0, 1}]}}
    return pc.build_one_plan(pp, name, result, [], slicer_colors=COLORS,
                             slicer_types=TYPES, num_aces=2,
                             live_slots=live)


@pytest.mark.parametrize("name", ["optimize", "layer"])
def test_plan_uses_the_loaded_petg_slots(name):
    plan = _plan(name)
    assert plan["feasible"]
    got = {m["t"]: (m["slot"]["ace"], m["slot"]["slot"],
                    m["slot"]["material"]) for m in plan["mapping"]}
    assert got == {0: (1, 1, "PETG"), 1: (1, 2, "PETG")}
    assert plan["swaps"] == 0


def test_no_fit_is_infeasible_not_a_wrong_slot():
    live = [s for s in LIVE if s["material"] == "PLA"] + [
        _slot(1, 1, "PETG", "#ffffff")]
    plan = _plan("optimize", live)
    # Both PETG colours need slot index 1 (the only PETG) - one head, two
    # colours, but only one MMU carries PETG there.
    assert not plan["feasible"]
    assert plan["mapping"] == []


def test_first_load_is_not_a_swap():
    mapping = [{"t": 0, "slot": {"ace": 1, "slot": 1}},
               {"t": 1, "slot": {"ace": 1, "slot": 2}}]
    assert pc._real_swap_count(EVENTS, mapping) == 0
    mapping[1]["slot"] = {"ace": 0, "slot": 1}
    assert pc._real_swap_count(EVENTS, mapping) == 5


def test_remap_material_check_refuses_wrong_slot():
    with pytest.raises(pc.PreflightRejected) as ei:
        pc._check_remap_materials({0: 1}, TYPES, LIVE)   # MMU 1 slot 2 = PLA
    assert "MMU 1 slot 2 (PLA)" in str(ei.value)
    pc._check_remap_materials({0: 5, 1: 6}, TYPES, LIVE)  # PETG slots


def test_material_gate_respects_nozzle_and_manual_heads():
    gate = pc.material_gate(TYPES, LIVE, {0: {1}}, skip_heads={2})
    assert gate[0] == {1}
    assert gate[1] == {1}
