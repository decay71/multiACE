# EXPERIMENTAL - background swap engine for a PARKED ACE head.
#
# While another head prints, a parked head is unloaded and (optionally)
# loaded with its next slot: heat -> stock cold-pull choreography
# (translated 1:1 from CONTROL_RETRACT_ACTION, executed as stall-free
# background moves) -> ACE bulk retract (device-direct, never touches the
# active device) -> bookkeeping -> feed to the toolhead sensor -> press +
# grip -> prime through the open dock. The arrival ACE_SWAP_HEAD then finds
# the head ready (or, after an abort, continues inline from a safe state).
#
# HARD REQUIREMENTS:
#   - HEAD MODE with 1:1 head<->ACE wiring: the bg ACE must not be the
#     printing head's ACE (an ACE cannot push one slot and roll back another
#     at the same time).
#   - The head's dock must be OPEN below: the cold-pull PUSHES ~60mm of
#     molten filament first and the prime purges through the dock. Enabling
#     a head for bg (ACE_BG_SET_HEAD / [ace_bg_swap] heads) is that
#     declaration.
#   - Nozzle wipe/ooze-cut do NOT happen while docked (no discard access);
#     the ARRIVAL pick-check wipes at the discard position instead
#     (ace.py BG_PICK_WIPE).
#
# Sensors: the toolhead sensor cannot verify a bg unload the tracked way (bg
# moves bypass the extruder trapq), so after the verified ACE bulk retract
# the head is marked absent through the OFFICIAL helper API
# (note_filament_present(False)) - with the head added to
# ace._runout_suppress_heads FIRST so the state flip can never fire a runout
# PAUSE mid-print. head_source is cleared through the same bookkeeping a
# verified unload uses.
#
# Commands: ACE_BG_SWAP (unload-if-loaded + load), ACE_BG_UNLOAD (unload
# only), ACE_BG_STATUS, ACE_BG_SET_HEAD, ACE_BG_MOVE (debug). Failures leave
# head_source INTACT unless the ACE retract completed (the state where
# clearing is truthful).
#
# REMOVABLE: delete this file + the [ace_bg_swap] config section; the only
# ace.py coupling is the optional _wait_bg_op lookup.

import logging
import chelper
from . import force_move

BG_SWAP_VERSION = 'v0.9'

# --- background LOAD (the BG-Load v1 feature) ---
# The bowden feed runs the full padded get_load_length and is stopped by the
# TOOLHEAD SENSOR - the same stop marker as the inline load, hardware-
# agnostic (V1+V2; the insert event fires on a PARKED head).
# FA is turned ON after the feed and the GRIP phase pulls the tip from the
# sensor through the gears WITH the extruder turning + forward-assist (FA
# alone cannot push through the stationary gears). The V2 decoder span is
# logged ('bg-feed') as telemetry only - its flat-detect false-fired
# mid-bowden as a stop criterion (span 1512 of ~1970 on a snag).
BG_LOAD_GRIP_SEAT = 60.      # grip: sensor -> through the gears + seat
BG_LOAD_GRIP_SPEED = 5.      # mm/s
# PRESS-then-grip: the sensor-stop feed
# releases the ACE motor the moment the sensor fires, so the tip stands just
# ABOVE the gear nip with no press - during the grip only the spongy
# forward-assist must deliver it across the gap while the gears already
# spin; when it loses that race the gears turn EMPTY, the prime pumps air,
# and every host signal stays green (decoder=ACE-side, sensor=present,
# FA=assisting - only the pick coil disagrees). The INLINE load has the
# same seat gap but is protected by phase3 (coil verify + retry with ACE
# re-feed). The bg load has neither at load time, so it gets a built-in
# press AFTER the heat wait (a hot hotend, never cold filament into an
# unheated one): a short bounded second feed with the extruder still
# stationary, TIME-paced (short moves are time-paced, the slot status lags
# 1-2s), then terminate the command so the
# motor releases for the grip. V1 has no self-stop -> shorter bound (the
# inline V1 load blind-feeds into the head anyway, same class).
BG_LOAD_PRESS_V2 = 50.       # mm cap; V2 self-stops at the nip well before
BG_LOAD_PRESS_V1 = 30.       # mm, open-loop bounded press
BG_LOAD_PRESS_SPEED = 20.    # mm/s
# The press directly follows a motor stop (feed and/or assist) - on V1 that
# window is deterministically FORBIDDEN, so the press gets a paced busy
# retry like the other V1 motor commands; still busy after the ladder -> skip
# the press (= the old behaviour, grip proceeds).
BG_LOAD_PRESS_RETRIES = 3
BG_LOAD_PRESS_RETRY_DELAY = 2.0   # s between attempts (motor wind-down)
BG_LOAD_PRIME_SPEED = 4.     # mm/s (prime falls through the open dock)
# Prime chunk size = the pick-abort granularity of the PRIME phase. A queued
# stealth move cannot be cancelled and _check_docked only fires BETWEEN
# moves, so a pick mid-chunk keeps extruding for the REST of that chunk
# while the stock T already carries the head across the machine (a purge
# worm on the print bed). 5mm chunks cap it
# at ~5mm / ~1.3s. Cost: every chunk pays the stealth-move scheduling gap
# (SCHEDULE_DELAY + MOVE_SETTLE = ~0.65s) -> ~+13s on a 120mm prime, hidden
# in the background window; raise to 10 to halve that if the longer prime
# measurably increases pick-aborts.
BG_LOAD_PRIME_CHUNK = 5.
# Extra prime on TOP of the inline purge knob (get_purge_length, default 80).
# The inline swap purges MORE than its nominal 80: phase3 probe-extrudes
# (>=20mm) fill the melt zone BEFORE the flush, so the full 80 is real purge
# out of a full nozzle, plus INNER_FLUSH_FILAMENT does an ooze-CUTOFF +
# nozzle clean before AND after (stock fluidd.cfg:1106). The bg prime has
# none of that - part of its 80 only FILLS the melt zone (grip ends 60mm
# past the sensor) and there is no cutter at the dock. Without the bonus a
# bg swap carries visibly more old colour into the print. ~+10s bg time - invisible, it runs in the background.
BG_LOAD_PRIME_EXTRA = 40.
BG_LOAD_RETRACT_SPEED = 25.  # mm/s anti-ooze end retract
BG_FEED_MIN_MOVE = 100       # V2: span below this = nothing demonstrably moved
# EDGE-VERIFY POST-MORTEM: the eN toolhead sensor pin is a
# PRESENCE GATE, not a motion-pulse encoder - edges fire ONLY on tip-arrival
# and tail-departure transitions, never while filament runs through. Two
# perfect loads + a running print measured 0 edges over every grip/prime leg,
# and the clogged head measured the same 0 -> edge counting discriminates
# NOTHING with filament present. The whole edge-verify family (grip/prime
# counters, push-probe, pick-time flow check) was removed for that reason -
# do NOT rebuild a verify on note-call counting. Real signals: the V2
# decoder (ACE-side movement), presence TRANSITIONS (arrival/removal), and
# runout_buttun_state (per-edge raw pin), which is an unload gate here:
# a pin still True after a verified retract means a remnant or stretched
# tail stayed in the toolhead (see the pin gate in _unload_core). The gate
# only decides bg BOOKKEEPING (empty vs hand-to-inline) and fails open when
# the pin is unreadable.
# FA rescue when the feed motor stops mid-bowden without sensor arrival
# (a self-stop at a snag): forward-assist can push the tip the remaining way
# to the sensor. Give FA this
# long to complete the approach before declaring the feed 'partial'.
BG_FEED_FA_RESCUE = 20.      # s
# Stock cold-pull (CONTROL_RETRACT_ACTION, standard nozzle >=0.3mm, normal
# filament), translated from the config-dump macro. (dist mm, feedrate mm/min)
COLD_PULL_NORMAL = [
    (57.0, 400.),     # purge/pressurize - forms fresh tip material
    (3.0, 1500.),
    (-27.0, 2700.),   # fast pull out of the melt zone
    (-5.5, 40.),      # slow pull = the actual tip stretch
    (-37.5, 1500.),   # clear the heatbreak
]
COLD_PULL_SOFT = [
    (5.0, 600.),
    (-27.0, 2700.),
    (-5.5, 40.),
    (-37.5, 1500.),
]
# Net filament pulled INTO the bowden by the choreography - the interleaved
# ACE unwinds must reclaim it so a V2 (no freewheel) never accumulates slack.
CHOREO_ACCEL = 300.
HEAT_TIMEOUT = 240.
HEAT_HYST = 4.0
ACE_UNWIND_SPEED_FALLBACK = 80
MOVE_SETTLE = 0.30
# [diag] passive: log the V2 decoder SPAN during the bg BULK retract to
# multiace_feedlog.log (same 'unload-dec' format as the inline unload), so a
# manually-fired ACE_BG_UNLOAD yields the same movement data as an inline
# swap. Reuses ace._retract_with_decoder_span. Controls the LOGGING only -
# the short-retract decoder GATE below always samples (it is the verify).
BG_UNLOAD_DECODER_DIAG = True
# Short-first, decoder-verified bg retract (bg analog of the inline
# short-probe-retract). Pull this much FIRST and confirm the ACE really
# moved via the decoder span before committing the rest - so a stuck filament
# is caught after ~150mm instead of a full rollback ground into it, and a
# failure is bounded to <=short (recoverable) for a clean inline handover.
BG_UNLOAD_PROBE_RETRACT = 150.
# span < FRAC*short = the ACE barely moved -> stuck. Conservative (0.3): only a
# NEAR-ZERO short trips it (clean stuck), a healthy pull (bulk data: ~93-98% of
# commanded) passes with huge margin -> near-zero false-stall risk. A stuck
# that slips past is still caught by the rest's own _ace_unwind verify. Tune up
# once real bg-short span data confirms the healthy short magnitude.
BG_UNLOAD_STALL_FRAC = 0.3

# --- stall-free move mechanic (merged from the former ace_bg_move module) ---
# Never schedule closer to "now" than this - covers reactor jitter + the
# clock-sync estimate error. Also the latency floor of a bg move.
SCHEDULE_DELAY = 0.250
# Gap between our own consecutive moves and above any prior flush horizon.
SCHEDULE_EPS = 0.050
MAX_DISTANCE = 200.
MAX_VELOCITY = 60.
MAX_ACCEL = 2000.


class ToolheadNotClear(RuntimeError):
    """bg unload: the ACE-side retract verified clean (decoder) but the
    toolhead presence pin still reads filament - remnant or stretched
    tail left in the head (strand separation). The head must NOT be
    bookkept empty; the arrival swap runs the inline unload ladder."""
    pass


class AceBgSwap:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        # Per-head opt-in = the HARDWARE declaration "this head's dock is
        # open below" (the cold-pull purges ~60mm through it). Only listed
        # heads run bg unloads; a QUIET (gcode-stamped) request for another
        # head skips silently, a console request explains. FORCE=1 overrides
        # for experiments. Read unconditionally.
        heads_raw = config.get('heads', '')
        # Pick-time flow GATE: when True, the arrival pick check
        # (ace._bg_pick_flow_check) ESCALATES a verified NO_FLOW - re-grip on
        # the now-active head + re-measure, then a resumable pause - instead
        # of logging only. False = the pre-gate LOG-ONLY behaviour. Read
        # unconditionally.
        self.pick_gate = config.getboolean('pick_gate', True)
        # Unload-only switch: False disables the
        # bg LOAD half - ACE_BG_SWAP stamps run only the HW-proven unload
        # half, the arrival swap then loads INLINE with full phase3
        # verification (the exact path every bg-load abort already falls
        # back to, HW-proven). Use case: docks with a small chute (e.g.
        # Panda Breath) that swallows the ~60-70mm cold-pull tip but not
        # the full bg prime purge. Read unconditionally.
        self.load_enabled = config.getboolean('load_enabled', True)
        self.enabled_heads = set()
        for tok in heads_raw.replace(',', ' ').split():
            try:
                h = int(tok)
                if 0 <= h <= 3:
                    self.enabled_heads.add(h)
            except ValueError:
                pass
        # Web-UI toggle persistence (ACE_BG_SET_HEAD): a saved ace__bg_heads
        # REPLACES the config default, same precedence as ace__language over
        # [ace] language (the config option above is still read
        # unconditionally either way).
        save_vars = self.printer.lookup_object('save_variables', None)
        if save_vars is not None:
            saved = save_vars.allVariables.get('ace__bg_heads', None)
            if isinstance(saved, (list, tuple)):
                restored = set()
                for h in saved:
                    try:
                        h = int(h)
                        if 0 <= h <= 3:
                            restored.add(h)
                    except (TypeError, ValueError):
                        pass
                self.enabled_heads = restored
                logging.info('[multiACE] [bg-unload] enabled heads restored '
                             'from ace__bg_heads: %s'
                             % (sorted(restored) or 'NONE'))
        # Surfaced in ace.py's startup banner (bg-swap visibility line).
        self.version = BG_SWAP_VERSION
        # {head: state string} - IDLE/HEAT/PULL/RETRACT/DONE/FAILED:<reason>
        self.state = {}
        self._busy = set()
        # {head: previous fan value 0..1} while the bg dwell fan runs
        # (_dwell_fan) - per head, the engine is serialized but the OFF
        # must survive any exit path.
        self._dwell_fan_prev = {}
        # stall-free move mechanic (former ace_bg_move module)
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.stepper_kinematics = ffi_main.gc(
            ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)
        self._last_end = {}
        self.gcode.register_command('ACE_BG_UNLOAD', self.cmd_ACE_BG_UNLOAD,
                                    desc=self.cmd_ACE_BG_UNLOAD_help)
        self.gcode.register_command('ACE_BG_STATUS', self.cmd_ACE_BG_STATUS,
                                    desc=self.cmd_ACE_BG_STATUS_help)
        self.gcode.register_command('ACE_BG_MOVE', self.cmd_ACE_BG_MOVE,
                                    desc=self.cmd_ACE_BG_MOVE_help)
        self.gcode.register_command('ACE_BG_SET_HEAD', self.cmd_ACE_BG_SET_HEAD,
                                    desc=self.cmd_ACE_BG_SET_HEAD_help)
        self.gcode.register_command('ACE_BG_SWAP', self.cmd_ACE_BG_SWAP,
                                    desc=self.cmd_ACE_BG_SWAP_help)

    def get_status(self, eventtime):
        # Queried by the web backend (/api/state bg selector + the preflight
        # bg report) and Moonraker. Cheap copies only. TRAP: the Snapmaker
        # webhooks encoder is orjson (json_compat.dumps_bytes) and it is
        # STRICT - a non-str dict key raises "Dict key must be str" and
        # webhooks.send() responds with invoke_shutdown. Int-keyed state
        # shuts the printer down mid-print at the first bg op (the dict is
        # empty until then, so startup looks fine).
        # Every key here MUST be str; keep values JSON-primitive.
        return {
            'version': self.version,
            'enabled_heads': sorted(self.enabled_heads),
            'busy': sorted(self._busy),
            'state': {str(h): str(v) for h, v in self.state.items()},
        }

    cmd_ACE_BG_SET_HEAD_help = (
        '[multiACE] Declare a head bg-swap capable (= its dock is OPEN below,'
        ' the cold-pull purges through it). ACE_BG_SET_HEAD HEAD=n ENABLE=0|1'
        ' - write-through: writes the [ace_bg_swap] heads config line'
        ' (PERSIST=0 = until restart).')
    def cmd_ACE_BG_SET_HEAD(self, gcmd):
        head = gcmd.get_int('HEAD', minval=0, maxval=3)
        enable = bool(gcmd.get_int('ENABLE', minval=0, maxval=1))
        if head in self._busy:
            raise self._bg_error(gcmd,
                '[bg-unload] head %d has a RUNNING bg operation'
                ' - toggle after it finishes' % self._dh(head), head=head)
        if enable:
            self.enabled_heads.add(head)
        else:
            self.enabled_heads.discard(head)
        # Write-through: persist into the
        # [ace_bg_swap] heads line via ace's writer. getattr fallback to
        # the legacy save variable - ace.py is NOT in the bundle sha and
        # can be older than this module on the same printer.
        # ace via lookup_object - AceBgSwap has NO self.ace attribute; the
        # an AttributeError here is a full SHUTDOWN in this fork.
        _ace = self.printer.lookup_object('ace', None)
        _wt = getattr(_ace, '_wt_persist', None)
        if _wt is not None:
            sfx = _wt(gcmd, 'heads',
                      ','.join(str(h) for h in sorted(self.enabled_heads)),
                      'ace__bg_heads', section='ace_bg_swap')
        else:
            sfx = ''
            self.gcode.run_script_from_command(
                "SAVE_VARIABLE VARIABLE=ace__bg_heads VALUE=%s"
                % str(sorted(self.enabled_heads)).replace(' ', ''))
        self._say('head %d bg-swap %s (enabled heads now %s)%s'
                  % (self._dh(head), 'ENABLED - dock must be OPEN below' if enable
                     else 'disabled',
                     [self._dh(h) for h in sorted(self.enabled_heads)]
                     or 'NONE', sfx))

    def is_busy(self, head):
        # ace.py wait-hook (cmd_ACE_SWAP_HEAD/UNLOAD/LOAD entries): a
        # toolchange targeting this head WAITS for the bg op instead of
        # colliding. Waiting beats aborting: a mid-bulk abort would leave a
        # partially retracted filament, and the V2 rollback is FIXED-LENGTH
        # (no slot sensor) - the inline re-retract would over-pull the
        # filament out of the ACE gears.
        return head in self._busy

    # -- helpers ----------------------------------------------------------
    def _say(self, msg):
        logging.info('[multiACE] [bg-unload] %s' % msg)
        try:
            self.gcode.respond_raw('// [bg-unload] %s' % msg)
        except Exception:
            pass

    def _ext_name(self, head):
        return 'extruder' if head == 0 else 'extruder%d' % head

    def _raw(self, idx):
        # Internal (0-based) index for internal log lines.
        if idx is None:
            return '-'
        try:
            return int(idx)
        except (TypeError, ValueError):
            return idx

    def _dh(self, idx):
        # Display index (1-based per display_index_base) for USER-FACING
        # message text only - the raw internal index confused even the
        # maintainer (a head waiting on "itself" under two numbers).
        # Data fields (get_status, gcode params) stay 0-based internal.
        ace = self.printer.lookup_object('ace', None)
        try:
            if ace is not None:
                return ace._disp(idx)
        except Exception:
            pass
        return idx

    def _bg_error(self, gcmd, text, head=None):
        # Structured command error: route through
        # ace._ace_error so the touchscreen renders the message verbatim
        # (id=525 + the multiACE 200-band, code 209 = bg busy/refused)
        # instead of a bare "System error". Fail-open to the plain raise
        # when the [ace] object or the helper is missing (old ace.py) -
        # behaviour (level/action) is identical either way.
        ace = self.printer.lookup_object('ace', None)
        helper = getattr(ace, '_ace_error', None)
        if helper is not None:
            try:
                return helper(gcmd, text, 209, head=head)
            except Exception:
                pass
        return gcmd.error(text)

    def _pause(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)

    def _wait_move(self, toolhead, end):
        while toolhead.mcu.estimated_print_time(
                self.reactor.monotonic()) < end + MOVE_SETTLE:
            self._pause(0.10)

    def _ace_send(self, ace, ace_idx, request):
        """Send one request and return its RESPONSE dict (or None on
        timeout). The code must be checked: an unwind sent while the
        previous rollback still runs is ACCEPTED on the wire but not
        executed - 'done' alone is no truth."""
        done = [None]
        # ace.py callback convention: invoked as
        # c(self=<ace instance>, response=r) - KEYWORD args, so the first
        # parameter MUST be named `self` (it is the ace object, not ours).
        # And it runs in a reactor-timer context: raising here shuts the
        # printer down ("Unhandled exception during run").
        def _cb(self, response):
            try:
                done[0] = response if response is not None else {}
            except Exception:
                pass
        ace.send_request_to(ace_idx, request, _cb)
        deadline = self.reactor.monotonic() + 5.0
        while done[0] is None and self.reactor.monotonic() < deadline:
            self._pause(0.05)
        return done[0]

    def _resp_rejected(self, resp):
        """True when a motor command was NOT accepted. The ACE Pro (V1)
        rejects a feed/unwind while the previous one still runs with
        code=0 msg=FORBIDDEN - a bare code check reads that as success and
        the filament is never pulled. Treat FORBIDDEN as a busy rejection - retry, never success."""
        if not resp:
            return True
        if resp.get('code', -1) != 0:
            return True
        return str(resp.get('msg', '')).strip().upper() == 'FORBIDDEN'

    def _ace_quiesce(self, ace, ace_idx, slot, why):
        """Best-effort ACE-side cleanup after an ABNORMAL exit (pick
        abort, error): the cold-pull/load choreography brackets ACE motion
        (feed/unwind + assist), and an abort mid-bracket can leave the
        device with an open command state - a V1 left like that can report
        status=busy through serial reconnects until a power-cycle (the
        serial reopen resets nothing device-side). Close every bracket explicitly:
        stop_feed_assist + stop_feed_filament on the involved slot,
        FORBIDDEN-retried (a stop during wind-down is rejected code=0
        msg=FORBIDDEN and must be retried - the silent-stop class).
        Idempotent on an idle slot; the host FA cache is cleared so the
        monitors re-arm canonically instead of fighting the stop. Runs
        BEFORE the finally releases _busy, so the waiting arrival sees a
        quiesced unit. Never raises."""
        try:
            ace._feed_assist_per_ace[ace_idx] = -1
        except Exception:
            pass
        for method in ('stop_feed_assist', 'stop_feed_filament'):
            try:
                ok = False
                for _a in range(3):
                    resp = self._ace_send(ace, ace_idx, {
                        'method': method, 'params': {'index': slot}})
                    if not self._resp_rejected(resp):
                        ok = True
                        break
                    if _a < 2:
                        self._pause(0.4)
                logging.info('[multiACE] [bg] quiesce (%s): %s ACE %d '
                             'slot %d %s'
                             % (why, method, ace_idx, slot,
                                'ok' if ok else 'NOT accepted (3x)'))
            except Exception as e:
                logging.info('[multiACE] [bg] quiesce %s failed '
                             '(ignored): %s' % (method, e))

    def _fa_on(self, ace, ace_idx, slot, retries=3, backoff=1.0):
        """Arm feed-assist with a busy-rejection backoff. Right after a
        stop_feed_filament the V1 is still in its feed wind-down and
        rejects start_feed_assist with code=0 msg=FORBIDDEN. 50ms retries
        are useless against a motor wind-down - retry on a ~1s backoff instead. Updates the
        host FA cache ONLY on a real accept (FORBIDDEN carries code=0,
        a bare code check would stamp a stale cache). Returns True when
        armed."""
        for attempt in range(retries):
            resp = self._ace_send(ace, ace_idx, {
                'method': 'start_feed_assist', 'params': {'index': slot}})
            if not self._resp_rejected(resp):
                ace._feed_assist_per_ace[ace_idx] = slot
                return True
            if attempt < retries - 1:
                self._pause(backoff)
        return False

    def _check_docked(self, toolhead, ext, head):
        # Toolchange interlock, engine-side: if a T picked this head while
        # the bg op runs, ABORT immediately - the choreography must never
        # purge/pull on a head that is now printing. The abort keeps
        # head_source intact, so the inline swap that follows simply does a
        # normal full unload (a re-unload of a partially pulled filament is
        # the regular retry case).
        if toolhead.get_extruder() is ext:
            raise RuntimeError('head %d was PICKED mid-sequence - aborted, '
                               'inline paths take over' % self._dh(head))

    def _bg_fan_obj(self, head):
        # The PARKED head's own part fan as a directly drivable object.
        # M106 cannot reach it (M106 targets the ACTIVE head), but the
        # fans exist per head in the stock config: heads 1-3 are
        # [fan_generic e<n>_fan], head 0 is the primary [fan] section -
        # both wrap fan.Fan (verified against u1_firmware 1.5.2).
        if head == 0:
            o = self.printer.lookup_object('fan', None)
        else:
            o = self.printer.lookup_object('fan_generic e%d_fan' % head,
                                           None)
        return getattr(o, 'fan', None)

    def _dwell_fan(self, ace, head, on):
        """bg twin of ace._dwell_fan (the dwell-heat-soak mitigation):
        run the bg HEAD's own part fan during the passive
        ACE windows - bulk retract and bowden feed - where the head just
        sits hot at the dock. Driven via fan.Fan.set_speed_from_command
        directly (no gcode: this greenlet must not contend for the gcode
        mutex mid-print; the lookahead callback applies within the
        print's buffer like any M106). No collision by construction: a
        bg head is never the ACTIVE head, so its fan never belongs to
        the slicer - and the active head's M106 drives ITS fan object,
        not this one. Same [ace] swap_dwell_fan knob (0 = off,
        byte-identical). NOT during heat phases (a fan extends the
        dwell) and there is no coil measurement on a docked bg head
        (phase3/pickcheck run on the ACTIVE head later), so the rule
        "no fan during a coil measurement" is structurally satisfied. Fail-open; OFF restores the
        saved previous value (a parked head's fan is normally 0)."""
        try:
            if on:
                spd = int(getattr(ace, 'swap_dwell_fan', 0) or 0)
                if spd <= 0 or head in self._dwell_fan_prev:
                    return
                f = self._bg_fan_obj(head)
                if f is None:
                    return
                prev = float(getattr(f, 'last_fan_value', 0.) or 0.)
                self._dwell_fan_prev[head] = prev
                f.set_speed_from_command(min(spd, 255) / 255.)
                logging.info('[multiACE] [bg] dwell fan ON S%d head %d '
                             '(was S%d)'
                             % (spd, self._raw(head),
                                int(round(prev * 255.))))
            else:
                if head not in self._dwell_fan_prev:
                    return
                prev = self._dwell_fan_prev.pop(head)
                f = self._bg_fan_obj(head)
                if f is not None:
                    f.set_speed_from_command(prev)
                logging.info('[multiACE] [bg] dwell fan OFF head %d '
                             '(restored S%d)'
                             % (self._raw(head), int(round(prev * 255.))))
        except Exception as e:
            logging.info('[multiACE] [bg] dwell fan toggle failed '
                         '(ignored): %s' % e)
            self._dwell_fan_prev.pop(head, None)

    def _slot_status(self, ace, ace_idx, slot):
        # V2 slot truth = _v2_get_slot_status (the [v2-diag] source). The
        # _info_per_ace slot 'status' stays 'unknown' on a V2, polling it
        # misreads a running rollback as dropped.
        try:
            st = ace._v2_get_slot_status(ace_idx, slot)
            if st:
                return str(st)
        except Exception:
            pass
        info = ace._info_per_ace.get(ace_idx) or {}
        slots = info.get('slots') or []
        if slot < len(slots) and isinstance(slots[slot], dict):
            return str(slots[slot].get('status'))
        return ''

    def _wait_rollback_done(self, ace, ace_idx, slot, length, speed):
        """Device-truth pacing (a fixed timer can declare a bulk retract
        done while the device dropped it): wait for
        the slot to ENTER a moving state (heartbeat latency ~1-2s), then for
        it to LEAVE it. Returns the last seen status.

        Deliberately NO pick-interlock in here: a pick during
        the bulk retract must NOT cancel the rollback - the V2 rollback is
        fixed-length (no slot sensor), a cancel strands a partially
        retracted filament and the inline re-retract then over-pulls it out
        of the ACE gears. The pick itself is only carriage motion (filament
        already out of the toolhead here) and the arrival ACE_SWAP_HEAD
        WAITS on is_busy via ace._wait_bg_op. Do not re-add an abort."""
        moving = ('rollback', 'feeding')
        deadline = self.reactor.monotonic() + 4.0
        seen_moving = False
        while self.reactor.monotonic() < deadline:
            st = self._slot_status(ace, ace_idx, slot)
            if any(m in st for m in moving):
                seen_moving = True
                break
            self._pause(0.2)
        deadline = self.reactor.monotonic() + length / max(speed, 1) + 20.0
        while self.reactor.monotonic() < deadline:
            st = self._slot_status(ace, ace_idx, slot)
            if not any(m in st for m in moving):
                if seen_moving:
                    return st
                # never saw it move and it is idle again -> likely dropped
                return 'DROPPED:%s' % st
            seen_moving = True
            self._pause(0.3)
        return 'TIMEOUT'

    def _ace_unwind(self, ace, ace_idx, slot, length, speed, wait=True,
                    retries=3):
        """Unwind with the V2 rollback-lock release, RESP-code check,
        busy-retry and optional device-truth completion wait. ace._retract
        sends stop_feed_assist UNCONDITIONALLY before every unwind ('release
        rollback-lock') - skipping it when the FA cache is empty makes the
        device drop ALL unwinds."""
        if not ace._is_v2_idx(ace_idx):
            # V1: no per-slot motor state to poll - open-loop time pacing,
            # but WITH the retry loop: a too-early send is answered
            # code=0 msg=FORBIDDEN (busy rejection, see _resp_rejected) and
            # must be re-sent after a pause, and the dwell is generous
            # (+2s, the FW start/stop overhead exceeds length/speed).
            for attempt in range(1, retries + 1):
                resp = self._ace_send(ace, ace_idx, {
                    'method': 'unwind_filament',
                    'params': {'index': slot, 'length': int(length),
                               'speed': int(speed)}})
                if self._resp_rejected(resp):
                    self._say('unwind %dmm attempt %d: code=%s msg=%s - retry'
                              % (length, attempt,
                                 (resp or {}).get('code', 'none'),
                                 (resp or {}).get('msg', 'timeout')))
                    self._pause(2.0)
                    continue
                if wait:
                    self._pause(length / max(speed, 1) + 2.0)
                return True
            return False
        for attempt in range(1, retries + 1):
            self._ace_send(ace, ace_idx, {
                'method': 'stop_feed_assist', 'params': {'index': slot}})
            resp = self._ace_send(ace, ace_idx, {
                'method': 'unwind_filament',
                'params': {'index': slot, 'length': int(length),
                           'speed': int(speed)}})
            if self._resp_rejected(resp):
                self._say('unwind %dmm attempt %d: code=%s msg=%s - retry'
                          % (length, attempt,
                             (resp or {}).get('code', 'none'),
                             (resp or {}).get('msg', 'timeout')))
                self._pause(2.0)
                continue
            if not wait:
                return True
            st = self._wait_rollback_done(ace, ace_idx, slot, length, speed)
            if st.startswith('DROPPED') or st == 'TIMEOUT':
                self._say('unwind %dmm attempt %d: device %s - retry'
                          % (length, attempt, st))
                self._pause(2.0)
                continue
            return True
        return False

    def _ace_feed(self, ace, ace_idx, slot, length, speed, retries=3):
        """Feed with the same discipline as _ace_unwind: V2 pre-stop
        (rollback-lock release), RESP-code check and device-truth
        completion wait ('feeding' status cycle via _wait_rollback_done);
        V1 = open-loop time pacing. Returns 'ok' | 'none' (nothing
        demonstrably moved - safe to hand over an EMPTY head) | 'partial'
        (moved but completion unconfirmed - the caller MUST retract the
        commanded length before handover, else the inline load would
        double-feed into the gears). Retries only on 'none' conditions."""
        if length <= 0:
            return 'ok'
        if not ace._is_v2_idx(ace_idx):
            for attempt in range(1, retries + 1):
                resp = self._ace_send(ace, ace_idx, {
                    'method': 'feed_filament',
                    'params': {'index': slot, 'length': int(length),
                               'speed': int(speed)}})
                if self._resp_rejected(resp):
                    self._say('feed %dmm attempt %d: code=%s msg=%s - retry'
                              % (length, attempt,
                                 (resp or {}).get('code', 'none'),
                                 (resp or {}).get('msg', 'timeout')))
                    self._pause(2.0)
                    continue
                self._pause(length / max(speed, 1) + 2.0)
                return 'ok'
            return 'none'
        for attempt in range(1, retries + 1):
            self._ace_send(ace, ace_idx, {
                'method': 'stop_feed_assist', 'params': {'index': slot}})
            resp = self._ace_send(ace, ace_idx, {
                'method': 'feed_filament',
                'params': {'index': slot, 'length': int(length),
                           'speed': int(speed)}})
            if self._resp_rejected(resp):
                self._say('feed %dmm attempt %d: code=%s msg=%s - retry'
                          % (length, attempt,
                             (resp or {}).get('code', 'none'),
                             (resp or {}).get('msg', 'timeout')))
                self._pause(2.0)
                continue
            st = self._wait_rollback_done(ace, ace_idx, slot, length, speed)
            if st.startswith('DROPPED'):
                self._say('feed %dmm attempt %d: device %s - retry'
                          % (length, attempt, st))
                self._pause(2.0)
                continue
            if st == 'TIMEOUT':
                self._say('feed %dmm: device TIMEOUT after moving - NOT '
                          'retrying (fed amount unknown)' % length)
                return 'partial'
            return 'ok'
        return 'none'

    def _ace_feed_to_gears(self, ace, ace_idx, slot, length, speed, head):
        """Feed toward the extruder and STOP at the TOOLHEAD SENSOR - the
        same stop marker the inline load uses, hardware-agnostic (V1+V2).
        The sensor fires on a PARKED head (encoder edges need no extruder
        motion), and a decoder-flat criterion is unreliable as a stop (it
        false-fires mid-bowden on a snag that FA then pushes past). The V2 decoder is sampled as TELEMETRY only
        ('bg-feed' span log). Secondary stop: the slot leaves its moving
        state (V2 self-stops at real resistance, no grinding).
        Returns (result, span_tuple): 'ok' = sensor reached (tip at the
        toolhead sensor, just above the gears); 'none' = nothing demonstrably
        moved (safe EMPTY handover); 'partial' = moved but the sensor never
        fired (mid-bowden stop / sensor miss) - the caller must cleanup-
        retract before handover. 'stale' = the sensor already read present
        BEFORE the feed (cannot serve as a marker; nothing was sent)."""
        sensor = self.printer.lookup_object(
            'filament_motion_sensor e%d_filament' % head, None)
        def _detected():
            try:
                return bool(sensor is not None and
                            sensor.get_status(0).get('filament_detected'))
            except Exception:
                return False
        if sensor is None:
            return 'stale', (None, 0, None, None)
        if _detected():
            # A latched-present sensor on a supposedly EMPTY head: feeding
            # against it would stop instantly on the stale state -> refuse
            # before any motion.
            return 'stale', (None, 0, None, None)
        if length <= 0:
            return 'ok', (None, 0, None, None)
        is_v2 = ace._is_v2_idx(ace_idx)
        # release any rollback-lock, then fire the feed. A too-early send
        # is rejected code=0 msg=FORBIDDEN (V1 busy rejection) - re-send
        # after a pause instead of mis-reading it as 'nothing to do'.
        self._ace_send(ace, ace_idx, {
            'method': 'stop_feed_assist', 'params': {'index': slot}})
        resp = None
        for _try in range(3):
            resp = self._ace_send(ace, ace_idx, {
                'method': 'feed_filament',
                'params': {'index': slot, 'length': int(length),
                           'speed': int(speed)}})
            if not self._resp_rejected(resp):
                break
            self._say('feed %dmm start: code=%s msg=%s - retry'
                      % (length, (resp or {}).get('code', 'none'),
                         (resp or {}).get('msg', 'timeout')))
            self._pause(2.0)
        if self._resp_rejected(resp):
            return 'none', (None, 0, None, None)
        dmin = dmax = None
        n = 0
        arrived = False
        device_idle_since = None
        deadline = self.reactor.monotonic() + length / max(speed, 1) + 15.
        while self.reactor.monotonic() < deadline:
            if _detected():
                arrived = True
                break
            if is_v2:
                d = ace._read_decoder(ace_idx, slot)
                if d is not None:
                    n += 1
                    dmin = d if dmin is None else min(dmin, d)
                    dmax = d if dmax is None else max(dmax, d)
                # V2 self-stopped (resistance / command end) with no sensor
                # arrival: give the FA-less tail a short grace (the sensor
                # can lag the stop by a moment), then hand back 'partial'.
                st = self._slot_status(ace, ace_idx, slot) or ''
                if 'feeding' not in st and 'rollback' not in st:
                    if device_idle_since is None:
                        device_idle_since = self.reactor.monotonic()
                    elif (self.reactor.monotonic() - device_idle_since
                          >= 2.0):
                        break
                else:
                    device_idle_since = None
            self._pause(0.15)
        # terminate the feed command so the motor releases for the grip phase
        self._ace_send(ace, ace_idx, {
            'method': 'stop_feed_filament', 'params': {'index': slot}})
        span = (dmax - dmin) if (dmax is not None
                                 and dmin is not None) else None
        if arrived:
            return 'ok', (span, n, dmin, dmax)
        if is_v2 and (span is None or span < BG_FEED_MIN_MOVE):
            return 'none', (span, n, dmin, dmax)
        # Mid-bowden stop without arrival: FA RESCUE first (forward-assist
        # can push the tip past a snag to the sensor).
        resp = self._ace_send(ace, ace_idx, {
            'method': 'start_feed_assist', 'params': {'index': slot}})
        if not self._resp_rejected(resp):
            ace._feed_assist_per_ace[ace_idx] = slot
            rescue_deadline = self.reactor.monotonic() + BG_FEED_FA_RESCUE
            while self.reactor.monotonic() < rescue_deadline:
                if _detected():
                    arrived = True
                    break
                self._pause(0.3)
        if arrived:
            # FA stays armed - the caller turns it ON right after anyway.
            return 'ok', (span, n, dmin, dmax)
        self._ace_send(ace, ace_idx, {
            'method': 'stop_feed_assist', 'params': {'index': slot}})
        ace._feed_assist_per_ace[ace_idx] = -1
        # Moved (V2: measured; V1: open-loop, assume moved) but the sensor
        # never fired even with FA -> the caller retries or stages it in
        # place (every follow-up feed is sensor-gated, staged model).
        return 'partial', (span, n, dmin, dmax)

    def _gpio_diag(self, head, where):
        """Sample the raw per-edge pin state (runout_buttun_state on the
        stock EncoderSensor) plus the helper presence state at interesting
        moments, and RETURN the raw pin value (True/False/None). The
        unload path also GATES its bookkeeping on the returned value (see
        the pin gate in _unload_core). Returns
        None when the pin is unreadable (callers fail open)."""
        raw = None
        try:
            sensor = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % head, None)
            raw = getattr(sensor, 'runout_buttun_state', None)
            det = None
            try:
                det = sensor.get_status(0).get('filament_detected')
            except Exception:
                pass
            logging.info('[multiACE] [bg-gpio] head %d %s: '
                         'runout_buttun_state=%s filament_detected=%s'
                         % (head, where, raw, det))
        except Exception:
            pass
        return raw

    def _schedule_start(self, toolhead, name):
        # After everything the flush machinery may already have generated for
        # this stepper (global sg flush horizon <= print_time/step_gen_time),
        # after "now" with margin, and after our own previous move.
        est = toolhead.mcu.estimated_print_time(self.reactor.monotonic())
        return max(toolhead.print_time,
                   getattr(toolhead, 'step_gen_time', 0.),
                   est + SCHEDULE_DELAY,
                   self._last_end.get(name, 0.)) + SCHEDULE_EPS

    def _ensure_enabled(self, stepper_name, print_time):
        stepper_enable = self.printer.lookup_object('stepper_enable')
        enable = stepper_enable.lookup_enable(stepper_name)
        if not enable.is_motor_enabled():
            # Schedule the enable just before the move; leave the motor on
            # afterwards (the next toolchange manages it as usual).
            enable.motor_enable(max(print_time - 0.100, 0.))
            return True
        return False

    def queue_move(self, ext, dist, speed, accel):
        """Schedule one stall-free move on `ext`'s (parked) stepper. Returns
        (start, end, enabled_now) in print_time. Python API for the bg-swap
        sequencer; the caller is responsible for the guards (not the active
        extruder, hot enough, head stays docked)."""
        toolhead = self.printer.lookup_object('toolhead')
        stepper = ext.extruder_stepper.stepper
        name = ext.get_name() if hasattr(ext, 'get_name') else ext.name
        start = self._schedule_start(toolhead, name)
        enabled_now = self._ensure_enabled(stepper.get_name(), start)

        # force_move-style borrow, WITHOUT any toolhead flush: atomic within
        # this handler (single reactor thread), the parked extruder's own
        # trapq is empty, and generate_steps is monotonic per stepper - the
        # flush machinery calling it later with older times is a no-op.
        prev_pos = stepper.get_commanded_position()
        prev_sk = stepper.set_stepper_kinematics(self.stepper_kinematics)
        prev_trapq = stepper.set_trapq(self.trapq)
        stepper.set_position((0., 0., 0.))
        axis_r, accel_t, cruise_t, cruise_v = force_move.calc_move_time(
            dist, speed, accel)
        self.trapq_append(self.trapq, start, accel_t, cruise_t, accel_t,
                          0., 0., 0., axis_r, 0., 0.,
                          0., cruise_v, accel, 0xFFFFFFFF)
        end = start + accel_t + cruise_t + accel_t
        stepper.generate_steps(end)
        self.trapq_finalize_moves(self.trapq, end + 99999.9, end + 99999.9)
        stepper.set_trapq(prev_trapq)
        stepper.set_stepper_kinematics(prev_sk)
        # Restore the E bookkeeping so the extruder's own trapq/position stay
        # consistent (activation re-syncs via sync_to_extruder anyway - belt
        # and braces).
        stepper.set_position((prev_pos, 0., 0.))
        # Keep the MCU flush running past our steps even when idle.
        toolhead.note_mcu_movequeue_activity(end)
        self._last_end[name] = end
        return start, end, enabled_now

    cmd_ACE_BG_MOVE_help = (
        '[EXPERIMENTAL] Move a PARKED head\'s extruder without stalling the '
        'print. ACE_BG_MOVE HEAD=0-3 DISTANCE=<+-mm> [VELOCITY=5] [ACCEL=100] '
        '[FORCE=0]. Refuses the active extruder; FORCE=1 skips the '
        'cold-extrude check. No toolchange interlock yet - keep the head '
        'docked while it runs.')

    def cmd_ACE_BG_MOVE(self, gcmd):
        head = gcmd.get_int('HEAD', minval=0, maxval=3)
        dist = gcmd.get_float('DISTANCE',
                              minval=-MAX_DISTANCE, maxval=MAX_DISTANCE)
        speed = gcmd.get_float('VELOCITY', 5., above=0., maxval=MAX_VELOCITY)
        accel = gcmd.get_float('ACCEL', 100., above=0., maxval=MAX_ACCEL)
        force = gcmd.get_int('FORCE', 0)

        name = self._ext_name(head)
        ext = self.printer.lookup_object(name, None)
        if ext is None:
            raise self._bg_error(gcmd, '[bg-move] %s not configured' % name,
                                 head=head)
        toolhead = self.printer.lookup_object('toolhead')
        active = toolhead.get_extruder()
        if active is ext:
            raise self._bg_error(gcmd,
                '[bg-move] head %d is the ACTIVE toolhead extruder - '
                'bg moves are for parked heads only' % self._dh(head),
                head=head)
        if not dist:
            gcmd.respond_info('[bg-move] DISTANCE=0 - nothing to do')
            return
        heater = ext.get_heater()
        if not force and not bool(getattr(heater, 'can_extrude', True)):
            raise self._bg_error(gcmd,
                '[bg-move] %s is below min_extrude_temp - heat it first '
                '(M104 S<temp> T%d A0) or pass FORCE=1' % (name, head),
                head=head)

        start, end, enabled_now = self.queue_move(ext, dist, speed, accel)
        est = toolhead.mcu.estimated_print_time(self.reactor.monotonic())
        msg = ('[bg-move] %s %+0.2fmm @%.1fmm/s scheduled t+%.2fs, '
               'runs %.2fs%s' % (name, dist, speed, start - est, end - start,
                                 ' (motor enabled)' if enabled_now else ''))
        gcmd.respond_info(msg)
        logging.info('[multiACE] %s (start=%.3f end=%.3f print_time=%.3f)'
                     % (msg, start, end, toolhead.print_time))

    # -- command ----------------------------------------------------------
    cmd_ACE_BG_UNLOAD_help = (
        '[EXPERIMENTAL] Unload a PARKED ACE head in the background (heat + '
        'cold-pull via bg moves + ACE retract) while printing. '
        'ACE_BG_UNLOAD HEAD=0-3 [TEMP=<feed temp>]. Requires head mode, 1:1 '
        'wiring, an OPEN dock below the head (purges ~60mm!), and the head '
        'must stay docked for the whole ~3min sequence.')

    def cmd_ACE_BG_UNLOAD(self, gcmd):
        head = gcmd.get_int('HEAD', minval=0, maxval=3)
        temp = gcmd.get_float('TEMP', 0.)
        quiet = gcmd.get_int('QUIET', 0)
        force = gcmd.get_int('FORCE', 0)

        def _refuse(msg):
            # QUIET = stamped into print gcode by the preflight: a refusal
            # must never abort the print - skip with a log line. The inline
            # swap at the toolchange then simply does the full unload.
            if quiet:
                self._say('skip (quiet): %s' % msg)
                return
            raise self._bg_error(gcmd, '[bg-unload] %s' % msg, head=head)

        if head not in self.enabled_heads and not force:
            return _refuse('head %d not bg-enabled ([ace_bg_swap] heads: - '
                           'the open-dock declaration); FORCE=1 to override'
                           % self._dh(head))

        ace = self.printer.lookup_object('ace', None)
        if ace is None:
            return _refuse('needs the [ace] section')
        if head in self._busy:
            return _refuse('head %d already running (%s)'
                           % (self._dh(head), self.state.get(head)))
        if self._busy:
            # SERIALIZE bg ops: the stall-free move mechanic shares ONE trapq
            # (self.trapq). Two concurrent cold-pulls append moves for
            # different steppers into it -> generate_steps reads an
            # inconsistent sequence -> 'stepcompress Invalid sequence' ->
            # flush_handler SHUTDOWN. A single op is proven safe;
            # only concurrency breaks it. Refuse (QUIET stamp -> skip -> inline)
            # until the running op finishes.
            return _refuse('another bg op is running (head %s) - bg ops are '
                           'serialized (shared move queue), one at a time'
                           % ', '.join(str(self._dh(h))
                                       for h in sorted(self._busy)))
        if getattr(ace, '_ace_mode', 'multi') != 'head':
            return _refuse('v0 requires head mode (1:1 ACE per head)')
        if not ace.head_uses_ace(head):
            return _refuse('head %d is not ACE-driven' % self._dh(head))
        if getattr(ace, '_swap_in_progress', False):
            return _refuse('a swap is in progress')
        ext = self.printer.lookup_object(self._ext_name(head), None)
        toolhead = self.printer.lookup_object('toolhead')
        if ext is None:
            return _refuse('%s not configured' % self._ext_name(head))
        if toolhead.get_extruder() is ext:
            return _refuse('head %d is the ACTIVE toolhead - bg unload is for '
                           'parked heads' % self._dh(head))
        source = ace._head_source.get(head)
        if not source:
            return _refuse('head %d has no head_source - nothing to unload'
                           % self._dh(head))
        ace_idx = source.get('ace_index')
        slot = source.get('slot')
        if ace_idx is None or slot is None:
            return _refuse('head %d head_source incomplete' % self._dh(head))
        # The bg ACE must not be the ACE of the currently printing head.
        try:
            act_name = toolhead.get_extruder().get_name()
            act_head = (0 if act_name == 'extruder'
                        else int(act_name.replace('extruder', '')))
            act_src = ace._head_source.get(act_head)
            if act_src and act_src.get('ace_index') == ace_idx:
                return _refuse(
                    'ACE %d also feeds the printing head %d (1:1 wiring '
                    'violated?)' % (self._dh(ace_idx), self._dh(act_head)))
        except Exception:
            pass
        if ace._serial_failed_per_ace.get(ace_idx, False) or \
                ace._reconnecting_per_ace.get(ace_idx, False):
            return _refuse('ACE %d comms not healthy' % self._dh(ace_idx))

        if temp <= 0.:
            # Same source as the inline unload heat: the feed module's
            # unload temp ([ace_tipform] unloadtemp: parameter, else the
            # DB load temp). Fallback 250.
            temp = 250.
            try:
                module, channel = ace.EXTRUDER_MAP[head]
                feed = self.printer.lookup_object(
                    'filament_feed %s' % module, None)
                if feed is not None:
                    _g = getattr(feed, '_get_filament_unload_temp',
                                 feed._get_filament_temp)
                    temp = float(_g(channel))
            except Exception:
                pass
        soft = False
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc is not None:
                soft = bool(ptc.get_status()['filament_soft'][head])
        except Exception:
            pass

        self._busy.add(head)
        self.state[head] = 'QUEUED'
        self._say('head %d: bg unload queued [%s] (ACE %d slot %d, '
                  'temp %.0f, soft=%s) - head must stay docked, dock must '
                  'be OPEN below'
                  % (self._dh(head), BG_SWAP_VERSION, self._dh(ace_idx),
                     self._dh(slot), temp, soft))
        self.reactor.register_async_callback(
            lambda et, h=head, a=ace_idx, s=slot, t=temp, sf=soft:
                self._run_unload(h, a, s, t, sf))

    cmd_ACE_BG_SWAP_help = (
        '[EXPERIMENTAL] Background SWAP of a PARKED ACE head: unload (if '
        'loaded), then feed+grip+prime the target slot through the OPEN '
        'dock - the arrival toolchange becomes a no-op. ACE_BG_SWAP '
        'HEAD=0-3 SLOT=0-3 [ACE=n] [TEMP=] [ANTI_OOZE=] [QUIET=1] '
        '[FORCE=1]. Same requirements as ACE_BG_UNLOAD.')

    def cmd_ACE_BG_SWAP(self, gcmd):
        head = gcmd.get_int('HEAD', minval=0, maxval=3)
        slot_ld = gcmd.get_int('SLOT', minval=0, maxval=3)
        ace_ld = gcmd.get_int('ACE', -1, minval=-1, maxval=3)
        temp = gcmd.get_float('TEMP', 0.)
        quiet = gcmd.get_int('QUIET', 0)
        force = gcmd.get_int('FORCE', 0)
        anti_ooze = gcmd.get_float('ANTI_OOZE', -1.)
        # Per-pair prime length from the preflight's flush-matrix stamp
        # (PURGE_MATRIX_* in post_process). ON the line, not via the
        # global ACE_SET_PURGE override: this op primes MINUTES later in
        # the greenlet, by which time later inline stamps have moved the
        # override to THEIR pair. None/absent -> get_purge_length as
        # before (old files, old preflight - soft-degrading).
        purge = gcmd.get_float('PURGE', None, minval=0., maxval=200.)

        def _refuse(msg):
            # QUIET stamps must never abort a print (same as ACE_BG_UNLOAD).
            if quiet:
                self._say('skip (quiet): %s' % msg)
                return
            raise self._bg_error(gcmd, '[bg-swap] %s' % msg, head=head)

        # === SYNC MARKER: keep these checks aligned with cmd_ACE_BG_UNLOAD
        # (only difference: an EMPTY head is allowed here - it skips the
        # unload half and goes straight to the load). ===
        if head not in self.enabled_heads and not force:
            return _refuse('head %d not bg-enabled ([ace_bg_swap] heads: - '
                           'the open-dock declaration); FORCE=1 to override'
                           % self._dh(head))
        ace = self.printer.lookup_object('ace', None)
        if ace is None:
            return _refuse('needs the [ace] section')
        if head in self._busy:
            return _refuse('head %d already running (%s)'
                           % (self._dh(head), self.state.get(head)))
        if self._busy:
            # SERIALIZE bg ops: the stall-free move mechanic shares ONE trapq
            # (self.trapq). Two concurrent cold-pulls append moves for
            # different steppers into it -> generate_steps reads an
            # inconsistent sequence -> 'stepcompress Invalid sequence' ->
            # flush_handler SHUTDOWN. A single op is proven safe;
            # only concurrency breaks it. Refuse (QUIET stamp -> skip -> inline)
            # until the running op finishes.
            return _refuse('another bg op is running (head %s) - bg ops are '
                           'serialized (shared move queue), one at a time'
                           % ', '.join(str(self._dh(h))
                                       for h in sorted(self._busy)))
        if getattr(ace, '_ace_mode', 'multi') != 'head':
            return _refuse('requires head mode (1:1 ACE per head)')
        if not ace.head_uses_ace(head):
            return _refuse('head %d is not ACE-driven' % self._dh(head))
        if getattr(ace, '_swap_in_progress', False):
            return _refuse('a swap is in progress')
        ext = self.printer.lookup_object(self._ext_name(head), None)
        toolhead = self.printer.lookup_object('toolhead')
        if ext is None:
            return _refuse('%s not configured' % self._ext_name(head))
        if toolhead.get_extruder() is ext:
            return _refuse('head %d is the ACTIVE toolhead - bg swaps are '
                           'for parked heads' % self._dh(head))

        # Unload half: only when the head is actually loaded.
        un = None
        source = ace._head_source.get(head)
        if source and source.get('ace_index') is not None \
                and source.get('slot') is not None:
            un = (source['ace_index'], source['slot'])
        # Load target ACE: explicit param, else the unload source's ACE,
        # else the head's wiring (strict 1:1 in head mode).
        if ace_ld < 0:
            if un is not None:
                ace_ld = un[0]
            else:
                try:
                    ace_ld = int(ace.head_ace_for(head))
                except Exception:
                    ace_ld = head
        if un is not None and un == (ace_ld, slot_ld):
            return _refuse('head %d already loaded from ACE %d slot %d - '
                           'nothing to do'
                           % (self._dh(head), self._dh(ace_ld),
                              self._dh(slot_ld)))
        # The bg ACE must not be the ACE of the currently printing head.
        try:
            act_name = toolhead.get_extruder().get_name()
            act_head = (0 if act_name == 'extruder'
                        else int(act_name.replace('extruder', '')))
            act_src = ace._head_source.get(act_head)
            if act_src and act_src.get('ace_index') == ace_ld:
                return _refuse(
                    'ACE %d also feeds the printing head %d (1:1 wiring '
                    'violated?)' % (self._dh(ace_ld), self._dh(act_head)))
        except Exception:
            pass
        if ace._serial_failed_per_ace.get(ace_ld, False) or \
                ace._reconnecting_per_ace.get(ace_ld, False):
            return _refuse('ACE %d comms not healthy' % self._dh(ace_ld))

        u_temp = temp
        if temp <= 0.:
            # Load half heats to the DB LOAD temp; the unload half (the
            # cold-pull) runs at the unload temp (u_temp: [ace_tipform]
            # unloadtemp: else load temp) - the load re-heat happens in
            # parallel to the bowden feed anyway, so a cooler pull costs
            # no wall-clock. An explicit TEMP= drives both halves.
            temp = 250.
            u_temp = 0.
            try:
                module, channel = ace.EXTRUDER_MAP[head]
                feed = self.printer.lookup_object(
                    'filament_feed %s' % module, None)
                if feed is not None:
                    temp = float(feed._get_filament_temp(channel))
                    _g = getattr(feed, '_get_filament_unload_temp', None)
                    if _g is not None:
                        u_temp = float(_g(channel))
            except Exception:
                pass
            if u_temp <= 0.:
                u_temp = temp
        soft = False
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc is not None:
                soft = bool(ptc.get_status()['filament_soft'][head])
        except Exception:
            pass
        if anti_ooze < 0.:
            anti_ooze = float(getattr(ace, 'swap_anti_ooze_retract', 10))

        self._busy.add(head)
        self.state[head] = 'QUEUED'
        self._say('head %d: bg SWAP queued [%s] (%s -> ACE %d slot %d, '
                  'temp %.0f, anti_ooze %.1f) - head must stay docked, '
                  'dock must be OPEN below'
                  % (self._dh(head), BG_SWAP_VERSION,
                     ('unload ACE %d slot %d' % (self._dh(un[0]),
                                                 self._dh(un[1])))
                     if un else 'head empty',
                     self._dh(ace_ld), self._dh(slot_ld), temp, anti_ooze))
        self.reactor.register_async_callback(
            lambda et, h=head, u=un, l=(ace_ld, slot_ld), t=temp, sf=soft,
                   ao=anti_ooze, ut=u_temp, pg=purge:
                self._run_swap(h, u, l, t, sf, ao, ut, pg))

    cmd_ACE_BG_STATUS_help = '[EXPERIMENTAL] Show background unload states.'

    def cmd_ACE_BG_STATUS(self, gcmd):
        if not self.state:
            gcmd.respond_info('[bg-unload] no bg operations yet')
            return
        for h in sorted(self.state):
            gcmd.respond_info('[bg-unload] head %d: %s'
                              % (self._dh(h), self.state[h]))

    def _bg_dec_log(self, ace, head, slot, kind, length, span_tuple):
        """[diag] one decoder-span line for a bg retract segment, same
        'unload-dec' format as the inline unload. See BG_UNLOAD_DECODER_DIAG.
        head/slot are raw 0-based (matches the inline log)."""
        if not BG_UNLOAD_DECODER_DIAG:
            return
        fl = getattr(ace, '_feedlog', None)
        if fl is None:
            return
        try:
            _s, _n, _mn, _mx = span_tuple
            fl.info('unload-dec head=%d slot=%d kind=%s len=%d span=%s n=%s '
                    'min=%s max=%s'
                    % (head, slot, kind, int(length), _s, _n, _mn, _mx))
        except Exception:
            pass

    # -- the sequences (run in an async-callback greenlet; reactor.pause is
    #    allowed here and does NOT block the print) -----------------------
    def _run_unload(self, head, ace_idx, slot, temp, soft):
        ace = self.printer.lookup_object('ace')
        toolhead = self.printer.lookup_object('toolhead')
        ext = self.printer.lookup_object(self._ext_name(head))
        heater = ext.get_heater()
        pheaters = self.printer.lookup_object('heaters')
        retract_done = {'v': False}
        try:
            self._unload_core(head, ace, toolhead, ext, heater, pheaters,
                              ace_idx, slot, temp, soft, retract_done)
            self.state[head] = 'DONE'
            self._say('head %d: BACKGROUND UNLOAD COMPLETE - next '
                      'ACE_SWAP_HEAD/load on this head is load-only'
                      % self._dh(head))
        except Exception as e:
            self._ace_quiesce(ace, ace_idx, slot, 'bg unload abort')
            self._fail_unload(head, heater, pheaters, e, retract_done['v'])
        finally:
            self._dwell_fan(ace, head, False)
            self._busy.discard(head)

    def _fail_unload(self, head, heater, pheaters, e, retract_done):
        reason = str(e) or e.__class__.__name__
        self.state[head] = 'FAILED:%s' % reason
        try:
            pheaters.set_temperature(heater, 0.)
        except Exception:
            pass
        if isinstance(e, ToolheadNotClear):
            # Pin gate: ACE side verifiably clear, toolhead side not.
            # head_source and the presence latch are untouched (bookkeeping
            # never ran), so the arrival swap sees a loaded head and runs
            # the INLINE unload with its full hot-retry ladder - ending in
            # a correct unload-jam pause if the remnant is truly stuck.
            # Amber warn: the print continues, nothing is wedged yet.
            msg = ('head %d: unload NOT verified - toolhead still holds '
                   'filament (remnant/stretched tail); the arrival swap '
                   'unloads inline (hot retry ladder)' % self._dh(head))
            try:
                self.printer.lookup_object('ace').log_warn(msg)
            except Exception:
                self._say(msg)
            logging.warning('[multiACE] [bg-unload] head %d pin gate: %s'
                            % (head, reason))
            return
        if not retract_done:
            self._say('head %d: FAILED (%s) - head_source kept, filament '
                      'state unchanged; recover with a normal display/'
                      'web unload' % (self._dh(head), reason))
        else:
            self._say('head %d: FAILED after retract (%s) - treat the '
                      'head as unloaded, check the slot'
                      % (self._dh(head), reason))
        logging.exception('[multiACE] [bg-unload] head %d failed' % head)

    def _unload_core(self, head, ace, toolhead, ext, heater, pheaters,
                     ace_idx, slot, temp, soft, retract_done):
        """Steps 0-6 of the background unload. Raises on abort; on normal return the head is
        verifiably empty and bookkept. retract_done is a {'v': bool} ref
        for the caller's failure messaging (past the bulk = treat the
        head as unloaded)."""
        if True:
            # 0. Runout suppression FIRST - the sensor state flip at the end,
            # and any encoder flutter during the pull, must never fire a
            # runout PAUSE for this head mid-print.
            ace._runout_suppress_heads.add(head)

            # 1. Stop FA on the bg ACE (it is NOT the printing head's ACE -
            # guarded above), mirror the cache convention.
            self.state[head] = 'FA_STOP'
            armed = ace._feed_assist_per_ace.get(ace_idx, -1)
            if isinstance(armed, int) and 0 <= armed <= 3:
                self._ace_send(ace, ace_idx, {
                    'method': 'stop_feed_assist', 'params': {'index': armed}})
                ace._feed_assist_per_ace[ace_idx] = -1
                self._say('head %d: FA stopped on ACE %d slot %d'
                          % (self._dh(head), self._dh(ace_idx),
                             self._dh(armed)))

            # 2. Heat (async target set, greenlet-poll - never blocks gcode).
            self.state[head] = 'HEAT'
            pheaters.set_temperature(heater, temp)
            self._say('head %d: heating to %.0f' % (self._dh(head), temp))
            deadline = self.reactor.monotonic() + HEAT_TIMEOUT
            _retgt_said = False
            while True:
                self._check_docked(toolhead, ext, head)
                cur, _tgt = heater.get_temp(self.reactor.monotonic())
                if cur >= temp - HEAT_HYST:
                    break
                if _tgt < temp - HEAT_HYST:
                    # Departed-tool M104 S0 race - see the load heat-wait.
                    if not _retgt_said:
                        self._say('head %d: heater target was reset '
                                  'externally (%.0f) - re-asserting %.0f'
                                  % (self._dh(head), _tgt, temp))
                        _retgt_said = True
                    pheaters.set_temperature(heater, temp)
                if self.reactor.monotonic() > deadline:
                    raise RuntimeError('heat timeout (%.0f/%.0f)'
                                       % (cur, temp))
                self._pause(0.5)

            # 3. Cold-pull choreography as stall-free bg moves; after each
            # RETRACT segment reclaim the same length at the ACE so a V2
            # (no freewheel) never accumulates bowden slack.
            self.state[head] = 'PULL'
            # Per-material table from [ace_tipform] (same tables as the
            # inline path); the built-in constants stay the fallback. The
            # config parser guarantees a NET pull, so the per-segment
            # reclaim below adapts to any custom table automatically.
            seq = None
            try:
                seq = ace.tipform_table_for(
                    ace._tipform_material_for(head),
                    vendor=ace._tipform_vendor_for(head), soft=bool(soft))
            except Exception:
                seq = None
            if seq is not None:
                self._say('head %d: custom tip-form table (%d tokens)'
                          % (self._dh(head), len(seq)))
            else:
                seq = [('move', d, f) for d, f in
                       (COLD_PULL_SOFT if soft else COLD_PULL_NORMAL)]
            unwind_speed = ACE_UNWIND_SPEED_FALLBACK
            try:
                unwind_speed = int(ace.get_retract_speed(ace_idx))
            except Exception:
                pass
            # The retract segments run OVERLAPPED with an ACE unwind of the
            # same length: the unwind is dispatched first (wait=False), then
            # the extruder pull is queued - the ACE pays the filament out
            # while the extruder pulls, replacing the stock rollback-assist
            # (a V2 just BRAKES otherwise -> gear clicking).
            # After the pull the unwind completion is confirmed device-truth.
            reclaimed = 0.
            fwd_assist = False
            _fan_warned = False
            for tok in seq:
                self._check_docked(toolhead, ext, head)
                kind = tok[0]
                if kind == 'pause':
                    self._pause(float(tok[1]))
                    continue
                if kind == 'temp':
                    # target change only, no wait (mirrors the inline
                    # 'temp:' token semantics = M104).
                    pheaters.set_temperature(heater, float(tok[1]))
                    continue
                if kind == 'waittemp':
                    # Set target + wait until reached, both directions. On a
                    # PARKED head there is no fan, so a downward wait is
                    # PASSIVE cooling = slow (document; prefer small drops in
                    # bg tables). Bounded: on timeout proceed with a warning
                    # instead of failing the whole unload for a tuning
                    # nicety. Pick-abort stays live via _check_docked.
                    c = float(tok[1])
                    pheaters.set_temperature(heater, c)
                    _wt_deadline = self.reactor.monotonic() + 180.
                    while True:
                        self._check_docked(toolhead, ext, head)
                        cur, _t = heater.get_temp(self.reactor.monotonic())
                        if abs(cur - c) <= 3.:
                            break
                        if self.reactor.monotonic() > _wt_deadline:
                            self._say('head %d: waittemp:%d not reached in '
                                      '180s (at %.0f) - continuing'
                                      % (self._dh(head), int(c), cur))
                            break
                        self._pause(0.5)
                    continue
                if kind == 'fan':
                    # A parked head's part fan is not addressable from here
                    # (M106 targets the ACTIVE extruder) - skip honestly.
                    if not _fan_warned:
                        self._say('head %d: tip-form fan: token skipped '
                                  '(not addressable on a parked head)'
                                  % self._dh(head))
                        _fan_warned = True
                    continue
                dist, feedrate = float(tok[1]), float(tok[2])
                speed = feedrate / 60.
                if dist < 0.:
                    ln = int(round(-dist))
                    if ln < 3:
                        # Cooling-move sized retract (custom tables, e.g.
                        # -2@600 oscillations): no meaningful bowden slack,
                        # and an unwind+pacing per wiggle would turn a 6-
                        # segment oscillation into 6+ wasted seconds. Just
                        # move - BEFORE the fwd_assist disarm, so an armed
                        # forward assist stays armed across the wiggle (the
                        # net motion is tiny; disarm+rearm per wiggle would
                        # thrash the device). Net slack is reclaimed by the
                        # LARGE retracts / the decoder-verified bulk after.
                        _s, end, _en = self.queue_move(ext, dist, speed,
                                                          CHOREO_ACCEL)
                        self._wait_move(toolhead, end)
                        continue
                    if fwd_assist:
                        # _ace_unwind's pre-stop disarms it device-side;
                        # keep the host cache in sync (FA convention).
                        ace._feed_assist_per_ace[ace_idx] = -1
                        fwd_assist = False
                    # Short unwinds are TIME-paced like ace._retract: their
                    # rollback (0.1-0.5s motor time) is over faster than the
                    # ~1s status updates can catch - a status confirm flags
                    # them DROPPED although they ran. Code-check still
                    # applies.
                    ok = self._ace_unwind(ace, ace_idx, slot, ln,
                                          unwind_speed, wait=False)
                    _s, end, _en = self.queue_move(ext, dist, speed,
                                                      CHOREO_ACCEL)
                    self._wait_move(toolhead, end)
                    self._pause(max(1.0, ln / max(unwind_speed, 1) + 0.5))
                    if ok:
                        reclaimed += ln
                    else:
                        self._say('head %d: WARN unwind %dmm rejected by '
                                  'the device - bowden slack accumulating'
                                  % (self._dh(head), ln))
                else:
                    if not fwd_assist and ace._is_v2_idx(ace_idx):
                        # Forward-assist for the purge pushes: stock arms FA
                        # before the unload so the ACE feeds WITH the
                        # extruder - a V2 just brakes otherwise (grinding on
                        # the +57mm push). Host cache kept in
                        # sync so ace's velocity monitor treats it as a
                        # legitimate arm.
                        # No backoff here (would stall the cold-pull
                        # timing) - the loop re-tries on the next push
                        # while fwd_assist stays False. _resp_rejected
                        # keeps a FORBIDDEN from stamping the FA cache.
                        resp = self._ace_send(ace, ace_idx, {
                            'method': 'start_feed_assist',
                            'params': {'index': slot}})
                        if not self._resp_rejected(resp):
                            ace._feed_assist_per_ace[ace_idx] = slot
                            fwd_assist = True
                    _s, end, _en = self.queue_move(ext, dist, speed,
                                                      CHOREO_ACCEL)
                    self._wait_move(toolhead, end)
            if fwd_assist:
                self._ace_send(ace, ace_idx, {
                    'method': 'stop_feed_assist', 'params': {'index': slot}})
                ace._feed_assist_per_ace[ace_idx] = -1
            self._say('head %d: cold-pull done (%d mm confirmed reclaimed '
                      'at ACE)' % (self._dh(head), int(reclaimed)))

            # 4. Heater off (stock does M104 S0 right after the pull).
            pheaters.set_temperature(heater, 0.)

            # 5. ACE retract to the SWAP length, minus what the interleaved
            # cold-pull unwinds already reclaimed. The bg unload is the unload
            # HALF OF A SWAP, so it retracts swap_retract_length exactly like
            # the inline swap (ace.py Z.7838): get_swap_retract_length honors
            # the per-ACE/per-slot overrides, and 0 -> the full retract_length
            # (also override-aware). Was a bug: bg pulled the full retract_length
            # (~1879) even with swap_retract_length=900 -> ~1000mm over-retract.
            self.state[head] = 'RETRACT'
            try:
                _srl = int(ace.get_swap_retract_length(ace_idx, slot))
            except Exception:
                _srl = 0
            try:
                full = _srl if _srl > 0 else int(
                    ace.get_retract_length(ace_idx, slot))
            except Exception:
                full = 1950
            rest = max(0, full - int(reclaimed))
            self._check_docked(toolhead, ext, head)
            # Never send a retract into a still-running rollback: wait idle.
            deadline = self.reactor.monotonic() + 15.0
            while self.reactor.monotonic() < deadline:
                st = self._slot_status(ace, ace_idx, slot)
                if not any(m in st for m in ('rollback', 'feeding')):
                    break
                self._pause(0.3)
            self._pause(1.0)
            if rest > 0:
                # Short-first, decoder-verified (bg analog of the inline
                # short-probe-retract): pull BG_UNLOAD_PROBE_RETRACT first
                # and confirm the ACE really moved (decoder span) before
                # committing the rest. Catches a stuck filament after ~150mm
                # instead of grinding a full rollback into it, and bounds a
                # failure to <=short (recoverable) for a clean inline
                # handover. NO pick-abort inside a rollback: the ace.py
                # wait-hook makes a toolchange WAIT for us.
                short = min(int(BG_UNLOAD_PROBE_RETRACT), rest)
                # Dwell-fan window 1 (bg twin of the inline bulk-retract
                # window): passive dock wait, no heat phase, no coil
                # measurement on this head. OFF before the pin gate below;
                # the _run_* finally is the backstop for every raise.
                self._dwell_fan(ace, head, True)
                self._say('head %d: ACE %d slot %d retract %d mm (%d short + '
                          '%d rest, probe+decoder-verified) @%d'
                          % (self._dh(head), self._dh(ace_idx),
                             self._dh(slot), rest, short, rest - short,
                             unwind_speed))
                # -- short (decoder-gated) --
                _sk = {'v': False}
                def _do_short(_o=_sk):
                    _o['v'] = self._ace_unwind(ace, ace_idx, slot, short,
                                               unwind_speed, wait=True,
                                               retries=4)
                _sps = ace._retract_with_decoder_span(ace_idx, slot, _do_short)
                self._bg_dec_log(ace, head, slot, 'bg-short', short, _sps)
                if not _sk['v']:
                    raise RuntimeError('bg short retract not confirmed by the '
                                       'device (slot %d)' % slot)
                _span = _sps[0]
                if _span is not None and _span < short * BG_UNLOAD_STALL_FRAC:
                    # decoder: the ACE barely moved -> filament stuck. Do NOT
                    # commit the rest (a fixed-length V2 rollback into a stuck
                    # filament grinds/strands). Bounded to <=short ->
                    # clean handover to the inline unload (probe + hot retry).
                    raise RuntimeError(
                        'bg short retract STALLED (decoder span %s < %d of '
                        '%dmm) - filament likely stuck, handing to inline'
                        % (_span, int(short * BG_UNLOAD_STALL_FRAC), short))
                # The V2 decoder above is the ONE stuck gate (the push-probe
                # that stood here was removed: the presence-gate pin gives no
                # edges either way, see the EDGE-VERIFY POST-MORTEM). Raw-pin
                # diagnostic: after a SUCCESSFUL short retract the tail should
                # have passed the gate - if runout_buttun_state tracks that
                # reliably it becomes the future V1 gate (log-only for now).
                self._gpio_diag(head, 'after short retract (expected clear)')
                # -- rest (committed; _ace_unwind still verifies completion) --
                rest2 = rest - short
                if rest2 > 0:
                    _rk = {'v': False}
                    def _do_rest(_o=_rk):
                        _o['v'] = self._ace_unwind(ace, ace_idx, slot, rest2,
                                                   unwind_speed, wait=True,
                                                   retries=4)
                    _spr = ace._retract_with_decoder_span(ace_idx, slot,
                                                          _do_rest)
                    self._bg_dec_log(ace, head, slot, 'bg-rest', rest2, _spr)
                    if not _rk['v']:
                        raise RuntimeError('bg rest retract not confirmed by '
                                           'the device (slot %d)' % slot)
                # PIN GATE (the bg twin of the inline pin-first unload
                # verify, same [ace] unload_gpio knob): after the FULLY
                # verified retract the toolhead presence pin must read
                # clear. A pin still True with healthy decoder spans = the
                # strand separated (remnant or cold-pull-stretched tail left
                # in the head) - a failure geometry the decoder physically
                # cannot see, and the next feed would ram the occupied path.
                # Gated AFTER the rest, not the short: the rest pull gives a
                # lagging stretched tail more travel time to clear. Pin
                # unreadable (None) -> fail open. A false True costs one
                # wasted inline unload (~60s); a missed remnant costs a
                # wedged double-feed pause.
                self._dwell_fan(ace, head, False)
                _pin = self._gpio_diag(head,
                                       'after full retract (pin gate)')
                if _pin is True and getattr(ace, 'unload_gpio', True):
                    raise ToolheadNotClear(
                        'toolhead still holds filament after the verified '
                        'ACE retract (presence pin) - remnant or stretched '
                        'tail')
            retract_done['v'] = True

            # 6. Bookkeeping: mark the head empty through the OFFICIAL paths.
            # The motion sensor cannot see bg moves (they bypass the extruder
            # trapq), so presence is set via the helper API - runout events
            # for this head are suppressed (step 0), so this can never PAUSE.
            self.state[head] = 'BOOKKEEP'
            try:
                sensor = self.printer.lookup_object(
                    'filament_motion_sensor e%d_filament' % head, None)
                if sensor is not None:
                    sensor.runout_helper.note_filament_present(False, True)
            except Exception as e:
                self._say('head %d: sensor state update failed (%s) - the '
                          'next inline unload probe will clear it'
                          % (self._dh(head), e))
            ace._head_source[head] = None
            # An unloaded head has no pending flow verify - drop a stale
            # pick-check flag from an earlier bg load whose arrival never came
            # (and any prime deficit with it, same reasoning).
            try:
                ace._bg_load_unverified.discard(head)
                getattr(ace, '_bg_prime_deficit', {}).pop(head, None)
            except Exception:
                pass
            try:
                ace._save_head_source()
                ace._push_slot_rfid_to_extruder(head)
                ace._push_rfid_info()
            except Exception:
                pass

    def _run_swap(self, head, un, ld, temp, soft, anti_ooze,
                  unload_temp=None, purge=None):
        """Background SWAP: optional unload (un=(ace,slot) or None when the
        head is already empty), then feed+grip+prime of ld=(ace,slot).
        Unload failures go through _fail_unload;
        load failure semantics live in _load_core: ANY abort after filament
        movement leaves it STAGED in the path (at the sensor or mid-bowden,
        position agnostic - follow-ups are sensor-gated), a GRIP/PRIME
        abort declares the head loaded (it factually is)."""
        ace = self.printer.lookup_object('ace')
        toolhead = self.printer.lookup_object('toolhead')
        ext = self.printer.lookup_object(self._ext_name(head))
        heater = ext.get_heater()
        pheaters = self.printer.lookup_object('heaters')
        retract_done = {'v': False}
        try:
            if un is not None:
                self._unload_core(head, ace, toolhead, ext, heater,
                                  pheaters, un[0], un[1],
                                  (unload_temp or temp), soft,
                                  retract_done)
                if self.load_enabled:
                    self._say('head %d: unload half done - loading ACE %d '
                              'slot %d in the background'
                              % (self._dh(head), self._dh(ld[0]),
                                 self._dh(ld[1])))
        except Exception as e:
            self._ace_quiesce(ace, un[0], un[1], 'bg swap unload abort')
            self._fail_unload(head, heater, pheaters, e, retract_done['v'])
            self._busy.discard(head)
            return
        if not self.load_enabled:
            # Unload-only mode: stop after the unload half. The head is
            # verifiably empty and bookkept (sensor False, head_source
            # None), so the arrival swap skips its unload and loads inline
            # - phase3-verified, same as every bg-load abort handover.
            self.state[head] = 'DONE'
            self._say('head %d: %s (load_enabled=False) - the arrival '
                      'swap loads inline'
                      % (self._dh(head),
                         'BACKGROUND UNLOAD COMPLETE' if un is not None
                         else 'already empty, nothing to do'))
            self._busy.discard(head)
            return
        try:
            self._load_core(head, ace, toolhead, ext, heater, pheaters,
                            ld[0], ld[1], temp, anti_ooze, purge=purge)
            self.state[head] = 'DONE'
            # A grip/prime pick-abort returns normally (the head IS loaded)
            # but may have recorded a prime deficit - say so instead of
            # claiming "primed" (the old message lied in that case).
            _deficit = None
            try:
                _deficit = getattr(ace, '_bg_prime_deficit', {}).get(head)
            except Exception:
                pass
            if _deficit is not None:
                self._say('head %d: BACKGROUND SWAP handed over with a '
                          'SHORT prime (%d mm pending) - the arrival pick '
                          'tops it up before printing'
                          % (self._dh(head), int(_deficit)))
            else:
                self._say('head %d: BACKGROUND SWAP COMPLETE - ACE %d slot '
                          '%d loaded + primed; the arrival toolchange is a '
                          'no-op'
                          % (self._dh(head), self._dh(ld[0]),
                             self._dh(ld[1])))
        except Exception as e:
            reason = str(e) or e.__class__.__name__
            self.state[head] = 'FAILED:%s' % reason
            self._ace_quiesce(ace, ld[0], ld[1], 'bg load abort')
            try:
                pheaters.set_temperature(heater, 0.)
            except Exception:
                pass
            self._say('head %d: LOAD FAILED (%s) - the arrival swap loads '
                      'inline' % (self._dh(head), reason))
            logging.exception('[multiACE] [bg-load] head %d failed' % head)
        finally:
            self._dwell_fan(ace, head, False)
            self._busy.discard(head)

    def _load_bookkeeping(self, head, ace, ace_idx, slot):
        """Mark the head loaded through the same fields the inline load
        stamps (head_source shape = filament_feed_ace FEED_ACT_LOAD finish,
        incl. the RFID identity), and set the sensor present via the
        official helper - which also LIFTS the runout suppression the
        unload half installed."""
        si = {}
        try:
            info = ace._info_per_ace.get(ace_idx) or {}
            slots = info.get('slots') or []
            if slot < len(slots) and isinstance(slots[slot], dict):
                si = slots[slot]
        except Exception:
            pass
        _ident = {
            'ace_index': ace_idx,
            'slot': slot,
            'type': si.get('type', 'PLA'),
            'color': ace.rgb2hex(*si.get('color', (0, 0, 0))),
            'brand': si.get('brand', 'Generic'),
        }
        # V2 identity snapshot: resolve the slot's override into the stamp
        # (declared truth beats raw RFID) before the same-lane inherit -
        # both getattr-guarded like every cross-file helper: ace.py is not
        # part of the bundle sha and CAN be older than this file on the
        # same printer.
        _ovl = getattr(ace, '_overlay_override', None)
        if _ovl is not None:
            _ident = _ovl(ace_idx, slot, _ident)
        _inh = getattr(ace, '_inherit_prev_capture', None)
        if _inh is not None:
            _ident = _inh(head, ace_idx, slot, _ident)
        ace._head_source[head] = _ident
        try:
            ace._save_head_source()
            ace._ghost_heads.discard(head)
        except Exception:
            pass
        # Flow is UNVERIFIED: grip/prime ran as stealth moves no sensor can
        # judge (EDGE-VERIFY POST-MORTEM above). The arrival swap's no-op
        # path runs the pick-time flow check on this one-shot flag
        # (ace._bg_pick_flow_check - coil + motion sensor, LOG-ONLY stage).
        try:
            ace._bg_load_unverified.add(head)
        except Exception:
            pass
        try:
            sensor = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % head, None)
            if sensor is not None:
                sensor.runout_helper.note_filament_present(True, True)
        except Exception as e:
            self._say('head %d: sensor present-flag update failed (%s)'
                      % (self._dh(head), e))
        try:
            ace._push_slot_rfid_to_extruder(head)
            ace._push_rfid_info()
        except Exception:
            pass
    def _load_core(self, head, ace, toolhead, ext, heater, pheaters,
                   ace_idx, slot, temp, anti_ooze, purge=None):
        """Background LOAD of ld slot into an EMPTY parked head: sensor-
        stopped bowden feed (heat runs in parallel - the transport needs
        none), extruder grip with forward-assist, prime through the OPEN
        dock into the bin, anti-ooze end retract. Abort semantics: ANY
        abort after filament movement leaves it STAGED in the path (at the
        sensor or mid-bowden - _bg_staged / _bg_left_empty; every follow-up
        feed is sensor-gated, the same-slot arrival just continues it, a
        different-slot load is refused); grip/prime aborts bookkeep the
        head as loaded and return normally. Only 'none' (nothing moved)
        hands over a genuinely empty head."""
        st = self._slot_status(ace, ace_idx, slot)
        try:
            if ace._is_empty_status(st):
                raise RuntimeError('target slot %d is empty (%s)'
                                   % (slot, st))
        except AttributeError:
            pass

        # 1. FEED: bowden transport, stopped at the TOOLHEAD SENSOR - the
        # inline load's stop marker, hardware-agnostic (V1+V2; the sensor
        # fires on a parked head). The heater target is set
        # NOW so the melt zone is ready by grip time (parallel, not serial).
        # get_load_length is the padded upper bound like inline; the sensor
        # cuts it short, so neither hardware rams the gears.
        self.state[head] = 'LD_FEED'
        pheaters.set_temperature(heater, temp)
        feed_speed = ACE_UNWIND_SPEED_FALLBACK
        try:
            feed_speed = int(ace.get_feed_speed(ace_idx))
        except Exception:
            pass
        feed_len = int(ace.get_load_length(ace_idx, slot))
        self._check_docked(toolhead, ext, head)
        self._say('head %d: ACE %d slot %d bg feed up to %d mm @%d to the '
                  'toolhead sensor (heating to %.0f in parallel)'
                  % (self._dh(head), self._dh(ace_idx), self._dh(slot),
                     feed_len, feed_speed, temp))
        # Feed with the INLINE load's retry discipline: on a no-arrival, back off load_retry_retract (50mm,
        # per-head knob) and re-push the full length - the sensor stops it,
        # so a re-push onto filament already partway in is harmless (V2
        # self-stops on resistance, the extruder gears do not turn during a
        # feed - nothing grinds). Background time is free, so the retries
        # cost nothing visible. Retract is TIME-paced (short unwinds are
        # too fast for the ~1s status updates).
        _retries = 0
        try:
            _retries = int(ace.head_load_retry.get(head, ace.load_retry))
        except Exception:
            _retries = int(getattr(ace, 'load_retry', 3))
        try:
            _retry_back = int(ace.head_load_retry_retract.get(
                head, ace.load_retry_retract))
        except Exception:
            _retry_back = 50
        feed_res, _fsp = 'none', (None, 0, None, None)
        _ever_moved = False
        # Dwell-fan window 2 (bg twin of the inline bowden-feed window):
        # the feed incl. retries is passive for the head - it just
        # sits docked while the ACE pushes. OFF right after the loop; the
        # _run_swap finally is the backstop for every raise.
        self._dwell_fan(ace, head, True)
        for _attempt in range(_retries + 1):
            if _attempt > 0:
                self._check_docked(toolhead, ext, head)
                self._say('head %d: feed retry %d/%d - %dmm back, re-push '
                          'to the sensor'
                          % (self._dh(head), _attempt, _retries, _retry_back))
                self._ace_unwind(ace, ace_idx, slot, _retry_back,
                                 feed_speed, wait=False)
                self._pause(max(1.0, _retry_back / max(feed_speed, 1) + 0.5))
            # V2 decoder span logged ('bg-feed') as telemetry.
            feed_res, _fsp = self._ace_feed_to_gears(
                ace, ace_idx, slot, feed_len, feed_speed, head)
            self._bg_dec_log(ace, head, slot, 'bg-feed', feed_len, _fsp)
            if feed_res in ('ok', 'stale'):
                break
            if feed_res == 'partial':
                _ever_moved = True
        self._dwell_fan(ace, head, False)
        if feed_res == 'stale':
            pheaters.set_temperature(heater, 0.)
            raise RuntimeError(
                'toolhead sensor of head %d reads PRESENT although the head '
                'is empty (stale latch) - display/web unload it once, then '
                'retry' % self._dh(head))
        if feed_res != 'ok':
            pheaters.set_temperature(heater, 0.)
            if feed_res == 'partial' or _ever_moved:
                # Filament moved but never reached the sensor: LEAVE IT IN
                # THE PATH (no cleanup retract - a commanded-length pull can
                # rip a short feed clean out of the slot gate). Every follow-up is sensor-gated: the same-slot
                # arrival load simply pushes it the rest of the way; a
                # different-slot load is refused by the STAGED guard. Same
                # bookkeeping as the post-arrival abort - one staged state
                # for every bg-load abort after movement, position agnostic.
                try:
                    ace._bg_left_empty.add(head)
                    getattr(ace, '_bg_staged', {})[head] = (ace_idx, slot)
                except Exception:
                    pass
                self._say('head %d: feed never reached the sensor after %d '
                          'attempt(s) - filament stays in the path (STAGED, '
                          'mid-bowden); the arrival load continues it'
                          % (self._dh(head), _retries + 1))
            raise RuntimeError('feed not confirmed by the device')

        # FA ON from here: start the ACE forward-assist right after
        # the feed so it holds/pushes the filament at the gear approach through
        # the heat-wait + grip, instead of only at grip time. FA alone can't
        # pass the stationary gears - the GRIP below turns the extruder WITH it.
        # Backoff retry: the V1 rejects this FORBIDDEN while its feed motor
        # winds down (_fa_on). Not armed after retries = tolerable here (V1
        # freewheels; grip re-tries below; the pick re-arm covers the print).
        fa_armed = self._fa_on(ace, ace_idx, slot)
        self._say('head %d: fed to the toolhead sensor, FA %s'
                  % (self._dh(head), 'ON' if fa_armed else
                     'not armed (busy) - grip/pick will re-arm'))

        gripped = False
        try:
            # 2. Wait out the heat (usually already there after the feed).
            self._check_docked(toolhead, ext, head)
            deadline = self.reactor.monotonic() + HEAT_TIMEOUT
            _retgt_said = False
            while True:
                cur, _tgt = heater.get_temp(self.reactor.monotonic())
                if cur >= temp - HEAT_HYST:
                    break
                if _tgt < temp - HEAT_HYST:
                    # The print's toolchange choreography can zero a PARKED
                    # head's target any time (departed-tool M104 S0 right
                    # after we set it -> dead wait -> heat timeout). The head
                    # is
                    # parked and bg-busy, the target is OURS: re-assert.
                    if not _retgt_said:
                        self._say('head %d: heater target was reset '
                                  'externally (%.0f) - re-asserting %.0f'
                                  % (self._dh(head), _tgt, temp))
                        _retgt_said = True
                    pheaters.set_temperature(heater, temp)
                if self.reactor.monotonic() > deadline:
                    raise RuntimeError('heat timeout (%.0f/%.0f)'
                                       % (cur, temp))
                self._check_docked(toolhead, ext, head)
                self._pause(0.5)

            # Prime target computed up front (used by the grip/prime abort
            # handler below to record how much purge is still MISSING - the
            # arrival pick-check tops that up, see _bg_prime_deficit).
            # Per-pair PURGE= from the stamp wins (see cmd_ACE_BG_SWAP);
            # fallback = the global override/config knob, as before.
            if purge is not None:
                prime_target = float(purge) + BG_LOAD_PRIME_EXTRA
            else:
                prime_target = (float(ace.get_purge_length() or 0) or 80.) \
                    + BG_LOAD_PRIME_EXTRA
            primed = 0.
            ooze_done = False

            # 3. GRIP: the extruder pulls the margin through its gears,
            # forward-assist keeps the ACE feeding WITH it (a V2 just
            # brakes otherwise). Segmented so a pick aborts within one
            # short segment, not a long move.
            self.state[head] = 'LD_GRIP'
            # PRESS-then-grip (see the BG_LOAD_PRESS_* const note): push the
            # tip from the sensor INTO the gear nip with the ACE feed motor
            # BEFORE the gears start turning - the inline mechanic. A pick
            # during the press aborts via _check_docked into the post-arrival
            # staged path (filament at the sensor); the press
            # command itself is bounded and self-terminating, so an abort
            # leaves no runaway feed behind. A busy rejection is RETRIED
            # with pacing (BG_LOAD_PRESS_RETRIES - on V1 the first attempt
            # is deterministically FORBIDDEN in the post-stop wind-down);
            # still busy after the ladder -> skip the press
            # (grip proceeds = the pre-retry behaviour).
            self._check_docked(toolhead, ext, head)
            # V2 keeps its own bound (50); the V1 bound follows the [ace]
            # seat_overshoot_length knob (one knob for inline + bg V1;
            # getattr default covers an older ace.py). 0 = press
            # off, matching the inline knob semantic - the GRIP below
            # still runs.
            if ace._is_v2_idx(ace_idx):
                press = BG_LOAD_PRESS_V2
            else:
                press = int(getattr(ace, 'seat_overshoot_length',
                                    BG_LOAD_PRESS_V1))
            if press <= 0:
                logging.info('[bg-swap] head %d: seat press disabled '
                             '(seat_overshoot_length 0)' % head)
            else:
                self._ace_send(ace, ace_idx, {
                    'method': 'stop_feed_assist', 'params': {'index': slot}})
                presp = None
                for _pa in range(BG_LOAD_PRESS_RETRIES):
                    presp = self._ace_send(ace, ace_idx, {
                        'method': 'feed_filament',
                        'params': {'index': slot, 'length': int(press),
                                   'speed': int(BG_LOAD_PRESS_SPEED)}})
                    if not self._resp_rejected(presp):
                        break
                    if _pa < BG_LOAD_PRESS_RETRIES - 1:
                        pdl = (self.reactor.monotonic()
                               + BG_LOAD_PRESS_RETRY_DELAY)
                        while self.reactor.monotonic() < pdl:
                            self._check_docked(toolhead, ext, head)
                            self._pause(0.3)
                if self._resp_rejected(presp):
                    self._say('head %d: nip press rejected (busy) after %d '
                              'attempts - gripping without press'
                              % (self._dh(head), BG_LOAD_PRESS_RETRIES))
                else:
                    # [diag] measure how far the press ACTUALLY moved. The
                    # commanded length is only an upper bound - the ACE stops
                    # by itself at resistance, so "<=50 mm" told us nothing
                    # about the real travel. A LONG-TAPER tip (sensor PRESENT,
                    # pick-check flow 0 even after press + re-grip) has two
                    # possible readings that the log cannot otherwise tell
                    # apart: the taper is longer than the
                    # press (span ~= commanded -> push further), or a thin
                    # taper already trips the stop (span << commanded -> more
                    # length would never be used). The span answers it.
                    def _do_press():
                        pdeadline = (self.reactor.monotonic()
                                     + press / float(BG_LOAD_PRESS_SPEED)
                                     + 1.0)
                        # COUPLED press: turn the gears
                        # WHILE the ACE pushes. An energized, holding
                        # extruder is a locked wall - the V2 resistance
                        # self-stop fires AT that wall (slot 'feeding', span
                        # 0). E is sized to span only the ACE
                        # window - the GRIP right after does the real
                        # pull-through; grip speed, segmented so a pick
                        # aborts within one segment.
                        _e_len = (press / float(BG_LOAD_PRESS_SPEED)
                                  + 1.0) * BG_LOAD_GRIP_SPEED
                        for _ei in range(2):
                            self._check_docked(toolhead, ext, head)
                            _ps, _pend, _pen = self.queue_move(
                                ext, _e_len / 2., BG_LOAD_GRIP_SPEED,
                                CHOREO_ACCEL)
                            self._wait_move(toolhead, _pend)
                        while self.reactor.monotonic() < pdeadline:
                            self._check_docked(toolhead, ext, head)
                            self._pause(0.3)
                        self._ace_send(ace, ace_idx, {
                            'method': 'stop_feed_filament',
                            'params': {'index': slot}})
                    _psp = (None, 0, None, None)
                    try:
                        _psp = ace._retract_with_decoder_span(
                            ace_idx, slot, _do_press)
                    except Exception:
                        _do_press()
                    self._bg_dec_log(ace, head, slot, 'bg-press', press, _psp)
                    # Zero span with turning gears = slip at the ACE (see
                    # ace.note_seat_press_span); tolerated on an old ace.py.
                    try:
                        if hasattr(ace, 'note_seat_press_span'):
                            ace.note_seat_press_span(ace_idx, slot, _psp[0])
                    except Exception:
                        pass
                    self._say('head %d: tip pressed into the gear nip '
                              '(<=%d mm, moved %s%s)'
                              % (self._dh(head), int(press),
                                 ('%s mm' % _psp[0]) if _psp[0] is not None
                                 else 'n/a (V1)',
                                 ', attempt %d' % (_pa + 1) if _pa else ''))
            self._fa_on(ace, ace_idx, slot)
            # Tip sits at the toolhead sensor (both hardwares) -> the grip
            # covers sensor->gears + seats the tip through them. NO edge
            # verify here: the pin is a presence gate, a perfect grip reads
            # the same 0 edges as a failed one (EDGE-VERIFY POST-MORTEM).
            grip = BG_LOAD_GRIP_SEAT
            seg = grip / 4.
            for _i in range(4):
                self._check_docked(toolhead, ext, head)
                _s, end, _en = self.queue_move(ext, seg, BG_LOAD_GRIP_SPEED,
                                               CHOREO_ACCEL)
                self._wait_move(toolhead, end)
            gripped = True

            # 4. PRIME through the open dock into the bin (fills the melt
            # zone, proves flow visually - no sensor can measure flow, see
            # the EDGE-VERIFY POST-MORTEM). Same knob as the inline flush
            # (swap_purge_length / Pro override; 0 = stock default 80).
            self.state[head] = 'LD_PRIME'
            # Inline knob + bg bonus (see BG_LOAD_PRIME_EXTRA: the bg prime
            # must also FILL the melt zone and has no cutoff/clean).
            # FINE-chunked (BG_LOAD_PRIME_CHUNK, was 4x ~30mm): caps the
            # blind purge a mid-chunk pick keeps extruding while the stock
            # T carries the head - see the const block.
            while primed < prime_target - 1e-6:
                self._check_docked(toolhead, ext, head)
                seg = min(BG_LOAD_PRIME_CHUNK, prime_target - primed)
                _s, end, _en = self.queue_move(ext, seg,
                                               BG_LOAD_PRIME_SPEED,
                                               CHOREO_ACCEL)
                self._wait_move(toolhead, end)
                primed += seg
                # Spool book-keeping: these are stealth moves on our OWN
                # trapq, so the extruder axis the ace.py sampler watches
                # never sees them - book the purge explicitly or a bg swap
                # would look free (SPOOL_* note in ace.py).
                try:
                    ace.book_spool_use(head, seg, 'bg-prime')
                except AttributeError:
                    pass

            # 5. Anti-ooze end retract: mirrors the slicer un-retract the
            # ARRIVAL will push back (the runtime arrival no-ops on the
            # already-loaded head, so OUR end state must match the value
            # the preflight stamped - the ANTI_OOZE contract).
            if anti_ooze > 0.:
                self._check_docked(toolhead, ext, head)
                _s, end, _en = self.queue_move(ext, -float(anti_ooze),
                                               BG_LOAD_RETRACT_SPEED,
                                               CHOREO_ACCEL)
                self._wait_move(toolhead, end)
            ooze_done = True
        except Exception as e:
            self._ace_send(ace, ace_idx, {
                'method': 'stop_feed_assist', 'params': {'index': slot}})
            ace._feed_assist_per_ace[ace_idx] = -1
            if gripped:
                # The gears hold the filament and the melt zone is (being)
                # filled - the head IS loaded. Bookkeep and hand over: the
                # arrival no-op + its FA arm continue printing. The heater
                # target is left as-is (the slicer preheat owns it by now;
                # cooling here could strand the arrival cold).
                #
                # A pick DURING the prime cuts the purge short - without a
                # top-up the melt zone keeps the OLD colour and the print
                # resumes contaminated/lean. Record the missing millimetres
                # (plus "cushion never retracted": deficit exists, even at
                # 0.0) - the arrival's pick-check pushes exactly that
                # remainder at the discard position and then establishes
                # the anti-ooze cushion (_bg_pick_flow_check).
                deficit = max(0., prime_target - primed)
                if deficit > 0. or not ooze_done:
                    try:
                        getattr(ace, '_bg_prime_deficit', {})[head] = deficit
                    except Exception:
                        pass
                self._say('head %d: pick during grip/prime (%s) - head is '
                          'loaded (prime %d/%d mm), bookkeeping and handing '
                          'over%s'
                          % (self._dh(head),
                             str(e) or e.__class__.__name__,
                             int(primed), int(prime_target),
                             '; the arrival pick tops up the missing '
                             '%d mm' % int(deficit) if deficit > 0.
                             else ' (small blob possible)'))
                self._load_bookkeeping(head, ace, ace_idx, slot)
                return
            # Heat/pick abort AFTER a confirmed sensor arrival: LAZY CLEANUP
            # - leave the filament STAGED at the sensor
            # instead of retracting it. Every follow-up path is safe: the
            # same-slot inline arrival feeds a few mm and its sensor stop
            # fires immediately (fastest recovery, ~45s saved vs retract +
            # full re-feed); a manual display unload works because the
            # sensor reads present (the filament really is there - the
            # presence state is TRUE, not a stale latch); a different-slot
            # load is refused by the ghost guard until unloaded. The
            # mid-bowden 'partial' case stages the same way (no retract) -
            # one staged model for every abort.
            self._say('head %d: abort before grip (%s) - filament stays '
                      'STAGED at the toolhead sensor; the arrival/inline '
                      'load continues from there'
                      % (self._dh(head), str(e) or e.__class__.__name__))
            pheaters.set_temperature(heater, 0.)
            self._gpio_diag(head, 'staged after abort (expected present)')
            # Bookkeep the staged state: the arrival swap must SKIP its
            # unload half (no head_source - it would guess a wrong slot from
            # the sensor-present state) and a manual unload must route its
            # retract to THIS slot. Both consumed in ace.py.
            try:
                ace._bg_left_empty.add(head)
                getattr(ace, '_bg_staged', {})[head] = (ace_idx, slot)
            except Exception:
                pass
            raise RuntimeError(str(e) or e.__class__.__name__)

        # 6. Success: assist off; cool ONLY if the target is still ours
        # (a slicer preheat M104 for the arrival may have overwritten it
        # mid-load - that target belongs to the print, leave it).
        self._ace_send(ace, ace_idx, {
            'method': 'stop_feed_assist', 'params': {'index': slot}})
        ace._feed_assist_per_ace[ace_idx] = -1
        try:
            if abs(float(getattr(heater, 'target_temp', 0.)) - temp) < 1.:
                pheaters.set_temperature(heater, 0.)
        except Exception:
            pheaters.set_temperature(heater, 0.)
        self._load_bookkeeping(head, ace, ace_idx, slot)


def load_config(config):
    return AceBgSwap(config)
