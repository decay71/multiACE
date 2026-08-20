

import sys, re, os, json
import urllib.request, urllib.error
from collections import defaultdict

def rewrite(gcode):

    def _fix_m104(m):
        return re.sub(r'T([4-9]|1[0-5])',
                      lambda t: 'T' + str(int(t.group(1)) % 4),
                      m.group(0))
    gcode = re.sub(r'^M10[49][^\n]*',
                   _fix_m104, gcode, flags=re.MULTILINE)

    gcode = re.sub(
        r'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=([4-9]|1[0-5])\n?',
        '',
        gcode)

    split_re = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                          re.MULTILINE)
    m = split_re.search(gcode)
    if m is None:
        pre, body = gcode, ''
    else:
        pre, body = gcode[:m.start()], gcode[m.start():]

    pre = re.sub(r'^T([4-9]|1[0-5])\s*$',
                 lambda x: 'T' + str(int(x.group(1)) % 4),
                 pre, flags=re.MULTILINE)

    def _expand_swap(m):
        n = int(m.group(1))
        head = n % 4
        ace = n // 4
        return 'T%d\nACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d' % (
            head, head, ace, head)

    body = re.sub(r'^T([4-9]|1[0-5])\s*$',
                  _expand_swap, body, flags=re.MULTILINE)

    head_loaded = {0: (0, 0), 1: (0, 1), 2: (0, 2), 3: (0, 3)}
    filtered_lines = []
    lines = body.splitlines()
    i = 0
    skipped = 0
    swapbacks = 0
    while i < len(lines):
        line = lines[i]

        m_t = re.match(r'^T([0-3])\s*$', line)
        if m_t:
            head = int(m_t.group(1))

            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].startswith('ACE_SWAP_HEAD'):

                filtered_lines.append(line)
            else:

                initial_key = (0, head)
                if head_loaded.get(head) != initial_key:
                    filtered_lines.append(line)
                    filtered_lines.append(
                        'ACE_SWAP_HEAD HEAD=%d ACE=0 SLOT=%d' % (head, head))
                    swapbacks += 1
                    head_loaded[head] = initial_key
                else:
                    filtered_lines.append(line)
            i += 1
            continue
        m_s = re.match(
            r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)(?:\s+\S+=\S+)*\s*$',
            line)
        if m_s:
            head = int(m_s.group(1))
            ace = int(m_s.group(2))
            slot = int(m_s.group(3))
            key = (ace, slot)
            if head_loaded.get(head) == key:
                filtered_lines.append('; %s  ; skipped (already loaded)' % line)
                skipped += 1
                i += 1
                continue
            head_loaded[head] = key
        filtered_lines.append(line)
        i += 1
    body = '\n'.join(filtered_lines)

    total_active = len([l for l in filtered_lines if l.startswith('ACE_SWAP_HEAD')])
    return pre + body, total_active, skipped, swapbacks

def parse_toolchanges(gcode):
    """Yield the ORIGINAL T-index in order of appearance.

    Uses the "; Change Tool X -> Tool Y" comment as the source of
    truth for the target tool, since after post-processing the bare
    T<n> line always reads T<head> (head = original_T % 4) and the
    ACE-slot info is moved into ACE_SWAP_HEAD. The comment line is
    preserved in both pre- and post-rewrite gcode, so parsing it
    lets the analyzer work on either input."""
    change_re = re.compile(
        r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')
    bare_re = re.compile(r'^T(\d{1,2})\b')
    saw_change = False
    for line in gcode.splitlines():
        s = line.strip()
        if not s:
            continue
        m = change_re.match(s)
        if m:
            saw_change = True
            yield int(m.group(1))
            continue

        if saw_change or s.startswith(';'):
            continue
        mb = bare_re.match(s)
        if mb:
            yield int(mb.group(1))

def parse_toolchanges_with_times(gcode):
    """parse_toolchanges + the slicer's remaining print time per event: the
    last M73 R=<minutes> seen BEFORE each toolchange. Mirrors plan_loadout's
    body-split so the sequence aligns 1:1 with its 'events' (verify with a
    list compare before trusting the times). Returns (events, times); times
    entries are float minutes or None (no M73 yet / no M73 in the file)."""
    lines = gcode.splitlines()
    has_change = any(_TC_CHANGE_RE.match(l.strip()) for l in lines)
    return _toolchanges_with_times(lines, has_change)

def parse_toolchanges_with_times_from_file(in_path):
    """File variant of parse_toolchanges_with_times (streaming, two cheap
    line passes - the rewrite pipeline works on multi-MB files)."""
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
        has_change = any(_TC_CHANGE_RE.match(l.strip()) for l in fin)
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
        return _toolchanges_with_times(fin, has_change)

_TC_CHANGE_RE = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')
_TC_BARE_RE = re.compile(r'^T(\d{1,2})\b')
_TC_M73_RE = re.compile(r'^M73\b.*?\bR(\d+(?:\.\d+)?)')

def _toolchanges_with_times(lines_iter, has_change):

    events = []
    times = []
    last_r = None
    for line in lines_iter:
        s = line.strip()
        if not s:
            continue
        mr = _TC_M73_RE.match(s)
        if mr:
            last_r = float(mr.group(1))
            continue
        mc = _TC_CHANGE_RE.match(s)
        if mc:
            events.append(int(mc.group(1)))
            times.append(last_r)
            continue
        if has_change or s.startswith(';'):
            continue
        mb = _TC_BARE_RE.match(s)
        if mb:
            events.append(int(mb.group(1)))
            times.append(last_r)
    return events, times

def lookup_live_slots(host, port=80, path='/multiace/api/state', timeout=5.0):
    """Query the printer's multiACE web for current slot occupation.

    host may include ":port" (e.g. "192.168.1.42:8080") which overrides
    the port arg. Returns a list of dicts:
        {'ace': N, 'slot': S, 'material': str, 'color': '#rrggbb' (lower)}
    for every non-empty slot, or None on any HTTP/parse error."""
    if ':' in host:
        host, _, port_str = host.partition(':')
        try:
            port = int(port_str)
        except ValueError:
            pass
    url = 'http://%s:%d%s' % (host, port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print('WARNING: live-lookup to %s failed: %s' % (url, e),
              file=sys.stderr)
        return None
    out = []
    for ace in data.get('aces', []) or []:
        ace_idx = ace.get('idx')
        for slot in ace.get('slots', []) or []:
            if slot.get('state') == 'empty':
                continue

            if 'source' in slot and slot['source'] not in ('rfid', 'override'):
                continue
            color = (slot.get('color') or '').strip().lower()
            material = (slot.get('material') or '').strip()
            if color or material:
                out.append({
                    'ace': ace_idx,
                    'slot': slot.get('idx'),
                    'material': material,
                    'color': color,
                })
    return out

def host_has_manual_head(host, port=80, path='/multiace/api/state', timeout=5.0):
    """True if the printer reports any toolhead set to manual/TPU. Kept
    SEPARATE from lookup_live_slots (which stays a pure slot fetch, reusable by
    the future Pro matcher that DOES place manual heads) so this guard is a
    one-line removal once manual heads are supported. Returns False on any
    HTTP/parse error (fail open - don't block on connectivity issues)."""
    if ':' in host:
        host, _, port_str = host.partition(':')
        try:
            port = int(port_str)
        except ValueError:
            pass
    url = 'http://%s:%d%s' % (host, port, path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='replace'))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return any(th.get('manual') for th in (data.get('toolheads', []) or []))

def check_material_availability(filament_types, live_slots):
    """Pre-check before matching. Returns sorted list of materials that
    the slicer needs (per `filament_types`) but that aren't loaded in
    any slot on the printer. An empty list means every required
    material has at least one slot available - matching can proceed
    even if individual colours fall back."""
    loaded = set()
    for s in live_slots or []:
        m = (s.get('material') or '').strip().lower()
        if m:
            loaded.add(m)
    required = set()
    if filament_types:
        for v in filament_types.values():
            m = (v or '').strip().lower()
            if m:
                required.add(m)
    return sorted(required - loaded)

def _hex_to_rgb_internal(s):
    s = (s or '').strip().lower().lstrip('#')
    if len(s) < 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None

def _find_color_match(candidates, slicer_color, strict_color, fuzzy_max_distance):
    """Pick the best slot from `candidates` for `slicer_color`. Returns
    (slot_dict, tier_str) or (None, None). Tier is one of:
      'exact_hex' | 'name_exact' | 'name_base' | 'name_canon' | 'fuzzy'."""
    if not slicer_color:
        return None, None

    for s in candidates:
        if s['color'].lstrip('#') == slicer_color:
            return s, 'exact_hex'
    if strict_color:
        return None, None

    slicer_name = approx_color_name('#' + slicer_color) or ''
    if slicer_name and slicer_name != '?':
        slicer_base = _strip_color_qualifier(slicer_name)
        slicer_canon = _COLOR_SYNONYMS.get(slicer_base, slicer_base)
        for stage_tier in (('name_exact', 'exact'),
                           ('name_base',  'base'),
                           ('name_canon', 'canon')):
            tier, stage = stage_tier
            for s in candidates:
                slot_name = approx_color_name(s['color']) or ''
                if not slot_name or slot_name == '?':
                    continue
                if stage == 'exact':
                    ok = (slot_name == slicer_name)
                elif stage == 'base':
                    ok = (_strip_color_qualifier(slot_name) == slicer_base)
                else:
                    slot_base  = _strip_color_qualifier(slot_name)
                    slot_canon = _COLOR_SYNONYMS.get(slot_base, slot_base)
                    ok = (slot_canon == slicer_canon)
                if ok:
                    return s, tier

    if fuzzy_max_distance is not None:
        slicer_rgb = _hex_to_rgb_internal(slicer_color)
        if slicer_rgb is not None:
            best, best_d = None, None
            for s in candidates:
                sr = _hex_to_rgb_internal(s['color'])
                if sr is None:
                    continue
                d2 = ((sr[0] - slicer_rgb[0]) ** 2
                      + (sr[1] - slicer_rgb[1]) ** 2
                      + (sr[2] - slicer_rgb[2]) ** 2)
                if best_d is None or d2 < best_d:
                    best_d, best = d2, s
            if best is not None and best_d ** 0.5 <= fuzzy_max_distance:
                return best, 'fuzzy'
    return None, None

def match_colors_to_slots(color_names, live_slots, num_heads=4,
                          filament_types=None,
                          strict_color=False,
                          fuzzy_max_distance=None,
                          nozzle_groups=None,
                          head_of_ace=None):
    """Build a remap {original_T -> synthetic_T} for the rewrite formula
    (ace = T // num_heads, slot = T % num_heads), choosing the physical
    slot whose colour best matches the slicer's colour for that T.

    Match algorithm is TIER-MAJOR: each tier is tried against EVERY
    still-unmatched slicer T-index globally before any later tier
    runs. That prevents the greedy-per-T failure mode where T0
    (Blue) grabs the only DarkBlue slot via the name_base fallback,
    leaving T1 (DarkBlue) - which would have matched exact_hex -
    stuck on a worse tier.

    Tier order (every tier stays within the slicer head's material —
    a different material is never substituted, even on fallback):
        1.  exact_hex                            every T tried
        2.  name_exact                           every T tried (skip if strict_color)
        3.  name_base   ('DarkRed' -> 'Red')     every T tried (skip if strict_color)
        4.  name_canon  (synonym table)          every T tried (skip if strict_color)
        5.  fuzzy RGB distance                   every T tried (skip if strict_color
                                                 or fuzzy_max_distance is None)
      Last resort (still material-matched):
        6.  any unclaimed slot of the same material  → tier='fallback'
        7.  share an already-claimed same-material slot → tier='duplicate'
        8.  nothing available                    → tier='no_slot'

    A slot is claimed once and removed from contention. T-indices
    whose matched physical slot equals their slicer index are
    omitted from the remap (no-op rewrite).

    Returns (remap, info, used_slots) where info[t_idx] = {
      'tier':       str   (see tier list above, or 'no_slot'),
      'slot':       dict  (the matched live_slot, or None),
      'loose_mat':  bool  (always False; kept for API compatibility),
    }."""
    filament_types = filament_types or {}

    def _hex_to_rgb(s):
        s = (s or '').strip().lower().lstrip('#')
        if len(s) < 6:
            return None
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            return None

    def _name_keys(hex_str):
        name = approx_color_name(hex_str) or ''
        if not name or name == '?':
            return '', '', ''
        base  = _strip_color_qualifier(name)
        canon = _COLOR_SYNONYMS.get(base, base)
        return name, base, canon

    t_meta = {}
    for t in sorted(color_names.keys()):
        c = (color_names[t] or '').strip().lower().lstrip('#')
        mat = (filament_types.get(t) or '').strip().lower()
        name, base, canon = _name_keys('#' + c) if c else ('', '', '')
        t_meta[t] = {
            'color': c, 'mat': mat,
            'name': name, 'base': base, 'canon': canon,
            'rgb': _hex_to_rgb(c) if c else None,
        }

    slot_meta = []
    for s in live_slots:
        c = (s.get('color') or '').strip().lower().lstrip('#')
        name, base, canon = _name_keys('#' + c) if c else ('', '', '')
        slot_meta.append({
            'slot':  s,
            'color': c,
            'mat':   (s.get('material') or '').lower(),
            'name':  name, 'base': base, 'canon': canon,
            'rgb':   _hex_to_rgb(c) if c else None,
        })

    used: set = set()
    info: dict = {}
    pending = list(t_meta.keys())

    def _head_of(s):
        """Which physical head a slot feeds - the axis the nozzle gate
        constrains. MULTI: head == slot index (synthetic_T = ace*4 + slot,
        and head = T % 4), so the slot index IS the head. HEAD MODE: each
        ACE is wired to exactly one head, so the head comes from
        head_of_ace and the slot index means nothing - using the slot index
        there would gate on the wrong axis entirely."""
        if head_of_ace:
            return head_of_ace.get(int(s['ace']))
        return s['slot']

    def _candidate_slots(t, strict_mat):
        """Iterate unclaimed slot_meta entries, optionally restricted
        to the slicer T's material.

        NOZZLE GATE (2026-08-07): with mixed nozzles the slicer bakes each
        tool's own line WIDTH into that tool's extrusions, so a tool may
        only land on a head carrying the SAME nozzle diameter - the head
        is s['slot'] here (synthetic_T = ace*num_heads + slot, so slot IS
        the physical head). nozzle_groups maps head -> the heads sharing
        its diameter; None/empty (uniform nozzles, unknown diameters, or
        any non-FOrca file) means no constraint and this stays
        byte-identical. Without it an ordinary reload in a different slot
        order remapped 4 of 4 tools onto wrong-sized nozzles, silently
        (HW-reproduced on FOrcaSlicer 2.3.2 cubes, 0.2/0.8/0.4/0.6)."""
        t_mat = t_meta[t]['mat']
        allowed = nozzle_groups.get(t) if nozzle_groups else None
        for sm in slot_meta:
            s = sm['slot']
            if (s['ace'], s['slot']) in used:
                continue
            if allowed is not None and _head_of(s) not in allowed:
                continue
            if strict_mat and t_mat and sm['mat'] != t_mat:
                continue
            yield sm

    def _match_pass(tier_name, strict_mat, predicate):
        """Iterate all currently-pending T-indices, in T-order. For
        each, claim the first unclaimed slot satisfying `predicate`."""
        for t in list(pending):
            tm = t_meta[t]
            chosen = None
            for sm in _candidate_slots(t, strict_mat):
                if predicate(tm, sm):
                    chosen = sm
                    break
            if chosen is None:
                continue
            s = chosen['slot']
            used.add((s['ace'], s['slot']))
            info[t] = {
                'tier': ('loose_' + tier_name) if not strict_mat else tier_name,
                'slot': s,
                'loose_mat': (not strict_mat) and bool(tm['mat']),
            }
            pending.remove(t)

    def _fuzzy_predicate(tm, sm):
        if fuzzy_max_distance is None:
            return False
        if tm['rgb'] is None or sm['rgb'] is None:
            return False
        d2 = ((tm['rgb'][0] - sm['rgb'][0]) ** 2
              + (tm['rgb'][1] - sm['rgb'][1]) ** 2
              + (tm['rgb'][2] - sm['rgb'][2]) ** 2)
        return d2 ** 0.5 <= fuzzy_max_distance

    color_tiers = [
        ('exact_hex',  False,
            lambda tm, sm: bool(tm['color']) and tm['color'] == sm['color']),
        ('name_exact', True,
            lambda tm, sm: bool(tm['name'])  and tm['name']  == sm['name']),
        ('name_base',  True,
            lambda tm, sm: bool(tm['base'])  and tm['base']  == sm['base']),
        ('name_canon', True,
            lambda tm, sm: bool(tm['canon']) and tm['canon'] == sm['canon']),
        ('fuzzy',      True, _fuzzy_predicate),
    ]

    for tier_name, skip_when_strict, pred in color_tiers:
        if skip_when_strict and strict_color:
            continue
        if tier_name == 'fuzzy' and fuzzy_max_distance is None:
            continue
        _match_pass(tier_name, True, pred)

    for t in list(pending):
        tm = t_meta[t]
        t_mat = (tm.get('mat') or '').strip().lower()
        allowed = nozzle_groups.get(t) if nozzle_groups else None
        chosen = None
        for sm in slot_meta:
            s = sm['slot']
            if (s['ace'], s['slot']) in used:
                continue
            if allowed is not None and _head_of(s) not in allowed:
                continue
            if t_mat and sm['mat'] and sm['mat'] != t_mat:
                continue
            chosen = sm
            break
        if chosen is None:
            continue
        s = chosen['slot']
        used.add((s['ace'], s['slot']))
        info[t] = {
            'tier': 'fallback',
            'slot': s,
            'loose_mat': False,
        }
        pending.remove(t)

    if pending:
        already = [sm for sm in slot_meta
                   if (sm['slot']['ace'], sm['slot']['slot']) in used]
        for t in list(pending):
            tm = t_meta[t]
            t_mat = (tm.get('mat') or '').strip().lower()

            allowed = nozzle_groups.get(t) if nozzle_groups else None
            candidates = [sm for sm in already
                          if (not t_mat or not sm['mat']
                              or sm['mat'] == t_mat)
                          and (allowed is None
                               or _head_of(sm['slot']) in allowed)]
            best = None
            best_d = None
            if tm['rgb'] is not None:
                for sm in candidates:
                    if sm['rgb'] is None:
                        continue
                    d2 = ((tm['rgb'][0] - sm['rgb'][0]) ** 2
                          + (tm['rgb'][1] - sm['rgb'][1]) ** 2
                          + (tm['rgb'][2] - sm['rgb'][2]) ** 2)
                    if best_d is None or d2 < best_d:
                        best_d, best = d2, sm
            if best is None:
                best = candidates[0] if candidates else None
            if best is None:
                info[t] = {'tier': 'no_slot', 'slot': None, 'loose_mat': False}
                pending.remove(t)
                continue
            s = best['slot']
            info[t] = {
                'tier': 'duplicate',
                'slot': s,
                'loose_mat': False,
            }
            pending.remove(t)

    remap = {}
    for t, entry in info.items():
        s = entry['slot']
        if s is None:
            continue
        synthetic_T = s['ace'] * num_heads + s['slot']
        if synthetic_T != t:
            remap[t] = synthetic_T
    return remap, info, used

def compute_head_mode_layout(slicer_colors, slicer_types, pinned_heads,
                             ace_slots, ace_head_of_ace, fuzzy_max_distance=None,
                             nozzle_groups=None):
    """Head-mode layout: K ACE-driven heads each print colours multiplexed via
    slot-swaps on THEIR OWN ACE (each ACE head is wired to exactly one ACE); the
    feeder heads are PINNED to a single fixed colour each (no swap).

    This is the "pinned heads + N swap heads" primitive - the same shape a
    future multi+manual matcher needs (a manual head = a pinned head).

    Args:
      slicer_colors:    {t: 'rrggbb'}   slicer tool colour per used T.
      slicer_types:     {t: material}   slicer material per T (material-strict).
      pinned_heads:     [{'head': int, 'material': str,
                          'color': 'rrggbb' | '#rrggbb'}]
                        the feeder heads' loaded identity; a slicer colour that
                        matches one (material-strict) pins to that head.
      ace_slots:        [{'ace','slot','material','color'}] - all loaded slots.
      ace_head_of_ace:  {ace_index: head} - which ACE head each ACE feeds. Only
                        slots on a wired ACE are usable; the matched slot's head
                        is looked up here.

    Returns dict with:
      'assignment': {t: entry}, entry one of
          {'kind':'pin', 'head':H,        'tier':str}
          {'kind':'ace', 'head':H, 'ace':a, 'slot':s, 'tier':str}
          {'kind':'none','tier':'no_slot'}      (no feeder AND no ACE slot)
      'feasible':   bool (no 'none' assignments)
      'infeasible': [t,...]
      'pinned':     sorted feeder heads actually used
      'ace_heads':  sorted ACE head indices
    """
    pins = {}
    for p in (pinned_heads or []):
        try:
            pins[int(p['head'])] = p
        except (KeyError, TypeError, ValueError):
            continue

    head_of_ace = {int(a): int(h) for a, h in (ace_head_of_ace or {}).items()}

    assignment = {}
    pinned_t = set()

    for t in sorted(slicer_colors.keys()):
        c = (slicer_colors.get(t) or '').strip().lstrip('#').lower()
        if not c:
            continue
        mat = (slicer_types.get(t) or '').strip().lower()

        allowed = nozzle_groups.get(t) if nozzle_groups else None
        cands = []
        for head, p in pins.items():
            pmat = (p.get('material') or '').strip().lower()
            if mat and pmat and pmat != mat:
                continue
            if allowed is not None and head not in allowed:
                continue
            cands.append({'head': head,
                          'color': (p.get('color') or '').strip().lstrip('#').lower()})
        match, tier = _find_color_match(
            cands, c, strict_color=False, fuzzy_max_distance=fuzzy_max_distance)
        if match is not None:
            assignment[t] = {'kind': 'pin', 'head': match['head'], 'tier': tier}
            pinned_t.add(t)

    usable_slots = [s for s in (ace_slots or []) if int(s['ace']) in head_of_ace]
    rest_colors = {t: slicer_colors[t] for t in slicer_colors if t not in pinned_t}
    rest_types = {t: (slicer_types.get(t) or '') for t in rest_colors}

    _remap, info, _used = match_colors_to_slots(
        rest_colors, usable_slots, num_heads=4,
        filament_types=rest_types, strict_color=False,
        fuzzy_max_distance=fuzzy_max_distance,
        nozzle_groups=nozzle_groups, head_of_ace=head_of_ace)
    for t, entry in info.items():
        s = entry.get('slot')
        if s is None:
            assignment[t] = {'kind': 'none', 'tier': entry.get('tier', 'no_slot')}
        else:
            assignment[t] = {'kind': 'ace', 'head': head_of_ace[int(s['ace'])],
                             'ace': s['ace'], 'slot': s['slot'],
                             'tier': entry.get('tier')}

    infeasible = sorted(t for t, e in assignment.items() if e['kind'] == 'none')
    pinned_used = sorted({e['head'] for e in assignment.values()
                          if e['kind'] == 'pin'})
    return {
        'assignment': assignment,
        'feasible': not infeasible,
        'infeasible': infeasible,
        'pinned': pinned_used,
        'ace_heads': sorted(set(head_of_ace.values())),
    }

def head_mode_swap_count(events, assignment):
    """Swaps the ACE heads perform for a head-mode `assignment` over the slicer
    toolchange sequence `events`. Pinned colours never swap (they print on their
    own feeder head); only (ace,slot) changes on ACE-assigned colours count, per
    ACE head. The first load on each ACE head counts as a swap."""
    cur = {}
    swaps = 0
    for t in events:
        e = assignment.get(t)
        if not e or e.get('kind') != 'ace':
            continue
        head = e.get('head')
        key = (e['ace'], e['slot'])
        if cur.get(head) != key:
            swaps += 1
            cur[head] = key
    return swaps

def head_mode_flush_cost(events, assignment, flush_matrix):
    """Total same-nozzle transition volume (mm3, slicer flush matrix) a
    head-mode `assignment` incurs over the toolchange sequence - the colour
    objective of compute_head_mode_optimize(objective='color'), computable
    for ANY assignment so plans are comparable in the preview. Pinned
    colours and cross-head switches cost nothing; the first load on a head
    purges no old colour. None when no matrix."""
    if flush_matrix is None:
        return None
    cur = {}
    total = 0.0
    for t in events:
        e = assignment.get(t)
        if not e or e.get('kind') != 'ace':
            continue
        head = e.get('head')
        key = (e['ace'], e['slot'])
        if cur.get(head) is not None and cur[head][0] != key:
            p_t = cur[head][1]
            if (0 <= p_t < len(flush_matrix)
                    and 0 <= t < len(flush_matrix)):
                total += flush_matrix[p_t][t]
        cur[head] = (key, t)
    return total

def head_mode_bg_stats(events, assignment, event_times=None, bg_heads=None):
    """Background-unload balance of a head-mode assignment: for every unload
    the rewrite WOULD stamp (same conditions as rewrite_head_mode_to_file -
    a released ACE head whose next arrival needs a different slot), classify
    the parked window:
      ok       - window >= BG_UNLOAD_MIN_WINDOW_MIN on a bg-enabled head
      small    - window known but too small (rewrite skips the stamp)
      unknown  - no M73 time info (stamped; engine abort = safety net)
      disabled - head not bg-enabled (no open dock declared)
    event_times = per-event remaining minutes (parse_toolchanges_with_times),
    bg_heads = bg-enabled head indices. Returns {'unloads','bg_ok','bg_small',
    'bg_unknown','bg_disabled','saved_s','details':[{head,window,verdict}]}."""
    bg_set = set(bg_heads or [])
    stats = {'unloads': 0, 'bg_ok': 0, 'bg_small': 0, 'bg_unknown': 0,
             'bg_disabled': 0, 'saved_s': 0, 'details': []}
    n_ev = len(events)
    have_t = bool(event_times) and len(event_times) == n_ev
    for i in range(1, n_ev):
        cur_e = assignment.get(events[i])
        rel_e = assignment.get(events[i - 1])
        if not cur_e or cur_e.get('kind') not in ('pin', 'ace'):
            continue
        if not rel_e or rel_e.get('kind') != 'ace':
            continue
        if events[i - 1] == events[i] or rel_e.get('head') == cur_e.get('head'):
            continue
        loaded_now = (rel_e['ace'], rel_e['slot'])
        nxt = None
        nxt_j = None
        for j in range(i + 1, n_ev):
            e2 = assignment.get(events[j])
            if (e2 and e2.get('kind') == 'ace'
                    and e2.get('head') == rel_e['head']):
                nxt = (e2['ace'], e2['slot'])
                nxt_j = j
                break
        if nxt is None or nxt == loaded_now:
            continue
        stats['unloads'] += 1
        head = rel_e['head']
        r_now = event_times[i] if have_t else None
        r_nxt = event_times[nxt_j] if have_t else None
        window = (r_now - r_nxt) if (r_now is not None
                                     and r_nxt is not None) else None
        if head not in bg_set:
            verdict = 'disabled'
            stats['bg_disabled'] += 1
        elif window is None:
            verdict = 'unknown'
            stats['bg_unknown'] += 1
        elif window < BG_UNLOAD_MIN_WINDOW_MIN:
            verdict = 'small'
            stats['bg_small'] += 1
        else:
            verdict = 'ok'
            stats['bg_ok'] += 1
        stats['details'].append(
            {'head': head, 'window': window, 'verdict': verdict})
    stats['saved_s'] = stats['bg_ok'] * BG_UNLOAD_INLINE_SAVING_S
    return stats

def compute_head_mode_optimize(events, feeder_heads, ace_heads, ace_num_of_head,
                               num_slots, layer_color_sets=None, max_colors=12,
                               event_times=None, bg_heads=None,
                               flush_matrix=None, objective='time'):
    """Head-mode loadout OPTIMIZER - the swap-minimal PROPOSED loadout that
    IGNORES the current physical load (plan 'optimize' + plan 'layer'/Belady).

    Cache model for the combiner hardware: each ACE head is a SINGLE active
    filament drawn from `num_slots` pre-loaded slots of ITS OWN ACE, and each
    feeder head holds one fixed colour:
      * F feeder "heads", capacity 1     -> a pinned colour, NEVER swaps.
      * K ACE heads, capacity num_slots  -> each swaps when ITS active colour
        changes; the <=num_slots distinct colours of that head sit pre-loaded in
        its ACE's slots so any switch is an automated combiner swap.
    This mirrors the multi optimizer (compute_swap_aware_layout) but with an
    asymmetric F-feeders + K-ACE-heads bin model; swaps are charged per ACE head.
    Brute-force over (F+K)^N (N = distinct colours, capped at `max_colors`).

    layer_color_sets (plan 'layer'/Belady): reject any assignment that routes
    2+ colours of a single layer through the SAME ACE head -> each ACE head only
    ever swaps at a layer boundary (Belady-/layer-optimal).

    Args:
      events:           toolchange sequence (slicer T-indices in print order).
      feeder_heads:     list of physical feeder (non-ACE) head indices to pin.
      ace_heads:        list of ACE-driven head indices (each a swap bin).
      ace_num_of_head:  {head: ace_index} - the ACE each ACE head feeds from.
      num_slots:        per-ACE slot capacity (pre-loadable colours, e.g. 4).
      layer_color_sets: per-layer colour sets; when given, layer-only swap mode.
      max_colors:       distinct-colour cap before bailing (brute-force guard).
      event_times:      per-event remaining minutes (parse_toolchanges_with_
                        times), for the bg-aware cost below. Optional.
      bg_heads:         bg-enabled (open-dock) head indices. Optional.

    Bg-aware cost (when event_times + bg_heads given): a swap whose unload
    can run in the BACKGROUND (released head, parked window >=
    BG_UNLOAD_MIN_WINDOW_MIN, head bg-enabled) costs BG_SWAP_COST_BG_S,
    every other swap BG_SWAP_COST_INLINE_S; the loadout with the lowest
    total print-stall cost wins (ties: fewer swaps, then fewer feeders).
    Without bg info every swap is inline and the ordering reduces to the
    old (swaps, pins) - byte-identical results.

    Returns (assignment, swaps) in the SAME format as compute_head_mode_layout
    (so head_mode_swap_count + rewrite_head_mode_to_file are reused unchanged):
      assignment[t] = {'kind':'pin','head':H,'tier':'optimize'}
                    | {'kind':'ace','head':H,'ace':a,'slot':s,'tier':'optimize'}
    or (None, None) when infeasible (too many colours for F + K*num_slots, too
    many colours to brute-force, or a layer needs >1 colour on one ACE head)."""
    from itertools import product

    colors_list = sorted(set(events))
    n = len(colors_list)
    feeders_sorted = sorted(feeder_heads)
    ace_sorted = sorted(ace_heads)
    F = len(feeders_sorted)
    K = len(ace_sorted)
    S = int(num_slots)
    if n == 0:
        return {}, 0
    if F <= 0 and K <= 0:
        return None, None
    if n > max_colors:
        return None, None

    best_c2b = None
    best_key = None
    for combo in product(range(F + K), repeat=n):
        feeder_count = [0] * F
        ace_count = [0] * K
        ok = True
        for b in combo:
            if b < F:
                feeder_count[b] += 1
                if feeder_count[b] > 1:
                    ok = False
                    break
            else:
                ace_count[b - F] += 1
                if ace_count[b - F] > S:
                    ok = False
                    break
        if not ok:
            continue

        c2b = {colors_list[i]: combo[i] for i in range(n)}

        if layer_color_sets is not None:
            conflict = False
            for lset in layer_color_sets:
                per_ace = {}
                for c in lset:
                    b = c2b.get(c)
                    if b is not None and b >= F:
                        per_ace[b] = per_ace.get(b, 0) + 1
                        if per_ace[b] > 1:
                            conflict = True
                            break
                if conflict:
                    break
            if conflict:
                continue

        have_t = bool(event_times) and len(event_times) == len(events)
        bg_set = set(bg_heads or [])
        cur = {}
        released_r = {}
        prev_bin = None
        swaps = 0
        bg_ok = 0
        flush = 0.0
        for i, t in enumerate(events):
            b = c2b[t]
            r_now = event_times[i] if have_t else None
            if prev_bin is not None and prev_bin != b:
                released_r.setdefault(prev_bin, r_now)
            prev_bin = b
            if b < F:
                continue
            if cur.get(b) != t:
                swaps += 1
                if cur.get(b) is not None:
                    head = ace_sorted[b - F]
                    rr = released_r.get(b)
                    if (head in bg_set and rr is not None
                            and r_now is not None
                            and rr - r_now >= BG_UNLOAD_MIN_WINDOW_MIN):
                        bg_ok += 1

                    if flush_matrix is not None:
                        p_t = cur.get(b)
                        if (0 <= p_t < len(flush_matrix)
                                and 0 <= t < len(flush_matrix)):
                            flush += flush_matrix[p_t][t]
                cur[b] = t
            released_r.pop(b, None)

        pins = sum(1 for b in combo if b < F)

        cost = ((swaps - bg_ok) * BG_SWAP_COST_INLINE_S
                + bg_ok * BG_SWAP_COST_BG_S)
        if objective == 'color' and flush_matrix is not None:
            key = (flush, cost, swaps, pins)
        else:
            key = (cost, swaps, pins)
        if best_key is None or key < best_key:
            best_key = key
            best_c2b = c2b

    if best_c2b is None:
        return None, None

    feeder_bin_to_head = {}
    next_feeder = 0
    ace_color_to_slot = {}
    ace_next_slot = {}
    assignment = {}
    for t in events:
        if t in assignment:
            continue
        b = best_c2b[t]
        if b >= F:
            head = ace_sorted[b - F]
            slot = ace_color_to_slot.get((head, t))
            if slot is None:
                slot = ace_next_slot.get(head, 0)
                ace_color_to_slot[(head, t)] = slot
                ace_next_slot[head] = slot + 1
            assignment[t] = {'kind': 'ace', 'head': head,
                             'ace': int(ace_num_of_head.get(head, head)),
                             'slot': slot, 'tier': 'optimize'}
        else:
            head = feeder_bin_to_head.get(b)
            if head is None:
                head = feeders_sorted[next_feeder]
                feeder_bin_to_head[b] = head
                next_feeder += 1
            assignment[t] = {'kind': 'pin', 'head': head, 'tier': 'optimize'}

    return assignment, head_mode_swap_count(events, assignment)

ANTI_OOZE_NO_UNRETRACT = 1.0

BG_UNLOAD_MIN_WINDOW_MIN = 1

BG_SWAP_COST_INLINE_S = 210
BG_SWAP_COST_BG_S = 30
BG_UNLOAD_INLINE_SAVING_S = BG_SWAP_COST_INLINE_S - BG_SWAP_COST_BG_S

BG_INITIAL_LOAD = False

PP_FORMAT_VERSION = 4

def detect_processed(text):
    """(processed: bool, fmt: int|None) for a gcode HEAD chunk or plan
    proxy: fmt is the stamped PP_FORMAT_VERSION, None = processed by a
    pre-versioning build (only the auto-load marker present)."""
    m = re.search(r'^;\s*multiACE processed:\s*format=(\d+)', text, re.M)
    if m:
        return True, int(m.group(1))
    if re.search(r'^;\s*multiACE auto-load:', text, re.M):
        return True, None
    return False, None

STANDBY_MIN_EXTRUDE_C = 170

def scan_cooling_standbys(in_path, head_of_tool):
    """Line numbers of M104/M109 lines that PROVABLY leave the printing head
    below min_extrude_temp - the ones the rewrite has to drop.

    Slicers with ooze prevention set every INACTIVE tool to a standby
    temperature (OrcaSlicer: nozzle_temperature + standby_temperature_delta,
    e.g. 240-150=90). Those lines address a slicer TOOL, and the remap
    collapses several tools onto one physical head - so a standby meant for a
    parked colour can land on the head that prints the next one.

    Most of them are HARMLESS and must be kept: a pre-toolchange cooldown
    parks its own head (that is the anti-ooze measure, S35 dock drooling),
    and even after a collision the arriving tool's M109 overrides it one line
    later. A 6-colour Snapmaker Orca file with the same setting and two
    colours on one head has 831 of them and 0 of 1,024,850 extrusions below
    the limit. Dangerous is only the case where nothing re-heats before the
    head extrudes again: at an object start in print-by-object mode the
    slicer sets all OTHER tools to standby AFTER the change, and the head
    then cools through the limit mid-object (djfilament 2026-08-11: 144
    extrusions at target 90 instead of 245, "under temp error").

    Telling those apart needs the forward view, not a per-line test, so this
    replays the file: track the active head and each head's target, and when
    an extruding move runs on a head whose target is below the limit, blame
    the line that set it. Only lines actually blamed get dropped, so a file
    without the collision comes out byte-identical.
    """
    danger = set()

    cold_setters = {}
    target = {}
    active = None
    t_re = re.compile(r'^T(\d{1,2})\s*$')
    m_re = re.compile(r'^M10[49]\b')
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
        for no, line in enumerate(fin):
            code = line.split(';')[0].strip()
            if not code:
                continue
            mt = t_re.match(code)
            if mt:
                h = head_of_tool(int(mt.group(1)))
                if h is not None:
                    active = h
                continue
            if m_re.match(code):
                ms = re.search(r'\bS(\d+)', code)
                if ms is None:
                    continue
                mh = re.search(r'\bT(\d{1,2})', code)
                h = head_of_tool(int(mh.group(1))) if mh else active
                if h is None:
                    continue
                val = int(ms.group(1))
                target[h] = val
                if 0 < val < STANDBY_MIN_EXTRUDE_C:
                    cold_setters.setdefault(h, []).append(no)
                else:

                    cold_setters[h] = []
                continue
            if code[:2] in ('G0', 'G1') and active is not None:
                me = re.search(r'\bE(-?[\d.]+)', code)
                if me is None:
                    continue
                try:
                    if float(me.group(1)) <= 0:
                        continue
                except ValueError:
                    continue
                tg = target.get(active)

                if tg is not None and 0 < tg < STANDBY_MIN_EXTRUDE_C:
                    danger.update(cold_setters.get(active, ()))
    return danger

PURGE_MATRIX_ENABLE = True
FILAMENT_MM3_PER_MM = 2.405

PURGE_MATRIX_TOPUP_FRAC = 0.45
PURGE_MATRIX_MIN_MM = 40.
PURGE_MATRIX_MAX_MM = 150.

_FLUSH_MATRIX_RE = re.compile(
    r'^;?\s*flush_volumes_matrix\s*=\s*([0-9.,\s]+)$')
_FLUSH_MULT_RE = re.compile(
    r'^;?\s*flush_multiplier\s*=\s*([0-9.]+)')

def parse_flush_matrix(gcode):
    """Text variant of parse_flush_matrix_from_file: same contract, takes
    the gcode as a string (e.g. the preflight's filtered plan proxy - the
    flush_ lines are on its keep-list)."""
    raw = None
    mult = 1.0
    for line in gcode.splitlines():
        m = _FLUSH_MATRIX_RE.match(line.strip())
        if m:
            raw = m.group(1)
            continue
        m = _FLUSH_MULT_RE.match(line.strip())
        if m:
            try:
                mult = float(m.group(1))
            except ValueError:
                pass
    return _flush_matrix_build(raw, mult)

def parse_flush_matrix_from_file(in_path):
    """Scan the slicer config block for flush_volumes_matrix (mm3, row-major
    NxN, row = from-tool) and flush_multiplier. Returns the multiplied NxN
    matrix as a list of lists, or None when the file carries none. Last
    occurrence wins (header copies exist in some slicer exports)."""
    raw = None
    mult = 1.0
    try:
        with open(in_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                m = _FLUSH_MATRIX_RE.match(line.strip())
                if m:
                    raw = m.group(1)
                    continue
                m = _FLUSH_MULT_RE.match(line.strip())
                if m:
                    try:
                        mult = float(m.group(1))
                    except ValueError:
                        pass
    except OSError:
        return None
    return _flush_matrix_build(raw, mult)

def parse_flush_matrix_raw_from_file(in_path):
    """The stamp basis: pair values as authored, with the file's
    flush_multiplier applied UPWARD ONLY (max(1, mult) - Dirk 2026-08-17,
    b/w squares test). The asymmetry is the whole point:
      - mult < 1 is the user's TOWER-economy calibration (Dirk's own BUGY
        trim ran 0.6) and must not scale our melt-zone top-up below the
        HW-proven floor (black->red washed under ~80 mm) - so downward it
        is ignored, exactly as before;
      - mult > 1 is the one slicer-side knob a TOWER-LESS print has to
        raise the stamps to the full transition (the b/w test: 0.45 x raw
        left half of each into-black square as wash in the part; there is
        no tower to absorb it). Deliberate per-pair reductions go through
        the raw cells, which keep working 1:1 in both directions.
    The multiplied variant stays the contract of the public parse
    functions (optimizer/flush-cost = tower model; changing their return
    would break preflight_core across a version skew, S21)."""
    raw = None
    mult = 1.0
    try:
        with open(in_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                m = _FLUSH_MATRIX_RE.match(line.strip())
                if m:
                    raw = m.group(1)
                    continue
                mm = _FLUSH_MULT_RE.match(line.strip())
                if mm:
                    try:
                        mult = float(mm.group(1))
                    except ValueError:
                        mult = 1.0
    except OSError:
        return None
    return _flush_matrix_build(raw, max(1.0, mult))

def _flush_matrix_build(raw, mult):
    if not raw:
        return None
    try:
        vals = [float(v) for v in raw.replace(' ', '').split(',') if v != '']
    except ValueError:
        return None
    n = int(round(len(vals) ** 0.5))
    if n < 2 or n * n != len(vals):
        return None

    if not mult or mult <= 0:
        mult = 1.0
    return [[vals[i * n + j] * mult for j in range(n)] for i in range(n)]

def _matrix_purge_mm(matrix, t_from, t_to):
    """Per-pair top-up flush length in mm, or None when the pair is
    unknown/identity (caller then emits no stamp -> engine default)."""
    if (matrix is None or t_from is None or t_to is None
            or t_from == t_to):
        return None
    if not (0 <= t_from < len(matrix) and 0 <= t_to < len(matrix)):
        return None
    pair_mm = matrix[t_from][t_to] / FILAMENT_MM3_PER_MM
    mm = pair_mm * PURGE_MATRIX_TOPUP_FRAC
    mm = max(PURGE_MATRIX_MIN_MM, min(PURGE_MATRIX_MAX_MM, mm))
    return int(round(mm))

_UNRETRACT_RE = re.compile(
    r'^G[01]\s+(?:F[0-9.]+\s+)?E([0-9.]+)(?:\s+F[0-9.]+)?\s*$')

def _scan_post_t_unretracts(in_path):
    """Map line_no of each bare T<n> line in the ORIGINAL file -> the explicit
    toolchange un-retract value that follows it (float), or None when none
    appears before the first extruding move (= the profile refills distributed
    in the wipe; SnOrca layer 0). The rewrite mirrors this value in the swap's
    ANTI_OOZE stamp: the swap's end-retract must equal exactly what the slicer
    pushes back, or the wipe starts lean (cushion too big) / the un-retract
    blobs into a fuller nozzle (cushion too small, eb775cf3)."""
    bare_t = re.compile(r'^T(\d{1,2})\s*$')
    result = {}
    open_t = None
    dist = 0
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
        for line_no, line in enumerate(fin):
            stripped = line.strip()
            if bare_t.match(stripped):
                open_t = line_no
                dist = 0
                continue
            if open_t is None:
                continue
            dist += 1
            m = _UNRETRACT_RE.match(stripped)
            if m:
                try:
                    result[open_t] = float(m.group(1))
                except ValueError:
                    pass
                open_t = None
            elif _is_extruding_move(stripped) or dist > 300:
                open_t = None
    return result

def _fmt_anti_ooze(v):
    s = ('%.2f' % v).rstrip('0').rstrip('.')
    return s or '0'

def _scan_body_tools(in_path):
    """Body toolchange sequence (bare T values after the first Change-Tool
    marker), for the background-unload look-ahead: when a head is RELEASED
    and its NEXT arrival needs a different (ace,slot), the rewrite stamps
    ACE_BG_UNLOAD HEAD=<h> QUIET=1 so the unload runs while other heads
    print. Same in_body semantics as the main rewrite loop.

    Returns (tools, times, line_nos): per body T also the slicer's
    remaining print time at that point (last M73 R=<minutes> BEFORE the
    T; None when the file carries no M73) and the raw 0-based line number
    of the T line - the same numbering _scan_post_t_unretracts keys on,
    so the bg-swap stamp can look up the FUTURE arrival's un-retract
    (its ANTI_OOZE contract, S35)."""
    change_t = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+')
    bare_t = re.compile(r'^T(\d{1,2})\s*$')
    m73_r = re.compile(r'^M73\b.*?\bR(\d+(?:\.\d+)?)')
    tools = []
    times = []
    line_nos = []
    last_r = None
    in_body = False
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
        for line_no, line in enumerate(fin):
            stripped = line.rstrip('\r\n')
            mr = m73_r.match(stripped)
            if mr:
                last_r = float(mr.group(1))
                continue
            if not in_body and change_t.match(stripped):
                in_body = True
                continue
            m = bare_t.match(stripped)
            if m and in_body:
                tools.append(int(m.group(1)))
                times.append(last_r)
                line_nos.append(line_no)
    return tools, times, line_nos

def rewrite_head_mode_to_file(in_path, out_path, assignment, ace_head=None,
                              progress=None, pickup_cleaning=False):
    """Streaming head-mode rewrite of the ORIGINAL slicer gcode. Each slicer
    T<n> is rewritten per `assignment` (compute_head_mode_layout):
      - 'pin' -> T<pin_head>                       (feeder head, no swap)
      - 'ace' -> T<entry head> + ACE_SWAP_HEAD HEAD=<head> ACE=a SLOT=s
                 (deduped per ACE head on consecutive same (ace,slot))
    Each 'ace' entry carries its OWN ACE head (one ACE per head), so the swap is
    emitted for that head and the loaded-slot state is tracked per head.
    M104/M109 T<n> and the pre-body tool selection are remapped to the assigned
    physical head (no swap). SM_PRINT_PREEXTRUDE_FILAMENT for an ACE colour is
    DROPPED (a real swap flushes anyway; per-swap primes stacked ooze drops on
    the tower and their SKIP_POS_RESTORE coupling knocked docked heads off,
    HW 2026-07-04); instead each ACE head gets ONE forced prime at its first
    body use, and the initial tool gets its prime from the auto-load block
    (inject_auto_load_to_file). For a pinned colour the line is remapped to the
    head (stock, un-forced; first one FORCE=1). No apply_remap step is needed -
    the assignment IS the remap. (`ace_head` is unused, kept for signature
    compatibility.) Returns (active_swaps, skipped_swaps)."""
    def head_of(n):
        e = assignment.get(n)
        if not e:
            return None
        if e.get('kind') in ('pin', 'ace'):
            return e.get('head')
        return None

    def ace_entry_of(n):
        e = assignment.get(n)
        if e and e.get('kind') == 'ace':
            return (e['head'], e['ace'], e['slot'])
        return None

    m104_re    = re.compile(r'^M10[49]\b')
    preextr_re = re.compile(r'^SM_PRINT_PREEXTRUDE_FILAMENT INDEX=(\d{1,2})\b')
    change_t   = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+')
    bare_t     = re.compile(r'^T(\d{1,2})\s*$')

    def fix_m104(line):
        def repl(m):
            h = head_of(int(m.group(1)))
            return 'T' + str(h if h is not None else int(m.group(1)) % 4)
        return re.sub(r'T(\d{1,2})', repl, line)

    in_body = False

    cooling_standbys = scan_cooling_standbys(in_path, head_of)
    cur = {}
    cur_tool = {}
    active = 0
    skipped = 0
    primed_ace = set()
    primed_pin = set()

    post_t_unret = _scan_post_t_unretracts(in_path)

    flush_matrix = (parse_flush_matrix_raw_from_file(in_path)
                    if PURGE_MATRIX_ENABLE else None)

    body_tools, body_times, body_lines = _scan_body_tools(in_path)
    bt_idx = 0
    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0

    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin,\
         open(out_path, 'w', encoding='utf-8') as fout:
        for line_no, line in enumerate(fin):
            seen += len(line.encode('utf-8', errors='ignore'))
            stripped = line.rstrip('\r\n')

            if m104_re.match(stripped):
                if line_no in cooling_standbys:
                    fout.write('; multiACE dropped: %s  ; would cool the '
                               'printing head below %d C\n'
                               % (stripped.strip(), STANDBY_MIN_EXTRUDE_C))
                else:
                    fout.write(fix_m104(line))
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mp = preextr_re.match(stripped)
            if mp:
                n = int(mp.group(1))
                ae = ace_entry_of(n)
                if ae is not None:

                    last_pr = _emit_progress(progress, seen, total, last_pr)
                    continue
                h = head_of(n)
                if h is None:
                    fout.write(line)
                elif h not in primed_pin:

                    primed_pin.add(h)
                    fout.write(
                        'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d FORCE=1\n' % h)
                else:
                    fout.write('SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d\n' % h)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            if not in_body and change_t.match(stripped):
                in_body = True

            mt = bare_t.match(stripped)
            if mt:
                n = int(mt.group(1))
                h = head_of(n)
                if h is None:
                    fout.write(line)

                    if (in_body and bt_idx < len(body_tools)
                            and body_tools[bt_idx] == n):
                        bt_idx += 1
                    last_pr = _emit_progress(progress, seen, total, last_pr)
                    continue
                fout.write('T%d\n' % h)

                if (in_body and bt_idx < len(body_tools)
                        and body_tools[bt_idx] == n):
                    if bt_idx > 0:
                        rel_tool = body_tools[bt_idx - 1]
                        ae_rel = ace_entry_of(rel_tool)
                        if (ae_rel is not None and rel_tool != n
                                and ae_rel[0] != head_of(n)):
                            loaded_now = (ae_rel[1], ae_rel[2])
                            nxt = None
                            nxt_j = None
                            for j in range(bt_idx + 1, len(body_tools)):
                                e2 = ace_entry_of(body_tools[j])
                                if e2 is not None and e2[0] == ae_rel[0]:
                                    nxt = (e2[1], e2[2])
                                    nxt_j = j
                                    break
                            if nxt is not None and nxt != loaded_now:

                                r_now = body_times[bt_idx]
                                r_nxt = body_times[nxt_j]
                                window = None
                                if r_now is not None and r_nxt is not None:
                                    window = r_now - r_nxt
                                if (window is not None
                                        and window < BG_UNLOAD_MIN_WINDOW_MIN):
                                    fout.write(
                                        '; multiACE bg-swap HEAD=%d '
                                        'skipped: parked window ~%dmin < '
                                        '%dmin\n'
                                        % (ae_rel[0], int(window),
                                           BG_UNLOAD_MIN_WINDOW_MIN))
                                else:

                                    ao = post_t_unret.get(
                                        body_lines[nxt_j])
                                    if ao is None:
                                        ao = ANTI_OOZE_NO_UNRETRACT

                                    _bgp = _matrix_purge_mm(
                                        flush_matrix, rel_tool,
                                        body_tools[nxt_j])
                                    fout.write(
                                        'ACE_BG_SWAP HEAD=%d ACE=%d '
                                        'SLOT=%d ANTI_OOZE=%s%s QUIET=1\n'
                                        % (ae_rel[0], nxt[0], nxt[1],
                                           _fmt_anti_ooze(ao),
                                           (' PURGE=%d' % _bgp)
                                           if _bgp is not None else ''))
                    bt_idx += 1
                ae = ace_entry_of(n)
                if not in_body:

                    if ae is not None:
                        primed_ace.add(ae[0])
                elif ae is not None:
                    head, a, s = ae
                    if cur.get(head) == (a, s):
                        fout.write('; ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d'
                                   '  ; skipped (already loaded)\n'
                                   % (head, a, s))
                        skipped += 1
                        cur_tool[head] = n

                        if pickup_cleaning:
                            fout.write('ACE_PICKUP_CLEAN HEAD=%d\n' % head)
                    else:

                        v = post_t_unret.get(line_no)
                        if v is None:
                            v = ANTI_OOZE_NO_UNRETRACT

                        _pp = _matrix_purge_mm(flush_matrix,
                                               cur_tool.get(head), n)
                        if _pp is not None:
                            fout.write('ACE_SET_PURGE LENGTH=%d\n' % _pp)
                        fout.write('ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d'
                                   ' ANTI_OOZE=%s\n'
                                   % (head, a, s, _fmt_anti_ooze(v)))
                        cur[head] = (a, s)
                        cur_tool[head] = n
                        active += 1
                    if head not in primed_ace:

                        primed_ace.add(head)
                        fout.write('SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d '
                                   'FORCE=1\n' % head)
                elif pickup_cleaning:

                    fout.write('ACE_PICKUP_CLEAN HEAD=%d\n' % h)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            fout.write(line)
            last_pr = _emit_progress(progress, seen, total, last_pr)

    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return active, skipped

def parse_filament_types(gcode):
    """Best-effort lookup table T-index -> material name (PLA, PETG, …).
    Slicers emit `filament_type = PLA;PETG;PLA` similar to filament_colour."""
    types = {}
    all_lines = gcode.splitlines()
    scan = all_lines[:300] + all_lines[-2000:]
    for line in scan:
        m = re.search(r';\s*filament[_ ]type\s*[:=]\s*(.+)', line, re.I)
        if m:
            for i, p in enumerate(re.split(r'[;,]', m.group(1))):
                p = p.strip()
                if p:
                    types[i] = p
            if types:
                break
    return types

def parse_nozzle_diameters(gcode):
    """T-index -> nozzle diameter (mm) from the slicer header line
    `; nozzle_diameter = 0.2,0.8,0.4,0.6`.

    The parser itself is slicer-agnostic (every Orca/Prusa descendant
    emits the line), but the GATE that consumes it is deliberately scoped
    to FOrcaSlicer files (Dirk 2026-08-07: "bitte erstmal an forca
    haengen"). Rationale, verified in stock 1.5.2: a mixed-nozzle job
    cannot reach the machine any other way. Stock Orca emits
    SET_PRINT_TASK_PARAMETERS, whose guard compares the file's
    nozzle_diameter[0] - index 0 ALWAYS - against every USED extruder
    (print_task_config.py:1286, exception_code 14), so a file whose used
    heads have differing actual diameters is refused outright; no other
    reader consults the per-index list. FOrcaSlicer reaches the printer
    only because it omits that whole briefing line. Scoping the gate to
    FOrca therefore covers the entire exposed population while leaving
    the normal workflow provably untouched.

    Empty dict when the line is absent - callers must then treat the
    diameters as UNKNOWN and skip the gate, never assume uniformity.

    Why it matters (HW-proven 2026-08-07 on FOrcaSlicer 2.3.2 cubes): with
    mixed nozzles the slicer bakes each tool's own line WIDTH into that
    tool's extrusions - T0 tops out at 0.274mm on a 0.2 nozzle while T1
    lays 0.8mm. match_colors_to_slots is colour/material driven and was
    diameter-blind, so a perfectly ordinary reload in a different slot
    order remapped all four tools onto wrong-sized nozzles (4/4) with no
    error - a silently ruined print."""
    dia = {}
    all_lines = gcode.splitlines()
    scan = all_lines[:300] + all_lines[-2000:]
    for line in scan:
        m = re.search(r';\s*nozzle[_ ]diameter\s*[:=]\s*(.+)', line, re.I)
        if not m:
            continue
        for i, p in enumerate(re.split(r'[;,]', m.group(1))):
            p = p.strip()
            if not p:
                continue
            try:
                v = float(p)
            except ValueError:
                continue
            if v > 0:
                dia[i] = v
        if dia:
            break
    return dia

def parse_slicer_name(gcode):
    """The slicer's own '; generated by <name> <version>' banner, or ''."""
    for line in gcode.splitlines()[:300]:
        m = re.search(r';\s*generated by\s+(.+?)\s+on\s', line, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r';\s*generated by\s+(.+)', line, re.I)
        if m:
            return m.group(1).strip()
    return ''

def is_forca_slicer(slicer_name):
    """True for a FOrcaSlicer banner. The mixed-nozzle handling hangs off
    this (see parse_nozzle_diameters for why that scoping is safe): FOrca
    is the only slicer that can put a mixed-nozzle job on the machine at
    all, so everything gated on it leaves the normal workflow untouched."""
    return 'forcaslicer' in (slicer_name or '').replace(' ', '').lower()

def nozzle_gate_groups(tool_dia, head_dia=None, num_heads=4):
    """{tool T -> set of heads that tool may print on}.

    TWO different things, deliberately kept apart (corrected 2026-08-07 after
    a FOrcaSlicer PTP file made the difference visible):
      tool_dia  {T: mm}    DEMAND - which nozzle each FILAMENT was sliced for.
                           From the file's `nozzle_diameter` header, which is
                           indexed PER FILAMENT (10 filaments -> 10 entries,
                           every used tool matching its measured line width).
      head_dia  {head: mm} SUPPLY - which nozzle each HEAD actually carries.
                           From the printer. The file never states this: a
                           mixed file declared 0.2,0.8,0.4,0.6 and nothing in
                           it says whether that is the physical order.

    Reading the first four demand entries as "head 0..3" is the old behaviour
    and stays the FALLBACK when the printer could not be asked - it is right
    whenever the slicer profile happens to list the nozzles in machine order,
    and it is what shipped before, so falling back never makes things worse.

    Empty dict = no constraint (uniform machine, no data). A tool whose
    diameter is unknown is ABSENT from the result -> unconstrained, and the UI
    flags it. A tool whose diameter no head carries maps to an EMPTY set ->
    it cannot print anywhere, which the matcher turns into no_slot rather
    than a silent wrong-nozzle assignment."""
    if not tool_dia:
        return {}
    if not head_dia:
        head_dia = {h: tool_dia.get(h) for h in range(num_heads)
                    if tool_dia.get(h)}
    if not head_dia:
        return {}
    groups = {}
    for t, want in tool_dia.items():
        if not want:
            continue
        groups[t] = {h for h, have in head_dia.items()
                     if have and abs(have - want) < 0.001}

    every = set(head_dia)
    if all(v == every for v in groups.values()):
        return {}
    return groups

def parse_color_names(gcode):
    """Best-effort lookup table T-index -> color name. Orca writes
    the filament_colour line at the end of the gcode, Bambu/Prusa
    often near the top - scan both."""
    names = {}
    all_lines = gcode.splitlines()
    scan = all_lines[:300] + all_lines[-2000:]
    for line in scan:
        m = re.search(r';\s*filament[_ ]colou?r\s*[:=]\s*(.+)', line, re.I)
        if m:
            for i, p in enumerate(re.split(r'[;,]', m.group(1))):
                p = p.strip()
                if p and p != '#':
                    names[i] = p
            if names:
                break
    return names

_NAMED_COLORS = (
    ('Black',      (0x00, 0x00, 0x00)),
    ('White',      (0xFF, 0xFF, 0xFF)),
    ('Gray',       (0x80, 0x80, 0x80)),
    ('DarkGray',   (0x40, 0x40, 0x40)),
    ('LightGray',  (0xD3, 0xD3, 0xD3)),
    ('Silver',     (0xC0, 0xC0, 0xC0)),
    ('Red',        (0xE0, 0x20, 0x20)),
    ('DarkRed',    (0x8B, 0x00, 0x00)),
    ('Pink',       (0xFF, 0xC0, 0xCB)),
    ('Orange',     (0xFF, 0x8C, 0x00)),
    ('Yellow',     (0xFF, 0xE0, 0x20)),
    ('Gold',       (0xDA, 0xA5, 0x20)),
    ('Brown',      (0x8B, 0x45, 0x13)),
    ('Beige',      (0xE6, 0xD6, 0xA5)),
    ('Green',      (0x20, 0xA0, 0x20)),
    ('DarkGreen',  (0x00, 0x64, 0x00)),
    ('LightGreen', (0x90, 0xEE, 0x90)),
    ('Cyan',       (0x20, 0xD0, 0xD0)),
    ('Blue',       (0x30, 0x50, 0xF0)),
    ('DarkBlue',   (0x00, 0x00, 0x8B)),
    ('LightBlue',  (0xAD, 0xD8, 0xE6)),
    ('Purple',     (0x80, 0x20, 0x80)),
    ('Magenta',    (0xE0, 0x20, 0xE0)),
)

_COLOR_QUALIFIERS = ('Dark', 'Light')

_COLOR_SYNONYMS = {
    'Silver': 'Gray',
    'Gold':   'Yellow',
}

def _strip_color_qualifier(name):
    """'DarkRed' -> 'Red', 'LightBlue' -> 'Blue', otherwise unchanged."""
    if not name:
        return ''
    for q in _COLOR_QUALIFIERS:
        if name.startswith(q) and len(name) > len(q):
            return name[len(q):]
    return name

def _canonical_color_name(name):
    """Apply qualifier-strip + synonym table.
       'DarkRed' -> 'Red', 'Silver' -> 'Gray', 'LightGray' -> 'Gray'."""
    base = _strip_color_qualifier(name)
    return _COLOR_SYNONYMS.get(base, base)

def approx_color_name(hex_str):
    """Nearest named color from #RRGGBB, or hex unchanged if not parseable."""
    if not hex_str:
        return '?'
    s = hex_str.strip().lstrip('#')
    if len(s) < 6:
        return hex_str
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return hex_str
    best, best_d = None, 1 << 30
    for name, (nr, ng, nb) in _NAMED_COLORS:
        d = (r - nr) ** 2 + (g - ng) ** 2 + (b - nb) ** 2
        if d < best_d:
            best_d, best = d, name
    return best

def _hex_to_rgb_tuple(hex_str):
    """('#rrggbb' or 'rrggbb') -> (r, g, b) ints, or None."""
    s = (hex_str or '').strip().lstrip('#')
    if len(s) < 6:
        return None
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None

def format_color_hex_rgb(hex_str):
    """'#831100' -> '#831100 RGB(131,17,0)' for paste-into-slicer convenience."""
    rgb = _hex_to_rgb_tuple(hex_str)
    if rgb is None:
        return hex_str or '?'
    return '%s RGB(%d,%d,%d)' % (hex_str.lower(), rgb[0], rgb[1], rgb[2])

def format_color(t_index, color_names):
    hex_val = color_names.get(t_index)
    if not hex_val:
        return '?'
    name = approx_color_name(hex_val)
    full = format_color_hex_rgb(hex_val)
    if hex_val.lstrip('#').lower() == name.lower():
        return full
    return '%s (%s)' % (name, full)

def infer_num_aces(gcode):
    """Detect how many ACEs the slicer's T-index assignment uses.

    For every used T<n> command (n >= 0), the canonical ACE is n // 4.
    Inferred count = max(ACE) + 1 across all used Ts. Returns at least
    1 (single-color prints have only T0 → ACE 0).

    This eliminates the need for a manual --aces flag: the slicer
    already knows which physical ACE/slot each colour lives in
    (because the user assigned cartridges that way), so the gcode
    itself is the source of truth.
    """
    lines = gcode.splitlines()
    max_ace = 0
    for line in lines:
        s = line.strip()
        m = re.match(r'^T(\d{1,2})\s*$', s)
        if m:
            ace = int(m.group(1)) // 4
            if ace > max_ace:
                max_ace = ace
    return max_ace + 1

def plan_loadout(gcode, num_aces=3):

    split_re = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                          re.MULTILINE)
    m = split_re.search(gcode)
    body_gcode = gcode[m.start():] if m else ''

    events = list(parse_toolchanges(body_gcode))
    if not events:
        return None
    color_names = parse_color_names(gcode)

    counts = defaultdict(int)
    for t in events:
        counts[t] += 1

    colors = sorted(counts.keys())
    plan = {}
    for c in colors:
        head = c % 4
        ace = c // 4
        if ace == 0:
            plan[c] = {'ace': 0, 'slot': head, 'head': head, 'role': 'initial'}
        else:
            plan[c] = {'ace': ace, 'slot': head, 'head': head, 'role': 'swap'}

    head_current = {h: h for h in range(4)}
    swaps = 0
    for t in events:
        info = plan.get(t)
        if info is None:
            continue
        h = info['head']
        if head_current.get(h) != t:
            swaps += 1
            head_current[h] = t

    layer_info = compute_layer_swap_plan(body_gcode, num_aces=num_aces)

    return {
        'plan': plan, 'counts': counts, 'color_names': color_names,
        'swaps': swaps,
        'total_changes': len(events), 'events': events,
        'layer_info': layer_info,
    }

def _suggest_layer_friendly_remap(layer_colors, num_aces):
    """When the current T-index assignment causes same-head conflicts in
    some layers, search for a remap of T-indices to head buckets that
    eliminates all conflicts while requiring minimal physical
    reordering.

    Each color (= existing T-index) lives at head c%4 today. We may
    reassign it to any head 0-3 (= new T-index k where k%4 = new_head).
    Constraints:
      - No two colors on the same head within any layer.
      - Per-head color count <= num_aces.
    Objective: minimize the number of colors moved off their current
    head (so the user has to physically rearrange as few cartridges as
    possible).

    Returns dict {old_T: new_T} or None if no feasible remap exists.
    Brute-force over 4^N head assignments where N = #colors. Practical
    up to ~12 colors (4^12 = ~17M).
    """
    colors = sorted({c for s in layer_colors for c in s})
    n = len(colors)
    if n == 0 or n > 12:
        return None
    current_head = {c: c % 4 for c in colors}

    from itertools import product

    best_assignment = None
    best_moved = n + 1

    layer_lists = [list(s) for s in layer_colors]

    for assignment in product(range(4), repeat=n):

        head_count = [0, 0, 0, 0]
        for h in assignment:
            head_count[h] += 1
        if any(c > num_aces for c in head_count):
            continue

        head_for_color = {colors[i]: assignment[i] for i in range(n)}

        conflict = False
        for layer_list in layer_lists:
            heads_used = set()
            for c in layer_list:
                h = head_for_color[c]
                if h in heads_used:
                    conflict = True
                    break
                heads_used.add(h)
            if conflict:
                break
        if conflict:
            continue

        moved = sum(1 for i, c in enumerate(colors)
                    if assignment[i] != current_head[c])
        if moved < best_moved:
            best_moved = moved
            best_assignment = assignment
            if moved == 0:
                break

    if best_assignment is None:
        return None

    head_groups = {h: [] for h in range(4)}
    for i, c in enumerate(colors):
        head_groups[best_assignment[i]].append(c)
    new_t = {}
    for h, cs in head_groups.items():
        used_aces = set()

        for c in cs:
            if c % 4 != h:
                continue
            cur_ace = c // 4
            if cur_ace not in used_aces and cur_ace < num_aces:
                new_t[c] = h + 4 * cur_ace
                used_aces.add(cur_ace)

        for c in cs:
            if c % 4 != h or c in new_t:
                continue
            for ace in range(num_aces):
                if ace not in used_aces:
                    new_t[c] = h + 4 * ace
                    used_aces.add(ace)
                    break

        for c in cs:
            if c in new_t:
                continue
            for ace in range(num_aces):
                if ace not in used_aces:
                    new_t[c] = h + 4 * ace
                    used_aces.add(ace)
                    break

    return new_t if any(v != k for k, v in new_t.items()) else None

def compute_swap_aware_layout(events, num_aces, num_heads=4,
                              layer_color_sets=None):
    """Search head assignments per color (free distribution - colors
    are NOT bound to head=T%4) for the one that minimizes the runtime
    swap count.

    Swap count under a given assignment c->head:
      Per head, walk the toolchange sequence; each time the head's
      currently-loaded color differs from the next event on that head
      counts as 1 swap. The first appearance on each head is free
      (covered by auto-load, not a runtime swap).

    Args:
        events: toolchange sequence as a list of T-indices in print order
        num_aces: max colors per head (= ACE capacity)
        num_heads: 4 (physical extruders on Snapmaker U1)
        layer_color_sets: optional list of {colors in each layer} sets;
            when provided, assignments that put 2+ colors on the same
            head within ANY single layer are rejected (= layer-only
            swap mode, no mid-layer changes).

    Returns:
        (color_to_head dict, swap_count) on success
        (None, None) if no assignment satisfies the constraints

    Brute-force over num_heads^N (N = distinct colors). Practical up
    to ~12 colors (4^12 ≈ 17M)."""
    from itertools import product

    colors_list = sorted(set(events))
    n = len(colors_list)
    if n == 0:
        return {}, 0
    if n > 12:
        return None, None

    best_assignment = None
    best_swaps = None

    for assignment in product(range(num_heads), repeat=n):
        head_count = [0] * num_heads
        for h in assignment:
            head_count[h] += 1
        if any(c > num_aces for c in head_count):
            continue

        c2h = {colors_list[i]: assignment[i] for i in range(n)}

        if layer_color_sets is not None:
            conflict = False
            for lset in layer_color_sets:
                heads_used = set()
                for c in lset:
                    h = c2h.get(c)
                    if h is None:
                        continue
                    if h in heads_used:
                        conflict = True
                        break
                    heads_used.add(h)
                if conflict:
                    break
            if conflict:
                continue

        head_current = [None] * num_heads
        swaps = 0
        for t in events:
            h = c2h[t]
            if head_current[h] != t:
                if head_current[h] is not None:
                    swaps += 1
                head_current[h] = t

        if best_swaps is None or swaps < best_swaps:
            best_swaps = swaps
            best_assignment = c2h

    if best_assignment is None:
        return None, None
    return best_assignment, best_swaps

def compute_layer_swap_plan(body_gcode, num_aces=4):
    """Analyze whether the print can be served with layer-boundary-only
    swaps (no mid-layer toolchanges) on a 4-slot printhead with at most
    `num_aces` physical ACE units.

    Walks the body gcode layer-by-layer (;LAYER_CHANGE markers), tracks
    the set of distinct colors active within each layer, then - if every
    layer fits in 4 slots - runs a budget-aware Belady cache-replacement
    that prefers to spread swaps across heads so no head's ACE index
    exceeds num_aces - 1.

    Returns a dict: {feasible, max_per_layer, num_layers, layer_swaps,
    aces_needed, initial_loadout, events, color_slots, histogram}.
    """

    lines = body_gcode.splitlines()
    current = None
    mfirst = re.match(r';\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)',
                      lines[0] if lines else '')
    if mfirst:
        current = int(mfirst.group(1))

    change_re = re.compile(
        r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')

    layer_seqs = []
    cur = None
    for line in lines:
        s = line.strip()
        if s.startswith(';LAYER_CHANGE'):
            if cur is not None:
                layer_seqs.append(cur)
            cur = []
            if current is not None:
                cur.append(current)
            continue
        mc = change_re.match(s)
        if mc:
            current = int(mc.group(1))
            if cur is not None:
                cur.append(current)
    if cur is not None:
        layer_seqs.append(cur)

    layer_colors = [set(seq) for seq in layer_seqs]
    n_layers = len(layer_colors)
    if n_layers == 0:
        return {'feasible': False, 'num_layers': 0, 'max_per_layer': 0,
                'layer_swaps': None, 'initial_loadout': None,
                'histogram': {}}

    max_per_layer = max(len(s) for s in layer_colors)
    histogram = {}
    for s in layer_colors:
        histogram[len(s)] = histogram.get(len(s), 0) + 1

    if max_per_layer > 4:
        return {'feasible': False, 'num_layers': n_layers,
                'max_per_layer': max_per_layer, 'layer_swaps': None,
                'initial_loadout': None, 'histogram': histogram,
                'reason': 'too_many_colors',
                'reason_detail': '>4 distinct colors in some layer',
                'layer_color_sets': [sorted(s) for s in layer_colors]}

    head_conflict_layers = []
    for li, layer_set in enumerate(layer_colors):
        per_head = {}
        for c in layer_set:
            per_head.setdefault(c % 4, []).append(c)
        conflicts = {h: cs for h, cs in per_head.items() if len(cs) > 1}
        if conflicts:
            head_conflict_layers.append((li, conflicts))
    if head_conflict_layers:

        examples = []
        for li, conflicts in head_conflict_layers[:3]:
            parts = ['head %d: %s' % (
                h, ', '.join('T%d' % c for c in sorted(cs)))
                for h, cs in sorted(conflicts.items())]
            examples.append('layer %d (%s)' % (li, '; '.join(parts)))
        more = (' +%d more' % (len(head_conflict_layers) - 3)
                if len(head_conflict_layers) > 3 else '')

        suggestion = _suggest_layer_friendly_remap(
            layer_colors, num_aces)
        return {'feasible': False, 'num_layers': n_layers,
                'max_per_layer': max_per_layer, 'layer_swaps': None,
                'initial_loadout': None, 'histogram': histogram,
                'reason': 'head_conflict',
                'reason_detail': 'same-head conflict in %d layer(s): %s%s' % (
                    len(head_conflict_layers), '; '.join(examples), more),
                'suggestion': suggestion,
                'layer_color_sets': [sorted(s) for s in layer_colors]}

    def next_use(col, since):
        for j in range(since, n_layers):
            if col in layer_colors[j]:
                return j
        return 1 << 30

    all_colors = sorted({c for s in layer_colors for c in s})

    def simulate(fixed_initial):
        """Strict-c%4 simulator. Each color c lives on head c%4 (its
        physical destination - ACE c//4 / Slot c%4 feeds head c%4).
        No free choice of head: when a layer needs c and head c%4 is
        occupied by another color c', evict c' and load c. If c' is
        also needed in the same layer (= layer uses two colors with
        the same %4), the print is infeasible at layer granularity
        (would need a mid-layer swap, which our caller filters out
        via max_per_layer check).

        Each color is loaded into its slicer-canonical ACE position
        (c // 4). Feasibility: c // 4 must be < num_aces. Distinct
        colors per head (= aces_needed) is the count that matters,
        not the total number of swaps - the same two colors can
        cycle on a head infinitely with only 2 ACE slots.

        Returns (swaps, aces_needed, events, color_slots,
        materialized_initial_loadout) or None if infeasible.
        """
        cache = [None, None, None, None]
        init_loadout = {}
        for c, h in fixed_initial.items():
            if h != c % 4:
                return None
            if cache[h] is not None:
                return None
            if c // 4 >= num_aces:
                return None
            cache[h] = c
            init_loadout[c] = h

        head_distinct_colors = [set(), set(), set(), set()]
        for c in init_loadout:
            head_distinct_colors[c % 4].add(c)

        events = []
        color_slots = {c: [(0, h, c // 4)] for c, h in init_loadout.items()}
        swaps = 0

        for i, needed in enumerate(layer_colors):
            loaded = set(c for c in cache if c is not None)
            for c in sorted(needed - loaded):
                h = c % 4
                if cache[h] is None:

                    if c // 4 >= num_aces:
                        return None
                    cache[h] = c
                    init_loadout[c] = h
                    head_distinct_colors[h].add(c)
                    color_slots.setdefault(c, []).append((i, h, c // 4))
                    continue
                if cache[h] in needed:

                    return None

                if c // 4 >= num_aces:
                    return None

                evicted = cache[h]
                cache[h] = c
                head_distinct_colors[h].add(c)
                events.append((i, c, evicted, h))
                color_slots.setdefault(c, []).append((i, h, c // 4))
                swaps += 1
                loaded = set(c for c in cache if c is not None)

        aces_needed = max(len(s) for s in head_distinct_colors)
        return (swaps, aces_needed, events, color_slots, init_loadout)

    fixed_initial = {}
    used_heads = set()
    seen = set()
    for layer_set in layer_colors:
        if len(used_heads) == 4:
            break
        for c in sorted(layer_set):
            if c in seen:
                continue
            seen.add(c)
            h = c % 4
            if h in used_heads:
                continue
            fixed_initial[c] = h
            used_heads.add(h)
            if len(used_heads) == 4:
                break

    best = simulate(fixed_initial)
    if best is None:

        best = simulate({})
    if best is None:
        return {'feasible': False, 'num_layers': n_layers,
                'max_per_layer': max_per_layer, 'layer_swaps': None,
                'initial_loadout': None, 'histogram': histogram}

    swaps, aces_needed, events, color_slots, initial_loadout = best

    return {'feasible': True, 'num_layers': n_layers,
            'max_per_layer': max_per_layer, 'layer_swaps': swaps,
            'initial_loadout': initial_loadout, 'events': events,
            'color_slots': color_slots, 'aces_needed': aces_needed,
            'histogram': histogram,
            'layer_color_sets': [sorted(s) for s in layer_colors]}

def compute_optimal_remap(result):
    """Return ({old_T: new_T}, best_swaps) that minimizes mid-print swaps,
    or (None, None) if no improvement is possible over the slicer's
    layout. Mirrors the optimizer loop used for printing recommendations,
    then converts the chosen primary/extra assignments into concrete
    T-index targets (primaries go to T0..T3, extras to T<head + 4*ace>).
    """
    from itertools import combinations
    counts = result['counts']
    colors = sorted(counts.keys())
    if len(colors) <= 4:
        return None, None

    best_swaps = sum(counts.values()) + 1
    best_primaries = None
    for primaries in combinations(colors, 4):
        primary_set = set(primaries)
        head_for_color = {c: i for i, c in enumerate(primaries)}
        head_extra_count = [0] * 4
        for c in sorted((c for c in colors if c not in primary_set),
                        key=lambda x: -counts[x]):
            h = min(range(4), key=lambda h: head_extra_count[h])
            head_for_color[c] = h
            head_extra_count[h] += 1
        head_loaded = {}
        sim_swaps = 0
        for t in result.get('events', []):
            if t not in head_for_color:
                continue
            h = head_for_color[t]
            if head_loaded.get(h) is None:
                head_loaded[h] = t
            elif head_loaded[h] != t:
                sim_swaps += 1
                head_loaded[h] = t
        if sim_swaps < best_swaps:
            best_swaps = sim_swaps
            best_primaries = primaries

    if best_primaries is None or best_swaps >= result['swaps']:
        return None, None

    primary_set = set(best_primaries)
    remap = {c: i for i, c in enumerate(best_primaries)}
    head_extra_count = [0] * 4
    for c in sorted((c for c in colors if c not in primary_set),
                    key=lambda x: -counts[x]):
        h = min(range(4), key=lambda h: head_extra_count[h])
        head_extra_count[h] += 1
        remap[c] = h + 4 * head_extra_count[h]

    if all(k == v for k, v in remap.items()):
        return None, None
    return remap, best_swaps

def apply_remap(gcode, remap):
    """Rewrite every T-index reference in the gcode according to the
    permutation `remap` ({old_T: new_T}). Touches bare T<n> lines,
    M104/M109 T<n> heater commands and SM_PRINT_PREEXTRUDE_FILAMENT
    INDEX=<n>. The `; Change Tool<a> -> Tool<b>` comments are left
    untouched so they remain the canonical source of the original
    slicer tool indices - this keeps the analyzer/optimizer idempotent
    across repeated runs on the same file. The downstream rewrite()
    logic only uses those comments as split markers and doesn't care
    about the numbers.
    """
    if not remap:
        return gcode

    def rm(n):
        return remap.get(int(n), int(n))

    def _bare_t(m):
        return 'T%d' % rm(m.group(1))

    def _m104_m109(m):
        return re.sub(r'T(\d+)',
                      lambda t: 'T%d' % rm(t.group(1)),
                      m.group(0))

    def _preextrude(m):
        return 'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d' % rm(m.group(1))

    gcode = re.sub(r'^T(\d{1,2})\s*$', _bare_t,
                   gcode, flags=re.MULTILINE)
    gcode = re.sub(r'^M10[49][^\n]*', _m104_m109,
                   gcode, flags=re.MULTILINE)
    gcode = re.sub(r'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=(\d+)',
                   _preextrude, gcode)
    return gcode

def apply_layer_remap(gcode, layer_info):
    """Rewrite T-references so the print uses layer-boundary-only swaps.

    Strategy: walk the gcode, tracking the current layer index via
    ;LAYER_CHANGE markers. For each `; Change Tool X -> Tool Y` we look
    up Tool Y's current (head, ace) slot from the Belady schedule and
    rewrite the bare T<Y> (and any following M104/M109 T<Y>) inside
    that toolchange block to T<head + 4*ace>. The downstream rewrite()
    step then emits ACE_SWAP_HEAD with HEAD=head SLOT=head ACE=ace, and
    its built-in skip logic marks the ~115 non-swap toolchanges as
    `; skipped (already loaded)` - leaving only the Belady-optimal
    swaps as real filament changes.

    Returns (rewritten_gcode, physical_loadout) where physical_loadout
    is a dict (ace, slot) -> original T index, so we can print the
    physical cartridge plan for the user.
    """
    if not layer_info or not layer_info.get('feasible'):
        return gcode, None

    split_re = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                          re.MULTILINE)
    m = split_re.search(gcode)
    if m is None:
        return gcode, None
    pre, body = gcode[:m.start()], gcode[m.start():]

    initial = layer_info['initial_loadout']
    events = layer_info['events']

    current_slot = {c: (h, c // 4) for c, h in initial.items()}

    events_by_layer = {}
    head_ace_counter = [0, 0, 0, 0]
    for i, c_in, c_out, h in events:
        events_by_layer.setdefault(i, []).append((c_in, c_out, h))

    loadout = {}
    for c, h in initial.items():
        loadout[(0, h)] = c

    ace_counter_pre = [0, 0, 0, 0]

    for (i, c_in, c_out, h) in events:
        ace_counter_pre[h] += 1
        loadout[(ace_counter_pre[h], h)] = c_in

    body_lines = body.splitlines()
    out = []
    layer_idx = 0

    pending_target = None

    change_re = re.compile(
        r'^(;\s*Change Tool\s*\d+\s*->\s*Tool\s*)(\d+)(.*)$')
    bare_re = re.compile(r'^T(\d{1,2})\s*$')
    m104_re = re.compile(r'^(M10[49]\b.*)$')

    def advance_to_layer(new_idx):
        for ll in range(layer_idx + 1, new_idx + 1):
            for c_in, c_out, h in events_by_layer.get(ll, []):
                head_ace_counter[h] += 1
                ace = head_ace_counter[h]
                current_slot[c_in] = (h, ace)

    for line in body_lines:
        s = line.strip()
        if s.startswith(';LAYER_CHANGE'):

            advance_to_layer(layer_idx + 1)
            layer_idx += 1
            out.append(line)
            continue

        mc = change_re.match(s)
        if mc:
            orig_y = int(mc.group(2))
            pending_target = orig_y

            out.append(line)
            continue

        mb = bare_re.match(s)
        if mb and pending_target is not None:

            h, ace = current_slot.get(pending_target,
                                      (pending_target % 4,
                                       pending_target // 4))
            out.append('T%d' % (h + 4 * ace))
            pending_target = None
            continue

        mh = m104_re.match(s)
        if mh:
            def _repl(mm, pt=pending_target):
                n = int(mm.group(1))

                if pt is not None and n == pt:
                    h, ace = current_slot.get(pt,
                                              (pt % 4, pt // 4))
                    return 'T%d' % (h + 4 * ace)
                return mm.group(0)
            out.append(re.sub(r'T(\d{1,2})', _repl, line))
            continue

        out.append(line)

    return pre + '\n'.join(out), loadout

def print_recommendation(result, num_aces, file=None):
    from itertools import combinations

    def p(*args):
        if file is not None:
            print(*args, file=file)
        else:
            print(*args)

    counts = result['counts']
    colors = sorted(counts.keys())
    n_colors = len(colors)
    max_slots = num_aces * 4
    color_names = result.get('color_names', {})

    p('=' * 60)
    p('multiACE plan')
    p('=' * 60)
    p('Colors: %d   Toolchanges: %d   Mid-print swaps: %d (~%.1f min)' % (
        n_colors, result['total_changes'], result['swaps'],
        result['swaps'] * 3.8))

    overflow = [c for c, info in result['plan'].items() if info.get('role') == 'OVERFLOW']
    if overflow:
        p()
        p('!! WARNING: %d color(s) exceed ACE capacity (%d slots, %d ACEs)' % (
            n_colors, max_slots, num_aces))
        p('!! Exceeding colors will NOT be printed.')

    p()
    p('Slicer Loadout:')
    for c in colors:
        info = result['plan'].get(c, {})
        ace = info.get('ace', c // 4)
        slot = info.get('slot', c % 4)
        role = info.get('role', '')
        p('  ACE %d Slot %d  T%-2d  %s  (%dx%s)' % (
            ace, slot, c, format_color(c, color_names),
            counts[c], '' if role != 'OVERFLOW' else ' OVERFLOW'))

    if n_colors > 4:
        best_swaps = sum(counts.values())
        best_primaries = None

        for primaries in combinations(colors, min(4, n_colors)):

            head_color = {}
            primary_set = set(primaries)
            head_for_color = {}
            for i, c in enumerate(primaries):
                head_for_color[c] = i

            non_primaries = [c for c in colors if c not in primary_set]
            primary_by_head = {i: primaries[i] for i in range(len(primaries))}

            head_extra_count = [0] * 4
            for c in sorted(non_primaries, key=lambda x: -counts[x]):
                h = min(range(4), key=lambda h: head_extra_count[h])
                head_for_color[c] = h
                head_extra_count[h] += 1

            head_loaded = {}
            sim_swaps = 0
            for t in result.get('events', []):
                if t not in head_for_color:
                    continue
                h = head_for_color[t]
                if head_loaded.get(h) is None:
                    head_loaded[h] = t
                elif head_loaded[h] != t:
                    sim_swaps += 1
                    head_loaded[h] = t

            if sim_swaps < best_swaps:
                best_swaps = sim_swaps
                best_primaries = primaries

        if best_primaries is not None:
            p()
            savings = result['swaps'] - best_swaps
            if savings > 0:
                p('--- OPTIMIZER: %d swaps possible (%d fewer, %.0f%% less) ---' % (
                    best_swaps, savings,
                    savings / result['swaps'] * 100 if result['swaps'] > 0 else 0))

                primary_set = set(best_primaries)
                head_for_color = {c: i for i, c in enumerate(best_primaries)}
                head_extra_count = [0] * 4
                non_p = [c for c in colors if c not in primary_set]

                extras_order = sorted(non_p, key=lambda x: -counts[x])
                extra_ace_of_color = {}
                for c in extras_order:
                    h = min(range(4), key=lambda h: head_extra_count[h])
                    head_for_color[c] = h
                    head_extra_count[h] += 1
                    extra_ace_of_color[c] = head_extra_count[h]
                p('Optimized Print Loadout:')

                rows = []
                for c in best_primaries:
                    rows.append((0, head_for_color[c], c, 'primary'))
                for c in extras_order:
                    rows.append((extra_ace_of_color[c], head_for_color[c], c, 'swap'))
                for ace, slot, c, role in sorted(rows):
                    p('  ACE %d Slot %d  T%-2d  %s  (%s, %dx)' % (
                        ace, slot, c, format_color(c, color_names),
                        role, counts[c]))
            else:
                p('--- OPTIMIZER: current assignment is already optimal ---')

    layer_info = result.get('layer_info')
    if layer_info:
        p()
        p('Layer-only swap analysis:')
        p('  Layers: %d   Max colors/layer: %d' % (
            layer_info['num_layers'], layer_info['max_per_layer']))
        if layer_info['feasible']:
            aces_needed = layer_info.get('aces_needed', 0)
            fits = aces_needed <= num_aces
            p('  Feasible: YES  Minimum layer-only swaps: %d (~%.1f min)' % (
                layer_info['layer_swaps'],
                layer_info['layer_swaps'] * 3.8))
            if fits:
                p('  ACEs needed: %d (you have %d - fits)' % (
                    aces_needed, num_aces))
            else:
                p('  ACEs needed: %d (you have %d - DOES NOT FIT, --layer will be skipped)' % (
                    aces_needed, num_aces))
            preload = layer_info.get('initial_loadout') or {}
            if preload:

                p('  Pre-load these colors before print:')
                for c, h in sorted(preload.items(), key=lambda kv: kv[1]):
                    p('    ACE %d Slot %d  T%-2d  %s' % (
                        c // 4, c % 4, c, format_color(c, color_names)))
            events = layer_info.get('events') or []
            if events:

                p('  Additional swap cartridges:')
                seen = set(preload.keys())
                for _lyr, c_in, _c_out, h in events:
                    if c_in in seen:
                        continue
                    seen.add(c_in)
                    p('    ACE %d Slot %d  T%-2d  %s' % (
                        c_in // 4, c_in % 4, c_in,
                        format_color(c_in, color_names)))
        else:
            reason = layer_info.get('reason')
            detail = layer_info.get('reason_detail', '')
            if reason == 'too_many_colors':
                p('  Feasible: NO  (%s - needs mid-layer swaps)' % detail)
            elif reason == 'head_conflict':
                p('  Feasible: NO  (%s)' % detail)
                p('    Each head N can only hold one color at a time;')
                p('    colors with the same N (where N = T%%4) compete:')
                p('    head 0: T0, T4, T8, T12   head 1: T1, T5, T9, T13')
                p('    head 2: T2, T6, T10, T14  head 3: T3, T7, T11, T15')
                suggestion = layer_info.get('suggestion')
                if suggestion:
                    moves = [(old, new) for old, new in sorted(suggestion.items())
                             if old != new]
                    p('')
                    p('  Suggested rearrangement (minimal moves to enable layer mode):')
                    for old, new in moves:
                        old_ace, old_slot = old // 4, old % 4
                        new_ace, new_slot = new // 4, new % 4
                        p('    T%-2d  %s   ACE %d Slot %d  →  ACE %d Slot %d  (T%d)' % (
                            old, format_color(old, color_names),
                            old_ace, old_slot,
                            new_ace, new_slot, new))
                    p('    %d color(s) need to move; reslice with the new T-indices' % len(moves))
                    p('    or physically swap cartridges to the suggested ACE/slot.')
                else:
                    p('')
                    p('  No conflict-free remap found within %d ACE budget.' % num_aces)
                    p('  Either reduce the number of colors or increase --aces.')
            else:
                p('  Feasible: NO')

    p('=' * 60)

_E_VAL_RE = re.compile(r'\bE(-?\d*\.?\d+)')

def _is_extruding_move(line):
    """True for a G0/G1 move that advances the extruder (positive E). The
    language-independent boundary for the auto-load: the heads must be loaded
    before ANY filament is extruded (the prime/draw-line). Retracts (E<0) and
    non-move lines are ignored."""
    s = line.strip()
    if not (s.startswith('G1') or s.startswith('G0')):
        return False
    m = _E_VAL_RE.search(s)
    if not m:
        return False
    try:
        return float(m.group(1)) > 0
    except ValueError:
        return False

def _structural_inject_idx(lines):
    """Section boundary right before the first extruding move - the geometry-
    safe, slicer/locale-independent auto-load anchor. We inject at the boundary
    (nearest preceding blank line or ;===== section header) rather than right at
    the extrusion so the prime's own positioning move still runs AFTER the
    auto-load. Returns an index or None (no extrusion found / no boundary)."""
    ext_idx = None
    for idx, line in enumerate(lines):
        if _is_extruding_move(line):
            ext_idx = idx
            break
    if ext_idx is None:
        return None
    for j in range(ext_idx - 1, max(-1, ext_idx - 200), -1):
        s = lines[j].strip()
        if s == '' or (s.startswith(';') and '=====' in s):
            return j
    return ext_idx

def inject_auto_load(gcode):
    """Insert ACE_SWAP_HEAD calls for each used head AT the safest point
    that is past G28 + heating but before the first move that needs the
    initial tool's filament.

    Use case: replace the manual preload step before a multi-color
    print. The slicer's start gcode emits heating + G28 + bed leveling
    (and a bare T<initial_extruder> command for heater selection that
    can come BEFORE G28 - that's why we don't inject before the first
    T).

    Injection-point fallback chain (highest priority first):

      1. Right BEFORE the first SM_PRINT_PREEXTRUDE_FILAMENT line.
         This is Snapmaker's stock prime move - it lives AFTER G28 +
         M109 in the slicer's start gcode and BEFORE the first body
         move. It also extrudes from the initial tool, so the initial
         tool's filament must be loaded by then or the runout sensor
         triggers an id=523 pause (observed 2026-04-26 14:56). This is
         the safest anchor for prints that use a single tool or whose
         initial tool is never targeted by a `; Change Tool` marker.

      2. Right BEFORE the first '; Change Tool X -> Tool Y' marker
         (Orca multi-tool prints). This anchor is the boundary between
         start_gcode and the print body - but it is AFTER any prior
         SM_PRINT_PREEXTRUDE_FILAMENT, which is why it is fallback 2,
         not 1.

      3. Right BEFORE the first ACE_SWAP_HEAD HEAD= line. Catches
         single-color prints where rewrite() generated swaps.

    cmd_ACE_SWAP_HEAD's empty-head detection (ace.py) makes this work
    for fresh / unloaded heads - the unload phase is skipped when the
    sensor reports no filament and head_source is None, so the swap
    reduces to a pure load. Already-loaded heads with the correct
    (ACE, slot) hit the 'already on' short-circuit (no-op). Mismatched
    loaded heads get unloaded + reloaded.

    Initial mapping per head is discovered from the first ACE_SWAP_HEAD
    line for that head. Heads that appear only as bare T<n> get the
    default mapping (ACE 0, slot=head) - that's the state the rewrite
    assumes for the initial loadout.

    Returns (gcode_with_injection, count_of_heads_loaded).
    """
    lines = gcode.split('\n')

    cleaned = []
    in_block = False
    for ln in lines:
        ls = ln.strip()
        if ls.startswith('; multiACE auto-load: load'):
            in_block = True
            continue
        if in_block:
            if ls.startswith('; multiACE auto-load: end'):
                in_block = False
            continue
        cleaned.append(ln)
    lines = cleaned

    inject_idx = _structural_inject_idx(lines)

    if inject_idx is None:
        for idx, line in enumerate(lines):
            if '画起始线' in line or 'draw the starting line' in line.lower():
                inject_idx = idx
                break

    if inject_idx is None:
        for idx, line in enumerate(lines):
            if re.match(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+',
                        line.strip()):
                inject_idx = idx
                break

    if inject_idx is None:
        for idx, line in enumerate(lines):
            if 'SM_PRINT_PREEXTRUDE_FILAMENT' in line:
                inject_idx = idx
                break

    if inject_idx is None:
        for idx, line in enumerate(lines):
            if line.strip().startswith('ACE_SWAP_HEAD HEAD='):
                inject_idx = idx
                break
    initial = {}
    used_heads = set()

    body_start = inject_idx if inject_idx is not None else 0

    _in_old_block = False
    for i in range(0, body_start):
        ls_pre = lines[i].strip()
        if _in_old_block:
            if ls_pre.startswith('; multiACE auto-load: end'):
                _in_old_block = False
            continue
        if ls_pre.startswith('; multiACE auto-load: load'):
            _in_old_block = True
            continue
        m_pre = re.match(
            r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)'
            r'(?:\s+\S+=\S+)*\s*$', ls_pre)
        if m_pre:
            initial[int(m_pre.group(1))] = (int(m_pre.group(2)),
                                            int(m_pre.group(3)))
    for i in range(body_start, len(lines)):
        line = lines[i]
        ls = line.strip()
        m_t = re.match(r'^T([0-3])\s*$', ls)
        if m_t:
            head = int(m_t.group(1))
            used_heads.add(head)
            if head not in initial:

                j = i + 1

                while j < len(lines):
                    sj = lines[j].strip()
                    if (sj == '' or sj.startswith(';')
                            or sj.startswith('ACE_BG_')
                            or sj.startswith('ACE_SET_PURGE')):
                        j += 1
                        continue
                    break
                ace_m = None
                if j < len(lines):
                    ace_m = re.match(
                        r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)'
                        r'(?:\s+\S+=\S+)*\s*$',
                        lines[j].strip())
                if ace_m and int(ace_m.group(1)) == head:
                    initial[head] = (int(ace_m.group(2)), int(ace_m.group(3)))
                else:
                    initial[head] = (0, head)
            continue
        m = re.match(
            r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)(?:\s+\S+=\S+)*\s*$',
            ls)
        if m:
            head = int(m.group(1))
            used_heads.add(head)
            if head not in initial:
                initial[head] = (int(m.group(2)), int(m.group(3)))

    for head in used_heads:
        if head not in initial:
            initial[head] = (0, head)
    if inject_idx is None or not initial:
        return gcode, 0
    inject = ['', '; multiACE auto-load: load initial filaments']
    for head in sorted(initial):
        ace, slot = initial[head]

        inject.append('ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d INITIAL=1'
                      % (head, ace, slot))
    inject.append('; multiACE auto-load: end')
    inject.append('')
    new_lines = lines[:inject_idx] + inject + lines[inject_idx:]
    return '\n'.join(new_lines), len(initial)

def rewrite_for_mode(gcode, result, mode='optimize', num_aces=None):
    """Server-side entry point: apply the optimize or layer remap to
    an already-live-lookup-rewritten gcode body. `result` is what
    plan_loadout() returned for that body.

    Returns the rewritten gcode (string). Raises ValueError for
    invalid mode or when layer mode isn't feasible for this gcode."""
    if mode not in ('slicer', 'optimize', 'layer'):
        raise ValueError('mode must be slicer/optimize/layer')
    if mode == 'slicer':
        return gcode
    if mode == 'layer':
        layer_info = (result or {}).get('layer_info') or {}
        if not layer_info.get('feasible'):
            raise ValueError('layer mode not feasible for this gcode')
        if num_aces is not None and layer_info.get('aces_needed', 0) > num_aces:
            raise ValueError('layer mode needs %d ACEs, have %d' % (
                layer_info['aces_needed'], num_aces))
        new_gcode, _loadout = apply_layer_remap(gcode, layer_info)
        return new_gcode

    remap, _opt_swaps = compute_optimal_remap(result)
    if remap:
        return apply_remap(gcode, remap)
    return gcode

def _emit_progress(progress, seen, total, last_pr):
    """Helper: invoke `progress` if at least 1 MB has elapsed since
    `last_pr`. Returns the new last_pr value."""
    if progress is None or seen < last_pr + (1 << 20):
        return last_pr
    try:
        progress(seen, total)
    except Exception:
        pass
    return seen

def plan_loadout_from_file(in_path, num_aces=3, progress=None):
    """Memory-efficient wrapper around plan_loadout(). Streams the file
    line-by-line and keeps ONLY the lines plan_loadout actually parses:
    `; Change Tool X -> Tool Y` markers, `;LAYER_CHANGE` markers, the
    `; filament_colour = ...` / `; filament_type = ...` header lines,
    and bare `T<n>` commands. For a typical multi-color gcode this
    proxy is well under 1 % of the source size, so plan_loadout's
    in-memory analysis runs on hundreds of KB instead of tens of MB.

    Returns the same dict shape as plan_loadout() - None when no
    body toolchanges are found."""
    keep_re = re.compile(
        r'^(;\s*Change Tool|;\s*LAYER_CHANGE|;\s*filament\b|T\d{1,2}\s*$)',
        re.IGNORECASE)
    parts = []
    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0
    with open(in_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            seen += len(line.encode('utf-8', errors='ignore'))
            if keep_re.match(line):
                parts.append(line.rstrip('\n'))
            last_pr = _emit_progress(progress, seen, total, last_pr)
    proxy = '\n'.join(parts)
    del parts
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return plan_loadout(proxy, num_aces=num_aces)

def apply_remap_to_file(in_path, out_path, remap, progress=None):
    """Streaming equivalent of apply_remap(gcode, remap). When remap
    is empty the input is just copied unchanged."""
    import shutil
    if not remap:
        shutil.copyfile(in_path, out_path)
        if progress is not None:
            try:
                size = os.path.getsize(in_path)
                progress(size, size)
            except Exception:
                pass
        return

    def rm(s):
        try:
            return remap.get(int(s), int(s))
        except (TypeError, ValueError):
            return s

    bare_t_re      = re.compile(r'^T(\d{1,2})\s*$')
    m104_re        = re.compile(r'^M10[49]\b')
    pre_extrude_re = re.compile(r'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=(\d+)')

    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin,\
         open(out_path, 'w', encoding='utf-8') as fout:
        for line in fin:
            seen += len(line.encode('utf-8', errors='ignore'))
            stripped = line.rstrip('\r\n')
            m = bare_t_re.match(stripped)
            if m:
                fout.write('T%d\n' % rm(m.group(1)))
            elif m104_re.match(stripped):
                fout.write(re.sub(
                    r'T(\d+)', lambda t: 'T%d' % rm(t.group(1)), line))
            else:
                fout.write(pre_extrude_re.sub(
                    lambda mm: 'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d' % rm(mm.group(1)),
                    line))
            last_pr = _emit_progress(progress, seen, total, last_pr)
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass

def rewrite_to_file(in_path, out_path, progress=None, pickup_cleaning=False):
    """Streaming equivalent of rewrite(gcode). Same M104/M109 +
    SM_PRINT_PREEXTRUDE_FILAMENT handling, same body T4-T15 expansion
    + ACE_SWAP_HEAD dedupe + swap-back insertion. Returns
    (active_swaps, skipped_swaps, swapback_count) matching the
    in-memory version's contract."""
    m104_re   = re.compile(r'^M10[49]\b')
    drop_pre  = re.compile(r'^SM_PRINT_PREEXTRUDE_FILAMENT INDEX=([4-9]|1[0-5])\b')
    low_pre   = re.compile(r'^SM_PRINT_PREEXTRUDE_FILAMENT INDEX=([0-3])\b')
    change_t  = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+')

    change_to = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')
    bare_hi   = re.compile(r'^T([4-9]|1[0-5])\s*$')
    bare_lo   = re.compile(r'^T([0-3])\s*$')
    swap_re   = re.compile(
        r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)(?:\s+\S+=\S+)*\s*$')

    def fix_m104(line):
        return re.sub(r'T([4-9]|1[0-5])',
                      lambda t: 'T' + str(int(t.group(1)) % 4),
                      line)

    in_body = False
    head_loaded = {0: (0, 0), 1: (0, 1), 2: (0, 2), 3: (0, 3)}

    head_slicer = {}

    flush_matrix = (parse_flush_matrix_raw_from_file(in_path)
                    if PURGE_MATRIX_ENABLE else None)
    active = 0
    skipped = 0
    swapbacks = 0

    cooling_standbys = scan_cooling_standbys(in_path, lambda n: n % 4)

    primed_first = set()

    post_t_unret = _scan_post_t_unretracts(in_path)

    def _ao_for(t_line_no):
        v = post_t_unret.get(t_line_no)
        return _fmt_anti_ooze(v if v is not None else ANTI_OOZE_NO_UNRETRACT)

    pending_head = None
    pending_line_no = None
    pending_blanks: list[str] = []

    pending_slicer = None
    pending_slicer_tool = None

    def _purge_pair(head, arriving_virtual, arriving_slicer):
        """(t_from, t_to) for the flush matrix, as SLICER filament indices.

        With '; Change Tool' data present those indices are authoritative,
        so they are used even when the head's previous filament is not known
        yet (first arrival) - _matrix_purge_mm then returns None and the swap
        goes out unstamped, i.e. on the engine default. That is deliberate:
        the default is the value with field history behind it, so an unknown
        pair falls back to what has always been printed rather than to a
        guess from the wrong row."""
        if arriving_slicer is not None:
            return head_slicer.get(head), arriving_slicer

        prev = head_loaded.get(head)
        return ((prev[0] * 4 + head) if prev else None), arriving_virtual

    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0

    def flush_pending_unmatched(fout):
        """Pending bare T<n> wasn't followed by ACE_SWAP_HEAD - emit
        it as a swap-back if the head currently holds a non-initial
        color. Swap-backs also count toward `active` because the
        in-memory rewrite() counts every ACE_SWAP_HEAD line in the
        output, regardless of provenance."""
        nonlocal pending_head, pending_line_no, swapbacks, active
        nonlocal pending_slicer_tool
        if pending_head is None:
            return
        head = pending_head
        t_no = pending_line_no
        arriving = pending_slicer_tool
        pending_head = None
        pending_line_no = None
        pending_slicer_tool = None
        initial_key = (0, head)
        fout.write('T%d\n' % head)
        for b in pending_blanks:
            fout.write(b)
        pending_blanks.clear()

        _pf, _pt = _purge_pair(head, head, arriving)
        if arriving is not None:
            head_slicer[head] = arriving
        if head_loaded.get(head) != initial_key:
            _pp = _matrix_purge_mm(flush_matrix, _pf, _pt)
            if _pp is not None:
                fout.write('ACE_SET_PURGE LENGTH=%d\n' % _pp)
            fout.write('ACE_SWAP_HEAD HEAD=%d ACE=0 SLOT=%d ANTI_OOZE=%s\n'
                       % (head, head, _ao_for(t_no)))
            swapbacks += 1
            active += 1
            head_loaded[head] = initial_key
        elif pickup_cleaning:

            fout.write('ACE_PICKUP_CLEAN HEAD=%d\n' % head)

    def flush_pending_paired(fout):
        """Pending bare T was followed by ACE_SWAP_HEAD - emit it
        and let the swap handler update head_loaded."""
        nonlocal pending_head, pending_line_no, pending_slicer_tool
        if pending_head is None:
            return
        head = pending_head
        if pending_slicer_tool is not None:
            head_slicer[head] = pending_slicer_tool
        pending_head = None
        pending_line_no = None
        pending_slicer_tool = None
        fout.write('T%d\n' % head)
        for b in pending_blanks:
            fout.write(b)
        pending_blanks.clear()

    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin,\
         open(out_path, 'w', encoding='utf-8') as fout:
        for _raw_no, line in enumerate(fin):
            seen += len(line.encode('utf-8', errors='ignore'))
            stripped = line.rstrip('\r\n')

            if m104_re.match(stripped):
                if pending_head is not None:
                    flush_pending_unmatched(fout)
                if _raw_no in cooling_standbys:
                    fout.write('; multiACE dropped: %s  ; would cool the '
                               'printing head below %d C\n'
                               % (stripped.strip(), STANDBY_MIN_EXTRUDE_C))
                else:
                    fout.write(fix_m104(line))
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mdp = drop_pre.match(stripped)
            if mdp:
                if pending_head is not None:
                    flush_pending_unmatched(fout)

                hpre = int(mdp.group(1)) % 4
                if hpre not in primed_first:
                    primed_first.add(hpre)
                    fout.write(
                        'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d FORCE=1\n' % hpre)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            if not in_body and change_t.match(stripped):
                in_body = True

            _mct = change_to.match(stripped)
            if _mct:
                pending_slicer = int(_mct.group(1))

            if not in_body:
                if pending_head is not None:
                    flush_pending_unmatched(fout)
                m = bare_hi.match(stripped)
                if m:
                    fout.write('T%d\n' % (int(m.group(1)) % 4))
                else:
                    fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            if pending_head is not None:
                if not stripped.strip():

                    pending_blanks.append(line)
                    last_pr = _emit_progress(progress, seen, total, last_pr)
                    continue
                if stripped.startswith('ACE_SWAP_HEAD'):
                    flush_pending_paired(fout)

                else:
                    flush_pending_unmatched(fout)

            mlp = low_pre.match(stripped)
            if mlp:
                h = int(mlp.group(1))
                if h not in primed_first:
                    primed_first.add(h)
                    fout.write(
                        'SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d FORCE=1\n' % h)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            m = swap_re.match(stripped)
            if m:
                head = int(m.group(1)); ace = int(m.group(2)); slot = int(m.group(3))
                key = (ace, slot)
                if head_loaded.get(head) == key:
                    fout.write('; ' + stripped + '  ; skipped (already loaded)\n')
                    skipped += 1
                else:
                    head_loaded[head] = key
                    fout.write(line if line.endswith('\n') else (line + '\n'))
                    active += 1
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            m = bare_hi.match(stripped)
            if m:
                n = int(m.group(1))
                head = n % 4
                ace = n // 4
                fout.write('T%d\n' % head)
                key = (ace, head)

                _pf, _pt = _purge_pair(head, n, pending_slicer)
                if pending_slicer is not None:
                    head_slicer[head] = pending_slicer
                if head_loaded.get(head) == key:
                    fout.write('; ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d  ; skipped (already loaded)\n' % (
                        head, ace, head))
                    skipped += 1
                else:
                    _pp = _matrix_purge_mm(flush_matrix, _pf, _pt)
                    if _pp is not None:
                        fout.write('ACE_SET_PURGE LENGTH=%d\n' % _pp)
                    fout.write('ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d '
                               'ANTI_OOZE=%s\n' % (
                        head, ace, head, _ao_for(_raw_no)))
                    head_loaded[head] = key
                    active += 1
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            m = bare_lo.match(stripped)
            if m:
                pending_head = int(m.group(1))
                pending_line_no = _raw_no

                pending_slicer_tool = pending_slicer
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            fout.write(line)
            last_pr = _emit_progress(progress, seen, total, last_pr)

        if pending_head is not None:
            flush_pending_unmatched(fout)

    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return active, skipped, swapbacks

def inject_auto_load_to_file(in_path, out_path, progress=None, only_heads=None,
                             bg_heads=None):
    """Streaming equivalent of inject_auto_load(gcode).

    Three passes:
      A. Find the injection anchor (highest priority across the four
         anchor types) and the byte ranges of any pre-existing auto-
         load block(s) to strip.
      B. Re-scan from the anchor onwards (skipping stripped ranges) to
         build initial[head] = (ace, slot) with the same logic as the
         in-memory inject_auto_load: bare T<head> peeks ahead for a
         paired ACE_SWAP_HEAD HEAD=<head>; if absent the head defaults
         to (0, head). Heads first seen as a direct ACE_SWAP_HEAD use
         that swap's (ace, slot).
      C. Write the output, injecting the auto-load block right before
         the anchor line and dropping any old-block lines.

    The previous single-pass implementation took the file's first
    ACE_SWAP_HEAD HEAD=X anywhere as initial[X]. That meant the bare
    T<head> at print start (no following swap) was ignored and
    initial[head] inherited from a much later mid-print swap - wrong
    cartridge auto-loaded for the print's initial tool.

    Anchor priority (must match the in-memory inject_auto_load):
      1. First line containing the Snapmaker prime-line section header
         ('画起始线') - anchors BEFORE the inline prime so the auto-
         load completes before the runout sensor fires on prime.
      2. First '; Change Tool X -> Tool Y' marker (Orca multi-tool).
      3. First SM_PRINT_PREEXTRUDE_FILAMENT line (fallback for
         single-tool prints without Change Tool markers).
      4. First ACE_SWAP_HEAD HEAD= line."""
    preextr_re = re.compile(r'^SM_PRINT_PREEXTRUDE_FILAMENT\b')
    chg_re     = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)')

    swap_re    = re.compile(
        r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)(?:\s+\S+=\S+)*\s*$')
    bare_t_re  = re.compile(r'^T([0-3])\s*$')
    auto_load_re = re.compile(r'^;\s*multiACE auto-load:\s')

    first_huaqi   = None
    first_chg     = None
    first_preextr = None
    first_swap    = None

    first_ext     = None
    ext_boundary  = None
    last_boundary = None

    in_old_block = False
    old_block_ranges: list[tuple[int, int]] = []
    block_start = None
    last_line_no = -1
    total = os.path.getsize(in_path) or 1

    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
        for line_no, line in enumerate(fin):
            last_line_no = line_no
            stripped = line.rstrip('\r\n')
            ls_strip = stripped.strip()

            if in_old_block:
                if ls_strip.startswith('; multiACE auto-load: end'):
                    old_block_ranges.append((block_start, line_no))
                    in_old_block = False
                    block_start = None
                continue
            if ls_strip.startswith('; multiACE auto-load: load'):
                in_old_block = True
                block_start = line_no
                continue

            if first_ext is None:
                if ls_strip == '' or (ls_strip.startswith(';') and '=====' in ls_strip):
                    last_boundary = line_no
                elif _is_extruding_move(stripped):
                    first_ext = line_no
                    ext_boundary = last_boundary

            if first_huaqi is None and ('画起始线' in stripped
                    or 'draw the starting line' in stripped.lower()):
                first_huaqi = line_no
            if first_chg is None and chg_re.match(stripped):
                first_chg = line_no
            if first_preextr is None and preextr_re.match(stripped):
                first_preextr = line_no
            if first_swap is None and stripped.startswith('ACE_SWAP_HEAD HEAD='):
                first_swap = line_no

    if in_old_block and block_start is not None:
        old_block_ranges.append((block_start, last_line_no))

    structural = ext_boundary if ext_boundary is not None else first_ext
    anchor_line_no = None
    for candidate in (structural, first_huaqi, first_chg, first_preextr, first_swap):
        if candidate is not None:
            anchor_line_no = candidate
            break

    in_block_set = set()
    for (a, b) in old_block_ranges:
        for j in range(a, b + 1):
            in_block_set.add(j)

    initial: dict[int, tuple[int, int]] = {}
    used_heads: set[int] = set()
    pending_t_head: int | None = None
    first_seen_head: int | None = None

    if anchor_line_no is not None:

        with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
            for line_no, line in enumerate(fin):
                if line_no >= anchor_line_no:
                    break
                if line_no in in_block_set:
                    continue
                m_s = swap_re.match(line.strip())
                if m_s:
                    initial[int(m_s.group(1))] = (int(m_s.group(2)),
                                                  int(m_s.group(3)))
        with open(in_path, 'r', encoding='utf-8', errors='replace') as fin:
            for line_no, line in enumerate(fin):
                if line_no < anchor_line_no:
                    continue
                if line_no in in_block_set:
                    continue
                stripped = line.strip()

                if pending_t_head is not None:

                    if (stripped == '' or stripped.startswith(';')
                            or stripped.startswith('ACE_BG_')
                            or stripped.startswith('ACE_SET_PURGE')):
                        continue
                    m_s = swap_re.match(stripped)
                    if m_s and int(m_s.group(1)) == pending_t_head:
                        if pending_t_head not in initial:
                            initial[pending_t_head] = (int(m_s.group(2)),
                                                       int(m_s.group(3)))
                        used_heads.add(pending_t_head)
                        pending_t_head = None
                        continue
                    if pending_t_head not in initial:
                        initial[pending_t_head] = (0, pending_t_head)
                    used_heads.add(pending_t_head)
                    pending_t_head = None

                m_t = bare_t_re.match(stripped)
                if m_t:
                    head = int(m_t.group(1))
                    used_heads.add(head)
                    if first_seen_head is None:
                        first_seen_head = head
                    if head not in initial:
                        pending_t_head = head
                    continue

                m_s = swap_re.match(stripped)
                if m_s:
                    head = int(m_s.group(1))
                    used_heads.add(head)
                    if first_seen_head is None:
                        first_seen_head = head
                    if head not in initial:
                        initial[head] = (int(m_s.group(2)), int(m_s.group(3)))

        if pending_t_head is not None and pending_t_head not in initial:
            initial[pending_t_head] = (0, pending_t_head)
            used_heads.add(pending_t_head)

    for head in used_heads:
        if head not in initial:
            initial[head] = (0, head)

    if only_heads is not None:
        initial = {h: v for h, v in initial.items() if h in only_heads}

    inject_block: list[str] = []
    if initial:
        inject_heads = sorted(initial.keys())
        inject_block.append('; multiACE auto-load: load %d head(s)\n' %
                            len(inject_heads))
        inject_block.append('; multiACE processed: format=%d\n'
                            % PP_FORMAT_VERSION)

        inject_block.append('ACE_SET_PURGE RESET=1\n')

        _bg_set = set(bg_heads or ())
        _use_bg = (BG_INITIAL_LOAD and only_heads is not None and bool(_bg_set))
        i = 0
        while i < len(inject_heads):
            h = inject_heads[i]
            a, s = initial[h]
            hb = inject_heads[i + 1] if i + 1 < len(inject_heads) else None
            if _use_bg and hb is not None and hb in _bg_set:
                ab, sb = initial[hb]
                inject_block.append(
                    'ACE_BG_SWAP HEAD=%d ACE=%d SLOT=%d ANTI_OOZE=%s QUIET=1\n'
                    % (hb, ab, sb, _fmt_anti_ooze(ANTI_OOZE_NO_UNRETRACT)))

                inject_block.append(
                    'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d INITIAL=1\n'
                    % (h, a, s))
                inject_block.append(
                    'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d INITIAL=1\n'
                    % (hb, ab, sb))
                i += 2
            else:
                inject_block.append(
                    'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d INITIAL=1\n'
                    % (h, a, s))
                i += 1

        if (only_heads is not None and first_seen_head is not None
                and first_seen_head in initial):
            inject_block.append('SM_PRINT_PREEXTRUDE_FILAMENT INDEX=%d '
                                'FORCE=1\n' % first_seen_head)
        inject_block.append('; multiACE auto-load: end\n')

    seen = 0
    last_pr = 0
    injected = False
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin,\
         open(out_path, 'w', encoding='utf-8') as fout:
        for line_no, line in enumerate(fin):
            seen += len(line.encode('utf-8', errors='ignore'))
            if line_no in in_block_set:
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue
            if (not injected and anchor_line_no is not None
                    and line_no == anchor_line_no and inject_block):
                for b in inject_block:
                    fout.write(b)
                injected = True
            fout.write(line)
            last_pr = _emit_progress(progress, seen, total, last_pr)
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return len(initial)

def apply_layer_remap_to_file(in_path, out_path, layer_info, progress=None):
    """Streaming equivalent of apply_layer_remap. Mirrors the in-memory
    semantics exactly: parses the original target Y from each
    `; Change Tool X -> Tool Y` comment, and on the NEXT bare T or
    M104/M109 line rewrites the T value to `T<head + 4*ace>` derived
    from current_slot[Y] (which advances as layer events fire on
    each ;LAYER_CHANGE)."""
    if not layer_info or not layer_info.get('feasible'):
        total = os.path.getsize(in_path) or 1
        seen = 0
        last_pr = 0
        with open(in_path, 'r', encoding='utf-8', errors='replace') as fin,\
             open(out_path, 'w', encoding='utf-8') as fout:
            for line in fin:
                seen += len(line.encode('utf-8', errors='ignore'))
                fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
        if progress is not None:
            try:
                progress(total, total)
            except Exception:
                pass
        return None

    initial = dict(layer_info.get('initial_loadout') or {})
    events = list(layer_info.get('events') or [])

    current_slot = {c: (h, c // 4) for c, h in initial.items()}
    events_by_layer: dict[int, list[tuple[int, int, int]]] = {}
    for i, c_in, c_out, h in events:
        events_by_layer.setdefault(i, []).append((c_in, c_out, h))

    loadout: dict[tuple[int, int], int] = {}
    for c, h in initial.items():
        loadout[(0, h)] = c
    ace_counter_pre = [0, 0, 0, 0]
    for (i, c_in, c_out, h) in events:
        ace_counter_pre[h] += 1
        loadout[(ace_counter_pre[h], h)] = c_in

    change_re = re.compile(
        r'^(;\s*Change Tool\s*\d+\s*->\s*Tool\s*)(\d+)(.*)$')
    bare_re   = re.compile(r'^T(\d{1,2})\s*$')
    m104_re   = re.compile(r'^(M10[49]\b.*)$')
    layer_re  = re.compile(r'^;\s*LAYER_CHANGE')
    chg_t_re  = re.compile(r'^;\s*Change Tool\s*\d+\s*->\s*Tool\s*\d+')

    layer_idx = 0
    pending_target: int | None = None
    head_ace_counter = [0, 0, 0, 0]
    in_body = False

    def advance_to_layer(new_idx: int) -> None:
        nonlocal layer_idx
        for ll in range(layer_idx + 1, new_idx + 1):
            for c_in, c_out, h in events_by_layer.get(ll, []):
                head_ace_counter[h] += 1
                ace = head_ace_counter[h]
                current_slot[c_in] = (h, ace)
        layer_idx = new_idx

    def m104_repl(line: str, pt: int) -> str:
        h, ace = current_slot.get(pt, (pt % 4, pt // 4))
        target = 'T%d' % (h + 4 * ace)
        def _r(mm):
            return target if int(mm.group(1)) == pt else mm.group(0)
        return re.sub(r'T(\d{1,2})', _r, line)

    total = os.path.getsize(in_path) or 1
    seen = 0
    last_pr = 0
    with open(in_path, 'r', encoding='utf-8', errors='replace') as fin,\
         open(out_path, 'w', encoding='utf-8') as fout:
        for line in fin:
            seen += len(line.encode('utf-8', errors='ignore'))
            stripped = line.rstrip('\r\n')
            s = stripped.strip()

            if not in_body:
                if chg_t_re.match(s):
                    in_body = True
                else:
                    fout.write(line)
                    last_pr = _emit_progress(progress, seen, total, last_pr)
                    continue

            if layer_re.match(s):
                advance_to_layer(layer_idx + 1)
                fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mc = change_re.match(s)
            if mc:
                pending_target = int(mc.group(2))
                fout.write(line)
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mb = bare_re.match(s)
            if mb and pending_target is not None:
                h, ace = current_slot.get(pending_target,
                                          (pending_target % 4,
                                           pending_target // 4))
                fout.write('T%d\n' % (h + 4 * ace))
                pending_target = None
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            mh = m104_re.match(s)
            if mh and pending_target is not None:
                fout.write(m104_repl(line.rstrip('\n'), pending_target) + '\n')
                last_pr = _emit_progress(progress, seen, total, last_pr)
                continue

            fout.write(line)
            last_pr = _emit_progress(progress, seen, total, last_pr)
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    return loadout

def main():

    args = sys.argv[1:]
    num_aces = None
    optimize = False
    layer_mode = False
    auto_load = True
    live_lookup_host = None
    strict_color = False
    fuzzy_max_distance = None
    if '--aces' in args:
        i = args.index('--aces')
        num_aces = int(args[i + 1])
        del args[i:i + 2]
    if '--optimize' in args:
        args.remove('--optimize')
        optimize = True
    if '--layer' in args:
        args.remove('--layer')
        layer_mode = True
    if '--no-auto-load' in args:
        args.remove('--no-auto-load')
        auto_load = False
    if '--auto-load' in args:

        args.remove('--auto-load')
        auto_load = True
    if '--live-lookup' in args:
        i = args.index('--live-lookup')

        next_arg = args[i + 1] if i + 1 < len(args) else None
        if next_arg and not next_arg.lower().endswith(('.gcode', '.gco', '.g')):
            live_lookup_host = next_arg
            del args[i:i + 2]
        else:
            live_lookup_host = os.environ.get('MULTIACE_HOST', '127.0.0.1')
            del args[i]
    if '--strict-material' in args:

        args.remove('--strict-material')
    if '--strict-color' in args:
        args.remove('--strict-color')
        strict_color = True
    if '--fuzzy-color' in args:
        i = args.index('--fuzzy-color')
        next_arg = args[i + 1] if i + 1 < len(args) else None
        if next_arg and not next_arg.lower().endswith(('.gcode', '.gco', '.g')):
            try:
                fuzzy_max_distance = int(next_arg)
                del args[i:i + 2]
            except ValueError:
                fuzzy_max_distance = 30
                del args[i]
        else:
            fuzzy_max_distance = 30
            del args[i]
    filepath = args[0]

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        gcode = f.read()

    if num_aces is None:
        num_aces = infer_num_aces(gcode)
        print('Auto-detected %d ACE(s) from slicer T-index assignment '
              '(override with --aces N if needed)' % num_aces)

    if live_lookup_host is not None:

        if host_has_manual_head(live_lookup_host):
            print('ERROR: a toolhead is set to manual - live-lookup '
                  'colour matching is disabled (cannot place a hand-fed manual '
                  'head). Switch the head back to auto, or run without '
                  '--live-lookup.', file=sys.stderr)
            sys.exit(2)
        live_slots = lookup_live_slots(live_lookup_host)
        if live_slots is None:
            print('ERROR: live-lookup failed (printer unreachable). '
                  'Either fix connectivity or remove --live-lookup.',
                  file=sys.stderr)
            sys.exit(1)
        if not live_slots:
            print('ERROR: live-lookup returned 0 loaded slots - load filaments '
                  'first (e.g. ACEB__Load_All) and try again.',
                  file=sys.stderr)
            sys.exit(1)

        print('Read printer loadout (live, %d slot(s)):' % len(live_slots))
        for s in sorted(live_slots, key=lambda x: (x['ace'], x['slot'])):
            color_str = format_color_hex_rgb(s['color']) if s['color'] else '(no color)'
            material = s['material'] or '?'
            name = approx_color_name(s['color']) if s['color'] else ''
            label = (' %s' % name) if name and name.lower() != (s['color'] or '').lstrip('#').lower() else ''
            print('  ACE %d Slot %d  %-6s %s%s' % (
                s['ace'], s['slot'], material, color_str, label))
        slicer_colors = parse_color_names(gcode)
        slicer_types = parse_filament_types(gcode)

        missing_mats = check_material_availability(slicer_types, live_slots)
        if missing_mats:
            print('ERROR: the slicer needs filament(s) of material(s) '
                  'that are not loaded anywhere on the printer:',
                  file=sys.stderr)
            for m in missing_mats:

                ts = sorted(t for t, mat in slicer_types.items()
                            if (mat or '').strip().lower() == m)
                ts_str = ', '.join('T%d' % t for t in ts) if ts else ''
                print('  %s%s' % (m.upper(),
                                   ('  (needed by ' + ts_str + ')') if ts_str else ''),
                      file=sys.stderr)
            print('Load filament of the missing material(s) into any slot '
                  '(e.g. ACEB__Load_All) and re-run.', file=sys.stderr)
            sys.exit(1)

        live_remap, match_info, _ = match_colors_to_slots(
            slicer_colors, live_slots, num_heads=4,
            filament_types=slicer_types,
            strict_color=strict_color,
            fuzzy_max_distance=fuzzy_max_distance)

        _tier_label = {
            'exact_hex':         'Exact match',
            'name_exact':        'Name match (exact)',
            'name_base':         'Name match (qualifier)',
            'name_canon':        'Name match (synonym)',
            'fuzzy':             'Fuzzy match',
            'loose_exact_hex':   'Loose-material exact',
            'loose_name_exact':  'Loose-material name',
            'loose_name_base':   'Loose-material name (qualifier)',
            'loose_name_canon':  'Loose-material name (synonym)',
            'loose_fuzzy':       'Loose-material fuzzy',
            'fallback':          'Fallback (no colour match)',
            'duplicate':         'Duplicate (shared slot - wrong colour)',
            'no_slot':           'No slot available',
        }
        _tier_warn = {'loose_exact_hex', 'loose_name_exact', 'loose_name_base',
                      'loose_name_canon', 'loose_fuzzy', 'fallback',
                      'duplicate', 'no_slot'}

        by_tier = {}
        for t in sorted(match_info.keys()):
            by_tier.setdefault(match_info[t]['tier'], []).append(t)
        print('Live-lookup match (slicer T -> physical ACE/Slot):')

        order = ('exact_hex', 'name_exact', 'name_base', 'name_canon',
                 'fuzzy', 'loose_exact_hex', 'loose_name_exact',
                 'loose_name_base', 'loose_name_canon', 'loose_fuzzy',
                 'fallback', 'no_slot')
        any_warn = False
        for tier in order:
            ts = by_tier.get(tier)
            if not ts:
                continue
            mark = '! ' if tier in _tier_warn else '  '
            if tier in _tier_warn:
                any_warn = True
            print('  [%s]' % _tier_label[tier])
            for t in ts:
                hex_c = slicer_colors.get(t, '?')
                mat = slicer_types.get(t, '?')
                inf = match_info[t]
                if inf['slot'] is None:
                    print('  %sT%-2d %-6s %s  ->  (no unclaimed slot left)' % (
                        mark, t, mat, format_color_hex_rgb(hex_c)))
                    continue
                s = inf['slot']
                slot_mat = s['material'] or '?'
                slot_hex = s['color']
                print('  %sT%-2d %-6s %s  ->  ACE %d Slot %d  %-6s %s' % (
                    mark, t, mat, format_color_hex_rgb(hex_c),
                    s['ace'], s['slot'], slot_mat,
                    format_color_hex_rgb(slot_hex)))
        if any_warn:
            print('  Note: tiers marked ! are degraded matches - the '
                  'print will proceed but colours/materials at those '
                  'tools will differ from what the slicer assumed.')

        no_slot_ts = by_tier.get('no_slot') or []
        if no_slot_ts:
            print('ERROR: too few loaded slots - the slicer uses %d '
                  'tools but only %d slots are loaded. Load more '
                  'filament and re-run.' % (
                      len(match_info), len(live_slots)),
                  file=sys.stderr)
            sys.exit(1)

        if live_remap:
            gcode = apply_remap(gcode, live_remap)

    result = plan_loadout(gcode, num_aces=num_aces)
    if result is not None:
        print_recommendation(result, num_aces)

    remap_info = None
    layer_remap_applied = False
    if layer_mode and result is not None:
        layer_info = result.get('layer_info')
        if (layer_info and layer_info.get('feasible')
                and layer_info.get('aces_needed', 0) <= num_aces):
            gcode, _loadout = apply_layer_remap(gcode, layer_info)
            layer_remap_applied = True
            print()
            print('--- LAYER MODE applied: %d swaps -> %d (%d saved) ---' % (
                result['swaps'], layer_info['layer_swaps'],
                result['swaps'] - layer_info['layer_swaps']))
            print('Load cartridges per the Pre-load + Additional swap lists above.')
        elif layer_info and layer_info.get('feasible'):
            print()
            print('--- LAYER MODE skipped: plan needs %d ACEs, you have %d (pass --aces %d to enable) ---' % (
                layer_info['aces_needed'], num_aces, layer_info['aces_needed']))

    if optimize and not layer_remap_applied and result is not None:
        remap, opt_swaps = compute_optimal_remap(result)
        if remap:
            gcode = apply_remap(gcode, remap)
            remap_info = (remap, result['swaps'], opt_swaps)
            print()
            print('--- AUTO-REMAP applied: %d swaps -> %d (%d saved) ---' % (
                result['swaps'], opt_swaps, result['swaps'] - opt_swaps))
            print('Load filaments per the Optimized Print Loadout above.')
            print('T remap (old -> new): %s' % ', '.join(
                'T%d->T%d' % (k, v) for k, v in sorted(remap.items())))

    gcode, active_swaps, skipped_swaps, swapback_count = rewrite(gcode)
    if active_swaps + skipped_swaps + swapback_count > 0:
        print('Rewrite: %d active ACE_SWAP_HEAD, %d skipped, %d swap-backs inserted' % (
            active_swaps, skipped_swaps, swapback_count))

    auto_load_count = 0
    if auto_load:
        gcode, auto_load_count = inject_auto_load(gcode)
        if auto_load_count > 0:
            print('Auto-load: injected ACE_SWAP_HEAD for %d head(s) before first T command' % auto_load_count)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(gcode)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    logpath = os.path.join(script_dir, 'multiace_postprocess.log')
    try:
        import io
        from datetime import datetime
        logbuf = io.StringIO()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        print('=== %s  %s ===' % (ts, os.path.abspath(filepath)), file=logbuf)
        if result is not None:
            print_recommendation(result, num_aces, file=logbuf)
        if layer_remap_applied and result is not None:
            li = result['layer_info']
            print('--- LAYER MODE applied: %d swaps -> %d (%d saved) ---' % (
                result['swaps'], li['layer_swaps'],
                result['swaps'] - li['layer_swaps']), file=logbuf)
        if remap_info is not None:
            print('--- AUTO-REMAP applied: %d swaps -> %d (%d saved) ---' % (
                remap_info[1], remap_info[2], remap_info[1] - remap_info[2]),
                file=logbuf)
            print('T remap (old -> new): %s' % ', '.join(
                'T%d->T%d' % (k, v) for k, v in sorted(remap_info[0].items())),
                file=logbuf)
        if active_swaps + skipped_swaps + swapback_count > 0:
            print('Rewrite: %d active ACE_SWAP_HEAD, %d skipped, %d swap-backs inserted' % (
                active_swaps, skipped_swaps, swapback_count), file=logbuf)
        if auto_load_count > 0:
            print('Auto-load: injected ACE_SWAP_HEAD for %d head(s) before first T command' % auto_load_count, file=logbuf)
        with open(logpath, 'a', encoding='utf-8') as f:
            f.write(logbuf.getvalue())
            if not logbuf.getvalue().endswith('\n'):
                f.write('\n')
    except Exception:
        pass

if __name__ == '__main__':
    main()
