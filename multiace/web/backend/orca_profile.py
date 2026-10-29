"""Orca printer profile with virtual multiACE tools.

Builds a user printer profile for Snapmaker Orca that inherits the stock
Snapmaker U1 profile and only lengthens the per-extruder lists: tools 0-3
(0-based) are the heads as loaded, tool n >= 4 is ACE (n-4)//4, slot
(n-4)%4. Nothing else changes - start, toolchange and end gcode stay stock. The printer resolves T4.. at runtime
(ACE_TOOL / T4..T31 in ace.py), so no post-processing script is needed.
"""
import json
import re

PROFILE_NAME = "Snapmaker U1 multiACE"
STOCK_INHERITS = "Snapmaker U1 (%s nozzle)"
NOZZLES = ("0.2", "0.4", "0.6", "0.8")
MAX_TOOLS = 32          # stock LOGICAL_EXTRUDER_NUM
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Per-extruder lists of the stock Snapmaker U1 printer profile (values as
# the stock profile exports them); every entry is repeated per tool.
PER_TOOL = {
    "deretraction_speed": "30",
    "extruder_offset": "0x0",
    "long_retractions_when_cut": "0",
    "max_layer_height": "0.32",
    "min_layer_height": "0.08",
    "retract_before_wipe": "0%",
    "retract_length_toolchange": "10",
    "retract_lift_above": "0",
    "retract_lift_below": "269",
    "retract_lift_enforce": "All Surfaces",
    "retract_restart_extra": "0",
    "retract_restart_extra_toolchange": "0",
    "retract_when_changing_layer": "1",
    "retraction_distances_when_cut": "18",
    "retraction_length": "1.5",
    "retraction_minimum_travel": "1",
    "retraction_speed": "30",
    "travel_slope": "3",
    "wipe": "1",
    "wipe_distance": "2",
    "z_hop": "0.4",
    "z_hop_types": "Auto Lift",
    "z_hop_when_prime": "0",
}

# Fallback swatches for slots without a declared colour (distinct, so the
# slicer's filament list stays readable).
_FALLBACK = ["#9E9E9E", "#F44336", "#2196F3", "#4CAF50", "#FFC107",
             "#9C27B0", "#FF9800", "#00BCD4", "#795548", "#E91E63",
             "#3F51B5", "#8BC34A", "#FF5722", "#607D8B", "#CDDC39",
             "#673AB7", "#009688", "#FFEB3B", "#03A9F4", "#F06292",
             "#A1887F", "#4DB6AC", "#BA68C8", "#FFB74D", "#90A4AE",
             "#AED581", "#7986CB", "#4FC3F7", "#DCE775", "#F48FB1",
             "#B0BEC5", "#C5E1A5"]


def tool_target(state, n):
    """(ace, slot) of virtual tool n >= 4: the printer's tool_map entry,
    else the formula."""
    tm = state.get("tool_map") or {}
    ent = tm.get(str(n)) if isinstance(tm, dict) else None
    if isinstance(ent, (list, tuple)) and len(ent) == 2:
        try:
            return int(ent[0]), int(ent[1])
        except (TypeError, ValueError):
            pass
    return divmod(n - 4, 4)


def tool_colours(state, tool_count):
    """Colour per tool from the dashboard state: tools 0-3 show the head's
    current identity, tool n >= 4 its ACE slot. Unknown -> fallback
    swatch."""
    aces = state.get("aces") or []
    heads = state.get("toolheads") or []
    out = []
    for n in range(tool_count):
        col = None
        if n < 4:
            if n < len(heads):
                col = (heads[n] or {}).get("color")
        else:
            ace, slot = tool_target(state, n)
            if ace < len(aces):
                slots = (aces[ace] or {}).get("slots") or []
                if slot < len(slots):
                    col = (slots[slot] or {}).get("color")
        col = str(col or "").strip()
        if not _COLOR_RE.match(col):
            col = _FALLBACK[n % len(_FALLBACK)]
        out.append(col.upper())
    return out


def build_profile(state, nozzle="0.4"):
    """The profile dict. `state` is the parsed dashboard state."""
    if nozzle not in NOZZLES:
        raise ValueError("nozzle must be one of %s" % ", ".join(NOZZLES))
    try:
        count = int(state.get("device_count") or 1)
    except (TypeError, ValueError):
        count = 1
    tool_count = min(MAX_TOOLS, 4 + 4 * max(1, count))
    prof = {
        "type": "machine",
        "from": "User",
        "instantiation": "true",
        "name": PROFILE_NAME,
        "printer_settings_id": PROFILE_NAME,
        "inherits": STOCK_INHERITS % nozzle,
        "printer_model": "Snapmaker U1",
        "printer_variant": nozzle,
        "version": "2.2.53.2",
        "printer_notes": ("multiACE virtual tools: filaments 1-4 = the "
                          "heads as loaded, filament n >= 5 (1-based) = "
                          "MMU (n-5)//4+1, slot (n-5)%%4+1; %d tools for %d "
                          "MMU(s). Resolved on the printer (ACE_TOOL)."
                          % (tool_count, count)),
        "nozzle_diameter": [nozzle] * tool_count,
        "extruder_colour": tool_colours(state, tool_count),
    }
    for key, value in PER_TOOL.items():
        prof[key] = [value] * tool_count
    return prof


def profile_json(state, nozzle="0.4"):
    return json.dumps(build_profile(state, nozzle), indent=2,
                      ensure_ascii=False)
