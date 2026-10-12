# multiACE preflight core - PURE preflight logic, shared by the FastAPI
# backend (main.py) AND the in-browser Pyodide worker.
#
# WHY this module exists
# ----------------------
# The preflight analysis (build the report: slicer colours, live slots, the
# slicer/optimize/layer plans, or the head-mode preview) and the rewrite
# pipeline (apply_remap -> rewrite -> inject_auto_load -> head-mode variants)
# used to live INSIDE main.py, tangled with FastAPI, the printer fetch,
# Moonraker upload and the async job state. That made it impossible to run the
# SAME logic in the browser without re-implementing it in JavaScript (a second
# source of truth that silently drifts - exactly the trap a community JS port
# fell into, see the compare_browser_preflight.js differ they had to add).
#
# So the pure parts are factored out here. They take:
#   - pp          : the post_process_virtual_toolheads module (the primitives)
#   - gcode lines / a src path : the input
#   - live_slots  : the ACE/slot identities (caller fetches them - printer in
#                   the backend, /multiace/api/state in the browser)
#   - head_ctx    : {"mode","ace_head","feeders"} (caller resolves it)
# and return plain dicts / write plain files. No FastAPI, no httpx, no asyncio,
# no printer access. Everything here runs unchanged under CPython on the printer
# AND under Pyodide (CPython-WASM) in the browser.
#
# main.py keeps the I/O shell (HTTP, file read/write, _live_slots_async,
# _head_mode_context, Moonraker upload, job progress) and calls in here.

from __future__ import annotations

import re
from collections import deque

# Default fuzzy colour-match distance (main.py's _PREFLIGHT_FUZZY). Callers pass
# their own; this is only the fallback so the module is usable standalone.
DEFAULT_FUZZY = 30

# "; Change Tool X -> Tool Y" - the canonical slicer-T transition source. Union
# of X and Y over the whole file = every T-index the print actually touches.
_TOOLCHANGE_RE = re.compile(
    r"^;\s*Change Tool\s*(\d+)\s*->\s*Tool\s*(\d+)", re.MULTILINE)

# Lines worth keeping as the "plan proxy" - a tiny stand-in for the full body
# that plan_loadout()/parse_toolchanges() can read without holding 100+ MB.
_PLAN_KEEP_RE = re.compile(
    r'^(;\s*Change Tool|;\s*LAYER_CHANGE|;\s*filament\b|T\d{1,2}\s*$|M73\b'
    r'|;?\s*flush_(volumes_matrix|multiplier)\s*='
    r'|;\s*multiACE (processed:|auto-load:))',
    re.IGNORECASE)
# M73 kept for the bg-unload window look-ahead (parse_toolchanges_with_times
# reads the remaining-minutes R off the plan proxy); the toolchange/layer
# parsers ignore M73 lines, so keeping them is side-effect-free.


class PreflightRejected(ValueError):
    """The FILE is unacceptable and no amount of retrying will change that -
    as opposed to the environment failing to process an acceptable one.

    The distinction is not cosmetic. The browser path used to treat every
    exception as "the browser could not manage it" and offered the in-printer
    preflight as a fallback - but that path runs this very function, so it
    refuses identically, just slower and after a large upload. Subclasses
    ValueError so the backend's existing handler still turns it into a 409
    with the message as detail."""


# --------------------------------------------------------------------------- #
# meta extraction                                                             #
# --------------------------------------------------------------------------- #

def parse_meta(pp, line_iter):
    """One streaming pass over the gcode lines → everything the report/rewrite
    need from the file metadata. Works on any iterable of lines, so the backend
    can pass an open file handle (memory-friendly for huge files) and the
    browser worker can pass text.splitlines(keepends=True).

    Returns (slicer_colors, slicer_types, num_aces, used, plan_proxy, meta).

    `meta` is a DICT on purpose (it carries the FOrca mixed-nozzle
    gate): parse_meta has four call sites - two here in the backend and two
    in the browser's Pyodide worker - and a positional 6th element would
    have to be threaded through all of them again on the next addition.
    Keys: 'slicer' (banner text), 'forca' (bool), 'nozzles' ({T: mm}).
    """
    head_lines: list = []
    tail_lines: deque = deque(maxlen=2000)
    plan_lines: list = []
    used: set = set()
    for i, line in enumerate(line_iter):
        if i < 300:
            head_lines.append(line)
        else:
            tail_lines.append(line)
        m = _TOOLCHANGE_RE.match(line)
        if m:
            used.add(int(m.group(1)))
            used.add(int(m.group(2)))
        if _PLAN_KEEP_RE.match(line):
            plan_lines.append(line.rstrip('\n'))
    meta_buf = "".join(head_lines) + "".join(tail_lines)
    plan_proxy = "\n".join(plan_lines)

    slicer_colors = pp.parse_color_names(meta_buf)
    slicer_types  = pp.parse_filament_types(meta_buf)
    num_aces      = pp.infer_num_aces(meta_buf)
    # Slicers declare a colour for every profile extruder even if unused; keep
    # only the T-indices the print actually activates.
    if used:
        slicer_colors = {t: c for t, c in slicer_colors.items() if t in used}
        slicer_types  = {t: m for t, m in slicer_types.items() if t in used}
    slicer_name = ''
    nozzles = {}
    try:
        slicer_name = pp.parse_slicer_name(meta_buf)
        nozzles = pp.parse_nozzle_diameters(meta_buf)
    except AttributeError:
        # Older post-processor on the printer (the tool file syncs
        # separately from the backend): degrade to
        # today's behaviour rather than 500 - no banner, no gate.
        pass
    meta = {
        'slicer':  slicer_name,
        'forca':   bool(slicer_name) and pp.is_forca_slicer(slicer_name)
                   if hasattr(pp, 'is_forca_slicer') else False,
        'nozzles': nozzles,
    }
    return slicer_colors, slicer_types, num_aces, used, plan_proxy, meta


def nozzle_context(pp, meta, head_ctx=None, num_heads=4):
    """(groups, mixed) for the matcher gate - the ONE place that decides
    whether the mixed-nozzle constraint applies, so preview and rewrite can
    never disagree (preview == print).

    Demand comes from the file (meta['nozzles'], per FILAMENT), supply from
    the printer (head_ctx['head_nozzles'], per HEAD). Without the printer's
    answer the gate falls back to reading the file's first four entries as
    heads - the older behaviour, right whenever the slicer lists the
    nozzles in machine order.

    Scoped to FOrca files by design (see pp.parse_nozzle_diameters): no other
    slicer can put a mixed-nozzle job on this machine, so the normal workflow
    stays byte-identical - groups is empty for every non-FOrca file, for a
    uniform machine, and when the header carries no diameters at all."""
    if not meta or not meta.get('forca'):
        return None, False
    head_dia = {}
    for k, v in ((head_ctx or {}).get('head_nozzles') or {}).items():
        try:
            head_dia[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    try:
        groups = pp.nozzle_gate_groups(
            meta.get('nozzles') or {}, head_dia or None, num_heads)
    except (AttributeError, TypeError):
        # Older post-processor: either the helper is missing entirely, or it
        # still has the one-argument signature. Degrade to no gate rather
        # than 500 - the tool file syncs separately from the backend, so
        # the two can lag each other.
        return None, False
    return (groups or None), bool(groups)


def used_tool_indices(pp, gcode: str) -> set:
    """The set of T-indices actually activated by the gcode (union of every
    'Change Tool X -> Tool Y'); falls back to the post-processor's bare-T scan
    for single-tool prints with no transitions."""
    used: set = set()
    for m in _TOOLCHANGE_RE.finditer(gcode):
        used.add(int(m.group(1)))
        used.add(int(m.group(2)))
    if not used:
        try:
            used = set(pp.parse_toolchanges(gcode))
        except Exception:
            used = set()
    return used


# --------------------------------------------------------------------------- #
# mapping / plan helpers (pure)                                               #
# --------------------------------------------------------------------------- #

def _slot_to_dict(s):
    if s is None:
        return None
    return {
        "ace":      s.get("ace"),
        "slot":     s.get("slot"),
        "material": s.get("material") or "",
        "color":    s.get("color") or "",
    }


def mapping_from_info(info: dict) -> list:
    out = []
    for t in sorted(info.keys()):
        out.append({
            "t":         t,
            "slot":      _slot_to_dict(info[t]["slot"]),
            "tier":      info[t]["tier"],
            "loose_mat": bool(info[t].get("loose_mat")),
        })
    return out


def _real_swap_count(events, mapping):
    by_t = {m["t"]: m["slot"] for m in mapping if m.get("slot")}
    head_current = {h: (0, h) for h in range(4)}
    swaps = 0
    for t in events:
        slot = by_t.get(t)
        if slot is None:
            continue
        h = slot["slot"]
        key = (slot["ace"], slot["slot"])
        if head_current.get(h) != key:
            swaps += 1
            head_current[h] = key
    return swaps


def _layout_from_head_assignment(c2h, slicer_colors, slicer_types):
    """{color: head} → a mapping list with (ace, slot=head) per colour. ACE
    within each head is first-come-first-served (sorted by T-index)."""
    head_ace = {h: 0 for h in range(4)}
    rows = []
    for c in sorted(c2h.keys(), key=lambda x: (c2h[x], x)):
        h = c2h[c]
        ace = head_ace[h]
        head_ace[h] += 1
        rows.append((ace, h, c, {
            "t":         c,
            "slot": {
                "ace":      ace,
                "slot":     h,
                "material": (slicer_types.get(c) or "") or "",
                "color":    (slicer_colors.get(c) or "").lower(),
            },
            "tier":      "planned",
            "loose_mat": False,
        }))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in rows]


def _swap_aware(pp, events, *, num_aces, layer_color_sets=None,
                allowed_heads=None):
    """compute_swap_aware_layout with the nozzle gate, tolerating an older
    post-processor on the printer: without the parameter the
    call degrades to today's unconstrained search rather than a 500."""
    kw = {"num_aces": num_aces}
    if layer_color_sets:
        kw["layer_color_sets"] = layer_color_sets
    if allowed_heads:
        try:
            return pp.compute_swap_aware_layout(
                events, allowed_heads=allowed_heads, **kw)
        except TypeError:
            pass
    return pp.compute_swap_aware_layout(events, **kw)


def build_one_plan(pp, plan_name, result, mapping,
                   slicer_colors=None, slicer_types=None, num_aces=4,
                   nozzle_groups=None):
    """One of the three multi-mode plans (slicer / optimize / layer).

    nozzle_groups ({T: allowed heads}) is the mixed-nozzle gate. The slicer
    plan gets it through match_colors_to_slots; optimize and layer build
    their own layout and would otherwise be free to move a filament onto a
    wrong-diameter nozzle.
    """
    slicer_colors = slicer_colors or {}
    slicer_types  = slicer_types  or {}
    events = result.get("events") or []
    tool_changes = int(result.get("total_changes") or 0)

    if plan_name == "slicer":
        return {
            "feasible":     True,
            "swaps":        _real_swap_count(events, mapping),
            "tool_changes": tool_changes,
            "mapping":      mapping,
        }

    if plan_name == "optimize":
        try:
            c2h, swaps = _swap_aware(pp, events, num_aces=num_aces,
                                     allowed_heads=nozzle_groups)
        except Exception:
            c2h, swaps = None, None
        if c2h is None:
            return {
                "feasible":     False,
                "swaps":        0,
                "tool_changes": tool_changes,
                "mapping":      [],
                "reason":       "no feasible head assignment",
            }
        return {
            "feasible":     True,
            "swaps":        swaps,
            "tool_changes": tool_changes,
            "mapping":      _layout_from_head_assignment(
                c2h, slicer_colors, slicer_types),
        }

    # plan_name == "layer"
    layer_info = result.get("layer_info") or {}
    layer_color_sets_raw = layer_info.get("layer_color_sets") or []
    layer_color_sets = [set(s) for s in layer_color_sets_raw]
    try:
        c2h, swaps = _swap_aware(
            pp, events, num_aces=num_aces,
            layer_color_sets=layer_color_sets if layer_color_sets else None,
            allowed_heads=nozzle_groups)
    except Exception:
        c2h, swaps = None, None
    if c2h is None:
        reason = "no layer-feasible head assignment"
        max_per = layer_info.get("max_per_layer", 0)
        if max_per > 4:
            reason = ">4 colors in some layer"
        return {
            "feasible":     False,
            "swaps":        0,
            "tool_changes": tool_changes,
            "mapping":      [],
            "reason":       reason,
        }
    return {
        "feasible":     True,
        "swaps":        swaps,
        "tool_changes": tool_changes,
        "mapping":      _layout_from_head_assignment(
            c2h, slicer_colors, slicer_types),
        "reason":       "",
    }


# --------------------------------------------------------------------------- #
# head-mode helpers (pure)                                                    #
# --------------------------------------------------------------------------- #

# Head-mode primitives the post-processor must provide. On a PAXX-bin upgrade a
# stale post_process_virtual_toolheads.py can sit in printer_data/config/tools/
# (the path the backend prefers) while the backend itself is fresh -> calling a
# new head-mode function raises a bare AttributeError -> HTTP 500. Check up front
# and raise a clear, actionable message instead.
_HEAD_MODE_PP_FUNCS = (
    "compute_head_mode_layout", "compute_head_mode_optimize",
    "head_mode_swap_count", "rewrite_head_mode_to_file")

def ensure_head_mode_support(pp):
    missing = [f for f in _HEAD_MODE_PP_FUNCS if not hasattr(pp, f)]
    if missing:
        raise RuntimeError(
            "post-processor is outdated (missing head-mode support: "
            + ", ".join(missing)
            + "). Re-run install_multiace.sh or reboot so the shipped "
              "post_process_virtual_toolheads.py is refreshed in "
              "printer_data/config/tools/.")


def is_multi_ctx(head_ctx) -> bool:
    return (head_ctx or {}).get("mode") == "multi"


def manual_head_list(head_ctx) -> list:
    """[{head, material, color}] of the hand-fed (manual) heads in multi
    mode, identity possibly empty (then the head cannot pin, but its slot
    index is still dead for ACE colours)."""
    out = []
    for m in ((head_ctx or {}).get("manual_heads") or []):
        try:
            out.append({"head": int(m["head"]),
                        "material": (m.get("material") or "").strip(),
                        "color": (m.get("color") or "").strip()})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def multi_assignment_shape(head_ctx) -> bool:
    """Multi mode uses the head-mode assignment pipeline (one target per
    colour, copies, pins) once colour copies are allowed or a hand-fed head
    exists; otherwise the classic synthetic-T pipeline runs, byte-identical
    to before."""
    if not is_multi_ctx(head_ctx):
        return False
    return max_copies(head_ctx) > 1 or bool(manual_head_list(head_ctx))


def head_maps(head_ctx: dict, pp=None) -> tuple:
    """Resolve the ACE-head topology from head_ctx into the maps the matcher
    needs: (ace_heads, ace_head_of_ace, ace_num_of_head, feeder_heads).
      ace_heads        - sorted list of ACE-driven head indices.
      ace_head_of_ace  - {ace_index: head} (each ACE feeds exactly one head).
      ace_num_of_head  - {head: ace_index} (the inverse, for output entries).
      feeder_heads     - the non-ACE heads available to pin.
    Falls back to the legacy single ACE head (head_ctx['ace_head']) when no
    ace_heads list is present, so an older context still works.

    MULTI (head_ctx mode 'multi', pp given): slot N of every ACE feeds
    head N, so ace_head_of_ace is a pp.MultiTopology resolver over the
    non-manual heads, ace_num_of_head is a placeholder and feeder_heads
    are the manual (hand-fed) heads = the pin candidates."""
    head_ctx = head_ctx or {}
    if is_multi_ctx(head_ctx):
        topo_cls = getattr(pp, "MultiTopology", None)
        if topo_cls is None:
            raise RuntimeError(
                "post-processor outdated: multi-mode colour copies / manual "
                "heads need a newer post_process_virtual_toolheads.py")
        manual = sorted({m["head"] for m in manual_head_list(head_ctx)})
        ace_heads = [h for h in range(4) if h not in manual]
        num_aces = int(head_ctx.get("num_aces") or 4)
        topo = topo_cls(ace_heads, num_aces)
        return ace_heads, topo, {h: 0 for h in ace_heads}, manual
    ace_heads = [int(h) for h in (head_ctx.get("ace_heads") or [])]
    raw = head_ctx.get("head_ace") or {}
    head_ace = {}
    for h in range(4):
        try:
            head_ace[h] = int(raw.get(str(h), raw.get(h, h)))
        except (TypeError, ValueError):
            head_ace[h] = h
    if not ace_heads:
        ace_heads = [int(head_ctx.get("ace_head", 3) or 3)]
    ace_heads = sorted(set(ace_heads))
    ace_num_of_head = {h: head_ace.get(h, h) for h in ace_heads}
    ace_head_of_ace = {ace_num_of_head[h]: h for h in ace_heads}
    feeder_heads = [h for h in range(4) if h not in ace_heads]
    return ace_heads, ace_head_of_ace, ace_num_of_head, feeder_heads


def _slot_head_fn(pp, ace_head_of_ace):
    """(ace, slot) -> head or None, from the post-processor's resolver
    when it has one (multi topology needs it), else the head-mode dict."""
    fn = getattr(pp, "_head_of_fn", None)
    if fn is not None:
        return fn(ace_head_of_ace)
    head_of_ace = {int(a): int(h) for a, h in (ace_head_of_ace or {}).items()}
    return lambda a, s: head_of_ace.get(int(a))


def head_mode_targets(pp, feeders: list, ace_slots: list,
                      ace_head_of_ace: dict) -> list:
    """The dropdown universe: each pin-able feeder + each ACE slot on a wired
    ACE (tagged with the ACE head that feeds it), with an id."""
    targets = []
    for f in feeders:
        targets.append({
            "id": "feeder-%d" % f["head"], "kind": "pin", "head": f["head"],
            "material": f["material"], "color": (f["color"] or "").lower(),
            "name": pp.approx_color_name(f["color"]) or ""})
    head_of = _slot_head_fn(pp, ace_head_of_ace)
    for s in sorted(ace_slots, key=lambda x: (x["ace"], x["slot"])):
        h = head_of(s["ace"], s["slot"])
        if h is None:
            continue                       # slot no ACE-fed head can use
        targets.append({
            "id": "slot-%d-%d" % (s["ace"], s["slot"]), "kind": "ace",
            "head": h,
            "ace": s["ace"], "slot": s["slot"],
            "material": s["material"], "color": (s["color"] or "").lower(),
            "name": pp.approx_color_name(s["color"]) or ""})
    return targets


def head_target_id(e: dict):
    if not e:
        return None
    if e.get("kind") == "pin":
        return "feeder-%d" % e["head"]
    if e.get("kind") == "ace":
        return "slot-%d-%d" % (e["ace"], e["slot"])
    return None


def assignment_from_target_ids(target_ids: dict, targets: list) -> dict:
    """Rebuild {t: entry} from the frontend's {t: target_id} via the universe.
    The ACE head of an 'ace' target comes from the target itself (each ACE is
    wired to one head)."""
    by_id = {t["id"]: t for t in targets}
    out = {}
    for k, tid in (target_ids or {}).items():
        try:
            t = int(k)
        except (TypeError, ValueError):
            continue
        tgt = by_id.get(tid)
        if tgt is None:
            out[t] = {"kind": "none"}
        elif tgt["kind"] == "pin":
            out[t] = {"kind": "pin", "head": tgt["head"]}
        else:
            out[t] = {"kind": "ace", "head": tgt["head"],
                      "ace": tgt["ace"], "slot": tgt["slot"]}
    return out


def max_copies(head_ctx) -> int:
    """The printer's preflight_max_copies setting (1 = one target per colour,
    today's behaviour), carried in head_ctx like pickup_cleaning."""
    try:
        return max(1, min(4, int((head_ctx or {}).get("max_copies", 1) or 1)))
    except (TypeError, ValueError):
        return 1


def color_copies(pp, assignment, slicer_colors, slicer_types, ace_slots,
                 ace_head_of_ace, head_ctx, nozzle_groups=None) -> dict:
    """{t: [extra 'ace' entries]}: second spools of a slicer colour that are
    LOADED on another head and unclaimed by the assignment, capped by the
    max_copies setting (1 -> {} and every consumer stays byte-identical).
    Soft-degrading: an older post-processor without detect_color_copies
    yields {}."""
    limit = max_copies(head_ctx)
    if limit <= 1:
        return {}
    det = getattr(pp, "detect_color_copies", None)
    if det is None:
        return {}
    strict = bool((head_ctx or {}).get("copies_strict"))
    try:
        try:
            found = det(assignment, slicer_colors, slicer_types, ace_slots,
                        ace_head_of_ace, nozzle_groups=nozzle_groups,
                        strict=strict) or {}
        except TypeError:
            # older post-processor without the strict kwarg
            found = det(assignment, slicer_colors, slicer_types, ace_slots,
                        ace_head_of_ace, nozzle_groups=nozzle_groups) or {}
    except Exception:
        return {}
    return {t: list(lst[:limit - 1]) for t, lst in found.items() if lst}


def copies_from_target_ids(copy_ids: dict, assignment: dict, targets: list,
                           head_ctx) -> dict:
    """Rebuild {t: [entries]} from the frontend's {t: [target_id, ...]}
    (the copies the preview showed, sent verbatim so the print matches it).
    Only ACE targets count, never the colour's own primary target or a
    target on the primary's head, capped by max_copies."""
    limit = max_copies(head_ctx)
    out = {}
    if limit <= 1:
        return out
    by_id = {t["id"]: t for t in targets}
    for k, ids in (copy_ids or {}).items():
        try:
            t = int(k)
        except (TypeError, ValueError):
            continue
        prim = (assignment or {}).get(t) or {}
        if prim.get("kind") not in ("ace", "pin"):
            continue
        heads = {prim.get("head")}
        lst = []
        for tid in (ids or []):
            tg = by_id.get(tid)
            if not tg or tg.get("kind") != "ace" or tg["head"] in heads:
                continue
            lst.append({"kind": "ace", "head": tg["head"],
                        "ace": tg["ace"], "slot": tg["slot"]})
            heads.add(tg["head"])
            if len(lst) >= limit - 1:
                break
        if lst:
            out[t] = lst
    return out


def head_swaps_with_copies(pp, events, assignment, copies, bg_heads=None):
    """head_mode_swap_count honoring copies where the post-processor can
    (bg_heads = the plan's tie-break, same as the rewriter)."""
    if copies:
        try:
            return pp.head_mode_swap_count(events, assignment, copies,
                                           bg_heads=bg_heads)
        except TypeError:
            pass
        try:
            return pp.head_mode_swap_count(events, assignment, copies)
        except TypeError:
            pass
    return pp.head_mode_swap_count(events, assignment)


def copy_plan(pp, events, assignment, head_ctx, ace_head_of_ace, copies,
              bg_heads=None, nozzle_groups=None) -> dict:
    """The loadout suggestion for the Multifilament row: which colours to
    load once more, into which EMPTY slot (head_ctx['empty_slots']), and
    the swap count before/after. max_copies 1 -> no suggestion."""
    limit = max_copies(head_ctx)
    out = {"max_copies": limit, "suggestions": [],
           "swaps_before": None, "swaps_after": None}
    fn = getattr(pp, "plan_color_copies", None)
    if limit <= 1 or fn is None:
        return out
    try:
        res = fn(events, assignment,
                 list((head_ctx or {}).get("empty_slots") or []),
                 ace_head_of_ace, limit, existing_copies=copies,
                 bg_heads=bg_heads, nozzle_groups=nozzle_groups) or {}
    except Exception:
        return out
    out["swaps_before"] = res.get("swaps_before")
    out["swaps_after"] = res.get("swaps_after")
    out["suggestions"] = [
        {"t": int(s["t"]), "ace": int(s["ace"]), "slot": int(s["slot"]),
         "head": int(s["head"]), "swaps": int(s["swaps"])}
        for s in (res.get("suggestions") or [])]
    return out


def proposal_copies(pp, events, assignment, head_ctx, ace_head_of_ace,
                    num_slots=4, bg_heads=None, nozzle_groups=None) -> dict:
    """Colour copies for a PROPOSED loadout (optimize / layer / color): the
    proposal ignores the current load, so every slot of an ACE head that the
    proposal leaves free is a candidate for a second spool of a colour.
    Same greedy as the loadout suggestion. Returns {t: [copy entries]};
    {} when max_copies is 1 or nothing saves a swap. Preview and print call
    this with the same inputs, so both see the same copies."""
    limit = max_copies(head_ctx)
    fn = getattr(pp, "plan_color_copies", None)
    if limit <= 1 or fn is None or not assignment:
        return {}
    used = set()
    for e in assignment.values():
        if e and e.get("kind") == "ace":
            used.add((int(e["ace"]), int(e["slot"])))
    _all = getattr(pp, "_all_ace_slots", None)
    if _all is not None:
        free = [{"ace": int(a), "slot": int(si)}
                for a, si, _h in _all(ace_head_of_ace, num_slots)
                if (int(a), int(si)) not in used]
    else:
        free = [{"ace": int(a), "slot": si}
                for a in (ace_head_of_ace or {}) for si in range(num_slots)
                if (int(a), si) not in used]
    try:
        res = fn(events, assignment, free, ace_head_of_ace, limit,
                 existing_copies={}, bg_heads=bg_heads,
                 nozzle_groups=nozzle_groups) or {}
    except Exception:
        return {}
    out = {}
    for t, lst in (res.get("targets") or {}).items():
        if len(lst) > 1:
            out[t] = [dict(x) for x in lst[1:]]
    return out


def _bg_context(pp, head_ctx, plan_proxy, events):
    """(event_times, bg_heads, bg_available) for the bg-aware preflight
    bits, all soft-degrading: an older post-processor without the time
    parser, a file without M73, or a misaligned event list simply yield
    event_times=None (bg windows unknown - everything reports/optimizes
    like before)."""
    bg_heads = [int(h) for h in ((head_ctx or {}).get("bg_heads") or [])]
    bg_available = bool((head_ctx or {}).get("bg_available"))
    if is_multi_ctx(head_ctx):
        # No background swaps in multi (one ACE feeds several heads), even
        # when the persisted bg head list still carries ticks.
        bg_heads, bg_available = [], False
    parse_t = getattr(pp, "parse_toolchanges_with_times", None)
    event_times = None
    if parse_t is not None:
        try:
            ev_t, times = parse_t(plan_proxy)
            if list(ev_t) == list(events) and any(
                    t is not None for t in times):
                event_times = times
        except Exception:
            event_times = None
    return event_times, bg_heads, bg_available


def _bg_stats_for(pp, events, assignment, event_times, bg_heads,
                  copies=None):
    """head_mode_bg_stats, soft-degrading (older pp -> None). Details are
    dropped from the wire format (the counts drive the UI line). With
    copies the stats follow the dynamic plan (an older pp without the
    kwarg falls back to the static walk)."""
    fn = getattr(pp, "head_mode_bg_stats", None)
    if fn is None or assignment is None:
        return None
    try:
        try:
            st = fn(events, assignment, event_times=event_times,
                    bg_heads=bg_heads, copies=copies)
        except TypeError:
            st = fn(events, assignment, event_times=event_times,
                    bg_heads=bg_heads)
        st.pop("details", None)
        return st
    except Exception:
        return None


def _head_proposal_plan(pp, events, slicer_colors, feeder_heads, ace_heads,
                        ace_num_of_head, num_slots, layer_sets,
                        event_times=None, bg_heads=None,
                        flush_matrix=None, objective="time",
                        head_ctx=None, ace_head_of_ace=None,
                        nozzle_groups=None) -> dict:
    """A head-mode PROPOSED-loadout plan (optimize / layer-Belady): the
    swap-minimal FREE assignment that ignores the current physical load. The
    user arranges spools to match before printing → read-only table. With
    event_times/bg_heads the optimizer prefers routing swap chains through
    bg-enabled heads (background unloads instead of inline stalls)."""
    try:
        try:
            assignment, swaps = pp.compute_head_mode_optimize(
                events, feeder_heads, ace_heads, ace_num_of_head, num_slots,
                layer_color_sets=layer_sets,
                event_times=event_times, bg_heads=bg_heads,
                flush_matrix=flush_matrix, objective=objective,
                topology=ace_head_of_ace)
        except TypeError:
            # older post-processor without the newer kwargs
            assignment, swaps = pp.compute_head_mode_optimize(
                events, feeder_heads, ace_heads, ace_num_of_head, num_slots,
                layer_color_sets=layer_sets)
    except Exception:
        assignment, swaps = None, None
    if assignment is None:
        reason = ("no layer-feasible loadout" if layer_sets
                  else "too many colours for the loadout")
        return {"feasible": False, "swaps": 0, "mapping": [], "reason": reason}
    mapping = []
    feasible = True
    for t in sorted(slicer_colors.keys()):
        e = assignment.get(t)
        if not e or e.get("kind") == "none":
            feasible = False
            mapping.append({"t": t, "kind": "none"})
        else:
            mapping.append({"t": t, "kind": e["kind"], "head": e.get("head"),
                            "ace": e.get("ace"), "slot": e.get("slot"),
                            "tier": e.get("tier")})
    # Colour copies in the proposal: a second spool of a colour in a slot
    # the proposal leaves free (rows tagged tier 'copy'); the swap count is
    # the simulation with those copies, so the header matches the table.
    copies = proposal_copies(pp, events, assignment, head_ctx,
                             ace_head_of_ace, num_slots, bg_heads,
                             nozzle_groups)
    if copies:
        for t, lst in copies.items():
            for e in lst:
                mapping.append({"t": t, "kind": "ace", "head": e.get("head"),
                                "ace": e.get("ace"), "slot": e.get("slot"),
                                "tier": "copy"})
        swaps = head_swaps_with_copies(pp, events, assignment, copies,
                                       bg_heads)
    # Proposal tables read like a re-stick instruction: feeders first (by
    # head), then ACE slots by (ace, slot); infeasible rows last. The
    # editable loadout table is slicer-T-keyed and stays unsorted.
    _kind_rank = {"pin": 0, "ace": 1}
    mapping.sort(key=lambda m: (
        _kind_rank.get(m.get("kind"), 2),
        m.get("ace") if m.get("ace") is not None else 99,
        m.get("slot") if m.get("slot") is not None else 99,
        m.get("head") if m.get("head") is not None else 99,
        m.get("t", 0)))
    out = {"feasible": feasible, "swaps": swaps, "mapping": mapping}
    bg = _bg_stats_for(pp, events, assignment, event_times, bg_heads, copies)
    if bg is not None:
        out["bg"] = bg
    # Same-nozzle transition volume (colour objective) for plan comparison:
    # grams at 1.24 g/cm3 PLA-ish density - a UI label, not an exact scale.
    fc_fn = getattr(pp, "head_mode_flush_cost", None)
    if flush_matrix is not None and fc_fn is not None:
        try:
            fc = fc_fn(events, assignment, flush_matrix)
            if fc is not None:
                out["flush_g"] = round(fc * 1.24 / 1000.0, 1)
        except Exception:
            pass
    return out


def head_mode_preview(pp, token, safe_name, upload_size, slicer_colors,
                      slicer_types, head_ctx, ace_slots, plan_proxy,
                      fuzzy=DEFAULT_FUZZY, meta=None) -> dict:
    """The head-mode preflight preview: THREE plans, mirroring multi:
      loadout  - match against the currently-loaded feeders + ACE slots (editable)
      optimize - swap-minimal proposed loadout (free, Belady per ACE head)
      layer    - same with layer-only swaps (Belady-/layer-optimal)
    Plus the colour grids at the top (available targets + slicer colours).
    """
    multi = is_multi_ctx(head_ctx)
    feeders = (manual_head_list(head_ctx) if multi
               else ((head_ctx or {}).get("feeders") or []))
    ace_heads, ace_head_of_ace, ace_num_of_head, feeder_heads = \
        head_maps(head_ctx, pp)
    targets = head_mode_targets(pp, feeders, ace_slots, ace_head_of_ace)
    # events + per-layer colour sets (the Belady/layer plan needs them).
    try:
        result = pp.plan_loadout(plan_proxy) or {}
    except Exception:
        result = {}
    events = list(result.get("events") or [])
    if not events:
        try:
            events = list(pp.parse_toolchanges(plan_proxy))
        except Exception:
            events = []
    lcs = (result.get("layer_info") or {}).get("layer_color_sets") or []
    layer_sets = [set(s) for s in lcs] if lcs else None

    # Background-swap context: M73 window times + the bg-enabled heads, for
    # the per-plan bg balance and the bg-aware optimizer (soft-degrading).
    event_times, bg_heads, bg_available = _bg_context(
        pp, head_ctx, plan_proxy, events)

    # Plan 1 - loadout (as currently loaded), the editable table.
    nz_groups, nz_mixed = nozzle_context(pp, meta, head_ctx)
    layout = pp.compute_head_mode_layout(
        slicer_colors, slicer_types, feeders, ace_slots, ace_head_of_ace,
        fuzzy_max_distance=fuzzy, nozzle_groups=nz_groups)
    assignment = layout["assignment"]
    loadout_mapping = []
    for t in sorted(slicer_colors.keys()):
        e = assignment.get(t) or {}
        loadout_mapping.append({"t": t, "target_id": head_target_id(e),
                                "tier": e.get("tier", "no_slot")})
    # Colour copies (Multifilament-Optimierung): the same colour loaded on
    # a second head. Detected from the loaded slots, capped by the printer's
    # max_copies setting (1 = off -> {} -> identical to before).
    copies = color_copies(pp, assignment, slicer_colors, slicer_types,
                          ace_slots, ace_head_of_ace, head_ctx, nz_groups)
    plans = {
        "loadout": {
            "feasible": layout["feasible"],
            "swaps": head_swaps_with_copies(pp, events, assignment, copies,
                                            bg_heads),
            "mapping": loadout_mapping},
    }
    bg_loadout = _bg_stats_for(pp, events, assignment, event_times, bg_heads,
                               copies)
    if bg_loadout is not None:
        plans["loadout"]["bg"] = bg_loadout

    # Plans 2+3 - proposed loadout (ignore current load). Per ACE head:
    # 4 slots of its own ACE (head mode), or one slot per ACE (multi).
    num_slots = (getattr(ace_head_of_ace, "num_aces", 4) if multi else 4)
    # Colour objective context: the slicer's flush matrix (kept on the plan
    # proxy). None -> no "color" plan, old preview shape.
    flush_matrix = None
    _pfm = getattr(pp, "parse_flush_matrix", None)
    if _pfm is not None:
        try:
            flush_matrix = _pfm(plan_proxy)
        except Exception:
            flush_matrix = None
    _cp = dict(head_ctx=head_ctx, ace_head_of_ace=ace_head_of_ace,
               nozzle_groups=nz_groups)
    plans["optimize"] = _head_proposal_plan(
        pp, events, slicer_colors, feeder_heads, ace_heads, ace_num_of_head,
        num_slots, None, event_times=event_times, bg_heads=bg_heads,
        flush_matrix=flush_matrix, **_cp)
    plans["layer"] = _head_proposal_plan(
        pp, events, slicer_colors, feeder_heads, ace_heads, ace_num_of_head,
        num_slots, layer_sets, event_times=event_times, bg_heads=bg_heads,
        flush_matrix=flush_matrix, **_cp)
    if flush_matrix is not None:
        # Fourth plan - colour-optimised: minimise same-nozzle contamination
        # volume (sensitive colours get pinned, expensive pairs move onto
        # head boundaries); time cost is the tiebreak. Honest trade-off
        # display: swaps may go UP while flush_g goes down.
        plans["color"] = _head_proposal_plan(
            pp, events, slicer_colors, feeder_heads, ace_heads,
            ace_num_of_head, num_slots, None,
            event_times=event_times, bg_heads=bg_heads,
            flush_matrix=flush_matrix, objective="color", **_cp)

    return {
        "token": token, "filename": safe_name, "size": upload_size,
        "head_mode": True, "ace_head": (ace_heads[0] if ace_heads else 3),
        "ace_heads": ace_heads,
        # 'multi': the assignment pipeline on the multi topology (slot N
        # feeds head N, pins = hand-fed heads); 'head': the wired 1:1 one.
        "topology": ("multi" if multi else "head"),
        "manual_heads": [int(h) for h in feeder_heads] if multi else [],
        "num_aces": (getattr(ace_head_of_ace, "num_aces", None)
                     if multi else None),
        # Head mode returns EARLY, before the multi block that sets
        # these - so it has to carry the display fields itself, or the UI
        # would never see a nozzle table in head mode.
        "slicer": (meta or {}).get("slicer") or "",
        "forca": bool((meta or {}).get("forca")),
        "nozzles": {str(t): d
                    for t, d in ((meta or {}).get("nozzles") or {}).items()},
        "head_nozzles": dict((head_ctx or {}).get("head_nozzles") or {}),
        "nozzles_mixed": nz_mixed,
        # The mixed-nozzle view needs to say WHERE a colour currently sits
        # ("in ACE 2 / slot 1, move it to slot 3") instead of the misleading
        # "not loaded" - it usually IS loaded, just in a lane feeding the
        # wrong nozzle size. multi already ships live_slots; head mode did
        # not, so it does now.
        "live_slots": [
            {"ace": s["ace"], "slot": s["slot"],
             "material": s["material"], "color": s["color"],
             "name": pp.approx_color_name(s["color"]) or ""}
            for s in sorted(ace_slots or [],
                            key=lambda x: (x["ace"], x["slot"]))],
        "bg_swap": {"available": bg_available, "enabled_heads": bg_heads,
                    "have_times": event_times is not None,
                    "min_window_min": getattr(
                        pp, "BG_UNLOAD_MIN_WINDOW_MIN", 3),
                    "inline_saving_s": getattr(
                        pp, "BG_UNLOAD_INLINE_SAVING_S", 180)},
        # Remaining minutes per toolchange (M73), so the browser can rate
        # the bg windows of a live-edited plan like the server does.
        "event_times": (list(event_times) if event_times is not None
                        else None),
        "slicer_colors": [
            {"t": t, "hex": (slicer_colors[t] or "").lower(),
             "name": pp.approx_color_name(slicer_colors[t]) or "",
             "material": slicer_types.get(t, "") or ""}
            for t in sorted(slicer_colors.keys())],
        "targets": targets,
        "events": events,
        "plans": plans,
        # Multifilament row: the copies in use (target ids per slicer T, all
        # loaded slots so the universe resolves them) + the suggestion.
        "copies": {str(t): [head_target_id(e) for e in lst]
                   for t, lst in copies.items()},
        "copy_plan": copy_plan(pp, events, assignment, head_ctx,
                               ace_head_of_ace, copies, bg_heads=bg_heads,
                               nozzle_groups=nz_groups),
        "empty_slots": [{"ace": int(e["ace"]), "slot": int(e["slot"])}
                        for e in ((head_ctx or {}).get("empty_slots") or [])
                        if e.get("ace") is not None
                        and e.get("slot") is not None],
    }


# --------------------------------------------------------------------------- #
# report builder (analyze)                                                    #
# --------------------------------------------------------------------------- #

def build_report(pp, *, slicer_colors, slicer_types, num_aces, plan_proxy,
                 live_slots, head_ctx, token, filename, size,
                 fuzzy=DEFAULT_FUZZY, meta=None) -> dict:
    """Build the full preflight report dict (the /api/preflight payload).

    Refuses an ALREADY-PROCESSED file (the "; multiACE processed:" /
    auto-load marker, kept on the plan proxy): re-processing scrambles the
    swaps and a clean un-process is impossible. One
    checkpoint covers the server AND the in-browser Pyodide path.

    head_ctx = {"mode": "normal"|"multi"|"head", "ace_head": int,
                "feeders": [{"head","material","color"}, ...]}.
    The caller has already fetched live_slots and resolved head_ctx (printer in
    the backend, /multiace/api/state in the browser). Mirrors main.py's old
    inline /api/preflight body 1:1 so backend and Pyodide produce identical
    reports.
    """
    _dp = getattr(pp, "detect_processed", None)
    if _dp is not None:
        try:
            _proc, _fmt = _dp(plan_proxy)
        except Exception:
            _proc, _fmt = False, None
        if _proc:
            # Lead with what the file IS - ready to print - because that is
            # the likeliest thing the user wants and it needs no preflight at
            # all. The constraint follows; telling them only "upload the
            # original" reads as a dead end when the file in their hand is
            # perfectly printable.
            raise PreflightRejected(
                "This file has already been processed by multiACE (%s), so "
                "it is ready to print as it is - upload it in Fluidd."
                % ("format %d" % _fmt if _fmt is not None
                   else "an older version, no format marker"))
    num_aces = max(num_aces, max((s["ace"] for s in live_slots), default=0) + 1)

    if (head_ctx or {}).get("mode") == "head":
        ensure_head_mode_support(pp)
        return head_mode_preview(
            pp, token, filename, size, slicer_colors, slicer_types,
            head_ctx, live_slots, plan_proxy, fuzzy=fuzzy, meta=meta)
    if multi_assignment_shape(head_ctx):
        # Multi with colour copies or a hand-fed head: the assignment
        # pipeline on the multi topology. Without either the classic
        # synthetic-T pipeline below runs, byte-identical to before.
        ensure_head_mode_support(pp)
        return head_mode_preview(
            pp, token, filename, size, slicer_colors, slicer_types,
            dict(head_ctx or {}, num_aces=num_aces), live_slots, plan_proxy,
            fuzzy=fuzzy, meta=meta)

    missing_mats = pp.check_material_availability(slicer_types, live_slots)

    out = {
        "token":         token,
        "filename":      filename,
        "size":          size,
        "num_aces":      num_aces,
        "slicer_colors": [
            {"t": t, "hex": (slicer_colors[t] or "").lower(),
             "name": pp.approx_color_name(slicer_colors[t]) or "",
             "material": slicer_types.get(t, "") or ""}
            for t in sorted(slicer_colors.keys())
        ],
        "live_slots": [
            {"ace": s["ace"], "slot": s["slot"],
             "material": s["material"], "color": s["color"],
             "name": pp.approx_color_name(s["color"]) or ""}
            for s in sorted(live_slots, key=lambda x: (x["ace"], x["slot"]))
        ],
        "missing_materials": missing_mats,
        "plans": {},
    }
    nz_groups, nz_mixed = nozzle_context(pp, meta, head_ctx)
    out["slicer"] = (meta or {}).get("slicer") or ""
    out["forca"] = bool((meta or {}).get("forca"))
    out["nozzles"] = {str(t): d
                      for t, d in ((meta or {}).get("nozzles") or {}).items()}
    out["head_nozzles"] = dict((head_ctx or {}).get("head_nozzles") or {})
    out["nozzles_mixed"] = nz_mixed
    if not missing_mats:
        remap, info, _ = pp.match_colors_to_slots(
            slicer_colors, live_slots, num_heads=4,
            filament_types=slicer_types,
            strict_color=False,
            fuzzy_max_distance=fuzzy,
            nozzle_groups=nz_groups,
        )
        mapping = mapping_from_info(info)
        proxy_remapped = pp.apply_remap(plan_proxy, remap) if remap else plan_proxy
        result = pp.plan_loadout(proxy_remapped, num_aces=num_aces) or {}
        # Original slicer-T toolchange sequence (original T-indices, same key
        # space as each plan's mapping[].t). The frontend replays it to recompute
        # the slicer-plan swap count after a manual reassignment with no round-trip.
        out["events"] = list(result.get("events") or [])
        for mode in ("slicer", "optimize", "layer"):
            out["plans"][mode] = build_one_plan(
                pp, mode, result, mapping,
                slicer_colors=slicer_colors, slicer_types=slicer_types,
                num_aces=num_aces, nozzle_groups=nz_groups)
    return out


# --------------------------------------------------------------------------- #
# rewrite pipeline (produce the print-ready gcode)                            #
# --------------------------------------------------------------------------- #

def _noop_stage(stage, percent):
    pass


def _noop_stage_cb(base, span):
    def cb(done, total):
        pass
    return cb


def rewrite_pipeline(pp, *, src_path, tmp_a, tmp_b, slicer_colors, slicer_types,
                     num_aces, live_slots, head_ctx, mode,
                     remap_override=None, head_assignment=None,
                     head_plan="loadout", fuzzy=DEFAULT_FUZZY,
                     set_stage=None, stage_cb=None, meta=None,
                     head_copies=None) -> str:
    """Run the rewrite pipeline on src_path, ping-ponging between tmp_a/tmp_b,
    and return the path holding the final print-ready gcode.

    Pure: operates only on file paths (real temp files in the backend, MEMFS
    paths under Pyodide) + the post-processor primitives. The caller handles
    the Moonraker upload and any SET_PRINT_PREFERENCES prepend afterwards.

    set_stage(stage, percent)  - coarse stage marker (optional).
    stage_cb(base, span) -> (done,total)->None - fine per-stage progress factory
    that the streaming *_to_file functions call (optional).
    Raises RuntimeError on an infeasible plan / missing material.
    """
    set_stage = set_stage or _noop_stage
    stage_cb  = stage_cb  or _noop_stage_cb

    # A2: a multi-colour file whose body cannot be found by any marker (no
    # '; Change Tool' AND no layer-change marker) stamps zero swaps and
    # would silently print one colour. Refuse it with an actionable message
    # instead of shipping it. Soft-degrades on an older post-processor.
    _detectable = getattr(pp, "file_body_detectable", None)
    if callable(_detectable) and len(slicer_colors or []) > 1 \
            and not _detectable(str(src_path)):
        raise RuntimeError(
            "This %d-colour file has no tool-change markers and no "
            "layer-change markers, so no tool changes can be detected - it "
            "would print as a single colour. The slicer's tool-change gcode "
            "('; Change Tool X -> Tool Y') is usually missing from the "
            "filament/printer profile. Check that field, then re-slice and "
            "upload the original export." % len(slicer_colors))
    # Defensive twin of the build_report refusal (an old client could reach
    # the print endpoint without the analyze step): never re-process an
    # already-processed file. First 512KB covers the auto-load block.
    _dp = getattr(pp, "detect_processed", None)
    if _dp is not None:
        try:
            with open(str(src_path), "r", encoding="utf-8",
                      errors="replace") as _f:
                _proc, _fmt = _dp(_f.read(512 * 1024))
        except OSError:
            _proc, _fmt = False, None
        if _proc:
            raise RuntimeError(
                "refusing to re-process: file is already multiACE-processed "
                "(%s) - upload the original slicer export"
                % ("format %d" % _fmt if _fmt is not None
                   else "older version"))
    num_aces = max(num_aces, max((s["ace"] for s in live_slots), default=0) + 1)

    if mode != "head":
        # Head mode pins colours to feeders too (materials not in an ACE slot),
        # so this ACE-slot-only availability check would false-fail there.
        missing_mats = pp.check_material_availability(slicer_types, live_slots)
        if missing_mats:
            raise RuntimeError(
                "required material(s) not loaded: " + ", ".join(missing_mats))

    if mode == "head":
        ensure_head_mode_support(pp)
        # mode 'head' = the assignment pipeline; in multi it runs on the
        # multi topology (a report with topology 'multi' prints this way).
        hm_multi = is_multi_ctx(head_ctx)
        if hm_multi:
            head_ctx = dict(head_ctx or {}, num_aces=num_aces)
        feeders = (manual_head_list(head_ctx) if hm_multi
                   else ((head_ctx or {}).get("feeders") or []))
        ace_heads, ace_head_of_ace, ace_num_of_head, feeder_heads = \
            head_maps(head_ctx, pp)
        hm_slots = (getattr(ace_head_of_ace, "num_aces", 4) if hm_multi
                    else 4)
        targets = head_mode_targets(pp, feeders, live_slots, ace_head_of_ace)
        # bg-enabled heads (for the optimizer AND the bg initial-load) - defined
        # for every head plan so the inject_auto_load call below always has it.
        hm_bg_heads = ([] if hm_multi else
                       [int(h) for h in ((head_ctx or {}).get("bg_heads") or [])])
        if head_plan in ("optimize", "layer", "color"):
            set_stage(head_plan, 1.0)
            hm_result = pp.plan_loadout_from_file(str(src_path), num_aces) or {}
            hm_events = list(hm_result.get("events") or [])
            hm_layer_sets = None
            if head_plan == "layer":
                lcs = (hm_result.get("layer_info") or {}).get(
                    "layer_color_sets") or []
                hm_layer_sets = [set(s) for s in lcs] if lcs else None
            # Bg-aware optimize (same context the preview used, so the
            # printed layout matches the previewed one) - soft-degrading.
            hm_times = None
            parse_tf = getattr(pp, "parse_toolchanges_with_times_from_file",
                               None)
            if parse_tf is not None:
                try:
                    ev_t, times = parse_tf(str(src_path))
                    if (list(ev_t) == hm_events
                            and any(t is not None for t in times)):
                        hm_times = times
                except Exception:
                    hm_times = None
            hm_matrix = None
            if head_plan == "color":
                _pfmf = getattr(pp, "parse_flush_matrix_from_file", None)
                if _pfmf is not None:
                    try:
                        hm_matrix = _pfmf(str(src_path))
                    except Exception:
                        hm_matrix = None
            try:
                assignment, _hm_swaps = pp.compute_head_mode_optimize(
                    hm_events, feeder_heads, ace_heads, ace_num_of_head,
                    hm_slots, layer_color_sets=hm_layer_sets,
                    event_times=hm_times, bg_heads=hm_bg_heads,
                    flush_matrix=hm_matrix,
                    objective=("color" if head_plan == "color" else "time"),
                    topology=ace_head_of_ace)
            except TypeError:
                assignment, _hm_swaps = pp.compute_head_mode_optimize(
                    hm_events, feeder_heads, ace_heads, ace_num_of_head,
                    hm_slots, layer_color_sets=hm_layer_sets)
            if assignment is None:
                raise RuntimeError(
                    "no feasible head-mode loadout for %s plan" % head_plan)
        elif head_assignment:
            assignment = assignment_from_target_ids(head_assignment, targets)
        else:
            layout = pp.compute_head_mode_layout(
                slicer_colors, slicer_types, feeders, live_slots,
                ace_head_of_ace, fuzzy_max_distance=fuzzy,
                nozzle_groups=nozzle_context(pp, meta, head_ctx)[0])
            assignment = layout["assignment"]

        # Colour copies (Multifilament-Optimierung). Proposed plans: the
        # same greedy over the slots the proposal leaves free as the preview
        # (preview == print). Loadout plan: the web sends the copies its
        # preview used verbatim; an older frontend sends none -> detect from
        # the loaded slots. max_copies 1 -> {} -> byte-identical rewrite.
        copies = {}
        if head_plan in ("optimize", "layer", "color"):
            if head_copies:
                # The preview planned its copies live (max-copies setting
                # of that moment); take them verbatim. Any slot of a wired
                # ACE is a legal copy target here, loaded or not.
                _all = getattr(pp, "_all_ace_slots", None)
                if _all is not None:
                    all_slots = [
                        {"id": "slot-%d-%d" % (int(a), int(si)),
                         "kind": "ace", "head": int(h), "ace": int(a),
                         "slot": int(si)}
                        for a, si, h in _all(ace_head_of_ace, hm_slots)]
                else:
                    all_slots = [
                        {"id": "slot-%d-%d" % (int(a), si), "kind": "ace",
                         "head": int(h), "ace": int(a), "slot": si}
                        for a, h in (ace_head_of_ace or {}).items()
                        for si in range(4)]
                copies = copies_from_target_ids(head_copies, assignment,
                                                all_slots, head_ctx)
            else:
                copies = proposal_copies(
                    pp, hm_events, assignment, head_ctx, ace_head_of_ace,
                    hm_slots, hm_bg_heads, nozzle_context(pp, meta, head_ctx)[0])
        else:
            if head_copies:
                copies = copies_from_target_ids(head_copies, assignment,
                                                targets, head_ctx)
            else:
                copies = color_copies(
                    pp, assignment, slicer_colors, slicer_types, live_slots,
                    ace_head_of_ace, head_ctx,
                    nozzle_context(pp, meta, head_ctx)[0])

        set_stage("rewrite", 10.0)
        _pc = bool((head_ctx or {}).get("pickup_cleaning"))
        try:
            if copies:
                try:
                    pp.rewrite_head_mode_to_file(
                        str(src_path), str(tmp_a), assignment, None,
                        stage_cb(10.0, 60.0), pickup_cleaning=_pc,
                        copies=copies, bg_heads=hm_bg_heads,
                        bg_stamps=not hm_multi)
                except TypeError:
                    # Older post-processor: plan without the bg tie-break.
                    pp.rewrite_head_mode_to_file(
                        str(src_path), str(tmp_a), assignment, None,
                        stage_cb(10.0, 60.0), pickup_cleaning=_pc,
                        copies=copies)
            else:
                try:
                    pp.rewrite_head_mode_to_file(
                        str(src_path), str(tmp_a), assignment, None,
                        stage_cb(10.0, 60.0), pickup_cleaning=_pc,
                        bg_stamps=not hm_multi)
                except TypeError:
                    pp.rewrite_head_mode_to_file(
                        str(src_path), str(tmp_a), assignment, None,
                        stage_cb(10.0, 60.0), pickup_cleaning=_pc)
        except TypeError:
            # Older post-processor without pickup_cleaning/copies - no stamps,
            # one target per colour.
            pp.rewrite_head_mode_to_file(
                str(src_path), str(tmp_a), assignment, None,
                stage_cb(10.0, 60.0))
        cur, nxt = tmp_a, tmp_b

        set_stage("inject_auto_load", 70.0)
        try:
            pp.inject_auto_load_to_file(
                str(cur), str(nxt), stage_cb(70.0, 12.0), set(ace_heads),
                bg_heads=set(hm_bg_heads))
        except TypeError:
            # Older post-processor without the bg_heads param (bg initial-load
            # not available on this build) - soft-degrade to the inline block.
            pp.inject_auto_load_to_file(
                str(cur), str(nxt), stage_cb(70.0, 12.0), set(ace_heads))
        cur, nxt = nxt, cur
        return str(cur)

    # ------- multi mode (slicer / optimize / layer) -------
    if mode == "slicer":
        if remap_override is not None:
            # User-edited slot assignment from the web - verbatim so the print
            # matches the preview. Keys/values arrive as JSON strings.
            remap = {}
            for k, v in remap_override.items():
                try:
                    ik, iv = int(k), int(v)
                except (TypeError, ValueError):
                    continue
                if 0 <= iv <= 15 and ik != iv:
                    remap[ik] = iv
        else:
            remap, _info, _ = pp.match_colors_to_slots(
                slicer_colors, live_slots, num_heads=4,
                filament_types=slicer_types,
                strict_color=False,
                fuzzy_max_distance=fuzzy,
                nozzle_groups=nozzle_context(pp, meta, head_ctx)[0],
            )
    else:
        set_stage(mode, 1.0)
        sa_result = pp.plan_loadout_from_file(str(src_path), num_aces) or {}
        sa_events = sa_result.get("events") or []
        sa_layer_sets = None
        if mode == "layer":
            lcs = (sa_result.get("layer_info") or {}).get("layer_color_sets") or []
            sa_layer_sets = [set(s) for s in lcs] if lcs else None
        c2h, _sa_swaps = _swap_aware(
            pp, sa_events, num_aces=num_aces,
            layer_color_sets=sa_layer_sets,
            allowed_heads=nozzle_context(pp, meta, head_ctx)[0])
        if c2h is None:
            raise RuntimeError("no feasible head assignment for %s mode" % mode)
        head_ace_counter = {h: 0 for h in range(4)}
        remap = {}
        for c in sorted(c2h.keys(), key=lambda x: (c2h[x], x)):
            h = c2h[c]
            remap[c] = head_ace_counter[h] * 4 + h
            head_ace_counter[h] += 1

    set_stage("apply_remap", 5.0)
    pp.apply_remap_to_file(str(src_path), str(tmp_a), remap, stage_cb(5.0, 25.0))
    cur, nxt = tmp_a, tmp_b

    set_stage("rewrite", 45.0)
    try:
        pp.rewrite_to_file(
            str(cur), str(nxt), stage_cb(45.0, 30.0),
            pickup_cleaning=bool((head_ctx or {}).get("pickup_cleaning")))
    except TypeError:
        pp.rewrite_to_file(str(cur), str(nxt), stage_cb(45.0, 30.0))
    cur, nxt = nxt, cur

    set_stage("inject_auto_load", 75.0)
    pp.inject_auto_load_to_file(str(cur), str(nxt), stage_cb(75.0, 10.0))
    cur, nxt = nxt, cur
    return str(cur)
