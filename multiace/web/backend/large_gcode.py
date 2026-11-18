# SPDX-License-Identifier: GPL-3.0-or-later
# FLEXI-INTEGRATION: dedicated large-file engine. This module is imported by
# main.py and is never used by Normal, Multi or Head dispatch.
"""Resource-bounded large G-code analysis and Flexi preparation.

The previous Flexi upload path read the whole print into RAM and silently
submitted the original file.  This module keeps the authoritative planner
primitives from post_process_virtual_toolheads.py, but feeds them only the
events they need:

* the file is scanned in fixed-size binary chunks;
* only a bounded metadata head/tail and the compact planning event stream are
  retained (tool changes, layer boundaries, layer colour sets);
* long lines are scanned from a bounded prefix but never truncated in the
  prepared file;
* rewriting is line-by-line, byte-preserving outside the selected tool/heat
  references, and is committed atomically;
* published output is verified against the source event stream and the
  prepared SHA-256.

No printer, Moonraker or file-system publication side effect lives here.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
from __future__ import annotations

from array import array
from collections import Counter, deque
from dataclasses import dataclass, field
import hashlib
from math import comb
import os
from pathlib import Path
import re
import shutil
import tempfile

import preflight_core

CHUNK_SIZE = 1024 * 1024
MAX_SCAN_PREFIX = 64 * 1024
SUPPORTED_TOOL_MAX = 31

_TOOLCHANGE_RE = re.compile(r"^\s*;\s*Change Tool\s*(\d+)\s*->\s*Tool\s*(\d+)")
_LAYER_RE = re.compile(r"^\s*;\s*LAYER_CHANGE\b", re.IGNORECASE)
_BARE_T_RE = re.compile(r"^\s*T(\d{1,2})\s*(?:A0)?\s*(?:;.*)?$")
_M104_RE = re.compile(r"^\s*M10[49]\b", re.IGNORECASE)
_PREEXTRUDE_RE = re.compile(
    r"^\s*SM_PRINT_PREEXTRUDE_FILAMENT\s+INDEX=(\d{1,2})\b", re.IGNORECASE)
_PROCESSED_RE = re.compile(
    r"^\s*;\s*multiACE\s+(?:processed:|auto-load:)", re.IGNORECASE)
_EOL_RE = re.compile(r"(\r\n|\r|\n)$")
_FILAMENT_LINE_RE = re.compile(r"^;\s*filament_(?:colour|color|type)\s*=",
                               re.IGNORECASE)


class LargeGcodeError(ValueError):
    """The input cannot be planned or prepared safely."""


class PreparationCancelled(LargeGcodeError):
    pass


@dataclass(frozen=True)
class ScanLine:
    number: int
    prefix: bytes
    length: int
    truncated: bool


@dataclass
class ScanResult:
    size: int
    sha256: str
    lines: int
    max_line_bytes: int
    truncated_lines: int
    slicer_colors: dict
    slicer_types: dict
    num_aces: int
    meta: dict
    tools: list
    tool_counts: dict
    events: list
    layer_color_sets: list
    layer_changes: int
    tool_commands: int
    heater_commands: int
    preextrude_commands: int
    processed: bool
    meta_text: str


@dataclass
class PlanResult:
    plan: str
    feasible: bool
    mapping: list
    swaps: int
    reason: str = ""
    tool_changes: int = 0


@dataclass
class FlexiReport:
    token: str
    filename: str
    scan: ScanResult
    sources: list
    plans: dict
    missing_materials: list
    events: list
    layer_changes: int
    nozzles_mixed: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class PreparationResult:
    source_path: str
    output_path: str
    source_sha256: str
    output_sha256: str
    source_size: int
    output_size: int
    source_lines: int
    output_lines: int
    tool_commands: int
    mapping: dict
    validation: dict

    @property
    def prepared(self) -> bool:
        return True


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _iter_scan_lines(path: str | Path, progress=None,
                     max_prefix: int = MAX_SCAN_PREFIX, state=None):
    """Yield bounded-prefix lines while hashing the entire file.

    The scanner handles CRLF, LF, bare CR and an unterminated final line.  It
    never holds more than one chunk plus ``max_prefix`` of a pathological line;
    the omitted middle is counted in ``line.length`` and does not affect the
    file hash (the hash is updated from every raw chunk).
    """
    total = os.path.getsize(path)
    digest = hashlib.sha256()
    pending = b""
    dropped = 0
    line_no = 0
    consumed = 0
    skip_lf = False
    last_progress = -1
    max_len = 0
    truncated_lines = 0

    def emit(raw: bytes, length: int, number: int):
        nonlocal max_len, truncated_lines
        if length > max_len:
            max_len = length
        cut = raw[:max_prefix]
        truncated = length > len(cut)
        if truncated:
            truncated_lines += 1
        return number, cut, length, truncated

    with open(path, "rb") as source:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            consumed += len(chunk)
            digest.update(chunk)
            if skip_lf and chunk.startswith(b"\n"):
                chunk = chunk[1:]
            skip_lf = False
            data = pending + chunk
            start = 0
            for match in re.finditer(rb"\r\n|\r|\n", data):
                raw = data[start:match.start()]
                length = dropped + len(raw)
                line_no += 1
                yield emit(raw, length, line_no)
                dropped = 0
                start = match.end()
                if match.group() == b"\r" and start == len(data):
                    # A CR at the chunk boundary may be the first half of a
                    # CRLF in the next chunk.  It is already one line ending;
                    # suppress the following LF only.
                    skip_lf = True
            pending = data[start:]
            if len(pending) > max_prefix:
                dropped += len(pending) - max_prefix
                pending = pending[:max_prefix]
            if progress is not None and total:
                pct = int(consumed * 100 / total)
                if pct != last_progress:
                    last_progress = pct
                    progress(min(consumed, total), total)
        if pending or dropped:
            line_no += 1
            yield emit(pending, dropped + len(pending), line_no)
        if progress is not None:
            progress(total, total)
        # Caller-local state, never function attributes: concurrent preflight
        # jobs must not overwrite one another's file identity.
        if state is not None:
            state["sha256"] = digest.hexdigest()
            state["max_line_bytes"] = max_len
            state["truncated_lines"] = truncated_lines


def scan_file(pp, path: str | Path, progress=None) -> ScanResult:
    """One streaming pass for metadata and the complete planning event stream."""
    head = []
    tail = deque(maxlen=2000)
    colors = {}
    types = {}
    tools = []
    counts = Counter()
    tool_ids = array("H")
    events = array("H")
    layer_sets = []
    current_layer = None
    current_tool = None
    line_count = 0
    layer_changes = 0
    saw_change_comment = False
    pending_change_comment = False
    tool_count = 0
    heater_count = 0
    preextrude_count = 0
    processed = False

    stream_state = {}
    for line_count, raw, length, truncated in _iter_scan_lines(
            path, progress=progress, state=stream_state):
        if line_count <= 300:
            head.append(_decode(raw) + "\n")
        else:
            tail.append(raw + b"\n")
        stripped = raw.lstrip()
        if stripped.startswith(b";"):
            if b"multiACE processed:" in raw or b"multiACE auto-load:" in raw:
                processed = True
            if b"Change Tool" in raw:
                text = _decode(raw)
                change = _TOOLCHANGE_RE.match(text)
                if change:
                    saw_change_comment = True
                    pending_change_comment = True
                    before, after = int(change.group(1)), int(change.group(2))
                    for t in (before, after):
                        if t not in counts:
                            tools.append(t)
                        counts[t] += 1
                    events.append(after)
                    current_tool = after
                    if current_layer is not None:
                        current_layer.add(after)
                    continue
            if b"LAYER_CHANGE" in raw:
                text = _decode(raw)
                if _LAYER_RE.match(text):
                    layer_changes += 1
                    if current_layer is not None:
                        layer_sets.append(current_layer)
                    current_layer = set()
                    if current_tool is not None:
                        current_layer.add(current_tool)
                    continue
            continue
        if stripped.startswith(b"T") and len(stripped) > 1 and stripped[1:2].isdigit():
            text = _decode(raw)
            bare = _BARE_T_RE.match(text)
            if bare:
                t = int(bare.group(1))
                tool_count += 1
                tool_ids.append(t)
                if t not in counts:
                    tools.append(t)
                counts[t] += 1
                current_tool = t
                if current_layer is not None:
                    current_layer.add(t)
                if not pending_change_comment:
                    events.append(t)
                pending_change_comment = False
                continue
        if stripped.startswith(b"M10") and (stripped.startswith(b"M104")
                                           or stripped.startswith(b"M109")):
            heater_count += 1
        elif stripped.startswith(b"SM_PRINT_PREEXTRUDE_FILAMENT"):
            preextrude_count += 1
    if current_layer is not None:
        layer_sets.append(current_layer)
    if not saw_change_comment:
        events = array("H", [tool_ids[i] for i in range(len(tool_ids))])

    meta_text = "".join(head) + b"".join(tail).decode("utf-8", "replace")
    slicer_colors = pp.parse_color_names(meta_text)
    slicer_types = pp.parse_filament_types(meta_text)
    num_aces = pp.infer_num_aces(meta_text)
    used = set(tools)
    if used:
        slicer_colors = {t: c for t, c in slicer_colors.items() if t in used}
        slicer_types = {t: m for t, m in slicer_types.items() if t in used}
    meta = {"slicer": "", "forca": False, "nozzles": {}}
    try:
        meta["slicer"] = pp.parse_slicer_name(meta_text)
        meta["nozzles"] = pp.parse_nozzle_diameters(meta_text)
        meta["forca"] = bool(meta["slicer"]) and pp.is_forca_slicer(meta["slicer"])
    except AttributeError:
        pass
    # get the generator's final attributes after it is exhausted
    return ScanResult(
        size=os.path.getsize(path),
        sha256=stream_state["sha256"],
        lines=line_count,
        max_line_bytes=stream_state["max_line_bytes"],
        truncated_lines=stream_state["truncated_lines"],
        slicer_colors=slicer_colors,
        slicer_types=slicer_types,
        num_aces=num_aces,
        meta=meta,
        tools=sorted(tools),
        tool_counts={str(k): v for k, v in sorted(counts.items())},
        events=list(events),
        layer_color_sets=[set(s) for s in layer_sets],
        layer_changes=layer_changes,
        tool_commands=tool_count,
        heater_commands=heater_count,
        preextrude_commands=preextrude_count,
        processed=processed,
        meta_text=meta_text,
    )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_rows(flexi_state: dict, live_slots=None) -> list:
    """Routed, physically present ACE sources with authoritative metadata."""
    production = (flexi_state or {}).get("production") or {}
    routes = (flexi_state or {}).get("routes") or {}
    targets = production.get("tool_targets") or {}
    by_source = {}
    for key, head in routes.items():
        try:
            ace, slot = (int(v) for v in key.split(":", 1))
            head = int(head)
        except (TypeError, ValueError):
            continue
        if head not in range(4):
            continue
        by_source[(ace, slot)] = {
            "key": key, "ace": ace, "slot": slot, "head": head,
            "material": "", "color": "", "present": None,
            "validated_actions": [],
        }
    for row in production.get("inventory") or []:
        try:
            ace, slot = row.get("source", [None, None])[:2]
            key = row.get("key") or "%d:%d" % (int(ace), int(slot))
            if key not in routes:
                continue
            target = by_source[(int(ace), int(slot))]
            target.update({
                "material": (row.get("material") or "").strip(),
                "color": (row.get("color") or "").strip().lower(),
                "present": row.get("present"),
                "validated_actions": list(row.get("validated_actions") or []),
            })
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    # Older callers may provide only the regular live-slot list.
    for row in live_slots or []:
        try:
            ace, slot = int(row["ace"]), int(row["slot"])
            key = "%d:%d" % (ace, slot)
            if key not in routes:
                continue
            target = by_source[(ace, slot)]
            target["material"] = target["material"] or (row.get("material") or "").strip()
            target["color"] = target["color"] or (row.get("color") or "").strip().lower()
        except (KeyError, TypeError, ValueError):
            continue
    for source, target in by_source.items():
        # Live Flexi routes address the four physical heads as T0-T3.
        # Older records may also expose virtual T4+ entries; prefer the
        # physical head that the native print path selects.
        matches = []
        for raw, entry in targets.items():
            try:
                if tuple(entry.get("source") or ()) == source:
                    matches.append(int(raw))
            except (TypeError, ValueError):
                continue
        physical = [t for t in matches if 0 <= t <= 3]
        tool = min(physical) if physical else (min(matches) if matches else None)
        target["virtual_tool"] = tool if tool is not None else target["head"]
        target["target_source"] = source
    return [by_source[k] for k in sorted(by_source)]


def _matcher_slot(source: dict, index: int) -> dict:
    # The matcher uses slot==head for its nozzle gate and
    # synthetic_T=ace*4+slot for identity.  Keep the real routed source on
    # private keys and give every candidate a unique synthetic ACE.
    return {
        "ace": 100 + index,
        "slot": source["head"],
        "material": source["material"],
        "color": source["color"],
        "_source": (source["ace"], source["slot"]),
        "_head": source["head"],
        "_virtual": source["virtual_tool"],
    }


def _material_matches(want: str, have: str) -> bool:
    want = (want or "").strip().lower()
    have = (have or "").strip().lower()
    return not want or not have or want == have


def _tool_maps(pp, slicer_colors, slicer_types, sources, fuzzy=30,
               nozzle_groups=None):
    """Return (mapping, info) using the original tier-major colour matcher."""
    slots = [_matcher_slot(s, i) for i, s in enumerate(sources)]
    if not slots:
        return {}, {}
    remap, info, _used = pp.match_colors_to_slots(
        slicer_colors, slots, num_heads=4, filament_types=slicer_types,
        strict_color=False, fuzzy_max_distance=fuzzy,
        nozzle_groups=nozzle_groups)
    mapping = {}
    for t, entry in info.items():
        slot = entry.get("slot")
        if not slot:
            continue
        mapping[t] = {
            "t": t,
            "slot": {
                "ace": slot["_source"][0], "slot": slot["_source"][1],
                "material": slot.get("material") or "",
                "color": slot.get("color") or "",
            },
            "head": slot["_head"],
            "virtual_tool": slot["_virtual"],
            "tier": entry.get("tier") or "",
        }
    return mapping, info


def _simulate_swaps(events, mapping):
    current = {}
    swaps = 0
    for t in events:
        row = mapping.get(t)
        if not row:
            continue
        head = row["head"]
        src = (row["slot"]["ace"], row["slot"]["slot"])
        if head not in current:
            current[head] = src
        elif current[head] != src:
            swaps += 1
            current[head] = src
    return swaps


def _assign_heads(pp, c2h, slicer_colors, slicer_types, sources, fuzzy,
                  nozzle_groups=None):
    """Resolve a colour->head assignment to concrete routed sources."""
    by_head = {}
    for source in sources:
        by_head.setdefault(source["head"], []).append(source)
    mapping = {}
    grouped = {}
    for t, head in c2h.items():
        grouped.setdefault(int(head), []).append(t)
    for head, colors in sorted(grouped.items()):
        candidates = [s for s in by_head.get(head, []) if s["present"] is not False]
        for t in sorted(colors):
            allowed = None
            if nozzle_groups:
                allowed = nozzle_groups.get(t)
            candidates_for_t = [
                s for s in candidates
                if _material_matches(slicer_types.get(t, ""), s.get("material"))
                and (allowed is None or s["head"] in allowed)
            ]
            if not candidates_for_t:
                return {}
            slots = [_matcher_slot(s, i) for i, s in enumerate(candidates_for_t)]
            _remap, info, _used = pp.match_colors_to_slots(
                {t: slicer_colors.get(t, "")}, slots, num_heads=4,
                filament_types={t: slicer_types.get(t, "")},
                strict_color=False, fuzzy_max_distance=fuzzy,
                nozzle_groups=nozzle_groups)
            row = info.get(t) or {}
            chosen = row.get("slot")
            if not chosen:
                return {}
            mapping[t] = {
                "t": t,
                "slot": {
                    "ace": chosen["_source"][0], "slot": chosen["_source"][1],
                    "material": chosen.get("material") or "",
                    "color": chosen.get("color") or "",
                },
                "head": head,
                "virtual_tool": chosen["_virtual"],
                "tier": row.get("tier") or "",
            }
            candidates.remove(next(
                s for s in candidates
                if (s["ace"], s["slot"]) == tuple(chosen["_source"])))
    return mapping


def build_report(pp, scan: ScanResult, flexi_state: dict, *, token: str,
                 filename: str, live_slots=None, head_ctx=None, fuzzy=30):
    """Build the Flexi report and its three original-planner plans."""
    if scan.processed:
        raise preflight_core.PreflightRejected(
            "This file has already been processed by multiACE, so it is "
            "ready to print as it is - upload it in Fluidd.")
    if not scan.tools:
        raise LargeGcodeError("Print file has no explicit supported tool selection")
    sources = _source_rows(flexi_state, live_slots=live_slots)
    sources = [s for s in sources if s["present"] is not False]
    if not sources:
        raise LargeGcodeError("No routed ACE source is available in Flexi mode")
    nozzle_groups, nozzles_mixed = preflight_core.nozzle_context(
        pp, scan.meta, head_ctx or {})
    colors = dict(scan.slicer_colors)
    types = dict(scan.slicer_types)
    # Every slicer tool used by the file must have a colour identity.  Falling
    # back to a source colour would invent an assignment the user never made.
    missing_identity = [t for t in scan.tools if t not in colors]
    if missing_identity:
        raise LargeGcodeError(
            "The slicer header does not declare colours for T%s"
            % ", T".join(str(t) for t in missing_identity))

    # Keep the compact array here.  Converting a multi-million-event 1 GiB
    # scan to a Python list would defeat the bounded-memory design.
    events = scan.events if len(scan.events) else scan.tools
    slicer_map, _info = _tool_maps(
        pp, colors, types, sources, fuzzy=fuzzy, nozzle_groups=nozzle_groups)
    plans = {}

    def plan_row(name, mapping, swaps, reason=""):
        plans[name] = PlanResult(
            plan=name, feasible=bool(mapping) and not reason,
            mapping=[mapping[t] for t in sorted(mapping)],
            swaps=int(swaps or 0), reason=reason,
            tool_changes=len(events)).__dict__

    slicer_reason = ""
    if set(slicer_map) != set(scan.tools):
        slicer_reason = "the loaded/routed sources cannot cover every slicer tool"
    plan_row("slicer", slicer_map, _simulate_swaps(events, slicer_map),
            slicer_reason)

    capacity = max((sum(1 for s in sources if s["head"] == h)
                    for h in range(4)), default=0)
    allowed_heads = None
    if nozzle_groups or len({s["head"] for s in sources}) < 4:
        allowed_heads = {}
        for t in colors:
            heads = {s["head"] for s in sources
                     if _material_matches(types.get(t, ""), s.get("material"))}
            if nozzle_groups and nozzle_groups.get(t) is not None:
                heads &= set(nozzle_groups.get(t))
            allowed_heads[t] = heads
    # The planner now uses exact subset DP over candidate colour groups,
    # not a 4**N head-assignment sweep.  Its event-dependent work is one
    # filtered-subsequence scan per candidate group.  Keep the full event
    # sequence while that product is practical; only enormous files fall
    # back to a representative sampling for CHOOSING the assignment.  The
    # exact full-sequence swap count is still computed below, and no
    # planning/execution record is dropped from the file or output.
    planner_events = events
    planner_layers = scan.layer_color_sets or None
    bounded_search = False
    capacity_for_search = max(1, capacity)
    candidate_groups = sum(
        comb(len(colors), size)
        for size in range(1, min(capacity_for_search, len(colors)) + 1))
    exact_event_budget = 60_000_000
    if len(events) * candidate_groups > exact_event_budget:
        bounded_search = True
        # Retain the opening sequence so every colour appears at least once;
        # a plain stride can alias with a repeating 4-tool pattern and omit
        # one colour from the assignment search entirely.
        budget = min(len(events), max(
            2000, min(20000,
                     exact_event_budget // max(1, candidate_groups))))
        if budget <= 4000:
            planner_events = events[:budget]
        else:
            prefix = events[:4000]
            stride = max(1, (len(events) - 4000) // (budget - 4000))
            planner_events = prefix + events[4000::stride][:budget - 4000]
        present = set(planner_events)
        for color in colors:
            if color not in present:
                try:
                    planner_events.append(events[events.index(color)])
                except ValueError:
                    pass
    for name, layer_sets in (
            ("optimize", None),
            ("layer", planner_layers)):
        if name == "layer" and not scan.layer_color_sets:
            plans[name] = PlanResult(
                name, False, [], 0, "no layer markers", len(events)).__dict__
            continue
        try:
            c2h, _search_swaps = pp.compute_swap_aware_layout(
                planner_events, num_aces=max(1, capacity),
                layer_color_sets=layer_sets,
                allowed_heads=allowed_heads)
        except Exception as exc:
            c2h = None
            reason = "planner error: %s" % exc
        else:
            reason = "" if c2h is not None else "no feasible Flexi routing layout"
        if c2h is None and name == "layer" and scan.layer_color_sets:
            max_layer_colors = max(len(layer) for layer in scan.layer_color_sets)
            if max_layer_colors > 4:
                reason = ("some layer uses %d colours; layer-only swaps need "
                          "at most 4 per layer" % max_layer_colors)
        if c2h is None:
            plans[name] = PlanResult(
                name, False, [], 0, reason, len(events)).__dict__
            continue
        if name == "layer":
            conflict = any(
                len({c2h[t] for t in layer if t in c2h}) < len(
                    {t for t in layer if t in c2h})
                for layer in scan.layer_color_sets)
            if conflict:
                plans[name] = PlanResult(
                    name, False, [], 0,
                    "the bounded planner search found a layer conflict",
                    len(events)).__dict__
                continue
        mapping = _assign_heads(
            pp, c2h, colors, types, sources, fuzzy=fuzzy,
            nozzle_groups=nozzle_groups)
        if set(mapping) != set(colors):
            plans[name] = PlanResult(
                name, False, [], 0,
                "routed sources cannot realise the complete planner assignment",
                len(events)).__dict__
            continue
        plan_row(name, mapping, _simulate_swaps(events, mapping))
    plans["planner_search"] = {
        "bounded": bounded_search,
        "events_scanned": len(events),
        "events_used_for_assignment": len(planner_events),
        "note": ("All events are retained and the exact full-sequence swap "
                 "count is used. The exact subset-DP head assignment scans "
                 "a representative event sample only on very large files."),
    }

    missing = []
    for t, material in sorted(types.items()):
        want = (material or "").strip().lower()
        if not want:
            continue
        if not any(_material_matches(want, s.get("material")) for s in sources):
            missing.append(want)
    return FlexiReport(
        token=token, filename=filename, scan=scan, sources=sources,
        plans=plans, missing_materials=sorted(set(missing)), events=events,
        layer_changes=scan.layer_changes, nozzles_mixed=nozzles_mixed,
        meta=scan.meta)


def report_payload(report: FlexiReport, extra=None, flexi_state=None) -> dict:
    scan = report.scan
    # A 1 GiB file can contain a very large number of tool changes. The plan
    # cards only need the sequence for display; planning already used the full
    # sequence and the browser does not need to receive all of it.
    events = list(report.events)
    events_truncated = False
    if len(events) > 10000:
        events = list(events[:5000]) + list(events[-5000:])
        events_truncated = True
    payload = {
        "token": report.token,
        "filename": report.filename,
        "size": scan.size,
        "sha256": scan.sha256,
        "flexi": True,
        "topology": "flexi",
        "head_mode": False,
        "slicer": report.meta.get("slicer") or "",
        "forca": bool(report.meta.get("forca")),
        "nozzles": {str(k): v for k, v in
                    (report.meta.get("nozzles") or {}).items()},
        "nozzles_mixed": report.nozzles_mixed,
        "num_aces": scan.num_aces,
        "slicer_colors": [
            {"t": t, "hex": (scan.slicer_colors.get(t) or "").lower(),
             "name": "", "material": scan.slicer_types.get(t, "") or ""}
            for t in sorted(scan.slicer_colors)],
        "live_slots": [
            {"ace": s["ace"], "slot": s["slot"],
             "material": s.get("material") or "",
             "color": s.get("color") or "",
             "head": s["head"], "virtual_tool": s["virtual_tool"]}
            for s in report.sources],
        "missing_materials": report.missing_materials,
        "events": events,
        "events_total": len(report.events),
        "events_truncated": events_truncated,
        "layer_changes": report.layer_changes,
        "tool_commands": scan.tool_commands,
        "tools": scan.tools,
        "plans": report.plans,
        "planner_search": report.plans.get("planner_search"),
        "routes": {s["key"]: s["head"] for s in report.sources},
        "targets": [
            {"id": s["key"], "kind": "ace", "ace": s["ace"],
             "slot": s["slot"], "head": s["head"],
             "material": s.get("material") or "",
             "color": s.get("color") or "", "name": ""}
            for s in report.sources
        ],
        "flexi_block": preflight_core.flexi_topology_block(flexi_state or {}),
        "requires_live_checks": True,
        "prepared": False,
    }
    if extra:
        payload.update(extra)
    return payload


def plan_manifest(report: FlexiReport) -> dict:
    return {
        "version": 1,
        "token": report.token,
        "filename": report.filename,
        "source_sha256": report.scan.sha256,
        "source_size": report.scan.size,
        "tool_commands": report.scan.tool_commands,
        "lines": report.scan.lines,
        "layer_changes": report.scan.layer_changes,
        "heater_commands": report.scan.heater_commands,
        "preextrude_commands": report.scan.preextrude_commands,
        "plans": {
            name: {str(row["t"]): "%d:%d" % (
                row["slot"]["ace"], row["slot"]["slot"])
                for row in plan["mapping"]}
            for name, plan in report.plans.items()
            if name in ("slicer", "optimize", "layer")},
    }


def write_plan_manifest(path: str | Path, report: FlexiReport) -> None:
    import json
    data = plan_manifest(report)
    target = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".plan-", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_plan_manifest(path: str | Path) -> dict:
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise LargeGcodeError("Prepared plan manifest is invalid")
    return data


def _tool_for_source(source: str, flexi_state: dict) -> int:
    production = (flexi_state or {}).get("production") or {}
    try:
        ace, slot = (int(v) for v in source.split(":", 1))
    except (TypeError, ValueError):
        raise LargeGcodeError("Invalid Flexi source %r" % (source,))
    matches = []
    for raw, entry in (production.get("tool_targets") or {}).items():
        try:
            if tuple(entry.get("source") or ()) == (ace, slot):
                matches.append(int(raw))
        except (TypeError, ValueError):
            continue
    physical = [t for t in matches if 0 <= t <= 3]
    if physical:
        return min(physical)
    if matches:
        return min(matches)
    # A saved route always names a physical head.  Use that head rather than
    # inventing a virtual T4+ identity the native print path cannot select.
    head = ((flexi_state or {}).get("routes") or {}).get(source)
    try:
        tool = int(head)
    except (TypeError, ValueError):
        tool = None
    if tool is None or not 0 <= tool <= SUPPORTED_TOOL_MAX:
        raise LargeGcodeError("Routed source has no authoritative Flexi tool")
    return tool


def resolve_mapping(manifest: dict, plan: str, flexi_state: dict,
                    override=None) -> dict:
    if plan not in ("slicer", "optimize", "layer"):
        raise LargeGcodeError("Unknown Flexi plan %r" % (plan,))
    raw = dict((manifest.get("plans") or {}).get(plan) or {})
    if override and plan == "slicer":
        # A manual source edit is meaningful only for the as-loaded slicer
        # plan.  Optimize/layer derive their own head assignment and must not
        # be silently replaced by the slicer table.
        raw.update({str(k): str(v) for k, v in override.items()})
    if not raw:
        raise LargeGcodeError("The selected Flexi plan has no assignments")
    mapping = {}
    routes = (flexi_state or {}).get("routes") or {}
    for key, source in raw.items():
        try:
            t = int(key)
        except (TypeError, ValueError):
            raise LargeGcodeError("Invalid slicer tool in plan")
        if source not in routes:
            raise LargeGcodeError("Selected source %s has no saved route" % source)
        mapping[t] = {"source": source, "head": int(routes[source]),
                      "tool": _tool_for_source(source, flexi_state)}
    return mapping


def _split_ending(line: str):
    match = _EOL_RE.search(line)
    if not match:
        return line, ""
    return line[:match.start()], match.group(1)


def _heater_has_only_zero_setpoints(body: str) -> bool:
    """True only for an explicit S/R command that cannot energize a heater."""
    values = re.findall(
        r"\b(?:S|R)\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
        body.partition(";")[0], re.IGNORECASE)
    return bool(values) and all(float(value) == 0 for value in values)


def _replace_heater_tools(body: str, tool_heads: dict) -> str:
    code, sep, comment = body.partition(";")
    def repl(match):
        t = int(match.group(1))
        if t not in tool_heads:
            # Some slicers emit an initial M104 S0 for every configured tool,
            # including tools that never print.  There is no colour source to
            # assign, but dropping the line would break the byte/line-count
            # proof.  Retarget a zero-only command to a routed head: it cannot
            # heat anything, while a non-zero command for an unselected tool
            # still fails closed.
            if _heater_has_only_zero_setpoints(body):
                return "T%d" % min(tool_heads.values())
            raise LargeGcodeError(
                "M104/M109 references T%d, which has no selected Flexi source" % t)
        return "T%d" % tool_heads[t]
    code = re.sub(r"T(\d{1,2})\b", repl, code)
    return code + (sep + comment if sep else "")


def _iter_raw_lines(path, max_line: int = 16 * 1024 * 1024, progress=None):
    """Yield complete raw lines and their exact terminators.

    Preparation must not truncate a long line.  The bound only guards against a
    pathological file with no line ending; normal G-code lines are far below it.
    """
    total = os.path.getsize(path)
    pending = b""
    skip_lf = False
    consumed = 0
    last_progress = -1
    with open(path, "rb") as source:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            consumed += len(chunk)
            if skip_lf and chunk.startswith(b"\n"):
                chunk = chunk[1:]
            skip_lf = False
            data = pending + chunk
            start = 0
            for match in re.finditer(rb"\r\n|\r|\n", data):
                raw = data[start:match.start()]
                ending = match.group()
                if len(raw) > max_line:
                    raise LargeGcodeError(
                        "G-code line exceeds the %d byte preparation bound"
                        % max_line)
                yield raw, ending
                start = match.end()
                if ending == b"\r" and start == len(data):
                    skip_lf = True
            pending = data[start:]
            if len(pending) > max_line:
                raise LargeGcodeError(
                    "G-code line exceeds the %d byte preparation bound"
                    % max_line)
            if progress is not None and total:
                pct = int(consumed * 100 / total)
                if pct != last_progress:
                    last_progress = pct
                    progress(min(consumed, total), total)
        if pending:
            yield pending, b""
    if progress is not None:
        progress(total, total)


def prepare_file(src_path: str | Path, out_path: str | Path, mapping: dict,
                 *, flexi_state: dict, prefs_line: str = "", header: str = "",
                 progress=None, cancelled=None) -> PreparationResult:
    """Stream a byte-preserving prepared derivative and commit it atomically.

    ``mapping`` maps each original slicer tool to ``{source, head, tool}``.
    The source is never modified.  A cancellation or any parse/mapping error
    removes the temporary output and leaves no partial prepared file.
    """
    total = os.path.getsize(src_path)
    stat = shutil.disk_usage(Path(out_path).parent)
    if stat.free < total + 16 * 1024 * 1024:
        raise LargeGcodeError("Insufficient space for prepared G-code")
    tool_heads = {int(t): int(row["head"]) for t, row in mapping.items()}
    tool_output = {int(t): int(row["tool"]) for t, row in mapping.items()}
    source_digest = hashlib.sha256()
    source_size = 0
    output_size = 0
    lines = 0
    tool_commands = 0
    fd, tmp_name = tempfile.mkstemp(
        prefix=".prepared-", suffix=".part", dir=Path(out_path).parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with open(src_path, "rb") as source:
            for block in iter(lambda: source.read(CHUNK_SIZE), b""):
                source_digest.update(block)
                source_size += len(block)
        with open(tmp, "wb") as fout:
            if header:
                line = header
                if not line.endswith(("\n", "\r")):
                    line += "\n"
                data = line.encode("utf-8", "surrogateescape")
                fout.write(data)
                output_size += len(data)
            if prefs_line:
                line = prefs_line
                if not line.endswith(("\n", "\r")):
                    line += "\n"
                data = line.encode("utf-8", "surrogateescape")
                fout.write(data)
                output_size += len(data)
            done = 0
            for body, ending in _iter_raw_lines(src_path):
                lines += 1
                if cancelled is not None and cancelled():
                    raise PreparationCancelled("Preparation cancelled")
                out = None
                stripped = body.lstrip()
                if stripped.startswith(b"T") and len(stripped) > 1 \
                        and stripped[1:2].isdigit():
                    decoded = body.decode("utf-8", "surrogateescape")
                    bare = _BARE_T_RE.match(decoded)
                    if bare:
                        t = int(bare.group(1))
                        if t not in tool_output:
                            raise LargeGcodeError(
                                "G-code selects T%d, but the selected Flexi "
                                "plan has no source for it" % t)
                        tool_commands += 1
                        # Preserve original prefix, spacing and comment exactly.
                        match = re.match(r"^(\s*)T(\d{1,2})(.*)$", decoded)
                        out = "%sT%d%s" % (
                            match.group(1), tool_output[t], match.group(3))
                elif stripped.startswith(b"M10") and (
                        stripped.startswith(b"M104")
                        or stripped.startswith(b"M109")):
                    decoded = body.decode("utf-8", "surrogateescape")
                    out = _replace_heater_tools(decoded, tool_heads)
                elif stripped.startswith(b"SM_PRINT_PREEXTRUDE_FILAMENT"):
                    decoded = body.decode("utf-8", "surrogateescape")
                    pre = _PREEXTRUDE_RE.match(decoded)
                    if pre:
                        t = int(pre.group(1))
                        if t not in tool_output:
                            raise LargeGcodeError(
                                "Pre-extrude references T%d without a selected "
                                "source" % t)
                        out = decoded[:pre.start(1)] + str(tool_output[t]) \
                            + decoded[pre.end(1):]
                data = (out.encode("utf-8", "surrogateescape")
                        if out is not None else body) + ending
                fout.write(data)
                output_size += len(data)
                done += len(body) + len(ending)
                if progress is not None and total:
                    progress(min(done, total), total)
            fout.flush()
            os.fsync(fout.fileno())
        if progress is not None:
            progress(total, total)
        output_size = os.path.getsize(tmp)
        output_digest = hashlib.sha256()
        with open(tmp, "rb") as finished:
            for block in iter(lambda: finished.read(CHUNK_SIZE), b""):
                output_digest.update(block)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return PreparationResult(
        source_path=str(src_path), output_path=str(out_path),
        source_sha256=source_digest.hexdigest(),
        output_sha256=output_digest.hexdigest(),
        source_size=source_size, output_size=output_size,
        source_lines=lines,
        output_lines=lines + (1 if header else 0) + (1 if prefs_line else 0),
        tool_commands=tool_commands, mapping={
            str(k): {"source": v["source"], "head": v["head"], "tool": v["tool"]}
            for k, v in mapping.items()},
        validation={})


def validate_prepared(pp, prepared: PreparationResult, scan,
                      *, status: dict, routes, extra_lines: int = 0,
                      extra_tool_commands: int = 0,
                      allow_print_prefs: bool = False,
                      allow_flow_cal: bool = False) -> dict:
    """Re-scan the derivative and prove it is complete and routed as planned.

    ``scan`` may be a ScanResult or the compact manifest summary written by
    analysis.  The latter avoids scanning the source a second time; its
    SHA-256 is re-checked against the file instead.
    """
    from ace_flexi_production import preflight_gcode_file

    def field(name, default=None):
        return scan.get(name, default) if isinstance(scan, dict) else getattr(scan, name, default)

    if not os.path.isfile(prepared.output_path):
        raise LargeGcodeError("Prepared G-code is missing")
    # prepare_file hashed the source while copying it.  Re-reading a 1 GiB
    # source only to repeat that hash is unnecessary; compare the hash it
    # actually prepared from with the analyzed manifest, then validate the
    # derivative itself below.
    if prepared.source_sha256 != field("sha256", field("source_sha256")):
        raise LargeGcodeError("Original G-code changed during preparation")
    validation = preflight_gcode_file(
        prepared.output_path, status, routes, strict_live=False,
        allow_print_prefs=allow_print_prefs, allow_flow_cal=allow_flow_cal)
    expected_lines = field("lines") + extra_lines
    if validation.get("lines") != expected_lines:
        raise LargeGcodeError(
            "Prepared G-code line count changed (%d -> %d)"
            % (expected_lines, validation.get("lines")))
    if validation.get("layer_changes") != field("layer_changes"):
        raise LargeGcodeError("Prepared G-code lost layer markers")
    expected_tool_commands = field("tool_commands") + extra_tool_commands
    if sum(int(v) for v in (validation.get("tool_counts") or {}).values()) != expected_tool_commands:
        raise LargeGcodeError("Prepared G-code lost tool selections")
    if validation.get("heater_commands") != field("heater_commands"):
        raise LargeGcodeError("Prepared G-code lost heater references")
    prepared.output_sha256 = validation.get("sha256") or ""
    prepared.output_size = int(validation.get("size") or os.path.getsize(prepared.output_path))
    prepared.output_lines = int(validation.get("lines") or 0)
    if validation.get("processed"):
        raise LargeGcodeError("Prepared G-code unexpectedly carries a processed marker")
    if prepared.output_sha256 == field("sha256", field("source_sha256")):
        raise LargeGcodeError(
            "Preparation produced the original bytes; no selected plan was applied")
    if not validation.get("ok"):
        raise LargeGcodeError(
            "Prepared G-code failed Flexi validation: "
            + "; ".join(validation.get("errors") or ["unknown validation error"]))
    if validation.get("tool_counts"):
        expected = Counter(str(int(row["tool"])) for row in prepared.mapping.values())
        actual = {str(k): int(v) for k, v in validation["tool_counts"].items()}
        for tool, needed in expected.items():
            if actual.get(tool, 0) < needed:
                raise LargeGcodeError(
                    "Prepared G-code is missing routed tool T%s" % tool)
    prepared.validation = validation
    return validation
