#!/usr/bin/env python3
"""Predicate inventory (M0): every place that branches on the unit KIND.

UNIT_DRIVER_DESIGN.md section 4 replaces these predicates by capabilities
in M4, batch by batch, and section 4's allowlist self-check fails when a
NEW place appears. This tool is the inventory both lean on. It walks the
AST of the Klipper-side modules and lists:

- calls of the kind predicates (_is_v2_idx, _is_v2, _is_v1,
  _is_open_fw_idx, _any_open_fw, _fw_of, ...);
- comparisons that read a protocol's NAME ('v1' / 'v2'), directly or via
  getattr(..., 'NAME', ...);
- firmware-string literals and the O-suffix test (`endswith('O')`).

ace_unit.py is deliberately NOT walked: it is the one module that may
compare by kind (the codec tables and the firmware-string helpers live
there, M3); a kind test anywhere else is a site that still has to move.

Output: JSON, one record per site {file, scope, kind, expr}. Line numbers
are printed for the reader but not part of the golden (they move with
every edit); the golden holds the sorted multiset of (file, scope, kind,
expr).

    python3 -m tests.units.tools.predicate_inventory            # print
    python3 -m tests.units.tools.predicate_inventory --update   # rewrite golden
    python3 -m tests.units.tools.predicate_inventory --check    # exit 1 on drift
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
EXTRAS = os.path.join(REPO, 'multiace', 'klipper', 'extras')
GOLDEN = os.path.join(os.path.dirname(HERE), 'goldens', 'predicates.json')

FILES = ('ace.py', 'ace_resistance.py', 'ace_display.py', 'ace_spool.py', 'ace_swap.py', 'ace_settings.py', 'ace_bg_swap.py', 'ace_rc522.py', 'ace_tipform.py',
         'ace_gen1_tunnel.py', 'filament_feed_ace.py',
         'filament_switch_sensor_ace.py')
PREDICATES = {'_is_v2_idx', '_is_v2', '_is_v1', '_is_open_fw_idx',
              '_is_open_fw', '_fw_of', '_firmware_of',
              '_is_cv_idx', '_gen1_tunnel_capable'}
KIND_LITERALS = {'v1', 'v2'}
FW_RE = re.compile(r'^C?V\d+\.\d+\.\d+O?$')


class _Walker(ast.NodeVisitor):
    def __init__(self, fname):
        self.fname = fname
        self.scope = []
        self.sites = []

    def _add(self, kind, node):
        self.sites.append({
            'file': self.fname,
            'scope': '.'.join(self.scope) or '<module>',
            'kind': kind,
            'expr': ast.unparse(node),
            'line': node.lineno,
        })

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        f = node.func
        name = None
        if isinstance(f, ast.Attribute):
            name = f.attr
        elif isinstance(f, ast.Name):
            name = f.id
        if name in PREDICATES:
            self._add('predicate_call', node)
        elif (name == 'endswith' and node.args
              and isinstance(node.args[0], ast.Constant)
              and node.args[0].value == 'O'):
            self._add('fw_suffix_O', node)
        self.generic_visit(node)

    @staticmethod
    def _mentions_name(expr):
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Attribute) and sub.attr == 'NAME':
                return True
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == 'getattr' and len(sub.args) >= 2
                    and isinstance(sub.args[1], ast.Constant)
                    and sub.args[1].value == 'NAME'):
                return True
        return False

    def visit_Compare(self, node):
        parts = [node.left] + list(node.comparators)
        literal = any(isinstance(p, ast.Constant) and p.value in KIND_LITERALS
                      for p in parts)
        fw = any(isinstance(p, ast.Constant) and isinstance(p.value, str)
                 and FW_RE.match(p.value) for p in parts)
        if literal and any(self._mentions_name(p) for p in parts):
            self._add('name_compare', node)
        elif literal:
            self._add('kind_literal_compare', node)
        elif fw:
            self._add('fw_literal_compare', node)
        self.generic_visit(node)


def inventory():
    sites = []
    for fname in FILES:
        path = os.path.join(EXTRAS, fname)
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=fname)
        w = _Walker(fname)
        w.visit(tree)
        sites.extend(w.sites)
    sites.sort(key=lambda s: (s['file'], s['line'], s['expr']))
    return sites


def golden_form(sites):
    return sorted(
        [s['file'], s['scope'], s['kind'], s['expr']] for s in sites)


def main(argv):
    sites = inventory()
    if '--update' in argv:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, 'w', encoding='utf-8') as f:
            json.dump(golden_form(sites), f, indent=1, ensure_ascii=False,
                      sort_keys=True)
            f.write('\n')
        print('wrote %s (%d sites)' % (GOLDEN, len(sites)))
        return 0
    if '--check' in argv:
        with open(GOLDEN, encoding='utf-8') as f:
            expected = json.load(f)
        actual = golden_form(sites)
        if expected == actual:
            print('predicate inventory unchanged (%d sites)' % len(actual))
            return 0
        exp_set = {tuple(x) for x in expected}
        act_set = {tuple(x) for x in actual}
        for x in sorted(act_set - exp_set):
            print('+ %s' % json.dumps(list(x), ensure_ascii=False))
        for x in sorted(exp_set - act_set):
            print('- %s' % json.dumps(list(x), ensure_ascii=False))
        return 1
    by_kind = {}
    for s in sites:
        by_kind[s['kind']] = by_kind.get(s['kind'], 0) + 1
    for s in sites:
        print('%s:%d %s [%s] %s' % (s['file'], s['line'], s['scope'],
                                   s['kind'], s['expr']))
    print('--- %d sites: %s' % (len(sites), sorted(by_kind.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
