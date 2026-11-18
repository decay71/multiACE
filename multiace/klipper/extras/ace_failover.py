# SPDX-License-Identifier: GPL-3.0-or-later
# @MULTIACE_FLEXI_SHARED module: runout failover policy for every MultiACE mode (added by the Flexi project)
"""Common runout-failover selection policy for every MultiACE mode.

Pure function, no I/O, no hardware. The caller supplies only candidates that
are PHYSICALLY able to reach the ran-out toolhead (head mode: the wired ACE's
other slots; multi: the same lane on the other ACEs; Flexi: the saved routes
of that toolhead). Metadata alone never moves a spool to another toolhead.

Matching rules (all must hold):
  * candidate is not the ran-out source and its slot reports filament;
  * material type equal (case-insensitive) -- always required;
  * brand equal when the ran-out spool declares a brand (case-insensitive);
    a candidate without a brand does not match a declared brand;
  * colour equal (RRGGBB) when the ran-out spool declares one, unless the
    per-print 'allow other colour' permission is set for this toolhead.
Order: the user's priority list for the toolhead first (in its order), then
the remaining candidates in the caller's route order.
"""

UNKNOWN_BRANDS = frozenset(('', 'generic', 'unknown', 'none', '-'))


def _norm_type(value):
    return str(value or '').strip().lower()


def _norm_brand(value):
    b = str(value or '').strip().lower()
    return '' if b in UNKNOWN_BRANDS else b


def _norm_color(value):
    return str(value or '').strip().lstrip('#').upper()[:6]


def parse_priority(value):
    """'1:1,1:2' / ['1:1', [1, 2]] -> [(1, 1), (1, 2)]; invalid entries raise."""
    if value in (None, ''):
        return []
    items = value.split(',') if isinstance(value, str) else list(value)
    out = []
    for item in items:
        if isinstance(item, str):
            a, s = item.strip().split(':')
            pair = (int(a), int(s))
        else:
            pair = (int(item[0]), int(item[1]))
        if not (0 <= pair[0] <= 3 and 0 <= pair[1] <= 3):
            raise ValueError('ACE/slot out of range in failover priority')
        if pair not in out:
            out.append(pair)
    return out


def select_backup(want, ran_out, candidates, slot_info, priority=None,
                  allow_other_color=False):
    """Return ((ace, slot) or None, trace list).

    want: {'type','color','brand'} of the ran-out spool.
    candidates: physically reachable (ace, slot) in route order.
    slot_info(ace, slot) -> {'present': bool, 'type', 'color', 'brand'} or None.
    """
    want_type = _norm_type(want.get('type'))
    want_color = _norm_color(want.get('color'))
    want_brand = _norm_brand(want.get('brand'))
    trace = []
    if not want_type:
        return None, ['ran-out spool has no material type']
    ordered = []
    pri = [tuple(p) for p in (priority or [])]
    for p in pri:
        if p in candidates and p not in ordered:
            ordered.append(p)
    for c in candidates:
        if tuple(c) not in ordered:
            ordered.append(tuple(c))
    for a, s in ordered:
        tag = 'ace%d/s%d' % (a, s)
        if (a, s) == tuple(ran_out):
            continue
        info = slot_info(a, s)
        if not info or info.get('present') is not True:
            trace.append(tag + ' empty')
            continue
        if _norm_type(info.get('type')) != want_type:
            trace.append('%s type %s!=%s' % (tag, _norm_type(info.get('type')) or '-', want_type))
            continue
        if want_brand and _norm_brand(info.get('brand')) != want_brand:
            trace.append('%s brand %s!=%s' % (tag, _norm_brand(info.get('brand')) or '-', want_brand))
            continue
        if want_color and not allow_other_color and _norm_color(info.get('color')) != want_color:
            trace.append('%s color %s!=%s' % (tag, _norm_color(info.get('color')) or '-', want_color))
            continue
        return (a, s), trace
    return None, trace or ['no reachable candidate']
