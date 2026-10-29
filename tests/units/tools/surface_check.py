#!/usr/bin/env python3
"""Surface check (R0): every `ace.X` a neighbour module uses exists.

The neighbour modules (filament_feed_ace, filament_switch_sensor_ace,
ace_bg_swap, ace_rc522, ace_tipform, ace_gen1_tunnel, ace_macro_layout)
reach into the MultiAce object: `self.ace.X`, `ace.X`, `_ace.X`,
`lookup_object('ace').X`. A refactor that renames or moves X breaks them
at the first call, not at import. This tool collects those names per
neighbour (the "surface") and checks each against what MultiAce defines:
methods, class attributes, and every `self.X = ...` anywhere in its
methods. The surface is frozen in `goldens/surface.json`; a new or dropped
name is a deliberate change and updates the golden in the same commit.

Names read through getattr(ace, 'X', default) are tolerant by design
(ace.py is not in the bundle sha and may be older than its neighbours,
CLAUDE.md §44); they are listed separately and not required to exist.

    python3 -m tests.units.tools.surface_check            # print
    python3 -m tests.units.tools.surface_check --update   # rewrite golden
    python3 -m tests.units.tools.surface_check --check    # exit 1 on drift/missing
"""
from __future__ import annotations

import ast
import json
import os
import sys

from . import modules as M

GOLDEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'goldens', 'surface.json')
ACE_NAMES = {'ace', '_ace', 'self.ace', 'self._ace', 'ace_obj', 'self.ace_obj',
             'self._ace_obj'}
ACE_PY = os.path.join(M.EXTRAS, 'ace.py')


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return base + '.' + node.attr if base else None
    return None


def _is_ace_lookup(node):
    """lookup_object('ace') or lookup_object('ace', None)"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'lookup_object' and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == 'ace')


def neighbour_surface(path):
    """-> (hard: {name: [scopes]}, soft: {name: [scopes]})"""
    tree = ast.parse(open(path, encoding='utf-8').read(), path)
    hard, soft = {}, {}

    class V(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def _s(self):
            return '.'.join(self.scope) or '<module>'

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node):
            base = _dotted(node.value)
            if (base in ACE_NAMES) or _is_ace_lookup(node.value):
                hard.setdefault(node.attr, set()).add(self._s())
            self.generic_visit(node)

        def visit_Call(self, node):
            # getattr(ace, 'name', default) / hasattr(ace, 'name')
            if (isinstance(node.func, ast.Name)
                    and node.func.id in ('getattr', 'hasattr')
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                base = _dotted(node.args[0])
                if base in ACE_NAMES or _is_ace_lookup(node.args[0]):
                    soft.setdefault(node.args[1].value, set()).add(self._s())
            self.generic_visit(node)

    V().visit(tree)
    return ({k: sorted(v) for k, v in hard.items()},
            {k: sorted(v) for k, v in soft.items()})


def multiace_defines(path=ACE_PY):
    """Names MultiAce defines: methods, class attrs, self.X assignments,
    including everything its mixin bases (modules.MIXIN_GROUPS) define."""
    names = set()
    for cls in _class_and_mixins(path):
        names |= _class_defines(cls)
    return names


def _class_and_mixins(path):
    tree = ast.parse(open(path, encoding='utf-8').read(), path)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == 'MultiAce')
    out = [cls]
    bases = {b.id for b in cls.bases if isinstance(b, ast.Name)}
    for p in M.module_paths():
        if os.path.basename(p) == 'ace.py':
            continue
        t = ast.parse(open(p, encoding='utf-8').read(), p)
        for n in t.body:
            if isinstance(n, ast.ClassDef) and n.name in bases:
                out.append(n)
    return out


def _class_defines(cls):
    names = set()
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    for node in ast.walk(cls):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if (isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == 'self'):
                        names.add(sub.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == 'setattr' and len(node.args) >= 2 \
                and isinstance(node.args[0], ast.Name) and node.args[0].id == 'self' \
                and isinstance(node.args[1], ast.Constant):
            names.add(node.args[1].value)
    return names


def neighbours():
    return [p for p in M.module_paths()
            if os.path.basename(p) not in ('ace.py', 'ace_protocol.py',
                                           'ace_protocol_v1.py',
                                           'ace_protocol_v2.py',
                                           'ace_protocol_cfs.py')]


def collect():
    out = {}
    for p in neighbours():
        hard, soft = neighbour_surface(p)
        if hard or soft:
            out[os.path.relpath(p, M.REPO)] = {'hard': hard, 'soft': soft}
    return out


def missing(surface, defined=None):
    defined = defined if defined is not None else multiace_defines()
    probs = []
    for rel, d in surface.items():
        for name, scopes in sorted(d['hard'].items()):
            if name not in defined:
                probs.append('%s reads ace.%s (in %s) which MultiAce does not define'
                             % (rel, name, ', '.join(scopes[:3])))
    return probs


def main(argv):
    surface = collect()
    probs = missing(surface)
    if '--update' in argv:
        with open(GOLDEN, 'w', encoding='utf-8') as f:
            json.dump(surface, f, indent=1, sort_keys=True)
            f.write('\n')
        print('wrote', os.path.relpath(GOLDEN, M.REPO))
    elif '--check' in argv:
        golden = json.load(open(GOLDEN, encoding='utf-8'))
        if golden != surface:
            for rel in sorted(set(golden) | set(surface)):
                g = golden.get(rel, {'hard': {}, 'soft': {}})
                s = surface.get(rel, {'hard': {}, 'soft': {}})
                for kind in ('hard', 'soft'):
                    added = sorted(set(s[kind]) - set(g[kind]))
                    gone = sorted(set(g[kind]) - set(s[kind]))
                    if added or gone:
                        print('DRIFT %s %s: +%s -%s' % (rel, kind, added, gone))
            probs.append('surface differs from golden')
    else:
        for rel, d in sorted(surface.items()):
            print('%s: %d hard, %d soft' % (rel, len(d['hard']), len(d['soft'])))
    for p in probs:
        print('PROBLEM:', p)
    print('OK' if not probs else '%d problem(s)' % len(probs))
    return 1 if probs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
