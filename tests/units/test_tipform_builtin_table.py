"""Inline tip form: a unit that cannot freewheel and has no rollback assist
gets the stock pull as a table (MMU take-up per pull); every other unit
keeps the stock macro."""
import os
import sys
import types

import importlib

EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                      'multiace', 'klipper', 'extras')
PKG = 'multiace_tipform_builtin_extras'


def _load():
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [EXTRAS]
        sys.modules[PKG] = pkg
    ace = importlib.import_module(PKG + '.ace')
    if 'chelper' not in sys.modules:
        sys.modules['chelper'] = types.ModuleType('chelper')
    fm = PKG + '.force_move'
    if fm not in sys.modules:
        stub = types.ModuleType(fm)
        stub.calc_move_time = None
        sys.modules[fm] = stub
        setattr(sys.modules[PKG], 'force_move', stub)
    return ace, importlib.import_module(PKG + '.ace_bg_swap')


ace_mod, bgmod = _load()


def _stub(caps, src={'ace_index': 0, 'slot': 2}, uses=True, nomap=False,
          pull_push=0.):
    s = types.SimpleNamespace(
        _head_source={1: dict(src)} if src else {},
        _unload_no_mapping={1: True} if nomap else {},
        head_uses_ace=lambda h: uses,
        _cap=lambda idx, name: caps.get(name, False),
        _raw=lambda h: h,
        printer=types.SimpleNamespace(lookup_object=lambda n, d=None:
                                      types.SimpleNamespace(pull_push=pull_push)))
    return s


def _table(s, soft=0):
    return ace_mod.MultiAce._tipform_builtin_table(s, 1, soft)


ACE2K = {'no_freewheel': True, 'has_rollback_assist': False}


def test_unit_without_rollback_assist_gets_the_table():
    t = _table(_stub(ACE2K))
    assert t == [('move', d, f) for d, f in bgmod.COLD_PULL_NORMAL]
    assert _table(_stub(ACE2K), soft=1) == [
        ('move', d, f) for d, f in bgmod.COLD_PULL_SOFT]


def test_opening_push_follows_the_bg_setting():
    t = _table(_stub(ACE2K, pull_push=20.))
    assert t[0] == ('move', 20., bgmod.COLD_PULL_NORMAL[0][1])
    assert t[1:] == [('move', d, f) for d, f in bgmod.COLD_PULL_NORMAL[1:]]


def test_other_units_keep_the_stock_macro():
    assert _table(_stub({'no_freewheel': True,
                         'has_rollback_assist': True})) is None   # ACE 2
    assert _table(_stub({'no_freewheel': False})) is None         # ACE Pro, CFS


def test_no_known_slot_keeps_the_stock_macro():
    assert _table(_stub(ACE2K, src=None)) is None
    assert _table(_stub(ACE2K, nomap=True)) is None
    assert _table(_stub(ACE2K, uses=False)) is None
