#!/usr/bin/env python3
"""The ONE list of shipped Klipper modules (R0, UNIT_DRIVER_DESIGN.md §11).

Six places used to carry their own copy of this list (installer check loop,
installer cp/chmod block, uninstaller, post-commit TRACKED_FILES, managed
manifest payload, PAXX bake glob) and twice a module was missing from one
of them (ace_bg_swap, ace_tipform in the hook). The canonical list lives in
`multiace/managed/manifest.json` ("payload", the klipper/ entries); this
module reads it and `module_lists.py` checks every other copy against it.
The shell scripts stay explicit on purpose (busybox ash on the printer has
no JSON reader), so the check is the single list, not a generated one.

CORE_FILES are the klipper files outside the bundle sha (ace.py and its
helper modules). ace.py hashes the same set at runtime for the banner's
`core=` value; `python3 -m tests.units.tools.modules` prints the repo's.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
MANIFEST = os.path.join(REPO, 'multiace', 'managed', 'manifest.json')
EXTRAS = os.path.join(REPO, 'multiace', 'klipper', 'extras')
KINEMATICS = os.path.join(REPO, 'multiace', 'klipper', 'kinematics')

# The three files that make up MULTIACE_BUNDLE_SHA1 (post-commit
# BUNDLE_FILES, ace.py _compute_bundle_sha1). Order matters for the sha.
BUNDLE_FILES = (
    'klipper/extras/filament_feed_ace.py',
    'klipper/extras/filament_switch_sensor_ace.py',
    'klipper/kinematics/extruder_ace.py',
)

# Files in the extras dir that are deliberately NOT shipped.
UNSHIPPED = ('bg_concurrency_probe.py',)

# R1 split: mixin class -> the class its methods belong to. move_check keys
# a mixin's methods under the canonical class so a move reads as MOVED, and
# surface_check counts the mixin's definitions as MultiAce's.
MIXIN_GROUPS = {
    'ResistanceMixin': 'MultiAce',
    'DisplayMixin': 'MultiAce',
    'SpoolMixin': 'MultiAce',
    'SwapMixin': 'MultiAce',
    'SettingsMixin': 'MultiAce',
}


def manifest_payload():
    with open(MANIFEST, encoding='utf-8') as f:
        return json.load(f)['payload']


def klipper_files():
    """Shipped klipper files, repo-relative under multiace/, sorted."""
    return sorted(p for p in manifest_payload() if p.startswith('klipper/'))


def core_files():
    """Klipper .py files that are not in the bundle sha (ace.py + helpers),
    in the fixed order the runtime hashes them."""
    return sorted(p for p in klipper_files()
                  if p.endswith('.py') and p not in BUNDLE_FILES)


def core_sha(root=None):
    root = root or os.path.join(REPO, 'multiace')
    h = hashlib.sha1()
    for rel in core_files():
        p = os.path.join(root, rel)
        try:
            with open(p, 'rb') as f:
                h.update(f.read())
        except OSError:
            h.update(b'<missing:' + rel.encode() + b'>')
    return h.hexdigest()[:7]


def module_paths():
    """Absolute paths of every shipped klipper .py file."""
    return [os.path.join(REPO, 'multiace', p) for p in klipper_files()
            if p.endswith('.py')]


if __name__ == '__main__':
    if '--files' in sys.argv:
        print('\n'.join(klipper_files()))
    elif '--core-files' in sys.argv:
        print('\n'.join(core_files()))
    else:
        print(core_sha())
