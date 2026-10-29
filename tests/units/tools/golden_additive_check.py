"""Prove a golden drift is ADDITIVE before refreezing (M5a pattern, design
section 10): strip the trace classes the change is known to add from the
ACTUAL trace and compare the rest byte for byte with the frozen golden.
Edit strip() for the next change (what it adds), run

    python3 -m tests.units.tools.golden_additive_check

and refreeze only when every scenario reads ADDITIVE OK. The M5a version
strips: the probe-uid entry, the unit-places SAVE_VARIABLE with its two
log lines (and renumbers ace__revision), the status keys unit_place,
unit_name and caps.has_stable_id."""
import json
import re

from .. import harness as H
from ..scenarios import SCENARIOS


def strip(entries):
    out = []
    pending_save = None
    shift = 0
    for e in entries:
        kind = e[1] if len(e) > 1 else ''
        if kind == 'probe':
            continue
        if kind == 'gcode' and isinstance(e[2], str) and e[2].startswith('SAVE_VARIABLE VARIABLE=ace__revision'):
            pending_save = e
            continue
        if kind == 'log' and isinstance(e[-1], str) and ('[multiACE] unit places ' in e[-1] or '[multiACE] unit order:' in e[-1]):
            if '[multiACE] unit places ' in e[-1] and pending_save is not None:
                pending_save = None
                shift += 1
            continue
        if pending_save is not None:
            out.append(pending_save); pending_save = None
        if kind == 'status' and isinstance(e[2], dict):
            st = json.loads(json.dumps(e[2]))
            for a in st.get('aces', []) or []:
                a.pop('unit_place', None); a.pop('unit_name', None)
                if isinstance(a.get('caps'), dict):
                    a['caps'].pop('has_stable_id', None)
            e = [e[0], kind, st]
        out.append(e)
    if pending_save is not None:
        out.append(pending_save)
    # renumber ace__revision values down by the number of removed saves
    if shift:
        fixed = []
        for e in out:
            if len(e) > 2 and e[1] == 'gcode' and isinstance(e[2], str):
                m = re.match(r'SAVE_VARIABLE VARIABLE=ace__revision VALUE=(\d+)$', e[2])
                if m:
                    e = [e[0], e[1], 'SAVE_VARIABLE VARIABLE=ace__revision VALUE=%d' % (int(m.group(1)) - shift)]
            fixed.append(e)
        out = fixed
    return out

bad = 0
for name, fn in SCENARIOS.items():
    actual = H.canonical(fn())
    exp = H.read_golden(name)
    act = strip(json.loads(json.dumps(actual)))
    if act != exp:
        bad += 1
        for i, (a, b) in enumerate(zip(act, exp)):
            if a != b:
                if isinstance(a[2], dict) and isinstance(b[2], dict):
                    def keys(d, pre=''):
                        out=set()
                        for k,v in d.items():
                            out.add(pre+k)
                            if isinstance(v, dict): out |= keys(v, pre+k+'.')
                            if isinstance(v, list):
                                for j,x in enumerate(v):
                                    if isinstance(x, dict): out |= keys(x, pre+k+'[%d].'%j)
                        return out
                    print(name, 'status key diff act-exp:', sorted(keys(a[2])-keys(b[2]))[:10], 'exp-act:', sorted(keys(b[2])-keys(a[2]))[:10])
                print(name, 'first diff at', i); print('  exp', json.dumps(b)[:300]); print('  act', json.dumps(a)[:300]); break
        else:
            print(name, 'length differs', len(act), len(exp))
    else:
        print(name, 'ADDITIVE OK')
print('bad =', bad)
