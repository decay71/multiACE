#!/usr/bin/env python3
"""Move check (R0): a refactor moved code, it did not change it.

Compares the shipped Klipper modules of two trees (git refs, or directories)
definition by definition and classifies every top-level function, method,
class attribute and module constant as

    unchanged   same AST, same file
    moved       same AST, different file (the point of R1)
    changed     same name, different AST          -> fails
    added / removed                               -> fails

The AST is compared with `ast.dump` (no line numbers, no column offsets),
the same technique that proved the comment strip (CLAUDE.md §38/§59).
Comments never count; docstrings do unless `--ignore-docstrings`.

Methods are keyed `Class.name`. For a mixin split the method's class name
changes (MultiAce.foo -> SpoolMixin.foo); `modules.MIXIN_GROUPS` (and
`--group SpoolMixin=MultiAce` for an ad-hoc one) keys both sides as
MultiAce.foo. Deliberate edits riding along with a move
are declared with `--allow-changed Class.name` (repeatable), new helpers with
`--allow-added`, deletions with `--allow-removed`.

    python3 -m tests.units.tools.move_check HEAD                 # HEAD vs worktree
    python3 -m tests.units.tools.move_check <old> <new>          # two refs
    python3 -m tests.units.tools.move_check --old-dir A --new-dir B
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import subprocess
import sys

from . import modules as M


# ---------------------------------------------------------------------------
# sources

def _git_show(ref, path):
    try:
        return subprocess.check_output(
            ['git', '-C', M.REPO, 'show', '%s:%s' % (ref, path)],
            stderr=subprocess.DEVNULL).decode('utf-8')
    except subprocess.CalledProcessError:
        return None


def _klipper_files_at(ref):
    if ref is None:
        return [p for p in M.klipper_files() if p.endswith('.py')]
    text = _git_show(ref, 'multiace/managed/manifest.json')
    if text is None:
        return [p for p in M.klipper_files() if p.endswith('.py')]
    return sorted(p for p in json.loads(text)['payload']
                  if p.startswith('klipper/') and p.endswith('.py'))


def sources_from_ref(ref):
    """{rel: source} for the shipped modules at `ref` (None = worktree)."""
    out = {}
    for rel in _klipper_files_at(ref):
        if ref is None:
            p = os.path.join(M.REPO, 'multiace', rel)
            if os.path.exists(p):
                out[rel] = open(p, encoding='utf-8').read()
        else:
            text = _git_show(ref, 'multiace/' + rel)
            if text is not None:
                out[rel] = text
    return out


def sources_from_dir(d):
    out = {}
    for root, _dirs, files in os.walk(d):
        for name in sorted(files):
            if name.endswith('.py'):
                p = os.path.join(root, name)
                out[os.path.relpath(p, d)] = open(p, encoding='utf-8').read()
    return out


# ---------------------------------------------------------------------------
# indexing

def _strip_docstrings(node):
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef, ast.Module)):
            body = sub.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                sub.body = body[1:] or [ast.Pass()]
    return node


def index_source(rel, src, groups, ignore_docstrings=False):
    """-> {key: (file, dump)}"""
    tree = ast.parse(src, rel)
    if ignore_docstrings:
        tree = _strip_docstrings(copy.deepcopy(tree))
    out = {}

    def dump(n):
        return ast.dump(n, annotate_fields=True, include_attributes=False)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out['def:' + node.name] = (rel, dump(node))
        elif isinstance(node, ast.ClassDef):
            cls = groups.get(node.name, node.name)
            if cls == node.name:
                # a grouped mixin's own bases are not the canonical class's
                out['class:' + cls + ':bases'] = (
                    rel, '|'.join(sorted(dump(b) for b in node.bases)))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out['meth:%s.%s' % (cls, sub.name)] = (rel, dump(sub))
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            out['cattr:%s.%s' % (cls, t.id)] = (rel, dump(sub.value))
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    out['cattr:%s.%s' % (cls, sub.target.id)] = (
                        rel, dump(sub.value) if sub.value else '<ann>')
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out['const:' + t.id] = (rel, dump(node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out['const:' + node.target.id] = (
                rel, dump(node.value) if node.value else '<ann>')
    return out


def index_tree(sources, groups, ignore_docstrings=False):
    out = {}
    for rel, src in sorted(sources.items()):
        for k, v in index_source(rel, src, groups, ignore_docstrings).items():
            if k in out and out[k][1] != v[1]:
                # same key in two files with different bodies: keep both
                # visible by suffixing the file
                out[k + '@' + rel] = v
            else:
                out.setdefault(k, v)
    return out


# ---------------------------------------------------------------------------
# comparison

def compare(old, new):
    res = {'unchanged': [], 'moved': [], 'changed': [], 'added': [],
           'removed': []}
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k), new.get(k)
        if o and n:
            if o[1] == n[1]:
                res['unchanged' if o[0] == n[0] else 'moved'].append(
                    (k, o[0], n[0]))
            else:
                res['changed'].append((k, o[0], n[0]))
        elif o:
            res['removed'].append((k, o[0], None))
        else:
            res['added'].append((k, None, n[0]))
    return res


def _short(key):
    return key.split(':', 1)[1] if ':' in key else key


def verdict(res, allow_changed=(), allow_added=(), allow_removed=()):
    probs = []
    for k, _o, _n in res['changed']:
        if _short(k) not in allow_changed:
            probs.append('CHANGED %s' % k)
    for k, _o, _n in res['added']:
        if _short(k) not in allow_added:
            probs.append('ADDED %s' % k)
    for k, _o, _n in res['removed']:
        if _short(k) not in allow_removed:
            probs.append('REMOVED %s' % k)
    return probs


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('old', nargs='?', help='git ref of the old tree')
    ap.add_argument('new', nargs='?', help='git ref of the new tree (default: worktree)')
    ap.add_argument('--old-dir')
    ap.add_argument('--new-dir')
    ap.add_argument('--group', action='append', default=[],
                    help='Mixin=Canonical: key Mixin methods under Canonical')
    ap.add_argument('--allow-changed', action='append', default=[])
    ap.add_argument('--allow-added', action='append', default=[])
    ap.add_argument('--allow-removed', action='append', default=[])
    ap.add_argument('--ignore-docstrings', action='store_true')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)
    groups = dict(M.MIXIN_GROUPS)
    groups.update(dict(g.split('=', 1) for g in a.group))
    if a.old_dir or a.new_dir:
        old_src = sources_from_dir(a.old_dir)
        new_src = sources_from_dir(a.new_dir)
    else:
        if not a.old:
            ap.error('old ref required')
        old_src = sources_from_ref(a.old)
        new_src = sources_from_ref(a.new)
    old = index_tree(old_src, groups, a.ignore_docstrings)
    new = index_tree(new_src, groups, a.ignore_docstrings)
    res = compare(old, new)
    for kind in ('moved', 'changed', 'added', 'removed'):
        for k, o, n in res[kind]:
            print('%-9s %s  %s -> %s' % (kind.upper(), k, o, n))
    if a.verbose:
        for k, o, _n in res['unchanged']:
            print('same      %s  %s' % (k, o))
    print('unchanged %d, moved %d, changed %d, added %d, removed %d'
          % tuple(len(res[k]) for k in ('unchanged', 'moved', 'changed',
                                        'added', 'removed')))
    probs = verdict(res, a.allow_changed, a.allow_added, a.allow_removed)
    print('OK' if not probs else '%d problem(s)' % len(probs))
    return 1 if probs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
