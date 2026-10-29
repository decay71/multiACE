#!/usr/bin/env python3
"""P3 checker (UNIT_DRIVER_DESIGN.md section 10, M4): a capability swap is
a pure rewrite of the kind predicate it replaces.

A batch of M4 replaces `self._is_v2(idx)` / `self._is_v2_idx(idx)` by
`self._cap(idx, '<cap>')` at listed sites. P2 (test_caps.py) proves that
each cap VALUE equals the predicate on every input. P3 proves that the
CODE around it did not change: both trees are normalised to one canonical
kind call, then every definition must compare equal by ast.dump, exactly
as move_check classifies a moved method.

Normalisation, applied to BOTH trees:
- `X._is_v2(A)`, `X._is_v2_idx(A)`  -> `X.__kind__('v2', A)`
- `X._is_v1(A)`                      -> `X.__kind__('v1', A)`
- `X._cap(A, '<cap>')` with <cap> in CAP_EQUIV -> `X.__kind__(kind, A)`,
  wrapped in `not` when the cap's polarity is False (a V1 flag);
- `Y._cap(X, A, '<cap>')` (the bg form: the unit's owner is passed in)
  -> the same `X.__kind__(kind, A)`;
- `not not E` -> `E`.

A cap that is not in CAP_EQUIV is left alone, so a site that uses it shows
up as CHANGED and has to be declared (`--allow-changed`), never slips
through. The table is the written-down meaning of each cap in terms of
the old predicate; extend it per batch, with the codec columns of the
design's table in mind: the equivalence holds only while a codec is
present (NO_CAPS has every flag False, the old `not _is_v2` was True).

    python3 -m tests.units.tools.p3_check <old-ref> [<new-ref>]
    python3 -m tests.units.tools.p3_check --old-dir D1 --new-dir D2
    python3 -m tests.units.tools.p3_check --sites [<ref>]   # list _cap sites
"""
from __future__ import annotations

import argparse
import ast
import copy
import sys

from . import modules as M
from . import move_check as MV

KIND_PREDICATES = {'_is_v2': 'v2', '_is_v2_idx': 'v2', '_is_v1': 'v1'}

# cap -> (kind, polarity): cap value == (unit is <kind>) if polarity else
# cap value == not (unit is <kind>). Only caps whose old predicate was a
# plain kind test belong here; a cap that bundles more (probe state,
# firmware string) has no entry and must be declared per site.
CAP_EQUIV = {
    # M4.1: the `not v2 -> V1` sites
    'v1_fa_health_log': ('v2', False),
    'fw_tag_pair_dedupe': ('v2', False),
    'tag_identity_in_status': ('v2', False),
    'has_humidity': ('v2', True),
    # M4.2: decoder (signed lane encoder, get_feed_info)
    'has_decoder': ('v2', True),
    # M4.3: slot_status / feed assist
    'has_slot_status_truth': ('v2', True),
    'fa_monitor': ('v2', True),
    'needs_fa_prestop': ('v2', True),
    'has_rollback_assist': ('v2', True),
    'no_freewheel': ('v2', True),
    'self_stops_on_resistance': ('v2', True),
    'busy_wait_floor': ('v2', True),
    'blocking_write_on_reactor': ('v1', True),
    # M4.4: engine choice (transport_engine is a value cap, no entry)
    'has_feed_check': ('v2', True),
    # M4.5: dryer / humidity / tags
    'has_exhaust_valve': ('v2', True),
    'v2_command_set': ('v2', True),
}


def _kind_call(x, kind, arg):
    return ast.Call(func=ast.Attribute(value=x, attr='__kind__',
                                       ctx=ast.Load()),
                    args=[ast.Constant(kind), arg], keywords=[])


def _is_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _cap_parts(node):
    """(receiver, unit index, cap) of a `_cap` call: `X._cap(A, cap)`, the
    same with a positional default `X._cap(A, cap, dflt)`, or the bg form
    `Y._cap(X, A, cap)`, where X owns the unit. The cap is the string
    argument; a third argument that is no string is a default."""
    if len(node.args) == 3 and _is_str(node.args[2]):
        return node.args[0], node.args[1], node.args[2]
    return node.func.value, node.args[0], node.args[1]


class _Normalize(ast.NodeTransformer):
    def __init__(self, equiv):
        self.equiv = equiv

    def visit_Call(self, node):
        self.generic_visit(node)
        f = node.func
        if not isinstance(f, ast.Attribute) or node.keywords:
            return node
        if f.attr in KIND_PREDICATES and len(node.args) == 1:
            return _kind_call(f.value, KIND_PREDICATES[f.attr], node.args[0])
        if f.attr == '_cap' and len(node.args) in (2, 3):
            recv, arg, cap = _cap_parts(node)
            if isinstance(cap, ast.Constant) and cap.value in self.equiv:
                kind, pol = self.equiv[cap.value]
                call = _kind_call(recv, kind, arg)
                if pol:
                    return call
                return ast.UnaryOp(op=ast.Not(), operand=call)
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if (isinstance(node.op, ast.Not) and isinstance(node.operand, ast.UnaryOp)
                and isinstance(node.operand.op, ast.Not)):
            return node.operand.operand
        return node


def normalize_source(src, rel='<src>', equiv=None):
    tree = ast.parse(src, rel)
    tree = _Normalize(CAP_EQUIV if equiv is None else equiv).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def index_normalized(sources, groups, equiv=None):
    return MV.index_tree(
        dict((rel, normalize_source(src, rel, equiv))
             for rel, src in sources.items()), groups)


def compare_trees(old_src, new_src, groups=None, equiv=None):
    groups = dict(M.MIXIN_GROUPS) if groups is None else groups
    return MV.compare(index_normalized(old_src, groups, equiv),
                      index_normalized(new_src, groups, equiv))


# ---------------------------------------------------------------------------
# site listing: every _cap(...) call with the predicate it stands for

def cap_sites(sources, equiv=None):
    equiv = CAP_EQUIV if equiv is None else equiv
    out = []
    for rel, src in sorted(sources.items()):
        tree = ast.parse(src, rel)
        scope = []

        def walk(node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                scope.append(node.name)
                for ch in ast.iter_child_nodes(node):
                    walk(ch)
                scope.pop()
                return
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == '_cap' and len(node.args) in (2, 3)
                    and isinstance(_cap_parts(node)[2], ast.Constant)):
                recv, arg, cap = _cap_parts(node)
                cap = cap.value
                arg = ast.unparse(arg)
                recv = ast.unparse(recv)
                if cap in equiv:
                    kind, pol = equiv[cap]
                    old = '%s%s._is_%s(%s)' % ('' if pol else 'not ', recv,
                                               kind, arg)
                else:
                    old = '<no plain predicate: declare the site>'
                out.append({'file': rel, 'line': node.lineno,
                            'scope': '.'.join(scope), 'cap': cap,
                            'new': ast.unparse(node), 'old': old})
            for ch in ast.iter_child_nodes(node):
                walk(ch)

        walk(tree)
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('old', nargs='?', help='git ref of the old tree')
    ap.add_argument('new', nargs='?',
                    help='git ref of the new tree (default: worktree)')
    ap.add_argument('--old-dir')
    ap.add_argument('--new-dir')
    ap.add_argument('--allow-changed', action='append', default=[])
    ap.add_argument('--allow-added', action='append', default=[])
    ap.add_argument('--allow-removed', action='append', default=[])
    ap.add_argument('--sites', action='store_true',
                    help='list the _cap sites of the new tree and exit')
    a = ap.parse_args(argv)
    if a.old_dir or a.new_dir:
        old_src = MV.sources_from_dir(a.old_dir) if a.old_dir else None
        new_src = MV.sources_from_dir(a.new_dir)
    else:
        new_src = MV.sources_from_ref(a.new)
        old_src = MV.sources_from_ref(a.old) if a.old else None
    if a.sites or old_src is None:
        for s in cap_sites(new_src):
            print('%s:%d %s\n    new: %s\n    old: %s'
                  % (s['file'], s['line'], s['scope'], s['new'], s['old']))
        if a.sites or old_src is None:
            return 0
    res = compare_trees(old_src, new_src)
    for kind in ('moved', 'changed', 'added', 'removed'):
        for k, o, n in res[kind]:
            print('%-9s %s  %s -> %s' % (kind.upper(), k, o, n))
    print('unchanged %d, moved %d, changed %d, added %d, removed %d'
          % tuple(len(res[k]) for k in ('unchanged', 'moved', 'changed',
                                        'added', 'removed')))
    probs = MV.verdict(res, allow_changed=a.allow_changed,
                       allow_added=a.allow_added,
                       allow_removed=a.allow_removed)
    print('P3 OK' if not probs else 'P3: %d problem(s)' % len(probs))
    return 1 if probs else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
