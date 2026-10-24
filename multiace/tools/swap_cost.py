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
# the 210 s that post_process_virtual_toolheads.BG_SWAP_COST_INLINE_S has
# always used, and a background swap on its 30 s. Day one therefore behaves
# exactly as before and every later change is a deliberate constant edit.
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
            "swap_anti_ooze_retract": 10,
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
