#!/usr/bin/env python3
"""Module-list check (R0): every copy of the shipped-module list agrees.

Canonical list = `multiace/managed/manifest.json` payload (see modules.py).
Checked copies:

- install_multiace.sh: the pre-flight check loop AND the cp/chmod block;
- uninstall_multiace.sh: the rm -f block;
- multiace/tools/git-hooks/post-commit: TRACKED_FILES (a missing entry means
  a stale build= banner for that module, CLAUDE.md §39/§50);
- scripts/build_paxx_overlay.sh: copies by glob (ace*.py, filament_*_ace.py,
  extruder_ace.py) - every shipped file must match a glob and no unshipped
  file may;
- .github/workflows/release.yml: the py_compile list before the managed
  package is built (design §11 Packliste: ace_unit.py / ace_kmcu.py belong
  here when they come);
- the extras/kinematics directories themselves: no .py file that is in no
  list, except the documented UNSHIPPED ones.

    python3 -m tests.units.tools.module_lists        # report, exit 1 on drift
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys

from . import modules as M

INSTALL = os.path.join(M.REPO, 'multiace', 'install_multiace.sh')
UNINSTALL = os.path.join(M.REPO, 'multiace', 'uninstall_multiace.sh')
HOOK = os.path.join(M.REPO, 'multiace', 'tools', 'git-hooks', 'post-commit')
BAKE = os.path.join(M.REPO, 'scripts', 'build_paxx_overlay.sh')
RELEASE = os.path.join(M.REPO, '.github', 'workflows', 'release.yml')

_KPATH = r'klipper/(?:extras|kinematics)/[A-Za-z0-9_]+\.py'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _klipper_py(paths):
    return sorted(p for p in paths if p.startswith('klipper/')
                  and p.endswith('.py'))


def installer_lists():
    text = _read(INSTALL)
    check = set(re.findall(r'"(%s)"' % _KPATH, text))
    cp = set(re.findall(r'cp "\$INSTALL_DIR/(%s)"' % _KPATH, text))
    chmod = set()
    for m in re.finditer(r'^chmod 644 (.*)$', text, re.M):
        for d, name in re.findall(r'"\$(EXTRAS|KINEMATICS)_DIR/([A-Za-z0-9_]+\.py)"',
                                  m.group(1)):
            sub = 'extras' if d == 'EXTRAS' else 'kinematics'
            chmod.add('klipper/%s/%s' % (sub, name))
    # The check loop deliberately omits the protocol modules (they are
    # imported by ace.py and were never part of the user-facing list), so
    # only the cp block is compared 1:1; the check loop must be a subset.
    return sorted(check), sorted(cp), sorted(chmod)


def uninstaller_list():
    text = _read(UNINSTALL)
    out = set()
    for d, name in re.findall(r'rm -f "\$(EXTRAS|KINEMATICS)_DIR/([A-Za-z0-9_]+\.py)"',
                              text):
        if name.endswith('_pre_multiace.py'):
            continue
        sub = 'extras' if d == 'EXTRAS' else 'kinematics'
        out.add('klipper/%s/%s' % (sub, name))
    return sorted(out)


def hook_tracked():
    text = _read(HOOK)
    m = re.search(r'TRACKED_FILES=\((.*?)\)', text, re.S)
    return sorted(p.replace('multiace/', '', 1)
                  for p in re.findall(r'"multiace/(%s)"' % _KPATH, m.group(1)))


def bake_required():
    """The explicit required-file list at the top of build_paxx_overlay.sh."""
    text = _read(BAKE)
    return sorted(set(re.findall(r'"\$SRC/(%s)"' % _KPATH, text)))


def bake_globs():
    text = _read(BAKE)
    globs = []
    for m in re.finditer(r'cp "\$SRC/klipper/(extras|kinematics)/"?([A-Za-z0-9_*]+\.py)"?',
                         text):
        globs.append('klipper/%s/%s' % (m.group(1), m.group(2)))
    return globs


def release_compile_list():
    text = _read(RELEASE)
    return sorted(p for p in re.findall(r'multiace/(%s)' % _KPATH, text))


def on_disk():
    out = []
    for sub, d in (('extras', M.EXTRAS), ('kinematics', M.KINEMATICS)):
        for name in sorted(os.listdir(d)):
            if name.endswith('.py'):
                out.append('klipper/%s/%s' % (sub, name))
    return out


def check():
    canon = _klipper_py(M.klipper_files())
    problems = []

    def cmp(label, got, want=canon, subset=False):
        got = set(got)
        want = set(want)
        if subset:
            extra = got - want
            if extra:
                problems.append('%s lists files not in manifest: %s'
                                % (label, sorted(extra)))
            return
        if got != want:
            problems.append('%s differs from manifest: missing %s, extra %s'
                            % (label, sorted(want - got), sorted(got - want)))

    check_loop, cp, chmod = installer_lists()
    cmp('install_multiace.sh check loop', check_loop, subset=True)
    cmp('install_multiace.sh cp block', cp)
    cmp('install_multiace.sh chmod', chmod)
    cmp('uninstall_multiace.sh', uninstaller_list())
    cmp('post-commit TRACKED_FILES', hook_tracked())
    cmp('release.yml py_compile', release_compile_list(),
        want=[p for p in canon if p not in M.BUNDLE_FILES], subset=True)
    cmp('build_paxx_overlay.sh required list', bake_required())
    globs = bake_globs()
    for p in canon:
        if not any(fnmatch.fnmatch(p, g) for g in globs):
            problems.append('build_paxx_overlay.sh: %s matches no cp glob %s'
                            % (p, globs))
    disk = on_disk()
    for p in disk:
        if p in canon:
            continue
        if os.path.basename(p) in M.UNSHIPPED:
            if any(fnmatch.fnmatch(p, g) for g in globs):
                problems.append('build_paxx_overlay.sh would ship unshipped %s' % p)
            continue
        problems.append('%s is on disk but in no list (add to manifest.json '
                        'payload + every copy, or to modules.UNSHIPPED)' % p)
    for p in canon:
        if p not in disk:
            problems.append('manifest lists %s but the file is missing' % p)
    # core/bundle split covers everything
    core_plus_bundle = set(M.core_files()) | set(M.BUNDLE_FILES)
    if core_plus_bundle != set(canon):
        problems.append('core_files + BUNDLE_FILES != manifest klipper .py')
    # ace.py hashes CORE_FILES at runtime for the banner's core= value; the
    # tuple there must be the same files in the same order as core_files().
    ace_core = ace_py_core_files()
    want_core = [os.path.basename(p) for p in M.core_files()]
    if ace_core != want_core:
        problems.append('ace.py CORE_FILES %s != modules.core_files() %s'
                        % (ace_core, want_core))
    return problems


def ace_py_core_files():
    import ast
    tree = ast.parse(_read(os.path.join(M.EXTRAS, 'ace.py')))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'CORE_FILES'
                for t in node.targets):
            return [e.value for e in node.value.elts]
    return None


if __name__ == '__main__':
    probs = check()
    canon = _klipper_py(M.klipper_files())
    print('manifest klipper .py files: %d' % len(canon))
    for p in probs:
        print('DRIFT:', p)
    print('OK' if not probs else '%d problem(s)' % len(probs))
    sys.exit(1 if probs else 0)
