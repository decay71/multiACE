"""[ace] unload_cut_macro: a cutter mod replaces the tip forming. Every
inline unload goes through _run_tipform, which then only calls the macro
(with HEAD=n) and returns; background unloads refuse, since a parked head
cannot reach the cutter."""
import ast
import importlib
import os
import types

from . import harness as H


def _ace(cut):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    sent = []
    ace = types.SimpleNamespace(unload_cut_macro=cut)
    ace._check_calibration_unload_cancel = lambda: None
    ace._disp = lambda h: h + 1
    ace.gcode = types.SimpleNamespace(
        run_script_from_command=lambda s: sent.append(s))
    ace.tipform_prime_for = lambda *a: (_ for _ in ()).throw(
        AssertionError('tip forming must not run'))
    return types.MethodType(A.MultiAce._run_tipform, ace), sent


def test_cut_macro_replaces_the_tip_form():
    run, sent = _ace('CUT')
    run(2, 215, 0, 0.4)
    assert sent == ['CUT HEAD=2\r\n']


def test_bg_ops_refuse_with_a_cutter():
    path = os.path.join(H.EXTRAS, 'ace_bg_swap.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    for name in ('cmd_ACE_BG_SWAP', 'cmd_ACE_BG_UNLOAD'):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        assert "'unload_cut_macro'" in ast.unparse(fn), name
