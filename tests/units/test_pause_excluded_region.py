"""PAUSE wrapper: an error pause right after an excluded object is not
dropped by stock's 'in excluded region' guard."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))

from tests.units import harness  # noqa: E402,F401  (installs module shims)
from extras import ace as ace_mod  # noqa: E402


class _Gcmd:
    def __init__(self, **p):
        self.p = p

    def get_int(self, name, default=None):
        return int(self.p.get(name, default))

    def error(self, msg):
        return Exception(msg)


def _stub(mode='multi', from_sd=True, flag=True, current=None,
          excluded=('CUBE_B',)):
    vsd = types.SimpleNamespace(cmd_from_sd=from_sd)
    exo = types.SimpleNamespace(in_excluded_region=flag,
                                current_object=current,
                                excluded_objects=list(excluded))
    seen = []
    objs = {'virtual_sdcard': vsd, 'exclude_object': exo}
    s = types.SimpleNamespace(
        _ace_mode=mode,
        printer=types.SimpleNamespace(
            lookup_object=lambda n, d=None: objs.get(n, d)),
        _orig_pause=lambda g: seen.append(vsd.cmd_from_sd))
    s._pause_guard_lift = lambda g: ace_mod.MultiAce._pause_guard_lift(s, g)
    s._object_exclusion_suspended = (
        lambda w='': ace_mod.MultiAce._object_exclusion_suspended(s, w))
    return s, vsd, seen


def _run(s, **p):
    ace_mod.MultiAce._wrap_pause(s, _Gcmd(**p))


def test_stale_flag_lifts_guard_and_restores():
    s, vsd, seen = _stub()
    _run(s, ON_ERROR=1)
    assert seen == [False]
    assert vsd.cmd_from_sd is True


def test_plain_pause_with_stale_flag_lifts_too():
    s, vsd, seen = _stub()
    _run(s)
    assert seen == [False]


def test_inside_excluded_object_only_error_pause_lifts():
    s, vsd, seen = _stub(current='CUBE_B')
    _run(s)
    assert seen == [True]
    _run(s, ON_ERROR=1)
    _run(s, IS_RUNOUT=1)
    assert seen == [True, False, False]


def test_no_flag_or_not_from_file_is_stock():
    s, vsd, seen = _stub(flag=False)
    _run(s, ON_ERROR=1)
    s2, vsd2, seen2 = _stub(from_sd=False)
    _run(s2, ON_ERROR=1)
    assert seen == [True] and seen2 == [False]
    assert vsd2.cmd_from_sd is False


def test_normal_mode_is_stock():
    s, vsd, seen = _stub(mode='normal')
    _run(s, ON_ERROR=1)
    assert seen == [True]


def _exo_stub(current, excluded=('CUBE_B',)):
    exo = types.SimpleNamespace(current_object=current,
                                excluded_objects=list(excluded))
    s = types.SimpleNamespace(printer=types.SimpleNamespace(
        lookup_object=lambda n, d=None: exo if n == 'exclude_object' else d))
    return s, exo


def test_excluded_object_is_suspended_and_restored():
    s, exo = _exo_stub('CUBE_B')
    with ace_mod.MultiAce._object_exclusion_suspended(s, 'test'):
        assert exo.current_object is None
        # nested operation (swap -> load) sees nothing to suspend
        with ace_mod.MultiAce._object_exclusion_suspended(s, 'inner'):
            assert exo.current_object is None
        assert exo.current_object is None
    assert exo.current_object == 'CUBE_B'


def test_restored_after_an_error():
    s, exo = _exo_stub('CUBE_B')
    try:
        with ace_mod.MultiAce._object_exclusion_suspended(s, 'test'):
            raise RuntimeError('swap failed')
    except RuntimeError:
        pass
    assert exo.current_object == 'CUBE_B'


def test_printed_object_is_left_alone():
    s, exo = _exo_stub('CUBE_A')
    with ace_mod.MultiAce._object_exclusion_suspended(s, 'test'):
        assert exo.current_object == 'CUBE_A'
    assert exo.current_object == 'CUBE_A'


def test_no_exclude_object_module():
    s = types.SimpleNamespace(printer=types.SimpleNamespace(
        lookup_object=lambda n, d=None: d))
    with ace_mod.MultiAce._object_exclusion_suspended(s, 'test'):
        pass
