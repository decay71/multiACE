"""The per-swap cost model (plan E1a).

The arithmetic is derived from ace.cfg, not invented: a longer bowden
really has to produce a longer estimate, and a background swap really has
to be cheaper than an inline one, because the loadout planner optimises
against these numbers.
"""
import pytest

from multiace.tools import swap_cost as sc

CFG = {
    "feed_speed": 80, "retract_speed": 80,
    "load_length": 2100, "retract_length": 1950,
    "swap_retract_length": 900, "seat_overshoot_length": 20,
    "swap_anti_ooze_retract": 10,
}



@pytest.fixture
def model():
    return sc.SwapCostModel.from_params(CFG)


class TestSwapArithmetic:
    def test_terms_come_from_the_config(self, model):
        """Term by term: retract 910 mm @ 80, load 2100 mm @ 80, seat
        2x20 mm @ 80, plus the named unmeasured constants."""
        mech = (910 / 80.0) + (2100 / 80.0) + (40 / 80.0)
        const = sum(sc.UNMEASURED_S[k] for k in (
            "tip_form", "ace_spool_change", "seat_press", "heat_settle",
            "tool_pickup", "sensor_wait"))
        assert model.swap_seconds("same_ace") == pytest.approx(mech + const)

    def test_day_one_matches_the_old_hardcoded_costs(self, model):
        """The constants were seeded so the shipped defaults reproduce
        BG_SWAP_COST_INLINE_S=210 / BG_SWAP_COST_BG_S=30. If this drifts,
        every existing plan silently changes shape."""
        assert model.swap_seconds("cross_ace_inline") == pytest.approx(210,
                                                                       abs=2)
        assert model.swap_seconds("cross_ace_bg") == pytest.approx(30, abs=1)

    def test_a_longer_bowden_costs_more(self, model):
        slow = sc.SwapCostModel.from_params(dict(CFG, load_length=4200))
        assert slow.swap_seconds("same_ace") > model.swap_seconds("same_ace")

    def test_a_faster_feed_costs_less(self, model):
        fast = sc.SwapCostModel.from_params(dict(CFG, feed_speed=160))
        assert fast.swap_seconds("same_ace") < model.swap_seconds("same_ace")

    def test_background_beats_inline(self, model):
        """§3.1's whole lever: the other head loads while this one prints."""
        assert model.swap_seconds("cross_ace_bg") \
            < model.swap_seconds("cross_ace_inline")

    def test_a_background_swap_ignores_the_bowden_length(self, model):
        """None of the mechanical time is on the critical path, so a longer
        load must not make a bg swap look worse - that would push the
        optimizer away from exactly the placement it should prefer."""
        long_bowden = sc.SwapCostModel.from_params(dict(CFG,
                                                        load_length=4200))
        assert long_bowden.swap_seconds("cross_ace_bg") \
            == pytest.approx(model.swap_seconds("cross_ace_bg"))

    def test_unknown_kind_is_a_programming_error(self, model):
        with pytest.raises(ValueError):
            model.swap_seconds("teleport")

    def test_per_ace_overrides_win_over_the_global(self):
        m = sc.SwapCostModel.from_params(
            CFG, {1: {"swap_retract_length": 1800}})
        assert m.swap_seconds("same_ace", ace=1) \
            > m.swap_seconds("same_ace", ace=0)

    def test_per_slot_overrides_win_over_per_ace(self):
        m = sc.SwapCostModel.from_params(
            CFG, {1: {"swap_retract_length": 1800,
                      "swap_retract_length_2": 300}})
        assert m.swap_seconds("same_ace", ace=1, slot=2) \
            < m.swap_seconds("same_ace", ace=1, slot=0)


class TestBgWindow:
    def test_the_window_is_derived_not_a_constant(self, model):
        """BG_UNLOAD_MIN_WINDOW_MIN=1 would call a window feasible that a
        2100 mm load cannot fit."""
        assert model.bg_window_minutes() > 1.0

    def test_a_longer_load_needs_a_longer_window(self, model):
        long_bowden = sc.SwapCostModel.from_params(dict(CFG,
                                                        load_length=4200))
        assert long_bowden.bg_window_minutes() > model.bg_window_minutes()


class TestCalibration:
    def test_modelled_until_history_says_otherwise(self, model):
        assert model.confidence() == "modelled"

    def test_measured_medians_win_once_there_are_enough_samples(self, model):
        cal = model.with_calibration(
            {"cross_ace_inline": {"median_s": 300.0, "n": 8}})
        assert cal.swap_seconds("cross_ace_inline") == pytest.approx(300.0)
        assert cal.confidence() == "calibrated"

    def test_three_swaps_of_anecdote_is_not_a_calibration(self, model):
        cal = model.with_calibration(
            {"cross_ace_inline": {"median_s": 300.0, "n": 3}})
        assert cal.swap_seconds("cross_ace_inline") \
            == pytest.approx(model.swap_seconds("cross_ace_inline"))
        assert cal.confidence() == "modelled"

    def test_calibrating_one_kind_leaves_the_others_modelled(self, model):
        cal = model.with_calibration({"same_ace": {"median_s": 400.0, "n": 9}})
        assert cal.swap_seconds("same_ace") == pytest.approx(400.0)
        assert cal.swap_seconds("cross_ace_bg") \
            == pytest.approx(model.swap_seconds("cross_ace_bg"))
