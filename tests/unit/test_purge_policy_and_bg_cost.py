"""The purge scale/floor/cap policy (engine and cost model agree) and the
background swap cost that depends on the parked window."""
import pytest

from multiace.tools import post_process_virtual_toolheads as pp
from multiace.tools import swap_cost as sc

CFG = {
    "feed_speed": 80, "retract_speed": 80,
    "load_length": 2100, "retract_length": 1950,
    "swap_retract_length": 900, "seat_overshoot_length": 20,
    "swap_anti_ooze_retract": 10, "swap_purge_length": 0,
}


def _model(**extra):
    return sc.SwapCostModel.from_params(dict(CFG, **extra))


def _ace(head, ace=0, slot=None):
    return {"kind": "ace", "head": head, "ace": ace,
            "slot": head if slot is None else slot}


class TestModelPurgePolicy:
    def test_defaults_change_nothing(self):
        m = _model()
        assert m.resolve_swap_purge_mm() == sc.STOCK_FLUSH_DEFAULT_MM
        assert m.resolve_swap_purge_mm(stamp_mm=60) == 60

    def test_scale_applies_to_the_stock_length_and_to_a_stamp(self):
        m = _model(swap_purge_scale=50)
        assert m.resolve_swap_purge_mm() == 40
        assert m.resolve_swap_purge_mm(stamp_mm=100) == 50

    def test_scale_applies_to_swap_purge_length(self):
        assert _model(swap_purge_length=100,
                      swap_purge_scale=150).resolve_swap_purge_mm() == 150

    def test_floor_then_cap_bound_the_scaled_length(self):
        m = _model(swap_purge_scale=50, swap_purge_floor=60)
        assert m.resolve_swap_purge_mm() == 60
        m = _model(swap_purge_scale=200, swap_purge_cap=100)
        assert m.resolve_swap_purge_mm() == 100

    def test_the_cap_wins_over_a_higher_floor(self):
        m = _model(swap_purge_floor=120, swap_purge_cap=90)
        assert m.resolve_swap_purge_mm() == 90

    def test_never_below_40_mm_whatever_scale_floor_or_cap_say(self):
        assert _model(swap_purge_scale=25).resolve_swap_purge_mm(
            stamp_mm=40) == 40
        assert _model(swap_purge_scale=25, swap_purge_cap=10
                      ).resolve_swap_purge_mm(stamp_mm=100) == 40
        assert _model(swap_purge_cap=20).resolve_swap_purge_mm() == 40

    def test_floor_is_an_extra_minimum_above_40(self):
        m = _model(swap_purge_scale=25, swap_purge_floor=55)
        assert m.resolve_swap_purge_mm(stamp_mm=40) == 55

    def test_a_fixed_length_below_40_is_untouched_at_the_defaults(self):
        assert _model(swap_purge_length=25).resolve_swap_purge_mm() == 25

    def test_never_above_the_flush_macro_limit(self):
        m = _model(swap_purge_length=200, swap_purge_scale=200)
        assert m.resolve_swap_purge_mm() == sc.PURGE_LENGTH_MAX_MM

    def test_the_scale_is_kept_inside_its_range(self):
        assert _model(swap_purge_scale=1).resolve_swap_purge_mm() == 40
        assert _model(swap_purge_scale=900).resolve_swap_purge_mm() == 160

    def test_estimate_grams_follow_the_policy(self):
        tl = [{"kind": "same_ace", "t": 0, "seconds": 100.0,
               "purge_mm": _model(swap_purge_scale=50).resolve_swap_purge_mm()}]
        full = [dict(tl[0], purge_mm=_model().resolve_swap_purge_mm())]
        half = sc.build_estimate(_model(swap_purge_scale=50), None, tl)
        whole = sc.build_estimate(_model(), None, full)
        assert half["purge"]["mm"] == whole["purge"]["mm"] / 2


class TestEnginePurgePolicy:
    @pytest.fixture
    def engine(self):
        from multiace.klipper.extras.ace_swap import SwapMixin

        class Stub(SwapMixin):
            _purge_length_override = None
            swap_purge_length = 0
            swap_purge_scale = 100
            swap_purge_floor = 0
            swap_purge_cap = 0
        return Stub()

    def test_defaults_keep_the_zero_meaning_stock(self, engine):
        assert engine.get_purge_length() == 0
        engine.swap_purge_length = 90
        assert engine.get_purge_length() == 90

    def test_a_policy_turns_the_stock_default_into_a_real_length(self, engine):
        engine.swap_purge_scale = 50
        assert engine.get_purge_length() == 40

    def test_never_below_40_mm_once_a_policy_is_set(self, engine):
        engine._purge_length_override = 40
        engine.swap_purge_scale = 25
        assert engine.get_purge_length() == 40
        engine.swap_purge_cap = 15
        assert engine.get_purge_length() == 40

    def test_floor_raises_the_minimum_above_40(self, engine):
        engine._purge_length_override = 40
        engine.swap_purge_scale = 25
        engine.swap_purge_floor = 55
        assert engine.get_purge_length() == 55

    def test_a_fixed_length_below_40_is_untouched_at_the_defaults(self, engine):
        engine.swap_purge_length = 25
        assert engine.get_purge_length() == 25

    def test_the_stamp_override_goes_through_the_policy(self, engine):
        engine._purge_length_override = 100
        engine.swap_purge_scale = 150
        engine.swap_purge_cap = 120
        assert engine.get_purge_length() == 120

    def test_engine_and_model_agree(self, engine):
        for scale, floor, cap, stamp in [(50, 0, 0, 0), (130, 70, 0, 60),
                                         (200, 0, 110, 80), (25, 50, 0, 0), (25, 0, 0, 40),
                                         (100, 0, 20, 0)]:
            engine.swap_purge_scale = scale
            engine.swap_purge_floor = floor
            engine.swap_purge_cap = cap
            engine._purge_length_override = stamp or None
            m = _model(swap_purge_scale=scale, swap_purge_floor=floor,
                       swap_purge_cap=cap)
            assert engine.get_purge_length() == pytest.approx(
                m.resolve_swap_purge_mm(stamp_mm=stamp or None), abs=0.51)


class TestBgSwapSeconds:
    def test_a_long_window_pays_only_the_handover(self):
        m = _model()
        assert m.bg_swap_seconds(window_min=20) == m.swap_seconds(
            "cross_ace_bg")

    def test_an_unknown_window_counts_as_a_fit(self):
        m = _model()
        assert m.bg_swap_seconds(window_min=None) == m.swap_seconds(
            "cross_ace_bg")

    def test_a_shorter_window_pays_what_it_could_not_hide(self):
        m = _model()
        handover = m.swap_seconds("cross_ace_bg")
        inline = m.swap_seconds("cross_ace_inline")
        need = inline - handover
        window_s = 60.0
        assert m.bg_swap_seconds(window_min=window_s / 60) == pytest.approx(
            handover + need - window_s)

    def test_cost_never_drops_when_the_window_shrinks(self):
        m = _model()
        costs = [m.bg_swap_seconds(window_min=w)
                 for w in (10, 5, 3, 2, 1, 0.5, 0.2, 0.0)]
        assert costs == sorted(costs)

    def test_an_arrival_inside_the_early_abort_pays_the_inline_swap(self):
        m = _model()
        assert m.bg_swap_seconds(
            window_min=(sc.BG_EARLY_ABORT_S - 1) / 60) == m.swap_seconds(
                "cross_ace_inline")

    def test_never_above_the_inline_cost(self):
        m = _model()
        assert m.bg_swap_seconds(window_min=0.5) <= m.swap_seconds(
            "cross_ace_inline")

    def test_a_measured_median_wins(self):
        m = _model().with_calibration(
            {"cross_ace_bg": {"median_s": 33.0, "n": 9}})
        assert m.bg_swap_seconds(window_min=0.5) == 33.0


class TestTimelineUsesTheWindow:
    ASSIGNMENT = {0: _ace(0, ace=0), 1: _ace(0, ace=1), 2: _ace(1)}

    def _bg_seconds(self, window_min):
        m = _model()
        tl = pp.build_swap_timeline(
            [0, 2, 1], self.ASSIGNMENT,
            event_times=[60, 50, 50 - window_min],
            bg_heads=[0], cost_model=m)
        assert tl[-1]["kind"] == "cross_ace_bg"
        return tl[-1]["seconds"]

    def test_a_tight_bg_window_costs_more_than_a_roomy_one(self):
        assert self._bg_seconds(1.5) > self._bg_seconds(20)

    def test_the_roomy_window_matches_the_flat_handover(self):
        m = _model()
        assert self._bg_seconds(20) == pytest.approx(
            m.swap_seconds("cross_ace_bg"), abs=0.1)


class TestPurgePolicyCommand:
    """ACE_SET_PURGE SCALE=/FLOOR=/CAP=: ranged by hand, errors name the
    option, and nothing is applied when any value is bad."""

    @pytest.fixture
    def ace(self):
        import types
        from multiace.klipper.extras import ace as ace_mod
        cls = next(v for v in vars(ace_mod).values()
                   if isinstance(v, type) and hasattr(v, "_set_purge_policy"))
        a = object.__new__(cls)
        a.swap_purge_scale, a.swap_purge_floor, a.swap_purge_cap = 100, 0, 0
        a.log_always = lambda m: None
        a._cfg_write_ace_option = lambda k, v: None
        a._ace_error = lambda gcmd, text, code=0, **kw: ValueError(text)
        a._said = types.SimpleNamespace()
        return a

    @staticmethod
    def _gcmd(**params):
        class G:
            def get(self, name, default=None):
                return params.get(name, default)
        return G()

    def test_a_good_value_is_applied(self, ace):
        ace._set_purge_policy(self._gcmd(SCALE="60", FLOOR="45"))
        assert (ace.swap_purge_scale, ace.swap_purge_floor) == (60, 45)

    @pytest.mark.parametrize("arg,val", [("SCALE", "10"), ("SCALE", "300"),
                                         ("FLOOR", "-1"), ("CAP", "250"),
                                         ("CAP", "abc")])
    def test_a_bad_value_is_refused_by_name(self, ace, arg, val):
        with pytest.raises(ValueError, match=arg):
            ace._set_purge_policy(self._gcmd(**{arg: val}))

    def test_a_bad_value_leaves_the_good_ones_unapplied(self, ace):
        with pytest.raises(ValueError, match="CAP"):
            ace._set_purge_policy(self._gcmd(SCALE="60", CAP="999"))
        assert ace.swap_purge_scale == 100
