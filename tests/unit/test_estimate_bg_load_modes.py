"""The estimate follows [ace_bg_swap] load_enabled and the short-window
LOAD=transport stamp: a lighter background load leaves heat, grip, prime
(and for 'none' the whole bowden feed) to the arrival, so the swap costs
more; a full load is unchanged."""
import pytest

from multiace.tools import post_process_virtual_toolheads as pp
from multiace.tools import swap_cost as sc
from multiace.web.backend import preflight_core as pc

HEADER = (
    "; filament_colour = #FF0000;#00FF00;#0000FF\n"
    "; filament_type = PLA;PLA;PLA\n"
    "; filament used [mm] = 1000.0, 2000.0, 500.0\n"
    "; filament used [g] = 3.0, 6.0, 1.5\n"
    "; total filament used [g] = 10.5\n"
    "; estimated printing time (normal mode) = 1h 0m 0s\n")


def _ace(head, ace=0, slot=None):
    return {"kind": "ace", "head": head, "ace": ace,
            "slot": head if slot is None else slot}


@pytest.fixture
def model():
    return sc.SwapCostModel.default()


ASSIGN = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}


def _bg(model, window_min, **kw):
    """Last toolchange of T0 -> T2 -> T1: head 0 is parked `window_min`
    minutes, then swapped to another ACE in the background."""
    tl = pp.build_swap_timeline(
        [0, 2, 1], ASSIGN, event_times=[60, 50, 50 - window_min],
        bg_heads=[0], cost_model=model, **kw)
    return tl[-1]


class TestModel:
    def test_full_is_the_old_number(self, model):
        assert model.bg_load_extra_seconds("full") == 0
        assert model.bg_swap_seconds() == model.bg_swap_seconds(
            load_mode="full")
        assert model.bg_swap_seconds() == model.swap_seconds("cross_ace_bg")

    def test_lighter_modes_cost_more_in_order(self, model):
        full = model.bg_swap_seconds()
        transport = model.bg_swap_seconds(load_mode="transport")
        none = model.bg_swap_seconds(load_mode="none")
        assert full < transport < none
        # and never more than the plain inline swap
        assert none < model.swap_seconds("cross_ace_inline")

    def test_the_extra_is_built_from_the_config_not_a_flat_number(self):
        slow = sc.SwapCostModel.from_params(
            {"feed_speed": 40, "load_length": 2100,
             "seat_overshoot_length": 20})
        fast = sc.SwapCostModel.from_params(
            {"feed_speed": 100, "load_length": 2100,
             "seat_overshoot_length": 20})
        assert (slow.bg_load_extra_seconds("none")
                > fast.bg_load_extra_seconds("none"))
        # 'none' adds the bowden feed: length / speed, plus the sensor wait
        d = (slow.bg_load_extra_seconds("none")
             - fast.bg_load_extra_seconds("none"))
        assert d == pytest.approx(
            2100 / 40 - 2100 / 100 + 2 * 20 / 40 - 2 * 20 / 100, abs=0.01)
        # transport does not feed (the engine did): only the seat moves
        d = (slow.bg_load_extra_seconds("transport")
             - fast.bg_load_extra_seconds("transport"))
        assert d == pytest.approx(2 * 20 / 40 - 2 * 20 / 100, abs=0.01)

    def test_a_measured_median_wins_over_the_mode(self, model):
        measured = model.with_calibration(
            {"cross_ace_bg": {"n": 9, "median_s": 77.0}})
        assert measured.bg_swap_seconds(load_mode="none") == 77.0
        assert measured.bg_swap_seconds(load_mode="full") == 77.0

    def test_an_unknown_mode_counts_as_full(self, model):
        assert model.bg_swap_seconds(load_mode="banana") == \
            model.bg_swap_seconds()


class TestTimeline:
    def test_without_a_mode_nothing_changes(self, model):
        e = _bg(model, 20)
        assert e["load_mode"] is None
        assert e["seconds"] == pytest.approx(model.swap_seconds("cross_ace_bg"),
                                             abs=0.1)

    @pytest.mark.parametrize("mode", ["full", "transport", "none"])
    def test_each_configured_mode_is_costed_and_named(self, model, mode):
        e = _bg(model, 20, bg_load_mode=mode)
        assert e["load_mode"] == mode
        assert e["seconds"] == pytest.approx(
            model.bg_swap_seconds(window_min=20, load_mode=mode), abs=0.1)
        assert e["purge_inline"] is (mode != "full")

    def test_a_lighter_mode_makes_the_swap_dearer(self, model):
        secs = [_bg(model, 20, bg_load_mode=m)["seconds"]
                for m in ("full", "transport", "none")]
        assert secs[0] < secs[1] < secs[2]

    def test_a_short_window_is_costed_as_transport(self, model, monkeypatch):
        monkeypatch.setattr(pp, "BG_FULL_LOAD_MIN_WINDOW_MIN", 2)
        short = _bg(model, pp.BG_FULL_LOAD_MIN_WINDOW_MIN - 0.5,
                    bg_load_mode="full")
        assert short["load_mode"] == "transport"
        assert short["purge_inline"] is True
        assert "short window" in short["note"]
        long_ = _bg(model, pp.BG_FULL_LOAD_MIN_WINDOW_MIN + 5,
                    bg_load_mode="full")
        assert long_["load_mode"] == "full" and long_["purge_inline"] is False

    def test_a_short_window_stays_full_by_default(self, model):
        # no LOAD= stamp is emitted by default, so none is costed
        assert pp.BG_FULL_LOAD_MIN_WINDOW_MIN == 0
        e = _bg(model, 1, bg_load_mode="full")
        assert e["load_mode"] == "full" and e["purge_inline"] is False

    def test_a_short_window_never_raises_a_lighter_configured_mode(
            self, model, monkeypatch):
        monkeypatch.setattr(pp, "BG_FULL_LOAD_MIN_WINDOW_MIN", 2)
        e = _bg(model, pp.BG_FULL_LOAD_MIN_WINDOW_MIN - 0.5,
                bg_load_mode="none")
        assert e["load_mode"] == "none"

    def test_an_unknown_window_keeps_the_configured_mode(self, model):
        tl = pp.build_swap_timeline([0, 2, 1], ASSIGN, bg_heads=[0],
                                    cost_model=model, bg_load_mode="full")
        assert tl[-1]["window_min"] is None
        assert tl[-1]["load_mode"] == "full"

    def test_only_background_swaps_carry_a_mode(self, model):
        tl = pp.build_swap_timeline([0, 2, 1], ASSIGN, bg_heads=[0],
                                    cost_model=model, bg_load_mode="none")
        assert tl[0]["load_mode"] is None and tl[1]["load_mode"] is None

    def test_a_model_from_before_the_modes_still_costs_a_bg_swap(self):
        class OldModel(object):
            def swap_seconds(self, kind, ace=None, slot=None, **kw):
                return 30.0

            def bg_swap_seconds(self, ace=None, slot=None, window_min=None):
                return 31.0
        assert pp._model_bg_cost(OldModel(), 0, 0, 20, "none") == 31.0


class TestEstimate:
    @pytest.mark.parametrize("mode,expected", [
        ("true", "full"), ("True", "full"), ("transport", "transport"),
        ("false", "none"), ("False", "none")])
    def test_the_config_value_becomes_the_mode(self, mode, expected):
        ctx = pc.make_estimate_ctx(
            pp, HEADER, cost_params={"main": {"bg_load": mode}})
        assert ctx["bg_load_mode"] == expected

    def test_without_a_setting_it_is_full(self):
        assert pc.make_estimate_ctx(pp, HEADER)["bg_load_mode"] == "full"

    def test_the_total_follows_the_setting(self):
        totals = {}
        for mode in ("true", "transport", "false"):
            ctx = pc.make_estimate_ctx(
                pp, HEADER, bg_heads=[0], event_times=[60, 50, 30],
                cost_params={"main": {"bg_load": mode}})
            plan = pc.attach_estimate(ctx, {"feasible": True}, [0, 2, 1],
                                      ASSIGN)
            totals[mode] = plan["estimate"]["total_s"]
        assert totals["true"] < totals["transport"] < totals["false"]

    def test_a_cost_change_reaches_the_mode_too(self):
        ctx = pc.make_estimate_ctx(pp, HEADER, bg_heads=[0])
        again = pc.ctx_with_params(ctx, {"main": {"bg_load": "transport"}})
        assert again["bg_load_mode"] == "transport"
        assert ctx["bg_load_mode"] == "full"

    def test_the_card_says_what_the_arrival_does(self):
        ctx = pc.make_estimate_ctx(
            pp, HEADER, bg_heads=[0], event_times=[60, 50, 30],
            cost_params={"main": {"bg_load": "transport"}})
        plan = pc.attach_estimate(ctx, {"feasible": True}, [0, 2, 1], ASSIGN)
        text = " ".join(plan["estimate"]["assumptions"])
        assert "stage the filament at the toolhead sensor" in text
        full = pc.make_estimate_ctx(
            pp, HEADER, bg_heads=[0], event_times=[60, 50, 30])
        plan = pc.attach_estimate(full, {"feasible": True}, [0, 2, 1], ASSIGN)
        assert "toolhead sensor" not in " ".join(plan["estimate"]["assumptions"])

    def test_an_old_post_processor_without_the_keyword_still_works(self):
        class OldPP(object):
            def __init__(self):
                self.seen = None

            def build_swap_timeline(self, events, assignment, event_times=None,
                                    bg_heads=None, cost_model=None,
                                    flush_matrix=None, bg_purge_inline=False,
                                    copies=None):
                self.seen = bg_purge_inline
                return []
        old = OldPP()
        ctx = pc.make_estimate_ctx(
            old, HEADER, cost_params={"main": {"bg_load": "transport"}})
        assert pc.ctx_timeline(ctx, [0], {}) == []
        assert old.seen is True
