#!/usr/bin/env python3
"""Globals check (R0): every global name a function reads is defined.

When a method moves to another module (R1 mixin split) it carries its
references to module globals (constants, helper functions, imported names)
with it; a name it forgot to bring along is a NameError that Klipper only
raises when the line runs - typically mid-print. `symtable` sees every
global reference statically, so this check fails at test time instead.

For each shipped Klipper module: collect the names each function/method
scope references as GLOBAL (symtable: is_global and is_referenced, or
free in a nested scope and unresolved up the chain), then subtract what
the module defines at top level (imports, defs, classes, assignments,
`__file__`/`__name__`/...) and the builtins. Anything left is reported.

    python3 -m tests.units.tools.globals_check          # exit 1 on findings
    python3 -m tests.units.tools.globals_check --all    # also list per-module totals
"""
from __future__ import annotations

import ast
import builtins
import os
import symtable
import sys

from . import modules as M

MODULE_DUNDERS = {'__file__', '__name__', '__doc__', '__package__',
                  '__spec__', '__loader__', '__builtins__', '__path__',
                  '__cached__', '__annotations__', '__dict__'}


def module_defined_names(tree):
    """Names bound at module top level (not inside functions/classes)."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == '*':
                    continue
                names.add((a.asname or a.name).split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            names.add(node.name)
        else:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        if a.name != '*':
                            names.add((a.asname or a.name).split('.')[0])
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.ExceptHandler) and sub.name:
                    names.add(sub.name)
    return names


def referenced_globals(table, out, scope=''):
    """Recursive walk: names referenced as global in function scopes."""
    for child in table.get_children():
        name = scope + '.' + child.get_name() if scope else child.get_name()
        if child.get_type() == 'function':
            for sym in child.get_symbols():
                if not sym.is_referenced():
                    continue
                if sym.is_global() or (sym.is_free() and not _bound_above(child, sym.get_name())):
                    out.setdefault(sym.get_name(), set()).add(name)
        referenced_globals(child, out, name)


def _bound_above(table, name):
    # symtable has no parent link; free names are resolved by the compiler
    # against enclosing function scopes. We approximate: a free name is
    # fine if ANY enclosing function scope in the module binds it. Caller
    # passes the child; we cannot climb, so treat free names as resolved
    # (they are, by construction, or the compiler would have made them
    # global). Kept as a hook for stricter checks.
    return True


def check_file(path):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src, path)
    table = symtable.symtable(src, path, 'exec')
    defined = module_defined_names(tree) | MODULE_DUNDERS | set(dir(builtins))
    refs = {}
    referenced_globals(table, refs)
    missing = {n: sorted(s) for n, s in refs.items() if n not in defined}
    return missing, len(refs)


def check(paths=None):
    paths = paths or M.module_paths()
    findings = {}
    totals = {}
    for p in paths:
        missing, total = check_file(p)
        rel = os.path.relpath(p, M.REPO)
        totals[rel] = total
        if missing:
            findings[rel] = missing
    return findings, totals


if __name__ == '__main__':
    findings, totals = check()
    if '--all' in sys.argv:
        for rel, n in sorted(totals.items()):
            print('%-55s %4d distinct global refs' % (rel, n))
    for rel, missing in sorted(findings.items()):
        for name, scopes in sorted(missing.items()):
            print('UNDEFINED %s: %s  (used in %s)' % (rel, name, ', '.join(scopes[:4])))
    print('OK' if not findings else '%d module(s) with findings'
          % len(findings))
    sys.exit(1 if findings else 0)
