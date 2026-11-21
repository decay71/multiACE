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


class _Tmpl(object):
    def __init__(self, text):
        self.text = text
        self.ctx = None

    def create_template_context(self):
        return {'printer': None}

    def render(self, ctx):
        self.ctx = ctx
        return self.text


def _stub(caps, src={'ace_index': 0, 'slot': 2}, uses=True, nomap=False,
          pull_push=0., table_on=False, macro_text=None):
    macro = (types.SimpleNamespace(template=_Tmpl(macro_text), variables={})
             if macro_text is not None else None)

    def lookup(name, default=None):
        if name == 'gcode_macro CONTROL_RETRACT_ACTION':
            return macro
        return types.SimpleNamespace(pull_push=pull_push)
    s = types.SimpleNamespace(
        _head_source={1: dict(src)} if src else {},
        _unload_no_mapping={1: True} if nomap else {},
        head_uses_ace=lambda h: uses,
        _cap=lambda idx, name: caps.get(name, False),
        _raw=lambda h: h,
        stock_pull_table=table_on,
        printer=types.SimpleNamespace(lookup_object=lookup))
    s._stock_pull_moves = lambda h, soft: ace_mod.MultiAce._stock_pull_moves(
        s, h, soft)
    s.stock_pull_sequence = (
        lambda h, soft: ace_mod.MultiAce.stock_pull_sequence(s, h, soft))
    s.macro = macro
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


ACE2 = {'no_freewheel': True, 'has_rollback_assist': True}

STOCK_2_0 = """
    M83
    G1 E20 F400
    G1 E10 F1500
    G1 E-35 F3000
    G1 E-5 F50
    G1 E25 F1000
    G1 E15 F1500
    G1 E-35 F3300
    G1 E-5 F25
    G1 E-30 F1500
"""
STOCK_2_0_MOVES = [(20., 400.), (10., 1500.), (-35., 3000.), (-5., 50.),
                   (25., 1000.), (15., 1500.), (-35., 3300.), (-5., 25.),
                   (-30., 1500.)]


def test_ace2_with_switch_on_runs_the_firmware_sequence():
    s = _stub(ACE2, table_on=True, macro_text=STOCK_2_0)
    t = _table(s)
    assert t == [('move', d, f) for d, f in STOCK_2_0_MOVES]
    assert s.macro.template.ctx['params'] == {'SOFT': '0', 'EXTRUDER': '1'}


def test_ace2_with_switch_off_keeps_the_stock_macro():
    assert _table(_stub(ACE2, macro_text=STOCK_2_0)) is None


def test_ace2k_switch_off_keeps_the_1_5_sequence():
    t = _table(_stub(ACE2K, macro_text=STOCK_2_0))
    assert t == [('move', d, f) for d, f in bgmod.COLD_PULL_NORMAL]


def test_ace2k_switch_on_runs_the_firmware_sequence():
    t = _table(_stub(ACE2K, table_on=True, macro_text=STOCK_2_0), soft=1)
    assert t == [('move', d, f) for d, f in STOCK_2_0_MOVES]


def test_unreadable_macro_falls_back_to_the_1_5_sequence():
    for text in ('G1 E20 F400\nG4 P100\nG1 E-50 F1500', 'G1 E20 F400', ''):
        t = _table(_stub(ACE2, table_on=True, macro_text=text))
        assert t == [('move', d, f) for d, f in bgmod.COLD_PULL_NORMAL], text
    t = _table(_stub(ACE2, table_on=True, macro_text=None))
    assert t == [('move', d, f) for d, f in bgmod.COLD_PULL_NORMAL]


def test_comments_and_case_are_ignored():
    s = _stub(ACE2, table_on=True,
              macro_text='m83 ; relative\ng1 e10 f600\n# note\nG1 E-40 F1500')
    assert _table(s) == [('move', 10., 600.), ('move', -40., 1500.)]
