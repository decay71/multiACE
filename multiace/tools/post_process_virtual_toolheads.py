import sys, re, os
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
        m_s = re.match(r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$', line)
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

def parse_color_names(gcode):
    """Best-effort lookup table T-index -> color name. Orca writes
    the filament_colour line at the end of the gcode, Bambu/Prusa
    often near the top — scan both."""
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

def format_color(t_index, color_names):
    hex_val = color_names.get(t_index)
    if not hex_val:
        return '?'
    name = approx_color_name(hex_val)
    if hex_val.lstrip('#').lower() == name.lower():
        return hex_val
    return '%s (%s)' % (name, hex_val)

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

def compute_layer_swap_plan(body_gcode, num_aces=4):
    """Analyze whether the print can be served with layer-boundary-only
    swaps (no mid-layer toolchanges) on a 4-slot printhead with at most
    `num_aces` physical ACE units.

    Walks the body gcode layer-by-layer (;LAYER_CHANGE markers), tracks
    the set of distinct colors active within each layer, then — if every
    layer fits in 4 slots — runs a budget-aware Belady cache-replacement
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
                'reason_detail': '>4 distinct colors in some layer'}

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
                'suggestion': suggestion}

    def next_use(col, since):
        for j in range(since, n_layers):
            if col in layer_colors[j]:
                return j
        return 1 << 30

    all_colors = sorted({c for s in layer_colors for c in s})

    def simulate(fixed_initial):
        """Strict-c%4 simulator. Each color c lives on head c%4 (its
        physical destination — ACE c//4 / Slot c%4 feeds head c%4).
        No free choice of head: when a layer needs c and head c%4 is
        occupied by another color c', evict c' and load c. If c' is
        also needed in the same layer (= layer uses two colors with
        the same %4), the print is infeasible at layer granularity
        (would need a mid-layer swap, which our caller filters out
        via max_per_layer check).

        Each color is loaded into its slicer-canonical ACE position
        (c // 4). Feasibility: c // 4 must be < num_aces. Distinct
        colors per head (= aces_needed) is the count that matters,
        not the total number of swaps — the same two colors can
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
            'histogram': histogram}

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
    slicer tool indices — this keeps the analyzer/optimizer idempotent
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
    `; skipped (already loaded)` — leaving only the Belady-optimal
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
                p('  ACEs needed: %d (you have %d — fits)' % (
                    aces_needed, num_aces))
            else:
                p('  ACEs needed: %d (you have %d — DOES NOT FIT, --layer will be skipped)' % (
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
                p('  Feasible: NO  (%s — needs mid-layer swaps)' % detail)
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

def inject_auto_load(gcode):
    """Insert ACE_SWAP_HEAD calls for each used head AT the safest point
    that is past G28 + heating but before the first move that needs the
    initial tool's filament.

    Use case: replace the manual preload step before a multi-color
    print. The slicer's start gcode emits heating + G28 + bed leveling
    (and a bare T<initial_extruder> command for heater selection that
    can come BEFORE G28 — that's why we don't inject before the first
    T).

    Injection-point fallback chain (highest priority first):

      1. Right BEFORE the first SM_PRINT_PREEXTRUDE_FILAMENT line.
         This is Snapmaker's stock prime move — it lives AFTER G28 +
         M109 in the slicer's start gcode and BEFORE the first body
         move. It also extrudes from the initial tool, so the initial
         tool's filament must be loaded by then or the runout sensor
         triggers an id=523 pause (observed 2026-04-26 14:56). This is
         the safest anchor for prints that use a single tool or whose
         initial tool is never targeted by a `; Change Tool` marker.

      2. Right BEFORE the first '; Change Tool X -> Tool Y' marker
         (Orca multi-tool prints). This anchor is the boundary between
         start_gcode and the print body — but it is AFTER any prior
         SM_PRINT_PREEXTRUDE_FILAMENT, which is why it is fallback 2,
         not 1.

      3. Right BEFORE the first ACE_SWAP_HEAD HEAD= line. Catches
         single-color prints where rewrite() generated swaps.

    cmd_ACE_SWAP_HEAD's empty-head detection (ace.py) makes this work
    for fresh / unloaded heads — the unload phase is skipped when the
    sensor reports no filament and head_source is None, so the swap
    reduces to a pure load. Already-loaded heads with the correct
    (ACE, slot) hit the 'already on' short-circuit (no-op). Mismatched
    loaded heads get unloaded + reloaded.

    Initial mapping per head is discovered from the first ACE_SWAP_HEAD
    line for that head. Heads that appear only as bare T<n> get the
    default mapping (ACE 0, slot=head) — that's the state the rewrite
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
    inject_idx = None
    for idx, line in enumerate(lines):
        if '画起始线' in line:
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
    for i in range(body_start, len(lines)):
        line = lines[i]
        ls = line.strip()
        m_t = re.match(r'^T([0-3])\s*$', ls)
        if m_t:
            head = int(m_t.group(1))
            used_heads.add(head)
            if head not in initial:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                ace_m = None
                if j < len(lines):
                    ace_m = re.match(
                        r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$',
                        lines[j].strip())
                if ace_m and int(ace_m.group(1)) == head:
                    initial[head] = (int(ace_m.group(2)), int(ace_m.group(3)))
                else:
                    initial[head] = (0, head)
            continue
        m = re.match(r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)$', ls)
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
        inject.append('ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace, slot))
    inject.append('; multiACE auto-load: end')
    inject.append('')
    new_lines = lines[:inject_idx] + inject + lines[inject_idx:]
    return '\n'.join(new_lines), len(initial)

def main():
    args = sys.argv[1:]
    num_aces = None
    optimize = False
    layer_mode = False
    auto_load = True
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
    filepath = args[0]

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        gcode = f.read()

    if num_aces is None:
        num_aces = infer_num_aces(gcode)
        print('Auto-detected %d ACE(s) from slicer T-index assignment '
              '(override with --aces N if needed)' % num_aces)

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
        logbuf = io.StringIO()
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
        with open(logpath, 'w', encoding='utf-8') as f:
            f.write(logbuf.getvalue())
    except Exception:
        pass

if __name__ == '__main__':
    main()
