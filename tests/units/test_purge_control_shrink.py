"""Purge control below 100 %: stamp, hard minimum and the dock extra shrink
together; 100 % and above keep the old behaviour."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'tools'))

from tests.units import harness  # noqa: E402,F401
from extras import ace_swap  # noqa: E402
import swap_cost  # noqa: E402


def _engine(scale, floor=0, cap=0, bg=None, length=0):
    s = types.SimpleNamespace(swap_purge_scale=scale, swap_purge_floor=floor,
                              swap_purge_cap=cap, swap_purge_length=length)
    s.printer = types.SimpleNamespace(lookup_object=lambda n, d=None: bg)
    s.purge_custom = lambda: ace_swap.SwapMixin.purge_custom(s)
    s.purge_scale_effective = (
        lambda: ace_swap.SwapMixin.purge_scale_effective(s))
    s.purge_shrink = lambda: ace_swap.SwapMixin.purge_shrink(s)
    return (lambda mm: ace_swap.SwapMixin.apply_purge_policy(s, mm),
            s.purge_shrink)


def test_standard_is_unchanged():
    f, shrink = _engine(100)
    assert (f(132), f(42), f(0)) == (132, 42, 0)
    assert shrink() == 1.0


def test_left_end_reaches_short_purges():
    f, shrink = _engine(25)
    assert f(132) == 33
    assert f(42) == 10          # minimum shrank from 40 to 10
    assert shrink() == 0.25     # dock extra 40 -> 10


def test_half_way():
    f, shrink = _engine(50)
    assert (f(132), f(42)) == (66, 21)
    assert shrink() == 0.5


def test_right_side_keeps_minimum_and_extra():
    f, shrink = _engine(150)
    assert (f(132), f(42)) == (198, 63)
    assert shrink() == 1.0
    f, _ = _engine(200)
    assert f(132) == 200        # the flush macro's own limit


def test_own_settings_switch_the_control_off():
    # floor, cap, fixed length, dock scale or dock extra: scale counts as 100
    for kw in (dict(floor=1), dict(cap=150), dict(length=60),
               dict(bg=types.SimpleNamespace(prime_scale=0.35,
                                             prime_extra=40.)),
               dict(bg=types.SimpleNamespace(prime_scale=1.0,
                                             prime_extra=0.))):
        f, shrink = _engine(25, **kw)
        assert f(132) == 132, kw
        assert shrink() == 1.0, kw
    # a short purge below 40 needs the user's own floor, as before
    f, _ = _engine(25, floor=1)
    assert f(8) == 8
    # dock options at their defaults do not count as own settings
    f, shrink = _engine(25, bg=types.SimpleNamespace(prime_scale=1.0,
                                                     prime_extra=40.))
    assert f(132) == 33 and shrink() == 0.25


def test_cost_model_switches_off_too():
    m = swap_cost.SwapCostModel.from_params(
        {'swap_purge_scale': 25, 'bg_prime_scale': 0.35})
    assert m.purge_custom() and m.purge_shrink() == 1.0
    assert m.apply_purge_policy(132) == 132


def test_cost_model_mirrors_the_engine():
    for scale in (25, 50, 100, 150, 200):
        f, shrink = _engine(scale)
        m = swap_cost.SwapCostModel.from_params({'swap_purge_scale': scale})
        assert m.purge_shrink() == shrink()
        for mm in (42, 132):
            assert round(m.apply_purge_policy(mm)) == f(mm)
