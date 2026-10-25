"""Config-accurate print time & filament estimate for multiACE (plan §1).

The slicer's `; estimated printing time` and its `M73` lines describe a
NORMAL-mode print: one head, no ACE swap, no purge. multiACE adds, per
mid-print swap:

    tip-form + retract to ACE + (ACE spool change) + load to nozzle
             + seat/press + purge + heat settle

Every mechanical term above has a config parameter in ace.cfg, so it is
COMPUTED here (length / speed). The terms we cannot compute - the ACE's
internal spool change, sensor waits, tool pickup - stay named constants in
`UNMEASURED_S`, seeded so the ace.cfg defaults reproduce today's
`BG_SWAP_COST_INLINE_S` / `BG_SWAP_COST_BG_S`.

Those constants are UNMEASURED GUESSES. They are not calibrated values,
and §4.3's history is what turns them into measured medians. Until then
every estimate this module produces carries `confidence: "modelled"` and
the UI must say *estimated*, never *measured*.

Pure stdlib on purpose: this module runs unchanged in the backend, in the
printer's Python and inside the browser's Pyodide worker, and it must not
import ace.py or the post-processor.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Unmeasured constants
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE CHANGING A NUMBER.
#
# None of these have been measured on a real machine. They were chosen so
# that, with the shipped ace.cfg defaults (feed_speed/retract_speed 80,
# load_length 2100, swap_retract_length 900, seat_overshoot_length 20,
# swap_anti_ooze_retract 10), the modelled cost of an inline swap lands on
# about 210 s and a background swap on 30 s - the whole-swap figures of
# post_process_virtual_toolheads.BG_SWAP_COST_INLINE_S / _BG_S. The purge
# is NOT in these seeds: build_estimate bills it on top, so an estimate with
# the default 80 mm flush reads about 28 s above the old inline figure.
# Every later change is a deliberate constant edit.
#
# Anything derived from a length and a speed is NOT in this table - it is
# computed from the config, because that is the part we actually know.
UNMEASURED_S = {
    # Tip forming: heat, wiggle, cool, break off. A whole macro sequence.
    "tip_form":         55.0,
    # The ACE's own spool change: unwind the outgoing filament past the hub
    # and feed the incoming one up to it. Internal to the unit, not visible
    # as any length/speed pair we control.
    "ace_spool_change": 62.0,
    # Seat/press dwell after the overshoot move (the move itself is computed).
    "seat_press":       6.0,
    # Waiting for the nozzle to recover the setpoint after a cold-ish load.
    "heat_settle":      24.0,
    # Toolhead pickup/dock at the changer. Paid by every toolchange.
    "tool_pickup":      12.0,
    # Slot/hub filament sensors settling before the load is trusted.
    "sensor_wait":      12.0,
    # The inline remainder of a background swap: the load already happened
    # on the idle head, so only the handover is on the critical path.
    "bg_handover":      18.0,
    # First load of a head before the print body - no tip-form, no spool
    # change, because there is nothing to remove yet.
    "first_load_extra": 0.0,
}

SWAP_KINDS = ("same_ace", "cross_ace_inline", "cross_ace_bg", "first_load")

#: Kinds whose cost lands on the print's critical path in full.
INLINE_KINDS = ("same_ace", "cross_ace_inline")

# Fallback densities in g/cm3, used only when the slicer wrote no
# `; filament used [g]` line (see parse_header - the Snapmaker Orca fork
# does write it, so this really is a fallback).
MATERIAL_DENSITY = {
    "PLA": 1.24, "PLA+": 1.24, "PLA-CF": 1.29, "SILK": 1.24,
    "PETG": 1.27, "PET": 1.27, "PETG-CF": 1.30,
    "ABS": 1.04, "ASA": 1.07, "PC": 1.20, "PA": 1.14, "PA-CF": 1.20,
    "TPU": 1.21, "PVA": 1.23, "HIPS": 1.04, "PP": 0.90,
}
DEFAULT_DENSITY = 1.24
DEFAULT_DIAMETER_MM = 1.75

#: Purge time per swap, measured on a real machine (head mode, ACE chute):
#: about 0.166 s per mm of filament plus a fixed ~15 s of travel and wipe.
#: Not flow-limited in practice. Easy to replace once more prints are timed.
PURGE_S_PER_MM = 0.166
PURGE_FIXED_S = 15.0

#: The post-processor clamps the flush-matrix stamp to this range
#: (post_process_virtual_toolheads.PURGE_MATRIX_MIN_MM / _MAX_MM). Only the
#: stamp: swap_purge_length and the stock default are used as set.
PURGE_MIN_MM = 40.0
PURGE_MAX_MM = 150.0

#: A background swap primes the head with the purge length plus this much
#: (ace_bg_swap.BG_LOAD_PRIME_EXTRA), so it uses that much more filament.
BG_LOAD_PRIME_EXTRA_MM = 40.0

#: What the stock INNER_FLUSH_FILAMENT macro (fluidd.cfg) purges when
#: nothing tells it otherwise - `ACE_SWAP_HEAD`/`get_purge_length()` omit
#: LENGTH= and the macro's own `params.LENGTH | default(80.0)` applies.
#: This is what a head-mode inline swap costs in filament with no flush
#: stamp and swap_purge_length at its 0 default.
STOCK_FLUSH_DEFAULT_MM = 80.0


_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.I)

# One line per metric, comma-separated PER EXTRUDER INDEX - not one line
# per tool. Index in the list = slicer tool index, so [g][2] is T2, and
# trailing 0.00 entries are unused extruders that must be kept as zeros or
# every tool index after them shifts.
_USED_RE = re.compile(
    r"^;\s*filament used\s*\[(mm|cm3|g)\]\s*[:=]\s*(.+)$", re.I)
_TOTAL_G_RE = re.compile(
    r"^;\s*total filament used\s*\[g\]\s*[:=]\s*([\d.]+)", re.I)
_LAYERS_RE = re.compile(r"^;\s*total layers count\s*[:=]\s*(\d+)", re.I)
_TIME_RE = re.compile(
    r"^;\s*estimated printing time(?:\s*\([^)]*\))?\s*[:=]\s*(.+)$", re.I)
_FIRST_LAYER_TIME_RE = re.compile(
    r"^;\s*estimated first layer printing time(?:\s*\([^)]*\))?\s*[:=]\s*(.+)$",
    re.I)
_DENSITY_RE = re.compile(r"^;\s*filament_density\s*[:=]\s*(.+)$", re.I)
_DIAMETER_RE = re.compile(r"^;\s*filament_diameter\s*[:=]\s*(.+)$", re.I)
_TYPE_RE = re.compile(r"^;\s*filament_type\s*[:=]\s*(.+)$", re.I)

#: Header keys that explicitly switch the prime/wipe tower on or off.
#: These are the only keys trusted to say "no tower" - an on/off switch
#: that reads false is definitive.
_TOWER_ENABLE_KEYS = ("enable_prime_tower", "prime_tower_enable",
                      "wipe_tower")
#: Tower GEOMETRY keys - width, brim, position. Some slicer forks
#: (Snapmaker Orca among them) write these with their last non-zero value
#: even when the tower itself is disabled, so they are consulted only when
#: the header carries no explicit on/off switch at all (see
#: purge_destination) - never used to override one that says "off".
_TOWER_GEOMETRY_KEYS = (
    "prime_tower_width", "prime_tower_brim_width", "wipe_tower_x",
    "wipe_tower_width",
)
#: Header keys that mean the slicer flushed into infill/support/objects.
_FLUSH_KEYS = (
    "flush_into_infill", "flush_into_support", "flush_into_objects",
)


def parse_duration(text):
    """'1d 2h 8m 30s' / '8m 30s' / '19s' -> seconds. None if unparseable.

    The slicer writes a DURATION STRING, not seconds, and any combination
    of the four units in any order.
    """
    if text is None:
        return None
    total = 0.0
    found = False
    for value, unit in _DURATION_RE.findall(str(text)):
        found = True
        mult = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}[unit.lower()]
        total += float(value) * mult
    return total if found else None


def _split_floats(text):
    out = []
    for part in re.split(r"[,;]", text):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            out.append(0.0)
    return out


def _any_key_truthy(text, keys):
    """True when one of `keys` appears in the header with a non-empty,
    non-zero, non-false value.

    Accepts `=` OR `:` as the separator, matching what parse_color_names
    in the post-processor already had to learn: slicer forks are not
    consistent about it, and a separator we do not accept reads as "key
    absent" - which here would mean "no prime tower" and would add a purge
    the slicer had already paid for.
    """
    for key in keys:
        m = re.search(r"^;\s*%s\s*[:=]\s*(.+)$" % re.escape(key), text,
                      re.I | re.M)
        if not m:
            continue
        for part in re.split(r"[,;]", m.group(1)):
            part = part.strip().lower()
            if part in ("", "0", "0.0", "false", "no", "nil", "none"):
                continue
            return True
    return False


def _any_key_present(text, keys):
    """True when one of `keys` appears in the header at all, whatever its
    value - distinct from `_any_key_truthy`, which also asks whether that
    value is "on". Used to tell "this fork wrote an explicit off-switch"
    from "this fork wrote no switch at all, only geometry"."""
    for key in keys:
        if re.search(r"^;\s*%s\s*[:=]\s*(.+)$" % re.escape(key), text,
                     re.I | re.M):
            return True
    return False


def purge_destination(header_text):
    """Where the SLICER'S OWN purge (if it modelled one at all) ends up:
    'tower' | 'flush' | 'mixed' | 'bin' | 'unknown'.

    Informational only - see build_estimate for why this no longer gates
    whether multiACE's own ACE-swap purge gets added. A prime tower or a
    flush-into-support target means the slicer printed an actual wipe
    tower or flushed into actual support/infill toolpaths, consuming real
    filament the post-processor never strips, so its seconds are in
    `base_s` and its grams are in `; filament used [g]` ALREADY - but that
    is the slicer's own mechanism, a plate feature multiACE neither needs
    nor controls. The ACE swap itself still retracts to the ACE, changes
    spool and purges through the hotend's own chute (INNER_FLUSH_FILAMENT)
    regardless of whether the slicer also built a tower - two separate
    physical events, both real, so both get reported.

    Read it from the file, never from a config default. `enable_prime_tower`
    (etc.) is trusted outright, even when false - a fork can write non-zero
    tower WIDTH/geometry lines whether or not the tower is enabled (seen on
    Snapmaker Orca with enable_prime_tower=0), so geometry is consulted only
    when the header carries no explicit on/off switch at all. When the
    header carries no recognisable slicer settings at all, say 'unknown'
    rather than guessing 'bin'.
    """
    text = header_text or ""
    if _any_key_present(text, _TOWER_ENABLE_KEYS):
        tower = _any_key_truthy(text, _TOWER_ENABLE_KEYS)
    else:
        tower = _any_key_truthy(text, _TOWER_GEOMETRY_KEYS)
    flush = _any_key_truthy(text, _FLUSH_KEYS)
    if tower and flush:
        return "mixed"
    if tower:
        return "tower"
    if flush:
        return "flush"
    # No tower and no flush. Only trust that if the header looks like it
    # carries slicer settings at all; an arbitrary gcode tells us nothing.
    if re.search(r"^;\s*[a-z_]+\s*[:=]\s*", text, re.I | re.M):
        return "bin"
    return "unknown"


class HeaderInfo(object):
    """Everything §1 needs out of the slicer's header block."""

    def __init__(self):
        self.per_tool_mm = []
        self.per_tool_cm3 = []
        self.per_tool_g = []
        self.total_g = None
        self.layers = None
        self.base_s = None
        self.first_layer_s = None
        self.densities = []
        self.diameters = []
        self.types = []
        self.purge_destination = "unknown"
        #: 'ok' when the per-tool grams agree with the slicer's own total;
        #: 'degraded' when they do not (we then trust neither silently).
        self.confidence = "ok"
        self.notes = []

    def density_for(self, tool, material=None):
        if tool < len(self.densities) and self.densities[tool]:
            return self.densities[tool]
        if material:
            key = str(material).strip().upper()
            if key in MATERIAL_DENSITY:
                return MATERIAL_DENSITY[key]
        return DEFAULT_DENSITY

    def diameter_for(self, tool):
        if tool < len(self.diameters) and self.diameters[tool]:
            return self.diameters[tool]
        return DEFAULT_DIAMETER_MM

    def grams_for(self, tool, material=None):
        """Grams printed by `tool`, from `[g]` when the slicer wrote it and
        from pi*(d/2)^2*rho otherwise."""
        if tool < len(self.per_tool_g):
            return self.per_tool_g[tool]
        return mm_to_grams(self.mm_for(tool), self.diameter_for(tool),
                           self.density_for(tool, material))

    def mm_for(self, tool):
        if tool < len(self.per_tool_mm):
            return self.per_tool_mm[tool]
        return 0.0

    def as_dict(self):
        return {
            "per_tool_mm": list(self.per_tool_mm),
            "per_tool_g": list(self.per_tool_g),
            "total_g": self.total_g,
            "layers": self.layers,
            "base_s": self.base_s,
            "first_layer_s": self.first_layer_s,
            "purge_destination": self.purge_destination,
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


def mm_to_mm3(length_mm, diameter_mm=DEFAULT_DIAMETER_MM):
    r = (diameter_mm or DEFAULT_DIAMETER_MM) / 2.0
    return 3.141592653589793 * r * r * float(length_mm or 0.0)


def mm3_to_mm(volume_mm3, diameter_mm=DEFAULT_DIAMETER_MM):
    """Inverse of mm_to_mm3 - the slicer's flush matrix is in mm3, the cost
    model's purge lengths are in mm of filament."""
    r = (diameter_mm or DEFAULT_DIAMETER_MM) / 2.0
    area = 3.141592653589793 * r * r
    return (float(volume_mm3 or 0.0) / area) if area else 0.0


def mm_to_grams(length_mm, diameter_mm=DEFAULT_DIAMETER_MM,
                density=DEFAULT_DENSITY):
    return mm_to_mm3(length_mm, diameter_mm) * (density or DEFAULT_DENSITY) \
        / 1000.0


def parse_header(header_text):
    """Parse the slicer's metadata block into a HeaderInfo.

    Tolerant by design: a missing line means the corresponding field stays
    None and the estimate degrades, never raises.
    """
    info = HeaderInfo()
    text = header_text or ""
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(";"):
            continue
        m = _USED_RE.match(s)
        if m:
            values = _split_floats(m.group(2))
            unit = m.group(1).lower()
            if unit == "mm":
                info.per_tool_mm = values
            elif unit == "cm3":
                info.per_tool_cm3 = values
            else:
                info.per_tool_g = values
            continue
        m = _TOTAL_G_RE.match(s)
        if m:
            info.total_g = float(m.group(1))
            continue
        m = _LAYERS_RE.match(s)
        if m:
            info.layers = int(m.group(1))
            continue
        m = _FIRST_LAYER_TIME_RE.match(s)
        if m:
            info.first_layer_s = parse_duration(m.group(1))
            continue
        m = _TIME_RE.match(s)
        if m:
            info.base_s = parse_duration(m.group(1))
            continue
        m = _DENSITY_RE.match(s)
        if m:
            info.densities = _split_floats(m.group(1))
            continue
        m = _DIAMETER_RE.match(s)
        if m:
            info.diameters = _split_floats(m.group(1))
            continue
        m = _TYPE_RE.match(s)
        if m:
            info.types = [p.strip() for p in re.split(r"[,;]", m.group(1))
                          if p.strip()]
            continue

    if not info.per_tool_g and info.per_tool_mm:
        # No [g] line: fall back to pi*(d/2)^2*rho. A file without [g]
        # normally has no filament_density either, so the per-material
        # table is what actually carries this path.
        info.per_tool_g = [
            mm_to_grams(mm, info.diameter_for(i),
                        info.density_for(i, info.types[i]
                                         if i < len(info.types) else None))
            for i, mm in enumerate(info.per_tool_mm)]
        info.notes.append("no `filament used [g]` - grams derived from "
                          "diameter and the material density table")

    # `; total filament used [g]` is an INDEPENDENT cross-check. On a
    # mismatch mark the estimate degraded rather than silently trusting
    # one of the two numbers.
    if info.total_g is not None and info.per_tool_g:
        summed = sum(info.per_tool_g)
        tol = max(0.05, 0.02 * max(summed, info.total_g))
        if abs(summed - info.total_g) > tol:
            info.confidence = "degraded"
            info.notes.append(
                "per-tool grams (%.2f) disagree with the slicer's total "
                "(%.2f)" % (summed, info.total_g))

    info.purge_destination = purge_destination(text)
    return info


# ---------------------------------------------------------------------------
# The cost model
# ---------------------------------------------------------------------------

class SwapCostModel(object):
    """Seconds and millimetres per swap, from ace.cfg + a constants table.

    `from_params` takes the same scalars ace.py reads out of `[ace]`, so
    the model stays honest when a user changes `load_length` - a longer
    bowden really is a longer swap.
    """

    def __init__(self, params=None, per_ace=None, constants=None,
                 calibration=None):
        p = dict(params or {})
        self.params = p
        self.per_ace = {int(k): dict(v) for k, v in (per_ace or {}).items()}
        self.constants = dict(UNMEASURED_S)
        self.constants.update(constants or {})
        #: {kind: {"median_s": float, "n": int}} from §4.3's swap_stats.json.
        self.calibration = dict(calibration or {})
        self.min_calibration_samples = 5

        self.feed_speed = float(p.get("feed_speed", 80) or 80)
        self.retract_speed = float(p.get("retract_speed", 80) or 80)
        self.load_length = float(p.get("load_length", 2100) or 0)
        self.retract_length = float(p.get("retract_length", 1950) or 0)
        self.swap_retract_length = float(p.get("swap_retract_length", 900) or 0)
        self.seat_overshoot_length = float(
            p.get("seat_overshoot_length", 20) or 0)
        self.swap_anti_ooze_retract = float(
            p.get("swap_anti_ooze_retract", 10) or 0)
        self.swap_purge_length = float(p.get("swap_purge_length", 0) or 0)

        self.swap_purge_min = float(p.get("swap_purge_min", PURGE_MIN_MM) or 0)
        self.swap_purge_max = float(p.get("swap_purge_max", PURGE_MAX_MM) or 0)
        #: ACE_SET_PURGE MATRIX=0 - ignore the slicer's flush-matrix stamps
        #: and purge the fixed length again.
        self.purge_matrix = bool(p.get("purge_matrix", True))
        self.filament_diameter = float(
            p.get("filament_diameter", DEFAULT_DIAMETER_MM)
            or DEFAULT_DIAMETER_MM)

    # -- construction ---------------------------------------------------

    @classmethod
    def from_params(cls, main_params, per_ace_params=None, constants=None,
                    calibration=None):
        return cls(main_params, per_ace_params, constants, calibration)

    @classmethod
    def default(cls):
        """The shipped ace.cfg defaults - the model a caller gets when the
        printer's real config is not reachable (e.g. offline mock mode)."""
        return cls({
            "feed_speed": 80, "retract_speed": 80,
            "load_length": 2100, "retract_length": 1950,
            "swap_retract_length": 900, "seat_overshoot_length": 20,
            "swap_anti_ooze_retract": 10, "swap_purge_length": 0,
        })

    def with_calibration(self, swap_stats):
        """Return a copy that prefers measured medians over the constants.

        `swap_stats` is §4.3's aggregate: {kind: {"median_s", "n"}}. Only
        kinds with at least `min_calibration_samples` completed jobs are
        trusted - three swaps of anecdote is not a calibration.
        """
        clone = SwapCostModel(self.params, self.per_ace, self.constants,
                              swap_stats)
        return clone

    # -- per-ACE / per-slot resolution -----------------------------------

    def _p(self, name, ace=None, slot=None, default=None):
        """Per-slot override wins over per-ACE, which wins over the global -
        the same precedence ace.py's get_swap_retract_length uses."""
        if ace is not None:
            sec = self.per_ace.get(int(ace)) or {}
            if slot is not None:
                v = sec.get("%s_%s" % (name, int(slot)))
                if v is not None:
                    return float(v)
            v = sec.get(name)
            if v is not None:
                return float(v)
        v = getattr(self, name, None)
        return float(v) if v is not None else default

    # -- the model ------------------------------------------------------

    def _mechanical_seconds(self, kind, ace=None, slot=None):
        """The part we can actually compute: length / speed."""
        feed = max(1.0, self._p("feed_speed", ace, slot, 80.0))
        retract = max(1.0, self._p("retract_speed", ace, slot, 80.0))
        load_len = self._p("load_length", ace, slot, 0.0)
        seat = self._p("seat_overshoot_length", ace, slot, 0.0)
        if kind == "first_load":
            return load_len / feed + (2.0 * seat) / feed
        if kind == "cross_ace_bg":
            # The unload and the reload happen on the idle head while the
            # other one keeps printing, so none of their mechanical time is
            # on the critical path.
            return 0.0
        pull = (self._p("swap_retract_length", ace, slot, 0.0)
                + self._p("swap_anti_ooze_retract", ace, slot, 0.0))
        return pull / retract + load_len / feed + (2.0 * seat) / feed

    def _constant_seconds(self, kind):
        c = self.constants
        if kind == "cross_ace_bg":
            return c["tool_pickup"] + c["bg_handover"]
        if kind == "first_load":
            return (c["tool_pickup"] + c["seat_press"] + c["heat_settle"]
                    + c["sensor_wait"] + c["first_load_extra"])
        return (c["tip_form"] + c["ace_spool_change"] + c["seat_press"]
                + c["heat_settle"] + c["tool_pickup"] + c["sensor_wait"])

    def calibrated_kinds(self):
        return sorted(
            k for k, v in self.calibration.items()
            if isinstance(v, dict)
            and int(v.get("n", 0) or 0) >= self.min_calibration_samples
            and v.get("median_s") is not None)

    def confidence(self):
        return "calibrated" if self.calibrated_kinds() else "modelled"

    def swap_seconds(self, kind, ace=None, slot=None, from_color=None,
                     to_color=None, material=None):
        """Mechanical + constant seconds this swap adds to the print's
        critical path - NOT including purge time.

        `kind` is one of SWAP_KINDS - §3.1's whole point is that where a
        colour sits decides what a change to it costs, so the caller must
        classify the swap before asking.

        Purge time is billed separately, once per swap, so it is never
        counted twice.
        """
        if kind not in SWAP_KINDS:
            raise ValueError("unknown swap kind: %r" % (kind,))
        measured = self.calibration.get(kind)
        if (isinstance(measured, dict)
                and int(measured.get("n", 0) or 0) >= self.min_calibration_samples
                and measured.get("median_s") is not None):
            return float(measured["median_s"])
        return self._mechanical_seconds(kind, ace, slot) \
            + self._constant_seconds(kind)

    def bg_window_minutes(self, ace=None, slot=None):
        """How much idle time a background swap actually needs, in minutes.

        `BG_UNLOAD_MIN_WINDOW_MIN = 1` was a constant, and a 1-minute window
        would call feasible what a 2100 mm load cannot possibly fit. This is
        the real unload+load duration plus a margin.
        """
        seconds = (self._mechanical_seconds("cross_ace_inline", ace, slot)
                   + self.constants["tip_form"]
                   + self.constants["ace_spool_change"]
                   + self.constants["sensor_wait"])
        return round((seconds * 1.25) / 60.0, 2)

    # -- purge ----------------------------------------------------------

    def resolve_swap_purge_mm(self, stamp_mm=None):
        """mm to purge for ONE swap, the way the engine does it today.

        First match wins: the flush-matrix stamp the preflight writes for
        this pair (ignored when `purge_matrix` is off), else
        `swap_purge_length` when above its 0 default, else the stock
        80 mm. Only the stamp is clamped (to `swap_purge_min` /
        `swap_purge_max`, as the post-processor does); the other two are
        used as set. Purge itself stays the slicer's business: the stamp
        comes from its flush matrix.
        """
        if stamp_mm and self.purge_matrix:
            return self.clamp_purge_mm(stamp_mm)
        if self.swap_purge_length > 0:
            return self.swap_purge_length
        return STOCK_FLUSH_DEFAULT_MM

    def clamp_purge_mm(self, requested_mm):
        """Clamp a flush-matrix stamp to the range the post-processor uses."""
        try:
            value = max(0.0, float(requested_mm or 0.0))
        except (TypeError, ValueError):
            return 0.0
        if self.swap_purge_max > 0:
            value = min(value, self.swap_purge_max)
        return max(value, min(self.swap_purge_min, self.swap_purge_max or value))

    def purge_seconds(self, purge_mm):
        """Time for one swap's purge: per-mm extrusion plus fixed work."""
        if not purge_mm:
            return 0.0
        return PURGE_FIXED_S + PURGE_S_PER_MM * float(purge_mm)

    def purge_grams(self, purge_mm, material=None):
        key = str(material or "").strip().upper()
        density = MATERIAL_DENSITY.get(key, DEFAULT_DENSITY)
        return mm_to_grams(purge_mm, self.filament_diameter, density)


# ---------------------------------------------------------------------------
# The estimate block (§1.3)
# ---------------------------------------------------------------------------

def _round(value, digits=1):
    return None if value is None else round(float(value), digits)


def build_estimate(model, header, timeline, *, materials=None,
                   used_tools=None, extra_assumptions=None):
    """Assemble the `estimate` block the report attaches to each plan.

    `timeline` is §3.2's per-event trace: a list of dicts with at least
    `kind`, and optionally `t`, `seconds`, `purge_mm`, `purge_inline`. Everything else -
    grams, per-colour breakdown, the purge total - is derived here so
    backend and browser produce identical numbers.
    """
    materials = materials or {}
    assumptions = list(extra_assumptions or [])

    counts = {"inline": 0, "bg": 0, "unknown_window": 0,
              "first_load": 0}
    added_s = 0.0
    purge_mm_total = 0.0
    per_tool_purge_mm = {}
    purging_swaps = 0
    added_purge_s = 0.0

    for ev in timeline or []:
        kind = ev.get("kind")
        seconds = ev.get("seconds")
        if seconds is None:
            seconds = model.swap_seconds(
                kind if kind in SWAP_KINDS else "cross_ace_inline",
                ace=ev.get("ace"), slot=ev.get("slot"),
                material=materials.get(ev.get("t")))
        purge = ev.get("purge_mm")
        if purge is None:
            # No post-processor-supplied number (an older installed copy,
            # or a synthetic event built by a test) - resolve one the
            # same way the rewrite would, just without a flush-matrix
            # stamp to prefer (see resolve_swap_purge_mm).
            purge = model.resolve_swap_purge_mm()
        purge = float(purge or 0.0)
        # A finished background swap purges on the parked head, off the
        # critical path; its prime is the purge plus BG_LOAD_PRIME_EXTRA_MM.
        # The flush is inline for every other kind, and for a background
        # swap in transport mode or one that was aborted (`purge_inline`).
        bg_parked = kind == "cross_ace_bg" and not ev.get("purge_inline")
        if bg_parked and purge > 0:
            purge += BG_LOAD_PRIME_EXTRA_MM
        added_s += float(seconds or 0.0)
        purge_mm_total += purge
        if not bg_parked:
            added_purge_s += model.purge_seconds(purge)
        if purge > 0:
            purging_swaps += 1
        t = ev.get("t")
        if t is not None:
            per_tool_purge_mm[t] = per_tool_purge_mm.get(t, 0.0) + purge
        if kind == "cross_ace_bg":
            counts["bg"] += 1
        elif kind == "first_load":
            counts["first_load"] += 1
        else:
            counts["inline"] += 1
        if ev.get("window_min") is None and kind in ("cross_ace_bg",
                                                     "cross_ace_inline"):
            counts["unknown_window"] += 1

    # The ACE-chute purge (INNER_FLUSH_FILAMENT) is a physically SEPARATE
    # event from anything the slicer itself did with its own flush
    # volumes - a prime tower or a flush-into-support target is real
    # filament the slicer already extruded on the plate (never stripped
    # by the post-processor), but the ACE swap still retracts, changes
    # spool and purges through the hotend on top of that. So this is
    # added to every plan's total regardless of `dest` - see
    # purge_destination's docstring for why double-counting was never
    # actually the risk here.
    dest = header.purge_destination if header else "unknown"
    if dest in ("tower", "flush", "mixed"):
        label = {"tower": "prime tower", "flush": "flush into infill/support",
                 "mixed": "prime tower + flush"}[dest]
        assumptions.append(
            "%s detected - that's the slicer's own purge, separate from "
            "the ACE-swap purge below" % label)
    elif dest == "unknown":
        assumptions.append(
            "no prime-tower or flush setting found in the header - the "
            "slicer's own purge destination is unknown (informational "
            "only; the ACE-swap purge below is unaffected)")
    if purging_swaps:
        assumptions.append(
            "%d ACE swap(s) purge through the chute - %.1f g added to "
            "the total, on top of anything the slicer itself printed"
            % (purging_swaps, sum(
                model.purge_grams(mm, materials.get(t))
                for t, mm in per_tool_purge_mm.items())))

    base_s = header.base_s if header else None
    total_s = None if base_s is None else base_s + added_s

    per_color = []
    totals_mm = 0.0
    totals_g = 0.0
    tools = sorted(used_tools) if used_tools else sorted(
        set(list(per_tool_purge_mm.keys())
            + list(range(len(header.per_tool_mm) if header else 0))))
    for t in tools:
        material = materials.get(t)
        print_mm = header.mm_for(t) if header else 0.0
        print_g = header.grams_for(t, material) if header else 0.0
        pmm = per_tool_purge_mm.get(t, 0.0)
        pg = model.purge_grams(pmm, material)
        # The ACE-chute purge is always extra: it is never the slicer's
        # own filament-used total, whatever purge_destination says (see
        # the note above).
        tool_total_g = print_g + pg
        totals_mm += print_mm + pmm
        totals_g += tool_total_g
        row = {
            "t": t,
            "print_mm": _round(print_mm), "print_g": _round(print_g, 2),
            "purge_mm": _round(pmm), "purge_g": _round(pg, 2),
            "total_m": _round((print_mm + pmm) / 1000.0, 2),
        }
        per_color.append(row)
    purge_g_total = sum(model.purge_grams(mm, materials.get(t))
                        for t, mm in per_tool_purge_mm.items())

    confidence = model.confidence()
    if header is not None and header.confidence == "degraded":
        confidence = "degraded"
        assumptions.extend(header.notes)

    out = {
        "base_s": _round(base_s, 0),
        "added_s": _round(added_s + added_purge_s, 0),
        "total_s": _round(None if total_s is None
                          else total_s + added_purge_s, 0),
        "inline_swaps": counts["inline"],
        "bg_swaps": counts["bg"],
        "unknown_window_swaps": counts["unknown_window"],
        "purge": {"mm": _round(purge_mm_total), "g": _round(purge_g_total, 2),
                  "destination": dest},
        "per_color": per_color,
        "totals": {"mm": _round(totals_mm), "m": _round(totals_mm / 1000.0, 2),
                   "g": _round(totals_g, 2)},
        "layers": header.layers if header else None,
        "first_layer_s": _round(header.first_layer_s, 0) if header else None,
        "confidence": confidence,
        "assumptions": assumptions,
    }
    return out
