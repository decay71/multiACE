"""The preflight estimate: the swap timeline, the estimate context and its
attachment to the preflight plans (estimate lane, F1a)."""
import importlib
import sys
from pathlib import Path

import pytest

from multiace.tools import post_process_virtual_toolheads as pp
from multiace.tools import swap_cost as sc
from multiace.web.backend import preflight_core as pc

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "multiace" / "web" / "backend"
FIXTURES = ROOT / "tests" / "fixtures"

HEADER = (
    "; filament_colour = #FF0000;#00FF00;#0000FF\n"
    "; filament_type = PLA;PLA;PLA\n"
    "; filament used [mm] = 1000.0, 2000.0, 500.0\n"
    "; filament used [g] = 3.0, 6.0, 1.5\n"
    "; total filament used [g] = 10.5\n"
    "; estimated printing time (normal mode) = 1h 0m 0s\n")


def _gcode(sequence, flush=""):
    """A tiny multi-colour print: one toolchange per entry of `sequence`."""
    lines = [HEADER, flush, "T%d\n" % sequence[0]]
    for i, (a, b) in enumerate(zip(sequence, sequence[1:])):
        lines.append(";LAYER_CHANGE\n; Change Tool %d -> Tool %d\n" % (a, b))
        lines.append("T%d\nM73 P%d R%d\n" % (b, 10 * (i + 1), 50 - 10 * i))
    return "".join(lines)


def _ace(head, ace=0, slot=None):
    return {"kind": "ace", "head": head, "ace": ace,
            "slot": head if slot is None else slot}


@pytest.fixture
def model():
    return sc.SwapCostModel.default()


class TestSwapTimeline:
    def test_first_load_then_a_swap_on_the_same_head(self, model):
        assignment = {0: _ace(0, slot=0), 1: _ace(0, slot=1)}
        tl = pp.build_swap_timeline([0, 1], assignment, cost_model=model)
        assert [e["kind"] for e in tl] == ["first_load", "same_ace"]
        assert tl[1]["from_t"] == 0 and tl[1]["t"] == 1
        assert tl[1]["seconds"] == pytest.approx(
            model.swap_seconds("same_ace"), abs=0.1)

    def test_a_toolchange_onto_an_unchanged_slot_is_only_a_head_change(
            self, model):
        assignment = {0: _ace(0), 1: _ace(1)}
        tl = pp.build_swap_timeline([0, 1, 0, 1], assignment,
                                    cost_model=model)
        assert [e["kind"] for e in tl] == [
            "first_load", "first_load", "head_change", "head_change"]
        change = tl[-1]
        assert change["seconds"] == pytest.approx(4.2)
        assert change["purge_mm"] == 0 and change["purge_inline"] is False

    def test_a_different_ace_is_a_cross_ace_swap(self, model):
        assignment = {0: _ace(0, ace=0), 1: _ace(0, ace=1)}
        tl = pp.build_swap_timeline([0, 1], assignment, cost_model=model)
        assert tl[1]["kind"] == "cross_ace_inline"

    def test_a_pinned_feeder_costs_a_head_change_per_toolchange(self, model):
        assignment = {0: {"kind": "pin", "head": 0}, 1: _ace(1)}
        tl = pp.build_swap_timeline([0, 1, 0], assignment, cost_model=model)
        assert [(e["t"], e["kind"]) for e in tl] == [
            (0, "head_change"), (1, "first_load"), (0, "head_change")]
        assert tl[0]["ace"] is None and tl[0]["seconds"] == pytest.approx(4.2)

    def test_the_head_change_follows_the_model_constant(self):
        m = sc.SwapCostModel.default()
        m.constants["head_change"] = 6.0
        tl = pp.build_swap_timeline(
            [0, 0], {0: _ace(0)}, cost_model=m)
        assert tl[-1]["seconds"] == pytest.approx(6.0)
        assert pp.build_swap_timeline(
            [0, 0], {0: _ace(0)})[-1]["seconds"] == pytest.approx(4.2)

    def test_a_long_enough_window_makes_the_swap_a_background_one(self, model):
        assignment = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}
        events = [0, 2, 1]
        # remaining minutes at each event: head 0 idle for 20 min before T1
        times = [60, 50, 30]
        tl = pp.build_swap_timeline(events, assignment, event_times=times,
                                    bg_heads=[0], cost_model=model)
        last = tl[-1]
        assert last["kind"] == "cross_ace_bg"
        assert last["window_min"] == pytest.approx(20.0)
        assert last["seconds"] == pytest.approx(
            model.swap_seconds("cross_ace_bg"), abs=0.1)

    def test_a_short_window_degrades_to_inline(self, model):
        assignment = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}
        tl = pp.build_swap_timeline([0, 2, 1], assignment,
                                    event_times=[60, 50, 49.9],
                                    bg_heads=[0], cost_model=model)
        assert tl[-1]["kind"] == "cross_ace_inline"

    def test_the_bg_window_is_the_one_the_rewrite_stamps_from(self, model):
        """The rewrite stamps a bg swap from BG_UNLOAD_MIN_WINDOW_MIN on, not
        from the model's longer unload + load time."""
        assignment = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}
        window = pp.BG_UNLOAD_MIN_WINDOW_MIN + 0.5
        assert window < model.bg_window_minutes()
        tl = pp.build_swap_timeline([0, 2, 1], assignment,
                                    event_times=[60, 50, 50 - window],
                                    bg_heads=[0], cost_model=model)
        assert tl[-1]["kind"] == "cross_ace_bg"

    def test_a_window_without_m73_time_is_stamped_as_bg_anyway(self, model):
        assignment = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}
        tl = pp.build_swap_timeline([0, 2, 1], assignment, bg_heads=[0],
                                    cost_model=model)
        assert tl[-1]["kind"] == "cross_ace_bg"
        assert tl[-1]["window_min"] is None
        # only a bg-enabled head: the others stay inline
        off = pp.build_swap_timeline([0, 2, 1], assignment, cost_model=model)
        assert off[-1]["kind"] == "cross_ace_inline"

    def test_unknown_windows_are_counted_in_the_estimate(self, model):
        assignment = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}
        ctx = pc.make_estimate_ctx(pp, HEADER, bg_heads=[0])
        plan = pc.attach_estimate(ctx, {"feasible": True}, [0, 2, 1],
                                  assignment)
        assert plan["estimate"]["unknown_window_swaps"] == 1
        assert plan["estimate"]["bg_swaps"] == 1
        ctx = pc.make_estimate_ctx(pp, HEADER)
        plan = pc.attach_estimate(ctx, {"feasible": True}, [0, 2, 1],
                                  assignment)
        assert plan["estimate"]["unknown_window_swaps"] == 0

    def test_purge_is_the_stamp_the_engine_writes(self, model):
        # 1000 mm3 per pair * 0.45 / 2.405 mm3 per mm = ~187 -> clamped 150
        matrix = [[0, 1000, 1000], [1000, 0, 1000], [1000, 1000, 0]]
        assignment = {0: _ace(0, slot=0), 1: _ace(0, slot=1)}
        tl = pp.build_swap_timeline([0, 1], assignment, cost_model=model,
                                    flush_matrix=matrix)
        assert tl[1]["purge_mm"] == pp._matrix_purge_mm(matrix, 0, 1)
        assert tl[1]["purge_mm"] == 150

    def test_without_a_matrix_the_stock_purge_applies(self, model):
        assignment = {0: _ace(0, slot=0), 1: _ace(0, slot=1)}
        tl = pp.build_swap_timeline([0, 1], assignment, cost_model=model)
        assert tl[1]["purge_mm"] == sc.STOCK_FLUSH_DEFAULT_MM

    def test_bg_purge_is_flagged_inline_when_the_bg_load_is_off(self, model):
        assignment = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}
        kw = dict(event_times=[60, 50, 30], bg_heads=[0], cost_model=model)
        on = pp.build_swap_timeline([0, 2, 1], assignment, **kw)
        off = pp.build_swap_timeline([0, 2, 1], assignment,
                                     bg_purge_inline=True, **kw)
        assert on[-1]["purge_inline"] is False
        assert off[-1]["purge_inline"] is True
        assert off[0]["purge_inline"] is False   # only bg swaps carry it

    def test_unclassifiable_input_yields_no_entries(self, model):
        assert pp.build_swap_timeline([], {}, cost_model=model) == []
        assert pp.build_swap_timeline([5], {}, cost_model=model) == []


class TestStampMatrix:
    def test_the_text_parser_applies_the_multiplier_upward_only(self):
        text = ("; flush_volumes_matrix = 0,100,100,0\n"
                "; flush_multiplier = 0.5\n")
        assert pp.parse_flush_matrix_raw(text) == [[0, 100], [100, 0]]
        text = text.replace("0.5", "2")
        assert pp.parse_flush_matrix_raw(text) == [[0, 200], [200, 0]]

    def test_no_matrix_is_none(self):
        assert pp.parse_flush_matrix_raw("G1 X1\n") is None


class TestEstimateContext:
    def test_no_header_means_no_estimate(self):
        assert pc.make_estimate_ctx(pp, None) is None

    def test_a_post_processor_without_a_timeline_means_no_estimate(self):
        class OldPP:
            pass
        assert pc.make_estimate_ctx(OldPP(), HEADER) is None

    def test_both_cost_param_shapes_are_accepted(self):
        flat = pc.make_estimate_ctx(pp, HEADER, cost_params={"load_length": 4200})
        split = pc.make_estimate_ctx(
            pp, HEADER, cost_params={"main": {"load_length": 4200}})
        assert flat["model"].load_length == split["model"].load_length == 4200

    @pytest.mark.parametrize("mode,inline", [
        ("true", False), ("True", False), ("false", True),
        ("transport", True), ("Transport", True)])
    def test_only_a_full_bg_load_keeps_the_purge_off_the_critical_path(
            self, mode, inline):
        ctx = pc.make_estimate_ctx(
            pp, HEADER, cost_params={"main": {"bg_load": mode}})
        assert ctx["bg_purge_inline"] is inline

    def test_without_a_bg_load_setting_the_bg_swap_is_a_full_one(self):
        assert pc.make_estimate_ctx(pp, HEADER)["bg_purge_inline"] is False

    def test_a_failing_timeline_leaves_the_plan_without_an_estimate(self):
        class BrokenPP:
            @staticmethod
            def build_swap_timeline(*a, **k):
                raise RuntimeError("boom")
        ctx = pc.make_estimate_ctx(BrokenPP(), HEADER)
        plan = pc.attach_estimate(ctx, {"feasible": True}, [0, 1],
                                  {0: _ace(0)})
        assert "estimate" not in plan and "timeline" not in plan

    def test_an_infeasible_plan_gets_no_estimate(self):
        ctx = pc.make_estimate_ctx(pp, HEADER)
        plan = pc.attach_estimate(ctx, {"feasible": False}, [0, 1],
                                  {0: _ace(0)})
        assert "estimate" not in plan


def _live(*colors):
    return [{"ace": 0, "slot": i, "material": "PLA", "color": c}
            for i, c in enumerate(colors)]


def _report(gcode, head_ctx, live, with_header=True, **kw):
    lines = gcode.splitlines(keepends=True)
    out = pc.parse_meta(pp, lines, with_header=True)
    colors, types, num_aces, _used, proxy, meta, header = out
    return pc.build_report(
        pp, slicer_colors=colors, slicer_types=types, num_aces=num_aces,
        plan_proxy=proxy, live_slots=live, head_ctx=head_ctx, token="t",
        filename="a.gcode", size=len(gcode), meta=meta,
        header_text=header if with_header else None, **kw)


class TestBuildReport:
    SEQ = [0, 1, 2, 0, 1, 2, 0]
    LIVE = _live("#FF0000", "#00FF00", "#0000FF")

    def test_multi_mode_plans_carry_a_timeline_and_an_estimate(self):
        rep = _report(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE)
        for name in ("slicer", "optimize", "layer"):
            plan = rep["plans"][name]
            assert plan["timeline"], name
            est = plan["estimate"]
            assert est["base_s"] == 3600
            assert est["total_s"] > 3600
            assert est["confidence"] == "modelled"

    def test_more_swaps_cost_more_time(self):
        calm = _report(_gcode([0, 1]), {"mode": "normal"}, self.LIVE)
        busy = _report(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE)
        assert (busy["plans"]["slicer"]["estimate"]["added_s"]
                > calm["plans"]["slicer"]["estimate"]["added_s"])

    def test_without_header_text_there_is_no_estimate(self):
        rep = _report(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE,
                      with_header=False)
        assert all("estimate" not in p for p in rep["plans"].values())

    def test_cost_params_move_the_estimate(self):
        base = _report(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE)
        long_bowden = _report(
            _gcode(self.SEQ), {"mode": "normal"}, self.LIVE,
            cost_params={"main": {"load_length": 4200}})
        assert (long_bowden["plans"]["slicer"]["estimate"]["added_s"]
                > base["plans"]["slicer"]["estimate"]["added_s"])

    def test_head_mode_plans_carry_an_estimate(self):
        # one ACE on head 3: every colour change is a swap on that head
        head_ctx = {"mode": "head", "ace_head": 3, "ace_heads": [3],
                    "head_ace": {3: 0}, "feeders": []}
        live = _live("#FF0000", "#00FF00", "#0000FF", "#FFFF00")
        rep = _report(_gcode(self.SEQ), head_ctx, live)
        assert rep["head_mode"]
        loadout = rep["plans"]["loadout"]
        assert loadout["timeline"]
        assert loadout["estimate"]["total_s"] > 3600
        # the proposals pin colours on the stock feeders, so they swap less
        for name in ("optimize", "layer"):
            plan = rep["plans"][name]
            assert plan["feasible"], name
            assert plan["estimate"]["base_s"] == 3600, name
            assert (plan["estimate"]["added_s"]
                    < loadout["estimate"]["added_s"]), name


def _load_main(monkeypatch, tmp_path, cfg_text):
    cfg = tmp_path / "ace.cfg"
    cfg.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("MULTIACE_CFG_PATH", str(cfg))
    monkeypatch.setenv("MULTIACE_MOCK_DIR", str(FIXTURES))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    monkeypatch.syspath_prepend(str(BACKEND))
    sys.modules.pop("main", None)
    return importlib.import_module("main")


class TestSwapCostParams:
    CFG = ("[ace]\nload_length: 3000\nfeed_speed: 100\nswap_purge_length: 60\n"
           "purge_matrix: false\nunrelated: 7\n"
           "[ace 1]\nload_length: 2500\nload_length_2: 1800\n")

    def test_scalars_come_out_of_the_ace_sections(self, monkeypatch, tmp_path):
        main = _load_main(monkeypatch, tmp_path, self.CFG)
        params = main._swap_cost_params()
        assert params["main"]["load_length"] == 3000
        assert params["main"]["swap_purge_length"] == 60
        assert params["main"]["purge_matrix"] is False
        assert "unrelated" not in params["main"]
        assert params["per_ace"][1] == {"load_length": 2500,
                                        "load_length_2": 1800}

    def test_bg_load_defaults_to_a_full_bg_swap(self, monkeypatch, tmp_path):
        main = _load_main(monkeypatch, tmp_path, self.CFG)
        assert main._swap_cost_params()["main"]["bg_load"] == "true"

    @pytest.mark.parametrize("value,mode", [
        ("True", "true"), ("False", "false"), ("transport", "transport"),
        ("Transport", "transport"), ("0", "false")])
    def test_bg_load_is_read_as_a_string(self, monkeypatch, tmp_path,
                                         value, mode):
        main = _load_main(
            monkeypatch, tmp_path,
            self.CFG + "[ace_bg_swap]\nheads: 3\nload_enabled: %s\n" % value)
        assert main._swap_cost_params()["main"]["bg_load"] == mode

    def test_a_missing_config_yields_empty_params(self, monkeypatch, tmp_path):
        main = _load_main(monkeypatch, tmp_path, "[ace]\n")
        monkeypatch.setattr(main, "MULTIACE_CFG_PATH",
                            str(tmp_path / "gone.cfg"))
        assert main._swap_cost_params() == {"main": {}, "per_ace": {}}


class TestColourCopies:
    """One colour on two heads: the timeline follows the per-event choice."""

    # T0 and T1 fight over head 0; a copy of T0 on head 1 ends the fight.
    ASSIGN = {0: _ace(0, ace=0, slot=0), 1: _ace(0, ace=0, slot=1)}
    COPIES = {0: [_ace(1, ace=0, slot=2)]}
    EVENTS = [0, 1, 0, 1, 0, 1]

    def test_a_copy_removes_the_swaps_it_saves(self, model):
        plain = pp.build_swap_timeline(self.EVENTS, self.ASSIGN,
                                       cost_model=model)
        with_copy = pp.build_swap_timeline(
            self.EVENTS, self.ASSIGN, cost_model=model, copies=self.COPIES)
        swaps = lambda tl: [e for e in tl if e["kind"] != "head_change"]
        assert len(swaps(plain)) == 6
        assert len(swaps(with_copy)) < len(swaps(plain))
        assert (pp.head_mode_swap_count(self.EVENTS, self.ASSIGN, self.COPIES)
                == len(swaps(with_copy)))
        # every toolchange is in the timeline, swap or not
        assert len(with_copy) == len(plain) == len(self.EVENTS)

    def test_a_copy_entry_names_the_head_that_took_the_event(self, model):
        tl = pp.build_swap_timeline(self.EVENTS, self.ASSIGN,
                                    cost_model=model, copies=self.COPIES)
        assert {e["head"] for e in tl} == {0, 1}
        assert all(e["slot"] in (0, 1, 2) for e in tl)

    def test_without_copies_nothing_changes(self, model):
        a = pp.build_swap_timeline(self.EVENTS, self.ASSIGN, cost_model=model)
        b = pp.build_swap_timeline(self.EVENTS, self.ASSIGN,
                                   cost_model=model, copies={})
        assert a == b

    def test_attach_estimate_prices_the_copy_plan_lower(self):
        ctx = pc.make_estimate_ctx(pp, HEADER)
        plain = pc.attach_estimate(ctx, {"feasible": True}, self.EVENTS,
                                   self.ASSIGN)
        copy = pc.attach_estimate(ctx, {"feasible": True}, self.EVENTS,
                                  self.ASSIGN, self.COPIES)
        assert copy["estimate"]["added_s"] < plain["estimate"]["added_s"]

    def test_the_copy_suggestion_reports_what_it_saves_in_time(self):
        ctx = pc.make_estimate_ctx(pp, HEADER)
        head_ctx = {"max_copies": 2, "empty_slots": [{"ace": 0, "slot": 2}]}
        plan = pc.copy_plan(
            pp, self.EVENTS, self.ASSIGN, head_ctx, _TwoHeads(), {},
            estimate_ctx=ctx)
        assert plan["swaps_after"] < plan["swaps_before"]
        assert (plan["estimate_after"]["added_s"]
                < plan["estimate_before"]["added_s"])

    def test_no_suggestion_means_no_estimate_fields(self):
        ctx = pc.make_estimate_ctx(pp, HEADER)
        plan = pc.copy_plan(pp, self.EVENTS, self.ASSIGN, {"max_copies": 1},
                            _TwoHeads(), {}, estimate_ctx=ctx)
        assert "estimate_after" not in plan


class _TwoHeads(dict):
    """Minimal ace_head_of_ace: slots 0,1 feed head 0 and slot 2 head 1."""
    heads = (0, 1)
    num_aces = 1

    def __call__(self, ace, slot):
        return 0 if slot < 2 else 1


FORCA_MIXED = (
    "; generated by FOrcaSlicer 2.3.2 on 2026-10-01\n"
    "; nozzle_diameter = 0.4,0.4,0.6\n")


class TestMixedNozzles:
    LIVE = _live("#FF0000", "#00FF00", "#0000FF")
    SEQ = [0, 1, 2, 0, 1, 2, 0]

    def test_a_gated_plan_still_gets_its_estimate(self):
        gcode = FORCA_MIXED + _gcode(self.SEQ)
        head_ctx = {"mode": "normal", "head_nozzles": {0: 0.4, 1: 0.4, 2: 0.6}}
        rep = _report(gcode, head_ctx, self.LIVE)
        assert rep["nozzles_mixed"] is True
        for name, plan in rep["plans"].items():
            if plan["feasible"]:
                assert plan["estimate"]["base_s"] == 3600, name
                heads = {e["head"] for e in plan["timeline"]}
                # the 0.6 mm filament (T2) can only sit on the 0.6 mm head
                assert all(e["head"] == 2 for e in plan["timeline"]
                           if e["t"] == 2), name
                assert heads <= {0, 1, 2}


def _capture(gcode, head_ctx, live, **kw):
    capture = {}
    rep = _report(gcode, head_ctx, live, estimate_capture=capture, **kw)
    return rep, capture


class TestRecompute:
    SEQ = TestBuildReport.SEQ
    LIVE = TestBuildReport.LIVE

    def test_the_capture_holds_what_a_recompute_needs(self):
        rep, cap = _capture(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE)
        assert cap["ctx"] and cap["events"] == rep["events"]
        assert set(cap["plans"]) == {"slicer", "optimize", "layer"}

    def test_a_swapped_mapping_changes_the_estimate(self):
        rep, cap = _capture(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE)
        mapping = rep["plans"]["slicer"]["mapping"]
        same = pc.recompute_estimate(cap, mapping=mapping, plan="slicer")
        assert same["estimate"]["added_s"] == pytest.approx(
            rep["plans"]["slicer"]["estimate"]["added_s"])
        # put every colour on ACE 1 slot 0: they all fight over one head
        crowded = [dict(r, slot=dict(r["slot"], ace=i, slot=0))
                   for i, r in enumerate(mapping)]
        edited = pc.recompute_estimate(cap, mapping=crowded)
        assert edited["estimate"]["added_s"] != same["estimate"]["added_s"]
        assert edited["timeline"]

    def test_no_context_means_none(self):
        _rep, cap = _capture(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE,
                             with_header=False)
        assert pc.recompute_estimate(cap, mapping=[]) is None
        assert pc.recompute_all_estimates(cap, {}) is None

    def test_new_cost_params_move_every_plan(self):
        rep, cap = _capture(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE)
        before = {n: p["estimate"]["added_s"] for n, p in rep["plans"].items()}
        out = pc.recompute_all_estimates(cap, {"main": {"load_length": 4200}})
        assert set(out) == set(before)
        for name, res in out.items():
            if before[name]:
                assert res["estimate"]["added_s"] > before[name], name
            # the plan dict itself is refreshed too
            assert rep["plans"][name]["estimate"] == res["estimate"]

    def test_an_edit_survives_a_later_cost_change(self):
        rep, cap = _capture(_gcode(self.SEQ), {"mode": "normal"}, self.LIVE)
        mapping = rep["plans"]["slicer"]["mapping"]
        crowded = [dict(r, slot=dict(r["slot"], ace=i, slot=0))
                   for i, r in enumerate(mapping)]
        edited = pc.recompute_estimate(cap, mapping=crowded, plan="slicer")
        out = pc.recompute_all_estimates(cap, {"main": {}})
        assert (out["slicer"]["estimate"]["added_s"]
                == pytest.approx(edited["estimate"]["added_s"]))

    def test_head_mode_recompute_follows_the_target_ids(self):
        head_ctx = {"mode": "head", "ace_head": 3, "ace_heads": [3],
                    "head_ace": {3: 0}, "feeders": []}
        live = _live("#FF0000", "#00FF00", "#0000FF", "#FFFF00")
        rep, cap = _capture(_gcode(self.SEQ), head_ctx, live)
        loadout = rep["plans"]["loadout"]
        target_ids = {m["t"]: m["target_id"] for m in loadout["mapping"]}
        res = pc.recompute_estimate(cap, target_ids=target_ids,
                                    plan="loadout")
        assert res["estimate"]["added_s"] == pytest.approx(
            loadout["estimate"]["added_s"])


class TestBrowserWorker:
    """What the Pyodide worker runs: flat copies of the three modules in one
    directory, no `multiace` package. Driven in a clean interpreter with the
    same calls the worker makes."""

    def test_the_flat_layout_produces_and_recomputes_an_estimate(self, tmp_path):
        import json
        import shutil
        import subprocess
        for src in (ROOT / "multiace" / "tools" / "post_process_virtual_toolheads.py",
                    ROOT / "multiace" / "tools" / "swap_cost.py",
                    BACKEND / "preflight_core.py"):
            shutil.copy(src, tmp_path / src.name)
        job = tmp_path / "job.py"
        job.write_text('''
import sys, json
sys.path.insert(0, %(d)r)
import post_process_virtual_toolheads as _pp
import preflight_core as _core
assert _core._swap_cost is not None, "flat swap_cost import failed"
_captures = {}
_gtext = %(g)r
_live = %(l)r
_colors, _types, _naces, _used, _plan, _meta, _hdr = _core.parse_meta(
    _pp, _gtext.splitlines(True), with_header=True)
_capture = {}
_report = _core.build_report(
    _pp, slicer_colors=_colors, slicer_types=_types, num_aces=_naces,
    plan_proxy=_plan, live_slots=_live, head_ctx={"mode": "normal"},
    token="", filename="a.gcode", size=1, meta=_meta, header_text=_hdr,
    cost_params={"load_length": 2100}, estimate_capture=_capture)
_captures["j"] = _capture
one = _core.recompute_estimate(
    _captures["j"], mapping=_report["plans"]["slicer"]["mapping"])
before = _report["plans"]["slicer"]["estimate"]["added_s"]
every = _core.recompute_all_estimates(_captures["j"], {"load_length": 4200})
print(json.dumps({
    "report": before,
    "one": one["estimate"]["added_s"],
    "all": every["slicer"]["estimate"]["added_s"]}))
''' % {"d": str(tmp_path), "g": _gcode(TestBuildReport.SEQ),
       "l": TestBuildReport.LIVE})
        out = subprocess.run([sys.executable, "-I", str(job)], cwd=tmp_path,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        got = json.loads(out.stdout.strip().splitlines()[-1])
        assert got["one"] == pytest.approx(got["report"])
        assert got["all"] > got["report"]


class TestWorkerEndpoints:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        main = _load_main(monkeypatch, tmp_path, "[ace]\nload_length: 3000\n")

        async def live():
            return []

        async def head():
            return {"mode": "normal"}

        monkeypatch.setattr(main, "_live_slots_async", live)
        monkeypatch.setattr(main, "_head_mode_context", head)
        with TestClient(main.app) as c:
            yield c

    def test_pysrc_serves_swap_cost_for_the_worker(self, client):
        body = client.get("/api/preflight/pysrc").json()
        assert body["swap_cost"].lstrip().startswith('"""Config-accurate')
        assert "build_swap_timeline" in body["postprocess"]
        assert "recompute_estimate" in body["core"]

    def test_livedata_carries_the_current_cost_params(self, client):
        body = client.get("/api/preflight/livedata").json()
        assert body["cost_params"]["main"]["load_length"] == 3000


class TestSwapCostLookup:
    """swap_cost.py is found like job_history.py: next to main.py first, then
    <config>/tools, then the checkout's multiace/tools."""

    SRC = ROOT / "multiace" / "tools" / "swap_cost.py"

    def _tools(self, tmp_path, marker):
        tools = tmp_path / "tools"
        tools.mkdir(exist_ok=True)
        (tools / "swap_cost.py").write_text(
            self.SRC.read_text(encoding="utf-8") + "\n# %s\n" % marker,
            encoding="utf-8")

    def test_next_to_main_wins_over_the_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MULTIACE_CONFIG_DIR", str(tmp_path))
        main = _load_main(monkeypatch, tmp_path, "[ace]\n")
        self._tools(tmp_path, "config copy")
        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "swap_cost.py").write_text("# next to main\n")
        monkeypatch.setattr(main, "__file__", str(backend / "main.py"))
        assert main._swap_cost_path() == (backend / "swap_cost.py").resolve()

    def test_the_config_dir_wins_over_the_checkout(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MULTIACE_CONFIG_DIR", str(tmp_path))
        main = _load_main(monkeypatch, tmp_path, "[ace]\n")
        self._tools(tmp_path, "config copy")
        assert main._swap_cost_path() == tmp_path / "tools" / "swap_cost.py"

    def test_the_checkout_copy_is_the_last_resort(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MULTIACE_CONFIG_DIR", str(tmp_path))
        main = _load_main(monkeypatch, tmp_path, "[ace]\n")
        assert main._swap_cost_path() == self.SRC

    def test_pysrc_serves_the_looked_up_copy(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        monkeypatch.setenv("MULTIACE_CONFIG_DIR", str(tmp_path))
        main = _load_main(monkeypatch, tmp_path, "[ace]\n")
        self._tools(tmp_path, "served from the config dir")
        with TestClient(main.app) as c:
            body = c.get("/api/preflight/pysrc").json()
        assert "served from the config dir" in body["swap_cost"]

    def test_the_backend_loads_it_when_its_own_imports_found_none(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("MULTIACE_CONFIG_DIR", str(tmp_path))
        main = _load_main(monkeypatch, tmp_path, "[ace]\n")
        self._tools(tmp_path, "config copy")
        core = main.preflight_core     # the backend's own import of it
        monkeypatch.setattr(core, "_swap_cost", None)
        assert core.make_estimate_ctx(pp, HEADER) is None
        main._ensure_swap_cost()
        assert core._swap_cost is not None
        assert core.make_estimate_ctx(pp, HEADER) is not None

    def test_an_unloadable_copy_means_no_estimate_not_an_error(
            self, monkeypatch, tmp_path):
        broken = tmp_path / "swap_cost.py"
        broken.write_text("raise RuntimeError('boom')\n")
        monkeypatch.setattr(pc, "_swap_cost", None)
        assert pc.load_swap_cost(broken) is None
        assert pc.load_swap_cost(tmp_path / "missing.py") is None
        assert pc._swap_cost is None

    def test_the_installer_puts_it_next_to_main_py(self):
        script = (ROOT / "multiace" / "install_multiace.sh").read_text(
            encoding="utf-8")
        assert 'cp "$INSTALL_DIR/tools/swap_cost.py" "$WEB_DEST/backend/"' \
            in script
