"""[ace] unload_cut_macro: a cutter mod replaces the tip forming. Every
inline unload goes through _run_tipform, which then only calls the macro
(with HEAD=n) and returns; background unloads refuse, since a parked head
cannot reach the cutter."""
import ast
import importlib
import os
import types

from . import harness as H


def _ace(cut, heads=()):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    sent = []
    ace = types.SimpleNamespace(unload_cut_macro=cut,
                                unload_cut_heads=set(heads))
    ace.unload_cut_macro_for = types.MethodType(
        A.MultiAce.unload_cut_macro_for, ace)
    ace._check_calibration_unload_cancel = lambda: None
    ace._disp = lambda h: h + 1
    ace.gcode = types.SimpleNamespace(
        run_script_from_command=lambda s: sent.append(s))
    ace._tipform_material_for = lambda *a: (_ for _ in ()).throw(
        AssertionError('tip forming must not run'))
    return types.MethodType(A.MultiAce._run_tipform, ace), sent


def test_cut_macro_replaces_the_tip_form():
    run, sent = _ace('CUT')
    run(2, 215, 0, 0.4)
    assert sent == ['CUT HEAD=2\r\n']


def test_heads_without_a_cutter_keep_the_tip_form():
    run, sent = _ace('CUT', heads={1, 2})
    try:
        run(3, 215, 0, 0.4)          # display head 4, not listed
    except AssertionError:
        pass                          # reached the tip forming: expected
    else:
        raise AssertionError('tip forming did not run on head 4')
    assert sent == []
    run(1, 215, 0, 0.4)              # display head 2, listed
    assert sent == ['CUT HEAD=1\r\n']


def test_bg_ops_refuse_with_a_cutter():
    path = os.path.join(H.EXTRAS, 'ace_bg_swap.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    for name in ('cmd_ACE_BG_SWAP', 'cmd_ACE_BG_UNLOAD'):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        assert "'unload_cut_macro_for'" in ast.unparse(fn), name
