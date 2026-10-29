"""R0 tools (UNIT_DRIVER_DESIGN.md §11): refactor safety nets run as tests.

- module_lists: every copy of the shipped-module list agrees with the
  manifest, and ace.py's CORE_FILES matches core_files();
- globals_check: no function reads an undefined module global (the
  NameError a moved method would raise mid-print);
- surface_check: every ace.X a neighbour module reads exists on MultiAce,
  and the set is the frozen one in goldens/surface.json;
- move_check: self-test on synthetic trees (moved vs changed), plus the
  shipped tree compared with itself.
"""
import os
import tempfile
import textwrap

from .tools import globals_check as G
from .tools import module_lists as L
from .tools import modules as M
from .tools import move_check as MV
from .tools import surface_check as S


def test_module_lists_agree():
    probs = L.check()
    assert not probs, '\n'.join(probs)


def test_core_sha_matches_runtime():
    from .harness import load_ace
    A = load_ace()
    assert A.compute_core_sha1(M.EXTRAS) == M.core_sha()


def test_no_undefined_globals():
    findings, _totals = G.check()
    msg = ['%s: %s (in %s)' % (rel, n, ', '.join(sc[:3]))
           for rel, miss in findings.items() for n, sc in miss.items()]
    assert not findings, '\n'.join(msg)


def test_globals_check_catches_a_missing_name(tmp_path):
    p = tmp_path / 'probe.py'
    p.write_text(textwrap.dedent('''
        import os
        LIMIT = 3
        class A:
            def f(self):
                return os.sep + str(LIMIT) + str(MISSING)
    '''))
    missing, _n = G.check_file(str(p))
    assert set(missing) == {'MISSING'}


def test_surface_defined_and_frozen():
    surface = S.collect()
    assert not S.missing(surface)
    import json
    golden = json.load(open(S.GOLDEN, encoding='utf-8'))
    assert golden == surface, (
        'neighbour surface changed; review and run '
        'python3 -m tests.units.tools.surface_check --update')


def test_move_check_self_is_unchanged():
    src = MV.sources_from_ref(None)
    idx = MV.index_tree(src, {})
    res = MV.compare(idx, idx)
    assert res['moved'] == res['changed'] == res['added'] == res['removed'] == []
    assert len(res['unchanged']) > 1000


def test_move_check_classifies_moved_and_changed(tmp_path):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    (old / 'ace.py').write_text(textwrap.dedent('''
        K = 1
        class MultiAce:
            def a(self):
                return 1  # comment
            def b(self):
                return 2
            def c(self):
                return 3
    '''))
    (new / 'ace.py').write_text(textwrap.dedent('''
        K = 1
        class MultiAce(SpoolMixin):
            def a(self):
                return 1
            def c(self):
                return 4
    '''))
    (new / 'ace_spool.py').write_text(textwrap.dedent('''
        class SpoolMixin:
            def b(self):
                return 2
            def d(self):
                return 5
    '''))
    groups = {'SpoolMixin': 'MultiAce'}
    res = MV.compare(MV.index_tree(MV.sources_from_dir(str(old)), groups),
                     MV.index_tree(MV.sources_from_dir(str(new)), groups))
    keys = {k: [x[0] for x in v] for k, v in res.items()}
    assert 'meth:MultiAce.a' in keys['unchanged']       # comment only
    assert 'meth:MultiAce.b' in keys['moved']           # same AST, other file
    assert 'meth:MultiAce.c' in keys['changed']
    assert 'meth:MultiAce.d' in keys['added']
    assert 'class:MultiAce:bases' in keys['changed']    # new base is a change
    probs = MV.verdict(res, allow_changed=('MultiAce.c', 'MultiAce:bases'),
                       allow_added=('MultiAce.d',))
    assert probs == []
