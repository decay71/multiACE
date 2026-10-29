"""Swap cluster of MultiAce: ACE_SWAP_HEAD and its tail (swap temp, dwell
fan, purge/retract lengths), the resumable recovery pause with its position
restore and load-slip classification, the discard wipe, and the arrival side
of a background swap (wait, park, FA re-arm, pick-time flow check).

Mixin of MultiAce, imported by ace.py. ``self`` is the MultiAce instance;
state is created in ace.py.__init__.
"""
import logging
import re
import time

from .ace_protocol import GATE_AVAILABLE, GATE_UNKNOWN
from .ace_resistance import RESISTANCE_PAUSE_STRIKES

# Pick-time flow check (LOG-ONLY stage): the arrival toolchange onto a head a
# BACKGROUND swap loaded runs a short verify extrude at the DISCARD position -
# the bg grip/prime ran as stealth moves the motion sensor cannot see, so bg
# "loaded" is bookkeeping truth, not flow truth. The check is the phase3
# pattern (filament_feed_ace LOAD_EXTRUDING): inductance-coil freq sampled
# during a real extrude - the only working extrude signal on ACE heads.
# Because the head is ACTIVE here the push also runs through the normal
# trapq, so the motion sensor works again (tracked extruder advance). The
# push must first refill the arrival's ANTI_OOZE cushion before real flow
# starts, so commanded push = anti_ooze + PICK_CHECK_FLOW_PUSH, and the
# cushion is retracted back afterwards (anti-ooze contract stays intact).
PICK_CHECK_FLOW_PUSH = 10.
PICK_CHECK_PUSH_FEEDRATE = 400     # mm/min, = the phase3 extrude feedrate
PICK_CHECK_COIL_SAMPLES = 5        # phase3 non-soft sampling: 5 x 0.5 s
PICK_CHECK_COIL_INTERVAL = 0.5
# Verdict/gate threshold, valid ONLY together with the enforced minimum
# push below. Derived from logged pickcheck deltas (not the shipped 5000,
# not the stock 1500): real NO_FLOW and real FLOW sit far apart on either
# side of 1000. The delta scales with push length, so the push is a FIXED
# minimum, independent of anti_ooze.
PICK_CHECK_COIL_THRESHOLD = 1000
PICK_CHECK_MIN_PUSH = 20.
# Gate recovery (pick_gate in [ace_bg_swap], default True): the FIRST
# NO_FLOW gets a re-grip on the now-ACTIVE head (full motion-system
# authority - completes the seat the docked stealth grip could not
# guarantee) plus a re-measure; only the SECOND NO_FLOW
# escalates to a resumable pause at the caller.
PICK_GATE_REGRIP = 40.
PICK_GATE_REGRIP_FEEDRATE = 300
# ACE push INTO the re-grip: an extruder-only re-grip cannot drag a LONG
# thin taper into the nip - the gears sit on sub-diameter material and only
# the idle forward-assist pushes from behind. So the re-grip feeds the slot
# a short bounded push in parallel, the same push the inline phase3
# wiggle-retries use. V2 self-stops at resistance; V1 is open-loop ->
# bounded length. The feed lands in the busy window right after the assist
# stop -> paced retry ladder.
PICK_GATE_ACE_PUSH_V2 = 40.
PICK_GATE_ACE_PUSH_V1 = 30.
PICK_GATE_ACE_PUSH_SPEED = 20
PICK_GATE_ACE_PUSH_RETRIES = 3
PICK_GATE_ACE_PUSH_RETRY_DELAY = 1.0
# Turbulence guard. A clean flow push only DIPS the coil (back-pressure:
# dip = start-min > 0, up-swing max-start ~ 0). A chew/bind/stick-slip
# episode swings the coil UP too, and judging on max(|dip|,|up|) would read
# that up-swing as flow and pass a real airprint. The verdict therefore
# judges on the DIP only; an up-swing >= this threshold means the reading
# ran inside turbulence -> settle + re-measure once, then judge the settled
# push. Healthy up-swing is ~0-few hundred, so this never trips a clean load.
PICK_TURBULENCE_UPSWING = 3000.
PICK_TURBULENCE_SETTLE = 2.0
# POST-(RE)START NO-OP WIPE: after a pause /
# power-loss recovery the resume replay re-runs the arrival ACE_SWAP_HEAD,
# which is a NO-OP on the already-loaded head - no flush, no wipe - while
# the head sat hot and drooling the whole pause (a blob that gets dragged
# into the print). For RESUME_NOOP_WIPE_WINDOW seconds
# after every print_stats:start, the first no-op swap on the ACTIVE head
# runs the shared discard wipe (_discard_wipe, the pickup-clean excursion)
# before printing continues. Skipped when the bg pick-check already wiped
# (BG_PICK_WIPE); cleared by any FULL swap (its flush+wipe covers). Fires
# at most once per (re)start.
RESUME_NOOP_WIPE_WINDOW = 180.
# Nozzle wipe at the bg pick (default on). A bg load primes through the OPEN
# DOCK while the head is docked - it cannot reach the discard/wipe edge, so
# no ooze-cutoff/nozzle-clean runs (a normal INLINE swap's INNER_FLUSH does
# both). Result: a bg-loaded head arrives with strings the first print move
# drags in. The pick-check already sits at the discard
# position (the flow push runs there), so scrape the nozzle with the stock
# clean macro before restoring the print position - no extra travel.
BG_PICK_WIPE = True

# min_extrude_temp of the U1; a swap-head target below it is no print temp.
SWAP_MIN_EXTRUDE_TEMP = 170


class SwapMixin:
    def get_swap_retract_length(self, ace_idx, slot):
        """Swap unload retract length with per-ACE/per-slot override
        priority, falling back to the global swap_retract_length. A value
        of 0 (empty or explicit) means 'use the normal default retract'."""
        v = self._ace_section_swap_retract_length_slot.get((ace_idx, slot))
        if v is not None:
            return v
        v = self._ace_section_swap_retract_length.get(ace_idx)
        if v is not None:
            return v
        return self.swap_retract_length

    def get_purge_length(self):
        """Flush LENGTH for the stock INNER_FLUSH_FILAMENT. A per-swap
        override (multiACE Pro) wins, else the swap_purge_length config
        value. 0 means 'use the stock default' (caller omits LENGTH=)."""
        if self._purge_length_override is not None:
            return self._purge_length_override
        return self.swap_purge_length

    def _restore_pos_for_pause(self, saved_pos):

        if not saved_pos:
            return
        try:
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command(
                'G0 Z%.3f F600' % (saved_pos[2] + 3.0))
            self.gcode.run_script_from_command(
                'G0 Y%.3f F12000' % saved_pos[1])
            self.gcode.run_script_from_command(
                'G0 X%.3f F12000' % saved_pos[0])
            self.gcode.run_script_from_command(
                'G0 Z%.3f F600' % (saved_pos[2] + 2.0))
            self.toolhead.wait_moves()
            logging.info(
                '[multiACE] Swap PAUSE: restored pos X=%.2f Y=%.2f Z=%.2f '
                '(pre-PAUSE, prevents RESUME-traverse ram)' % (
                    saved_pos[0], saved_pos[1], saved_pos[2]))
        except Exception as e:
            logging.info(
                '[multiACE] Swap PAUSE: pos restore failed: %s' % e)

    def _swap_back_to_orig_for_pause(self, switched_head, orig_ext_name):

        if not switched_head:
            return
        try:
            orig_head_idx = (0 if orig_ext_name == 'extruder'
                             else int(orig_ext_name.replace('extruder', '')))
            logging.info(
                '[multiACE] Swap PAUSE: switching active extruder back '
                'to %s before pause (was on swap head)' % orig_ext_name)
            self.gcode.run_script_from_command('T%d A0' % orig_head_idx)
            self.toolhead.wait_moves()
        except Exception as e:
            logging.info(
                '[multiACE] Swap PAUSE: T-switch back to %s failed: %s'
                % (orig_ext_name, e))

    GATE_KNOWN_WAIT = 5.0

    def _await_known_gate(self, gate_list, slot):
        """Gate of a slot, waiting up to GATE_KNOWN_WAIT while it reads
        unknown. A reconnect resets every gate to unknown until the next
        status frame; reading that as empty paused with a false
        "slot empty"."""
        deadline = self.reactor.monotonic() + self.GATE_KNOWN_WAIT
        while gate_list[slot] == GATE_UNKNOWN:
            if self.reactor.monotonic() >= deadline:
                logging.info('[multiACE] Swap: gate of slot %d still unknown '
                             'after %.1fs' % (slot, self.GATE_KNOWN_WAIT))
                break
            self.reactor.pause(self.reactor.monotonic() + 0.25)
        return gate_list[slot]

    # Feed-machine error states that prove the strand had reached the
    # toolhead when the load failed (filament_feed_ace: load_feeding ->
    # load_heating -> load_extruding -> load_finish).
    LOAD_ARRIVED_STATES = ('load_extruding',)

    def _feed_failed_after_arrival(self, head):
        """True when the feed module's error state for `head` says the
        failed load had already brought the filament to the toolhead."""
        try:
            module, channel = self.EXTRUDER_MAP[int(head)]
            ff = self.printer.lookup_object('filament_feed %s' % module, None)
            states = getattr(ff, 'channel_error_state', None)
            if states is None or not (0 <= channel < len(states)):
                return False
            return str(states[channel]) in self.LOAD_ARRIVED_STATES
        except Exception:
            return False

    def _load_slip_details(self, head, ace_index, slot):
        """Classify a failed ACE load into its two REAL modes and return
        (detail_msg, recovery_steps) for _pause_for_recovery.

        Used by the SWAP path and by the direct feed path in
        filament_feed_ace (a display/web load that fails): both fail for the
        same physical reasons, so they must say the same thing.

        Sensor clear = the filament NEVER REACHED the toolhead (no
        transport - spool/bowden/ACE problem; unload+reload is the right
        recovery) vs sensor present = the filament arrived and was gripped
        but the nozzle does not extrude (no flow - a clog; unload+reload
        cannot fix it and points the user at the wrong end). The toolhead
        sensor alone discriminates cleanly; the raw feed error stays in
        klippy.log. Fail-open to the transport wording if the sensor cannot
        be read. On a unit without a lane encoder the arrived case names
        BOTH ends (nozzle or tangle upstream), see below."""
        # Most specific evidence first: a fresh zero-span seat press (the
        # extruder turns during every press, so zero cannot be the old
        # energized-gear wall) = the ACE end is the problem, regardless of
        # what the toolhead sensor reads - a taut, non-moving strand can
        # leave the tip present while extrusion starves, and the no-flow
        # wording would then point at the wrong end (nozzle). Freshness
        # window generous (a retried load runs minutes); every re-press
        # refreshes or clears it.
        try:
            _pz = self._press_zero.get((int(ace_index), int(slot)))
        except Exception:
            _pz = None
        # Same verdict from the feed itself: the device ended the last
        # load attempt with slot_status 'feed_error' (its own slip check,
        # filament_feed_ace._note_feed_verdict). HW 2026-10-25: matte PLA
        # the ACE 2 gear could not grip, four feeds each stopped after
        # ~10 s, the toolhead never saw filament - NO TRANSPORT pointed at
        # the bowden while the spool was the problem.
        if _pz is None:
            try:
                _pz = getattr(self, '_load_feed_error', {}).get(
                    (int(ace_index), int(slot)))
            except Exception:
                _pz = None
        if _pz is not None and (self.reactor.monotonic() - _pz) < 1200.:
            detail = self._t('msg.pause_swap_load_ace_slip',
                head=self._disp(head), ace=self._disp(ace_index),
                slot=self._disp(slot))
            steps = [
                'Check the spool seat / path INTO MMU %d Slot %d (spool drag, lid, tangle)'
                    % (self._disp(ace_index), self._disp(slot)),
                'Open the MMU inlet latch and push the filament by hand if it will not move',
                'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (reload)'
                    % (head, ace_index, slot),
                'RESUME                           (continue the print)',
            ]
            return detail, steps
        # Arrival evidence, strongest first. (1) The feed machine itself: a
        # load that failed in load_extruding had passed load_feeding (ends
        # at the toolhead sensor) and load_heating, so the strand WAS at
        # the toolhead; phase3 then pushed without filament motion, which
        # flips the motion helper to "free" (CLAUDE.md section 9) and
        # named a clog NO TRANSPORT (HW 2026-10-25, head 2). (2) The static
        # presence pin, motion helper only as its fallback
        # (_head_filament_present).
        arrived = self._feed_failed_after_arrival(head)
        if not arrived:
            try:
                sensor = self.printer.lookup_object(
                    'filament_motion_sensor e%d_filament' % head, None)
                if sensor is not None:
                    arrived = bool(self._head_filament_present(sensor, head))
            except Exception:
                arrived = False
        if arrived and not self._cap(ace_index, 'has_decoder'):
            # No lane encoder (ACE Pro): once the strand sits at the toolhead
            # nothing tells a clogged nozzle from a tangle upstream that
            # holds the strand taut (HW 2026-10-25: a tangle in the ACE read
            # exactly like a clog). Name both ends instead of guessing one.
            detail = self._t('msg.pause_swap_load_no_extrude',
                head=self._disp(head), ace=self._disp(ace_index),
                slot=self._disp(slot))
            steps = [
                'Check the nozzle of Head %d for a clog (heat + manual push / cold pull)'
                    % self._disp(head),
                'Check the spool and the path into MMU %d Slot %d for a tangle or drag'
                    % (self._disp(ace_index), self._disp(slot)),
                'ACE_UNLOAD_HEAD HEAD=%d           (if the filament must come out)'
                    % head,
                'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (reload after clearing)'
                    % (head, ace_index, slot),
                'RESUME                           (continue the print)',
            ]
        elif arrived:
            detail = self._t('msg.pause_swap_load_no_flow',
                head=self._disp(head), ace=self._disp(ace_index),
                slot=self._disp(slot))
            steps = [
                'Check the nozzle of Head %d for a clog (heat + manual push / cold pull)'
                    % self._disp(head),
                'ACE_UNLOAD_HEAD HEAD=%d           (only if the filament must come out)'
                    % head,
                'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (reload after clearing)'
                    % (head, ace_index, slot),
                'RESUME                           (continue the print)',
            ]
        else:
            detail = self._t('msg.pause_swap_load_no_transport',
                head=self._disp(head), ace=self._disp(ace_index),
                slot=self._disp(slot))
            steps = [
                'Check spool / bowden / MMU path of MMU %d Slot %d (knot, tip, gate)'
                    % (self._disp(ace_index), self._disp(slot)),
                'ACE_UNLOAD_HEAD HEAD=%d           (clear partial filament)'
                    % head,
                'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (reload)'
                    % (head, ace_index, slot),
                'RESUME                           (continue the print)',
            ]
        return detail, steps

    def _pause_for_recovery(self, gcmd, detail_msg, recovery_steps, code=210):
        # detail_msg is the localized, [multiACE]-prefixed message (via _t).
        # No M117 (invisible on the Snapmaker touchscreen, HW-tested) and no
        # RESPOND TYPE=error - the raised gcmd.error below already reaches both
        # the display popup AND Fluidd; RESPOND too showed the error twice.
        for i, step in enumerate(recovery_steps, 1):
            try:
                self.gcode.run_script_from_command(
                    'RESPOND TYPE=echo MSG="  %d. %s"' % (
                        i, step.replace('"', "'")))
            except Exception:
                pass
        self.error_msg = detail_msg
        self._audit_state('PAUSE_RECOVERY', {
            'detail': detail_msg,
            'steps': recovery_steps,
        })

        active = self.toolhead.get_extruder().get_name() if self.toolhead else 'extruder'
        idx = 0 if active == 'extruder' else int(active.replace('extruder', '') or 0)
        # Hand the lane back to the user before the popup invites them to
        # touch it - see _stop_fa_for_head. Never blocks the pause itself.
        self._stop_fa_for_head(idx, why='recovery pause')
        # If the head we're pausing on is EMPTY, the stock RESUME would fire a
        # spurious runout on it (preflight initial-preload swap-fail: swap_back
        # lands the active extruder on an unloaded head; the recovery reloads a
        # different target head). Suppress runout for this head until it is
        # (re)loaded. Guarded on emptiness -> a loaded-but-jammed recovery keeps
        # its real runout, and a normal slicer print is untouched.
        if not self._head_is_loaded(idx):
            self._runout_suppress_heads.add(idx)
            logging.info(
                '[multiACE] recovery: runout suppressed on empty active head %d '
                'until it is (re)loaded' % idx)
        # Head mode only: INNER_RESUME re-picks last_extruder_index on resume,
        # which can be a DIFFERENT ACE head than the one this pause landed on -
        # e.g. a swap-fail on head 1 during the SEQUENTIAL initial load, where
        # head 2's own load (later in the file) never ran, so head 2 is still
        # empty and fires a spurious runout when the resume switches onto it
        # before its pending load runs. Suppress runout for every not-yet-loaded
        # ACE head until it is loaded. Head-mode gated so multi keeps its
        # single-head scope; a genuinely empty head still fails as
        # a LOAD error, not a runout. Cleared on (re)load / print start / end.
        if getattr(self, '_ace_mode', 'multi') == 'head':
            for h in range(4):
                if self.head_uses_ace(h) and not self._head_is_loaded(h):
                    if h not in self._runout_suppress_heads:
                        self._runout_suppress_heads.add(h)
                        logging.info(
                            '[multiACE] recovery (head mode): runout suppressed '
                            'on unloaded MMU head %d until it is loaded' % h)
        # Make the resulting PAUSE resumable: the swap drove main_state out of
        # PRINTING, which the stock RESUME guard rejects. Restore it before the
        # action='pause' exception fires.
        self._restore_machine_state_for_resume()
        raise gcmd.error(
            message=detail_msg.replace('"', "'")[:400], action='pause',
            id=525, index=idx, code=code, oneshot=1, level=2)

    cmd_ACE_PICKUP_CLEAN_help = (
        '[multiACE] Short nozzle wipe at the discard position after a bare-T '
        'tool pickup (HEAD=n). No-op unless Pickup-Cleaning is enabled. The '
        'preflight stamps this after picks that have no other cleaning move.')

    def cmd_ACE_PICKUP_CLEAN(self, gcmd):
        # No-op fast path: the preflight stamps this after every bare-T pick, so
        # keep it cheap when the feature is off.
        if not getattr(self, '_pickup_cleaning', False):
            return
        head = gcmd.get_int('HEAD', None)
        # Print-time only + not mid-swap (a swap does its own INNER_FLUSH wipe).
        try:
            ps = self.printer.lookup_object('print_stats', None)
            printing = (ps is not None and ps.get_status(
                self.reactor.monotonic()).get('state') == 'printing')
        except Exception:
            printing = False
        if not printing or self._swap_in_progress:
            return
        self.log_always('[multiACE] Pickup-Cleaning: nozzle wipe at the discard '
                        'position (head %s)'
                        % (self._disp(head) if head is not None else '?'))
        self._discard_wipe(head, 'pickup-clean')

    def _discard_wipe(self, head, tag):
        """Shared discard-wipe excursion: Z hop -> MOVE_TO_DISCARD -> stock
        ooze-cutoff wipe (INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=2)
        -> pos/E restore (the proven inline-swap/bg-pick tail, dock
        hazard respected). Callers: ACE_PICKUP_CLEAN (same-colour picks) and
        the post-resume no-op wipe (RESUME_NOOP_WIPE_WINDOW const note).
        Guards homed axes + min_extrude itself; every step fail-open -
        a wipe must never break the print it serves. Returns True when the
        wipe ran."""
        # The wipe excursion (Z hop + discard travel) needs homed axes; a
        # below-min_extrude head could extrude in the stock clean -> skip both.
        try:
            homed = self.toolhead.get_status(
                self.reactor.monotonic()).get('homed_axes', '')
        except Exception:
            homed = ''
        if not all(a in homed for a in 'xyz'):
            logging.info('[multiACE] [%s] axes not homed (%s) - '
                         'skipped' % (tag, homed or 'none'))
            return False
        try:
            heater = self.toolhead.get_extruder().get_heater()
            if not bool(getattr(heater, 'can_extrude', True)):
                logging.info('[multiACE] [%s] below min_extrude_temp '
                             '- skipped' % tag)
                return False
        except Exception:
            pass
        gcode_move = self.printer.lookup_object('gcode_move')
        (saved_pos, saved_speed, saved_absolute, saved_e_base,
         saved_e_last) = self._pos_snapshot()
        # Suppress the active head's runout for the off-print excursion (only if
        # WE set it; lifted in the finally).
        _added_suppress = (head is not None
                           and head not in self._runout_suppress_heads)
        if _added_suppress:
            self._runout_suppress_heads.add(head)
        try:
            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('G1 Z2 F600')
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command(
                'MOVE_TO_DISCARD_FILAMENT_POSITION')
            self.toolhead.wait_moves()
            self.gcode.run_script_from_command(
                'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=2')
            self.toolhead.wait_moves()
            logging.info('[multiACE] [%s] nozzle wipe done (head %s)'
                         % (tag, self._raw(head) if head is not None else '?'))
            _wipe_ok = True
        except Exception as e:
            logging.info('[multiACE] [%s] wipe failed (continuing): '
                         '%s' % (tag, e))
            _wipe_ok = False
        finally:
            if _added_suppress:
                self._runout_suppress_heads.discard(head)
            # Pos/E restore = the inline swap / bg-pick tail (dock hazard):
            # slicer E view unchanged, +2mm travel hop back to the print pos.
            try:
                e_diff = gcode_move.last_position[3] - saved_e_last
                gcode_move.base_position[3] = saved_e_base + e_diff
                self.gcode.run_script_from_command('G90')
                self.gcode.run_script_from_command(
                    'G0 Z%.3f F600' % (saved_pos[2] + 3.0))
                self.gcode.run_script_from_command(
                    'G0 Y%.3f F12000' % saved_pos[1])
                self.gcode.run_script_from_command(
                    'G0 X%.3f F12000' % saved_pos[0])
                self.gcode.run_script_from_command(
                    'G0 Z%.3f F600' % (saved_pos[2] + 2.0))
                self.toolhead.wait_moves()
                if saved_absolute:
                    self.gcode.run_script_from_command('G90')
                self.gcode.run_script_from_command(
                    'G1 F%d' % (saved_speed * 60))
            except Exception as re:
                logging.info('[multiACE] [%s] pos restore failed: %s'
                             % (tag, re))
        return _wipe_ok

    def _wait_bg_op(self, head, gcmd=None, rearm_target=None, park=False):
        # Background-swap interlock (experimental [ace_bg_swap] module,
        # ace_bg_swap.py): a feed op targeting a head whose background
        # unload is still running WAITS for it instead of colliding.
        # Waiting beats aborting: the V2 bulk rollback is FIXED-LENGTH
        # (no slot sensor) - aborting mid-retract strands a partially
        # retracted filament that a later full re-retract would over-pull
        # out of the ACE gears. Without the module this is a no-op lookup
        # (byte-identical behaviour).
        bg = self.printer.lookup_object('ace_bg_swap', None)
        if bg is None:
            return
        try:
            busy = bg.is_busy(head)
        except Exception:
            return
        if not busy:
            return
        self.log_always('[multiACE] head %d: waiting for the background '
                        'unload to finish before the feed op'
                        % self._disp(head))
        if park:
            # The stock T left the head wherever the slicer put the tool
            # change (Orca: on the wipe tower), hot and - after a prime
            # abort - with a pressurised nozzle. Waiting THERE drools a
            # blob onto the tower. Park over the discard chute instead;
            # the swap's own position capture uses the pre-park snapshot
            # (_pos_snapshot), so the print resumes where the T left it.
            self._bg_wait_park(head)
        # 300s > the engine's longest single-phase budget (HEAT_TIMEOUT
        # 240s): a hung phase must FAIL inside the greenlet (clean inline
        # handover) before this wait gives up and aborts the print.
        deadline = self.reactor.monotonic() + 300.
        waiting = getattr(self, '_bg_arrival_waiting', None)
        if waiting is not None:
            waiting.add(head)
        try:
            while self.reactor.monotonic() < deadline:
                try:
                    if not bg.is_busy(head):
                        self.log_always('[multiACE] head %d: background '
                                        'unload finished - continuing'
                                        % self._disp(head))
                        self._rearm_fa_after_bg_wait(head, rearm_target)
                        return
                except Exception:
                    return
                self.reactor.pause(self.reactor.monotonic() + 0.5)
        finally:
            if waiting is not None:
                waiting.discard(head)
        msg = ('[multiACE] head %d: background unload did not finish '
               'within 300s' % self._disp(head))
        if gcmd is not None:
            raise gcmd.error(msg)
        self.log_error(msg)

    def _bg_wait_park(self, head):
        """Move over the discard chute for the duration of a bg wait and
        remember where the print stood. Homed axes only (unhomed -> the
        head simply waits where it is, as before). The snapshot is handed
        to the next position capture (_pos_snapshot) or restored by
        _bg_wait_unpark when nothing consumed it."""
        try:
            homed = self.toolhead.get_status(
                self.reactor.monotonic()).get('homed_axes', '')
        except Exception:
            homed = ''
        if not all(a in homed for a in 'xyz'):
            logging.info('[multiACE] bg-wait park skipped: axes not homed '
                         '(%s)' % (homed or 'none'))
            return False
        try:
            gcode_move = self.printer.lookup_object('gcode_move')
            snap = {
                'pos': self.toolhead.get_position()[:3],
                'speed': gcode_move.speed,
                'absolute': gcode_move.absolute_coord,
                'e_base': gcode_move.base_position[3],
                'e_last': gcode_move.last_position[3],
            }
            self._bg_wait_parked = snap
            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('G1 Z2 F600')
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command(
                'MOVE_TO_DISCARD_FILAMENT_POSITION')
            self.toolhead.wait_moves()
            logging.info('[multiACE] head %d: parked over the discard chute '
                         'for the bg wait (print pos X=%.2f Y=%.2f Z=%.2f)'
                         % (self._disp(head), snap['pos'][0],
                            snap['pos'][1], snap['pos'][2]))
            return True
        except Exception as e:
            logging.info('[multiACE] bg-wait park failed (waiting in '
                         'place): %s' % e)
            return False

    def _bg_wait_unpark(self):
        """Backstop: a bg-wait park nobody consumed (the swap left before
        any position capture ran) is undone here - the pick-check's
        proven restore tail, E view kept."""
        snap = getattr(self, '_bg_wait_parked', None)
        if not snap:
            return
        self._bg_wait_parked = None
        try:
            gcode_move = self.printer.lookup_object('gcode_move')
            pos = snap['pos']
            e_diff = gcode_move.last_position[3] - snap['e_last']
            gcode_move.base_position[3] = snap['e_base'] + e_diff
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (pos[2] + 3.0))
            self.gcode.run_script_from_command('G0 Y%.3f F12000' % pos[1])
            self.gcode.run_script_from_command('G0 X%.3f F12000' % pos[0])
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (pos[2] + 2.0))
            self.toolhead.wait_moves()
            if snap['absolute']:
                self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command('G1 F%d' % (snap['speed'] * 60))
            logging.info('[multiACE] bg-wait park undone: back at X=%.2f '
                         'Y=%.2f Z=%.2f' % (pos[0], pos[1], pos[2]))
        except Exception as e:
            logging.info('[multiACE] bg-wait unpark failed: %s' % e)

    def _rearm_fa_after_bg_wait(self, head, target=None):
        # The pick that led us here fired _on_extruder_change while the bg
        # op was still busy - its FA arm was deliberately skipped (no
        # forward assist into a running rollback). If the bg op finished
        # with the head LOADED (BG-Load v1: bg swap complete -> the feed op
        # no-ops and never arms either), the print would continue with NO
        # feed assist = V2 airprint. Re-fire the arm here, device-truth
        # guarded by _arm_fa_for itself; gate + active-head checks mirror
        # _on_extruder_change.
        #
        # TARGET GATE: re-arm ONLY when the imminent op keeps printing on
        # the current lane (ACE_SWAP_HEAD arrival whose target == the
        # loaded source = the no-op case this re-arm exists for). A REAL
        # swap unloads the lane within a second - the fresh
        # start_feed_assist then collides with the unload's stop on a V1
        # fresh out of the bg abort, and such a race can leave the device
        # stuck in status=busy through reconnects.
        # UNLOAD/LOAD arrivals pass no target -> never re-arm (their ops
        # stop FA immediately / arm canonically after the load).
        try:
            if not self._auto_feed_enabled:
                return
            ext = self.toolhead.get_extruder()
            cur_head = getattr(ext, 'extruder_index',
                               getattr(ext, 'extruder_num', None))
            if cur_head != head or not self.head_uses_ace(head):
                return
            source = self._head_source.get(head)
            if not source:
                return
            if (target is None
                    or target != (source.get('ace_index'),
                                  source.get('slot'))):
                self._fa_trace(
                    'FA re-arm after bg wait SKIPPED: head=%d imminent op '
                    'changes the lane (target=%s source=%s/%s)'
                    % (head, target, source.get('ace_index'),
                       source.get('slot')))
                return
            self._ensure_active_ace_for_head(head)
            self._arm_fa_for(source['ace_index'], source['slot'])
            self._fa_trace('FA re-armed after bg-op wait: head=%d ace=%d '
                           'slot=%d' % (head, source['ace_index'],
                                        source['slot']))
        except Exception as e:
            logging.info('[multiACE] FA re-arm after bg wait failed: %s' % e)

    def _bg_pick_flow_check(self, head, anti_ooze):
        # Thin wrapper: flag the check for the airlog chew detector - the
        # check's own coil pushes read as turbulence windows and must never
        # count toward a chew run. The check has its own verdict machinery.
        self._pickcheck_active = True
        try:
            return self._bg_pick_flow_check_inner(head, anti_ooze)
        finally:
            self._pickcheck_active = False

    def _bg_pick_flow_check_inner(self, head, anti_ooze):
        # Pick-time flow check for a bg-loaded head (see the PICK_CHECK_*
        # const block). Runs in the arrival swap's no-op path: the head is
        # ACTIVE and already heated to swap_temp. Sequence: save pos/E state
        # -> Z hop -> MOVE_TO_DISCARD_FILAMENT_POSITION (the spot phase3
        # measures at - baselines comparable, extruding there is mid-print
        # proven by every inline flush; NEVER over the print) ->
        # coil-sampled push of max(anti_ooze+PICK_CHECK_FLOW_PUSH,
        # PICK_CHECK_MIN_PUSH) -> retract the cushion back -> restore pos
        # (inline-swap tail pattern). With [ace_bg_swap] pick_gate (default
        # True) the check GATES: the first NO_FLOW gets a re-grip on the
        # active head + a re-measure (heals a docked-grip seat
        # failure); a persistent NO_FLOW (or a sensor reading ABSENT on a
        # bg-loaded head) returns a verdict and the CALLER pauses resumably
        # AFTER this function restored pos/E - the function itself still
        # never raises. Gate off / no coil -> LOG-ONLY, returns None
        # (fail-open: never pause without a real measurement). The head is
        # runout-suppressed for the pushes: a genuinely clogged head would
        # otherwise trip a REAL runout PAUSE from the verify itself.
        self._bg_load_unverified.discard(head)
        _bg = self.printer.lookup_object('ace_bg_swap', None)
        gate_on = bool(getattr(_bg, 'pick_gate', False))
        # Prime deficit from a grip/prime pick-abort (see _bg_prime_deficit):
        # None = normal check; a number = the bg purge was cut short by that
        # many mm (0.0 = prime complete but the anti-ooze retract never ran).
        # Popped one-shot here; skip paths that re-arm the check re-arm it.
        _deficit = getattr(self, '_bg_prime_deficit', {}).pop(head, None)
        try:
            ext = self.toolhead.get_extruder()
            heater = ext.get_heater()
            if not bool(getattr(heater, 'can_extrude', True)):
                logging.info('[multiACE] [pick-check] head %d skipped: below '
                             'min_extrude_temp' % head)
                if _deficit is not None:
                    self._bg_prime_deficit[head] = _deficit
                return
            sensor = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % head, None)
            def _detected():
                try:
                    return bool(sensor is not None and
                                sensor.get_status(0).get('filament_detected'))
                except Exception:
                    return None
            # The check moves (Z hop + discard travel) - unhomed axes raise
            # "Must home first" (e.g. Z unhomed after a restart). In a
            # real print everything is homed; on an idle test skip cleanly and
            # RE-ARM the flag so the check runs at the next (homed) arrival.
            try:
                homed = self.toolhead.get_status(
                    self.reactor.monotonic()).get('homed_axes', '')
            except Exception:
                homed = ''
            if not all(a in homed for a in 'xyz'):
                self._bg_load_unverified.add(head)
                if _deficit is not None:
                    self._bg_prime_deficit[head] = _deficit
                self.log_always('[multiACE] [pick-check] head %d: axes not '
                                'homed (%s) - skipped, re-armed for the next '
                                'arrival (home first for an idle test)'
                                % (self._disp(head), homed or 'none'))
                return
            sensor_before = _detected()
            if sensor_before is False:
                # bg says loaded but the presence gate reads clear - that is
                # already the answer, no extrude needed. Gate on -> the
                # caller pauses (a re-grip without filament at the sensor is
                # pointless); gate off -> log-only as before.
                logging.info('[multiACE] [pick-check] head %d: sensor reads '
                             'ABSENT on a bg-loaded head - skipping the push'
                             % head)
                self._wiggle_log.info(
                    'pickcheck head=%d verdict=SENSOR_ABSENT gate=%s'
                    % (head, gate_on))
                return 'sensor_absent' if gate_on else None
            coil = None
            try:
                coil = ext.binding_probe.sensor
            except Exception:
                logging.info('[multiACE] [pick-check] inductance_coil not '
                             'found - sensor-only check')
            src = self._head_source.get(head) or {}
            self.log_always('[multiACE] [pick-check] head %d: verifying the '
                            'background load (short extrude at the discard '
                            'position)' % self._disp(head))

            gcode_move = self.printer.lookup_object('gcode_move')
            (saved_pos, saved_speed, saved_absolute, saved_e_base,
             saved_e_last) = self._pos_snapshot()
            # Runout suppression for the push (lifted right after, but only
            # if WE set it - never clobber an existing suppression): a clogged
            # head reads "no filament motion + extruder advance" = runout.
            _added_suppress = head not in self._runout_suppress_heads
            self._runout_suppress_heads.add(head)
            coil_start = coil_min = coil_max = None
            sensor_after = None
            try:
                self.gcode.run_script_from_command('G91')
                self.gcode.run_script_from_command('G1 Z2 F600')
                self.gcode.run_script_from_command('G90')
                self.gcode.run_script_from_command(
                    'MOVE_TO_DISCARD_FILAMENT_POSITION')
                self.toolhead.wait_moves()

                self.gcode.run_script_from_command('M83')
                def _measure(push_mm):
                    # One coil-sampled push (the phase3 pattern). Returns
                    # (start, min, max, dip, up): dip = start-min = the
                    # DOWN-flow signal (back-pressure pulls the coil down),
                    # up = max-start = the up-swing (turbulence). Both None
                    # = no coil signal. A clean flow push has dip>0, up~0.
                    c0 = mn = mx = None
                    if coil is not None:
                        try:
                            c0 = coil.get_coil_freq()
                            mn = mx = c0
                        except Exception:
                            c0 = None
                    self.gcode.run_script_from_command(
                        'G1 E%.2f F%d' % (push_mm, PICK_CHECK_PUSH_FEEDRATE))
                    self.reactor.pause(self.reactor.monotonic() + 0.5)
                    if coil is not None and c0 is not None:
                        for _i in range(PICK_CHECK_COIL_SAMPLES):
                            try:
                                f = coil.get_coil_freq()
                                mn = min(mn, f)
                                mx = max(mx, f)
                            except Exception:
                                break
                            self.reactor.pause(self.reactor.monotonic()
                                               + PICK_CHECK_COIL_INTERVAL)
                    self.toolhead.wait_moves()
                    dip = (c0 - mn) if c0 is not None else None
                    up = (mx - c0) if c0 is not None else None
                    return c0, mn, mx, dip, up
                if _deficit is not None:
                    # Prime top-up: finish the purge the pick cut short. The
                    # abort path never ran its anti-ooze retract, so the
                    # cushion is still IN the nozzle - do NOT re-push it on
                    # top (only the missing prime + the measuring push); the
                    # normal end retract below then establishes the cushion.
                    push = max(float(_deficit) + PICK_CHECK_FLOW_PUSH,
                               PICK_CHECK_MIN_PUSH)
                    self.log_always(
                        '[multiACE] [pick-check] head %d: topping up the '
                        'cut-short background prime (%d mm)'
                        % (self._disp(head), int(float(_deficit))))
                else:
                    # Fixed minimum push: the coil delta scales with push
                    # length; an 11 mm push (small anti_ooze) made the
                    # absolute threshold unsafe.
                    push = max(float(anti_ooze) + PICK_CHECK_FLOW_PUSH,
                               PICK_CHECK_MIN_PUSH)
                coil_start, coil_min, coil_max, coil_delta, coil_up = \
                    _measure(push)
                # Turbulence guard: a large up-swing = the coil was measured
                # inside a chew/bind episode -> the dip cannot
                # be trusted. Settle + re-measure ONCE so the verdict judges
                # a quiet coil; a genuine bind stays turbulent/low and falls
                # into the NO_FLOW path below.
                _t_remeasured = False
                if coil_up is not None and coil_up >= PICK_TURBULENCE_UPSWING:
                    self.log_always(
                        '[multiACE] [pick-check] head %d: coil TURBULENCE '
                        '(up=%.0f dip=%s) - settling %.1fs + re-measuring'
                        % (self._disp(head), coil_up, coil_delta,
                           PICK_TURBULENCE_SETTLE))
                    self.reactor.pause(self.reactor.monotonic()
                                       + PICK_TURBULENCE_SETTLE)
                    coil_start, coil_min, coil_max, coil_delta, coil_up = \
                        _measure(PICK_CHECK_MIN_PUSH)
                    _t_remeasured = True
                regripped = False
                ace_pushed = None
                _turbulent = (coil_up is not None
                              and coil_up >= PICK_TURBULENCE_UPSWING)
                # Baseline-relative LOW side (pickcheck_lowpass_check): a
                # delta that clears the absolute threshold but sits under
                # COIL_LOWPASS_FRAC of the lane's own push-bucket baseline
                # is a THIN pass -> route it through the same regrip+
                # re-measure recovery instead of trusting it. Only judged
                # on a clean first measure: after a turbulence re-measure
                # the delta came from a PICK_CHECK_MIN_PUSH push while the
                # bucket key is the original push - not comparable (and
                # the turbulent path enters the regrip anyway).
                _lp_ok, _lp_base, _lp_ratio = (True, None, None)
                if (gate_on and not _turbulent and not _t_remeasured
                        and coil_delta is not None
                        and coil_delta >= PICK_CHECK_COIL_THRESHOLD):
                    _lp_ok, _lp_base, _lp_ratio = \
                        self.pickcheck_lowpass_check(
                            head, src.get('ace_index'), src.get('slot'),
                            push, coil_delta)
                if (gate_on and coil_delta is not None
                        and (coil_delta < PICK_CHECK_COIL_THRESHOLD
                             or _turbulent or not _lp_ok)):
                    # NO_FLOW #1 -> RECOVERY, not alarm: re-grip on the now-
                    # ACTIVE head (real motion-system authority - completes
                    # the seat the docked stealth grip could not guarantee,
                    # the seat) WITH a bounded ACE push feeding the tip
                    # against the turning gears (see the PICK_GATE_ACE_PUSH_*
                    # const note - a long taper cannot be dragged in by the
                    # extruder alone), then re-measure. Only a persistent
                    # NO_FLOW escalates to the caller.
                    if not _lp_ok:
                        self.log_always(
                            '[multiACE] [pick-check] head %d: THIN pass '
                            '(delta=%s = %.2fx of lane baseline %.0f) - '
                            're-gripping %d mm (+MMU push) and re-measuring'
                            % (self._disp(head), coil_delta, _lp_ratio,
                               _lp_base, int(PICK_GATE_REGRIP)))
                    else:
                        self.log_always(
                            '[multiACE] [pick-check] head %d: NO FLOW '
                            '(delta=%s) - re-gripping %d mm (+MMU push) and '
                            're-measuring'
                            % (self._disp(head), coil_delta,
                               int(PICK_GATE_REGRIP)))
                    n_idx = src.get('ace_index')
                    n_slot = src.get('slot')
                    if (_bg is not None and n_idx is not None
                            and n_slot is not None):
                        try:
                            n_len = (PICK_GATE_ACE_PUSH_V2
                                     if self._cap(n_idx, 'self_stops_on_resistance')
                                     else PICK_GATE_ACE_PUSH_V1)
                            # Clear the host FA cache BEFORE the stop: the
                            # V2 disarm-monitor re-arms host-armed slots the
                            # device reports idle - with the cache stamped
                            # it would fight the nachschub with its own
                            # start_feed_assist mid-push. Cache -1 = "not
                            # armed", monitors stay quiet; the arm below
                            # re-sends cleanly.
                            self._feed_assist_per_ace[n_idx] = -1
                            _bg._ace_send(self, n_idx, {
                                'method': 'stop_feed_assist',
                                'params': {'index': n_slot}})
                            ace_pushed = False
                            for _na in range(PICK_GATE_ACE_PUSH_RETRIES):
                                nresp = _bg._ace_send(self, n_idx, {
                                    'method': 'feed_filament',
                                    'params': {
                                        'index': n_slot,
                                        'length': int(n_len),
                                        'speed': int(
                                            PICK_GATE_ACE_PUSH_SPEED)}})
                                if not _bg._resp_rejected(nresp):
                                    ace_pushed = True
                                    break
                                if _na < PICK_GATE_ACE_PUSH_RETRIES - 1:
                                    self.reactor.pause(
                                        self.reactor.monotonic()
                                        + PICK_GATE_ACE_PUSH_RETRY_DELAY)
                        except Exception as ne:
                            logging.info('[multiACE] [pick-check] MMU push '
                                         'failed (extruder-only re-grip): '
                                         '%s' % ne)
                    self.gcode.run_script_from_command(
                        'G1 E%.2f F%d' % (PICK_GATE_REGRIP,
                                          PICK_GATE_REGRIP_FEEDRATE))
                    self.toolhead.wait_moves()
                    if ace_pushed is not None:
                        # The push is bounded and long done (V1 open-loop
                        # ~1.5 s, V2 self-stopped) - the stop is a safety
                        # no-op; then restore FA through the canonical arm
                        # (cache is already -1, so the V1 prev-slot guard
                        # cannot skip the re-send).
                        try:
                            _bg._ace_send(self, n_idx, {
                                'method': 'stop_feed_filament',
                                'params': {'index': n_slot}})
                            self._arm_fa_for(n_idx, n_slot)
                        except Exception as ne:
                            logging.info('[multiACE] [pick-check] FA re-arm '
                                         'after MMU push failed: %s' % ne)
                    push = push + PICK_GATE_REGRIP + PICK_CHECK_MIN_PUSH
                    regripped = True
                    coil_start, coil_min, coil_max, coil_delta, coil_up = \
                        _measure(PICK_CHECK_MIN_PUSH)
                # Same trigger settle as the inline unload probe (the motion
                # sensor's event needs a moment after the tracked advance).
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                sensor_after = _detected()
                # Restore the cushion the arrival's un-retract expects.
                if anti_ooze > 0:
                    self.gcode.run_script_from_command(
                        'G1 E-%.2f F1500' % float(anti_ooze))
                    self.toolhead.wait_moves()
                # Nozzle wipe (BG_PICK_WIPE): the bg load could not wipe while
                # docked, so scrape the strings off here - the head is at the
                # discard position and hot, the stock clean does the wipe
                # strokes. E moves in it are absorbed by the finally's e_diff
                # bookkeeping. Fail-open: a wipe error never blocks the print.
                if BG_PICK_WIPE:
                    try:
                        self.gcode.run_script_from_command(
                            'INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=2')
                        self.toolhead.wait_moves()
                        logging.info('[multiACE] [pick-check] nozzle wipe '
                                     'done (head %d)' % self._raw(head))
                    except Exception as we:
                        logging.info('[multiACE] [pick-check] nozzle wipe '
                                     'failed (continuing): %s' % we)
            finally:
                if _added_suppress:
                    self._runout_suppress_heads.discard(head)
                # E bookkeeping + pos restore = the inline swap's proven tail
                # (ace.py swap exit): slicer E view unchanged, +2mm travel hop.
                try:
                    e_diff = gcode_move.last_position[3] - saved_e_last
                    gcode_move.base_position[3] = saved_e_base + e_diff
                    self.gcode.run_script_from_command('G90')
                    self.gcode.run_script_from_command(
                        'G0 Z%.3f F600' % (saved_pos[2] + 3.0))
                    self.gcode.run_script_from_command(
                        'G0 Y%.3f F12000' % saved_pos[1])
                    self.gcode.run_script_from_command(
                        'G0 X%.3f F12000' % saved_pos[0])
                    self.gcode.run_script_from_command(
                        'G0 Z%.3f F600' % (saved_pos[2] + 2.0))
                    self.toolhead.wait_moves()
                    if saved_absolute:
                        self.gcode.run_script_from_command('G90')
                    self.gcode.run_script_from_command(
                        'G1 F%d' % (saved_speed * 60))
                except Exception as re:
                    logging.info('[multiACE] [pick-check] pos restore '
                                 'failed: %s' % re)

            # Verdicts. Coil: the DIP (start-min) >= threshold = real flow,
            # AND not turbulent (a large up-swing = the reading was taken in
            # a chew/bind episode and cannot be trusted). Sensor: the
            # presence gate flipping to absent across a tracked push = no
            # filament motion = stuck (a stuck filament can ALSO read absent
            # - either way NOT flowing).
            _turbulent = (coil_up is not None
                          and coil_up >= PICK_TURBULENCE_UPSWING)
            coil_verdict = ('FLOW' if coil_delta is not None
                            and coil_delta >= PICK_CHECK_COIL_THRESHOLD
                            and not _turbulent
                            else 'NO_FLOW' if coil_delta is not None
                            else 'NO_COIL')
            sens_verdict = ('STUCK_OR_GONE' if sensor_after is False
                            else 'PRESENT' if sensor_after else 'UNKNOWN')
            # Resistance watch: only a PASSING delta is judged (a NO_FLOW
            # dip is small and would poison the baseline). A huge dip that
            # "passed" is the chew signature that precedes an airprint.
            res_verdict = res_base = res_ratio = None
            if coil_verdict == 'FLOW':
                res_verdict, res_base, res_ratio = self._resistance_note(
                    'pickcheck', head, src.get('ace_index'),
                    src.get('slot'), coil_delta, push)
            _line = ('pickcheck head=%d ace=%s slot=%s anti_ooze=%.1f '
                     'push=%.1f prime_topup=%s regrip=%s ace_push=%s '
                     'coil_start=%s '
                     'coil_min=%s coil_max=%s coil_delta=%s coil_up=%s '
                     'turbulent=%s thr=%d '
                     'coil_verdict=%s sensor_before=%s sensor_after=%s '
                     'sensor_verdict=%s gate=%s '
                     'resistance=%s res_baseline=%s res_ratio=%s lowpass=%s'
                     % (head, src.get('ace_index'), src.get('slot'),
                        float(anti_ooze), push,
                        ('%.0f' % float(_deficit)) if _deficit is not None
                        else '-', regripped, ace_pushed,
                        coil_start, coil_min, coil_max, coil_delta, coil_up,
                        _turbulent,
                        PICK_CHECK_COIL_THRESHOLD, coil_verdict,
                        sensor_before, sensor_after, sens_verdict, gate_on,
                        res_verdict or '-',
                        ('%.0f' % res_base) if res_base else '-',
                        ('%.2f' % res_ratio) if res_ratio else '-',
                        ('%.2f' % _lp_ratio) if _lp_ratio is not None
                        else '-'))
            # Wiggle log is debug-gated (_state_debug_enabled) - the baseline
            # data must survive either way, so mirror it to klippy.log.
            self._wiggle_log.info(_line)
            logging.info('[multiACE] [pick-check] %s' % _line)
            if gate_on and coil_verdict == 'NO_FLOW':
                self.log_always(
                    '[multiACE] [pick-check] head %d: NO FLOW persists '
                    'after the re-grip (delta=%s) - escalating to pause'
                    % (self._disp(head), coil_delta))
                return 'no_flow'
            if res_verdict == 'pause_due':
                _r_ace = src.get('ace_index')
                _r_slot = src.get('slot')
                self.log_always(
                    '[multiACE] [pick-check] head %d: sustained RESISTANCE '
                    'on MMU %s/Slot %s (delta=%s, %dx suspect) - escalating '
                    'to pause' % (self._disp(head),
                                  self._disp(_r_ace) if _r_ace is not None
                                  else '?',
                                  self._disp(_r_slot) if _r_slot is not None
                                  else '?', coil_delta,
                                  RESISTANCE_PAUSE_STRIKES))
                return 'resistance'
            if regripped and not _lp_ok and coil_verdict == 'FLOW':
                # Thin-pass demotion resolved: the re-measure after the
                # regrip cleared the absolute gate - pass with a visible
                # note (the analysis grep for the demote family).
                self.log_always(
                    '[multiACE] [pick-check] head %d: thin first read '
                    '(%.2fx of baseline) verified by re-grip '
                    '(re-measure delta=%s) - passing'
                    % (self._disp(head), _lp_ratio, coil_delta))
            self.log_always('[multiACE] [pick-check] head %d: coil %s '
                            '(delta=%s) / sensor %s%s'
                            % (self._disp(head), coil_verdict, coil_delta,
                               sens_verdict,
                               ' - recovered by re-grip' if regripped
                               else ''))
            return None
        except Exception as e:
            logging.exception('[multiACE] [pick-check] head %d failed '
                              '(log-only, swallowed)' % head)
            try:
                self.log_always('[multiACE] [pick-check] head %d: check '
                                'errored (%s) - log-only, print continues'
                                % (self._disp(head), e))
            except Exception:
                pass

    def _get_swap_temp(self, head):

        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            fp = self.printer.lookup_object('filament_parameters', None)
            if ptc is None or fp is None:
                logging.info(
                    '[multiACE] _get_swap_temp head=%d step1 skip '
                    '(ptc=%s fp=%s)' % (head, ptc is not None, fp is not None))
            else:
                status = ptc.get_status()
                vendor = status.get('filament_vendor', [''] * 4)
                ftype = status.get('filament_type', [''] * 4)
                subtype = status.get('filament_sub_type', [''] * 4)
                v = vendor[head] if head < len(vendor) else ''
                t = ftype[head] if head < len(ftype) else ''
                s = subtype[head] if head < len(subtype) else ''
                temp = fp.get_load_temp(v, t, s)
                logging.info(
                    '[multiACE] _get_swap_temp head=%d step1 ptc lookup: '
                    'vendor=%r type=%r sub=%r -> get_load_temp=%r'
                    % (head, v, t, s, temp))
                if temp and temp >= 170:
                    # Cap the swap HOLD temp at the live print target so the
                    # head doesn't sit hotter than the print itself during the
                    # swap dwell (the DB load_temp is often >print, which feeds
                    # heat-creep). Material-agnostic: uses this material's own
                    # print temperature. Only caps when actually printing
                    # (target valid); idle/test keeps the DB load_temp.
                    try:
                        _en = 'extruder' if head == 0 else 'extruder%d' % head
                        _ex = self.printer.lookup_object(_en, None)
                        _pt = int(_ex.get_heater().target_temp) if _ex else 0
                        if 170 <= _pt < int(temp):
                            logging.info(
                                '[multiACE] _get_swap_temp head=%d cap load %d '
                                '-> print target %d (hold)' % (head, int(temp), _pt))
                            return _pt
                    except Exception:
                        pass
                    return int(temp)
                logging.info(
                    '[multiACE] _get_swap_temp head=%d step1 rejected '
                    '(temp=%r not in [170,inf))' % (head, temp))
        except Exception as e:
            logging.info(
                '[multiACE] _get_swap_temp head=%d step1 raised: %s: %s'
                % (head, type(e).__name__, e))

        try:
            extruder_name = 'extruder' if head == 0 else 'extruder%d' % head
            extruder = self.printer.lookup_object(extruder_name, None)
            if extruder is None:
                extruder = self.printer.lookup_object('extruder', None)
            if extruder is None:
                logging.info(
                    '[multiACE] _get_swap_temp head=%d step2 skip '
                    '(%s not loaded)' % (head, extruder_name))
            else:
                target = extruder.get_heater().target_temp
                logging.info(
                    '[multiACE] _get_swap_temp head=%d step2 %s.target_temp=%s'
                    % (head, extruder_name, target))
                if target >= 170:
                    return int(target)
                logging.info(
                    '[multiACE] _get_swap_temp head=%d step2 rejected '
                    '(target=%s < 170)' % (head, target))
        except Exception as e:
            logging.info(
                '[multiACE] _get_swap_temp head=%d step2 raised: %s: %s'
                % (head, type(e).__name__, e))

        logging.info(
            '[multiACE] _get_swap_temp head=%d -> swap_default_temp=%d (fallback)'
            % (head, self.swap_default_temp))
        return self.swap_default_temp

    cmd_ACE_SWAP_HEAD_help = '[multiACE] Mid-print filament swap. Usage: ACE_SWAP_HEAD HEAD=0 ACE=1 [SLOT=0]'
    def _dwell_fan(self, on):
        """Part-fan bracket for the swap's PASSIVE dock waits (bulk retract
        to the ACE, bowden feed to the sensor) - the dwell-heat-soak
        mitigation test. Called from the filament_feed unload/load windows;
        ON is gated on [ace] swap_dwell_fan > 0 AND a swap in progress
        (standalone unloads stay untouched), OFF runs whenever a previous
        value is stored - so the finally backstop in cmd_ACE_SWAP_HEAD can
        restore even after _swap_in_progress was already cleared. M106 S
        targets the ACTIVE head, which in these windows is the swap head
        (the switch-back to the original head happens later). Fail-open,
        idempotent (nested ONs keep the first saved value)."""
        try:
            if on:
                if not (int(getattr(self, 'swap_dwell_fan', 0) or 0) > 0
                        and self._swap_in_progress):
                    return
                if self._dwell_fan_prev is not None:
                    return
                prev = 0.
                try:
                    _fan = self.printer.lookup_object('fan', None)
                    prev = float(getattr(_fan.fan, 'last_fan_value', 0.)
                                 or 0.)
                except Exception:
                    prev = 0.
                self._dwell_fan_prev = prev
                self.gcode.run_script_from_command(
                    'M106 S%d' % int(self.swap_dwell_fan))
                logging.info('[multiACE] dwell fan ON S%d (was S%d)'
                             % (self.swap_dwell_fan,
                                int(round(prev * 255.))))
            else:
                if self._dwell_fan_prev is None:
                    return
                prev = self._dwell_fan_prev
                self._dwell_fan_prev = None
                self.gcode.run_script_from_command(
                    'M106 S%d' % int(round(prev * 255.)))
                logging.info('[multiACE] dwell fan OFF (restored S%d)'
                             % int(round(prev * 255.)))
        except Exception as e:
            logging.info(
                '[multiACE] dwell fan toggle failed (ignored): %s' % e)
            self._dwell_fan_prev = None

    def cmd_ACE_SWAP_HEAD(self, gcmd):
        # Wrapper: a bg-wait park (see _wait_bg_op park=True) must never
        # outlive the command - whatever exit the body takes, the head
        # returns to the print position unless a capture consumed it.
        self._bg_wait_parked = None
        try:
            result = self._cmd_ACE_SWAP_HEAD_body(gcmd)
        finally:
            self._bg_wait_unpark()
        # Auto-load block: a flow calibration deferred for this head (it was
        # empty when the start gcode asked) runs now that it is loaded.
        if gcmd.get_int('INITIAL', 0):
            replay = getattr(self, '_flow_calibrate_replay', None)
            if replay is not None:
                replay(gcmd.get_int('HEAD'))
        return result

    def _cmd_ACE_SWAP_HEAD_body(self, gcmd):

        head = gcmd.get_int('HEAD')
        ace_index = gcmd.get_int('ACE')
        slot = gcmd.get_int('SLOT', head)
        self._refuse_during_manual_load(gcmd, head)
        # SKIP_POS_RESTORE is DEAD and deliberately IGNORED. The
        # rewrite used to set it on every swap on the promise "a prime follows
        # and repositions" - but the slicer only emits a preextrude line at a
        # colour's FIRST use, so on a repeat-colour swap nothing followed and
        # the print resumed from the LOAD position (dock row Y~300): the next
        # moves swept the dock line and knocked parked heads off their docks
        # Old processed gcode files still carry the
        # flag - accept it, warn, and ALWAYS restore. Do not re-honour it.
        if gcmd.get_int('SKIP_POS_RESTORE', 0):
            logging.info('[multiACE] Swap: SKIP_POS_RESTORE=1 ignored '
                         '(deprecated, stale processed gcode) - doing the '
                         'full pos-restore')
        # Per-swap anti-ooze override: the preflight stamps ANTI_OOZE=<n> with
        # the ARRIVING filament's toolchange retract (slicer footer key
        # filament_retract_length_toolchange) so the swap's end-retract mirrors
        # EXACTLY what the slicer will un-retract at the wipe tower (explicit
        # "G1 E<n> F1800" right before CP TOOLCHANGE WIPE, layer 1+). A fixed
        # cushion against a foreign profile either under-fills (10 vs a
        # 1.5-profile = half prime line) or blobs (cushion smaller than the
        # slicer's un-retract). Absent = config knob.
        anti_ooze = gcmd.get_float(
            'ANTI_OOZE', float(self.swap_anti_ooze_retract),
            minval=0., maxval=50.)
        # INITIAL=1: stamped ONLY by the preflight's auto-load block. There
        # the saved "print position" is just wherever the start gcode stood
        # when the block began (typically the bed CENTER) - the
        # tail's pos restore parked the freshly primed head there, oozing
        # over the bed while the bg partner's arrival line waited in
        # _wait_bg_op. With INITIAL=1 the tail parks at the DISCARD position
        # instead (ooze falls into the chute; stock display loads end there
        # too). Never emitted for body swaps - the mid-print restore
        # semantics are untouched, and an absent flag keeps the
        # old behaviour (stale processed files stay safe).
        initial_swap = gcmd.get_int('INITIAL', 0)

        if head < 0 or head > 3:
            raise self._ace_error(gcmd, 'HEAD must be 0-3', code=200)
        # A head without ACE (feeder or manual) has no ACE slot to swap to;
        # it carries one filament, loaded via its side feeder or by hand.
        # Such a line (e.g. the initial load an offline slicer-side script
        # writes for every head) is skipped quietly, log only. Running it
        # used to pull the tip of a loaded feeder head and arm feed-assist
        # on an ACE slot that belongs to another head.
        if not self.head_uses_ace(head):
            logging.info('[multiACE] ACE_SWAP_HEAD skipped: head %d has no '
                         'MMU (feeder/manual)' % self._disp(head))
            return
        self._wait_bg_op(head, gcmd, rearm_target=(ace_index, slot),
                         park=True)
        if ace_index < 0 or not self._ensure_ace_available(ace_index):
            raise self._ace_error(gcmd,
                'MMU %d not available' % self._disp(ace_index),
                code=208, head=head)
        if slot < 0 or slot > 3:
            raise self._ace_error(gcmd, 'SLOT must be 0-3', code=200,
                                  head=head)
        # 1:1 invariant (head mode): a swap may only target the head's wired
        # ACE (head_ace_for). Refusing a foreign ACE here turns a broken
        # caller (mis-generated gcode, wrong auto-load block) into a loud
        # error instead of silently feeding the wrong unit. Multi: unchanged.
        if (getattr(self, '_ace_mode', 'multi') == 'head'
                and self.head_uses_ace(head)
                and ace_index != self.head_ace_for(head)):
            raise self._ace_error(gcmd,
                'head %d is wired to MMU %d (one MMU per head) - '
                'refusing swap on MMU %d. Rewire via ACE_SET_HEAD_ACE first.'
                % (self._disp(head), self._disp(self.head_ace_for(head)),
                   self._disp(ace_index)),
                code=207, head=head)
        if head in self._ghost_heads:
            raise self._ace_error(gcmd,
                'SWAP refused: head %d is a ghost (filament at '
                'toolhead but no head_source mapping recorded). FA routing '
                'would have to guess which MMU to drive. '
                'Recover: ACEC__Unload_All then ACEB__Load_%d, then restart '
                'the print.' % (head, head),
                code=202, head=head)

        source = self._head_source.get(head)
        if (source and source['ace_index'] == ace_index and source['slot'] == slot
                and not source.get('load_failed')):
            logging.info('[multiACE] Swap: HEAD %d already on MMU %d / Slot %d - skipping' % (
                head, ace_index, slot))

            # Only (re)heat when this head is the active toolhead. A real
            # mid-print swap-back onto an already-loaded head is always
            # preceded by a T<head> (post_process rewrite() emits
            # "T%d\nACE_SWAP_HEAD ..."), so the head is active and needs
            # extrusion heat. Pre-print head-picking, by contrast, calls
            # ACE_SWAP_HEAD for every loaded head with no T in between
            # (inject_auto_load emits bare swaps), leaving the active
            # toolhead unchanged - heating those would needlessly hold
            # idle heads at load_temp for the whole print (and stall the
            # print start ~20-30s per head on TEMPERATURE_WAIT).
            try:
                active_ext = self.toolhead.get_extruder().get_name()
                active_head = (0 if active_ext == 'extruder'
                               else int(active_ext.replace('extruder', '')))
            except Exception:
                active_head = None
            swap_temp = self._get_swap_temp(head)
            if head == active_head and swap_temp >= 170:
                heater = 'extruder' if head == 0 else 'extruder%d' % head
                self.gcode.run_script_from_command(
                    'SET_HEATER_TEMPERATURE HEATER=%s TARGET=%d' % (heater, swap_temp))
                self.gcode.run_script_from_command(
                    'TEMPERATURE_WAIT SENSOR=%s MINIMUM=%d' % (heater, swap_temp - 5))
                # Arrival onto a head the BACKGROUND engine loaded: its
                # grip/prime ran as stealth moves no sensor verified.
                # One-shot pick-time flow check. With [ace_bg_swap]
                # pick_gate (default True) a PERSISTENT no-flow (the check
                # already re-gripped + re-measured internally) or an ABSENT
                # sensor escalates HERE to a resumable pause - after the
                # check restored pos/E, so RESUME continues cleanly.
                # Gate off / no coil -> verdict None, log-only.
                _had_pickcheck = head in getattr(self, '_bg_load_unverified', ())
                if _had_pickcheck:
                    _pick = self._bg_pick_flow_check(head, anti_ooze)
                    if _pick == 'no_flow':
                        self._pause_for_recovery(
                            gcmd,
                            detail_msg=self._t('msg.bg_pick_no_flow_pause',
                                               head=self._disp(head)),
                            recovery_steps=[
                                'Check the nozzle / filament path of head '
                                '%d (no flow despite re-grip)'
                                    % self._disp(head),
                                'Purge manually until filament extrudes',
                                'RESUME                (continue the print)',
                            ],
                        )
                    elif _pick == 'sensor_absent':
                        self._pause_for_recovery(
                            gcmd,
                            detail_msg=self._t('msg.bg_pick_absent_pause',
                                               head=self._disp(head)),
                            recovery_steps=[
                                'Load head %d manually (web Load or '
                                'ACE_LOAD_HEAD)' % self._disp(head),
                                'RESUME                (continue the print)',
                            ],
                        )
                    elif _pick == 'resistance':
                        # Two-strike resistance escalation ([ace]
                        # resistance_pause): the lane's back-pressure sat far
                        # above its own baseline on consecutive checks = the
                        # chew phase that precedes an airprint. Pause
                        # NOW, while the filament is still extractable.
                        self._pause_for_recovery(
                            gcmd,
                            detail_msg=self._t('msg.resistance_pause',
                                               head=self._disp(head)),
                            recovery_steps=[
                                'Check spool / bowden / nozzle of head %d '
                                '(rising back-pressure, extruder may click)'
                                    % self._disp(head),
                                'RESUME                (continue the print)',
                            ],
                        )
                # Post-(re)start no-op wipe (RESUME_NOOP_WIPE_WINDOW const
                # note): this head sat hot+idle through the pause and this
                # no-op runs no flush - wipe the accumulated drool at the
                # discard edge before the first print move. The bg
                # pick-check path above already wiped (BG_PICK_WIPE).
                if (self.reactor.monotonic()
                        < getattr(self, '_resume_wipe_deadline', 0.)):
                    self._resume_wipe_deadline = 0.
                    if not _had_pickcheck:
                        self.log_always(
                            '[multiACE] Post-resume ooze wipe: head %d sat '
                            'hot through the pause, no-op swap runs no '
                            'flush - wiping at the discard edge'
                            % self._disp(head))
                        self._discard_wipe(head, 'resume-wipe')
            elif head != active_head:
                logging.info('[multiACE] Swap: HEAD %d not active toolhead '
                             '(active=%s) - skip pre-heat to avoid holding '
                             'idle head at load_temp' % (head, active_head))
            return

        if ace_index in self._fa_load_disable:
            self.log_error(self._t('msg.swap_refused_fa_load_disable',
                ace=self._disp(ace_index), head=self._disp(head)))
            return

        target_gate = self._gate_status_per_ace.get(ace_index)
        if (target_gate is not None and slot < len(target_gate)
                and self._await_known_gate(target_gate, slot) == GATE_UNKNOWN):
            self._pause_for_recovery(
                gcmd,
                detail_msg=self._t('msg.pause_swap_gate_unknown',
                    head=self._disp(head), ace=self._disp(ace_index),
                    slot=self._disp(slot)),
                recovery_steps=[
                    'Check USB / power of MMU %d' % self._disp(ace_index),
                    'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d   (re-run swap)'
                        % (head, ace_index, slot),
                    'RESUME                            (continue the print)',
                ],
            )
            return
        if (target_gate is not None and slot < len(target_gate)
                and target_gate[slot] != GATE_AVAILABLE):
            cur_src = self._head_source.get(head)
            self._telemetry('SWAP_SUMMARY', {
                'head': head,
                'from_ace': cur_src['ace_index'] if cur_src else None,
                'from_slot': cur_src['slot'] if cur_src else None,
                'to_ace': ace_index,
                'to_slot': slot,
                'status': 'slot_empty_pre_unload',
                'total_ms': 0,
                'unload_ms': None,
                'load_ms': None,
                'context': self._fa_context,
            })
            self._pause_for_recovery(
                gcmd,
                detail_msg=self._t('msg.pause_swap_slot_empty',
                    head=self._disp(head), ace=self._disp(ace_index),
                    slot=self._disp(slot)),
                recovery_steps=[
                    'Load filament into MMU %d / Slot %d'
                        % (self._disp(ace_index), self._disp(slot)),
                    'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d   (re-run swap)'
                        % (head, ace_index, slot),
                    'RESUME                            (continue the print)',
                ],
                code=3,
            )
            return

        swap_temp = self._get_swap_temp(head)

        self.log_always(self._t('msg.swap_start',
            head=self._disp(head), ace=self._disp(ace_index), slot=self._disp(slot),
            temp=swap_temp))

        swap_start_ts = time.monotonic()
        unload_start_ts = None
        unload_end_ts = None
        load_start_ts = None
        load_end_ts = None
        swap_status = 'ok'
        prev_source = self._head_source.get(head)
        prev_ace_src = prev_source['ace_index'] if prev_source else None
        prev_slot_src = prev_source['slot'] if prev_source else None

        self._swap_in_progress = True
        self._swap_phase = 'unload'
        # A FULL swap flushes + wipes on its own - the post-(re)start no-op
        # wipe window is satisfied by it (RESUME_NOOP_WIPE_WINDOW const note).
        self._resume_wipe_deadline = 0.
        # Defensive: a pending resistance escalation from a PREVIOUS load
        # (e.g. an idle load outside any swap) must not fire on this swap.
        self._resistance_pause_pending = None
        self._ace_event(
            'swap_imminent', head=head, ace=ace_index, slot=slot,
            from_ace=(prev_ace_src if prev_ace_src is not None else -1),
            from_slot=(prev_slot_src if prev_slot_src is not None else -1))

        fa_prev_auto = self._auto_feed_enabled
        fa_prev_context = self._fa_context
        self._auto_feed_enabled = False
        self._fa_context = 'idle'
        self._fa_trace('gate CLOSE for swap unload (was auto=%s context=%s)' % (
            fa_prev_auto, fa_prev_context))

        try:

            gcode_move = self.printer.lookup_object('gcode_move')
            (saved_pos, saved_speed, saved_absolute, saved_e_base,
             saved_e_last) = self._pos_snapshot()
            logging.info('[multiACE] Swap: saved pos X=%.2f Y=%.2f Z=%.2f (pre-T-switch)' % (
                saved_pos[0], saved_pos[1], saved_pos[2]))

            self._fa_log.info(
                '[swap-trace] ENTRY head=%d ace=%d slot=%d '
                'saved_e_base=%.3f saved_e_last=%.3f '
                'abs_extrude=%s anti_ooze=%.2f'
                % (head, ace_index, slot, saved_e_base, saved_e_last,
                   gcode_move.absolute_extrude,
                   anti_ooze))

            orig_ext_name = self.toolhead.get_extruder().get_name()
            target_ext = 'extruder' if head == 0 else 'extruder%d' % head
            switched_head = (orig_ext_name != target_ext)
            # Expose to the comms-loss PAUSE (reactor timer) so it can move back
            # to the print position before pausing if the swap is interrupted.
            self._swap_saved_pos = saved_pos
            self._swap_orig_ext_name = orig_ext_name
            self._swap_switched_head = switched_head
            if switched_head:
                logging.info('[multiACE] Swap: switching to %s (was %s)' % (target_ext, orig_ext_name))
                self.gcode.run_script_from_command('T%d A0' % head)
                self.toolhead.wait_moves()

            saved_heater_target = 0
            try:
                extruder_obj = self.toolhead.get_extruder()
                if extruder_obj is not None:
                    saved_heater_target = int(extruder_obj.get_heater().target_temp)
            except Exception:
                pass
            logging.info('[multiACE] Swap: saved heater=%d (swap head)' % saved_heater_target)
            # A pause can leave the swap head at target 0, and its RESUME
            # replays this swap line before any slicer M104/M109. Restoring
            # that 0 at the swap tail lets the print run on with a cooling
            # nozzle. While printing, a target below min extrude is replaced
            # by the last print target a swap saw on this head, else by the
            # swap temp. The INITIAL block keeps the found target: the start
            # gcode heats the head itself.
            if saved_heater_target >= SWAP_MIN_EXTRUDE_TEMP:
                self._swap_print_temp[head] = saved_heater_target
            elif not initial_swap and self._is_actively_printing():
                fallback = self._swap_print_temp.get(head) or swap_temp
                logging.info('[multiACE] Swap: saved heater %d is below min '
                             'extrude while printing - restoring %d (%s)'
                             % (saved_heater_target, fallback,
                                'last print target' if head in
                                self._swap_print_temp else 'swap temp'))
                saved_heater_target = fallback
            # Reference for the cool forward-probe temp (material-agnostic):
            # the live print target of this head, before INNER zeroes it.
            self._swap_probe_ref_temp = saved_heater_target
            # The load's finally switches the part fan off (M107, as stock
            # does); the tail sets it back so the print does not run on
            # uncooled until the slicer's next M106.
            saved_part_fan = 0.
            try:
                _fan = self.printer.lookup_object('fan', None)
                saved_part_fan = float(
                    getattr(_fan.fan, 'last_fan_value', 0.) or 0.)
            except Exception:
                saved_part_fan = 0.

            prev_ace = self._active_device_index
            if self._feed_assist_per_ace.get(prev_ace, -1) != -1:
                self._disarm_fa_for(prev_ace)

            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('G1 Z2 F600')
            self.gcode.run_script_from_command('G90')
            self.toolhead.wait_moves()

            self.gcode.run_script_from_command('M83')

            sensor_obj = self.printer.lookup_object(
                'filament_motion_sensor e%d_filament' % head, None)
            # Presence pin first, motion helper only as its fallback: after a
            # hand removal the motion helper still reads True (it flips only
            # on extruder advance) and the swap then "unloaded" an empty head,
            # retracting a freshly inserted spool (HW 2026-10-25 07:06).
            sensor_present = (sensor_obj is not None and
                              bool(self._head_filament_present(sensor_obj, head)))
            # A background swap that aborted after its sensor arrival left the
            # filament STAGED at the toolhead sensor (no head_source, sensor
            # reads present) - trust the bg's explicit signal over the sensor
            # and skip the unload half: there is nothing in the hotend, the
            # load half feeds the same slot and its sensor stop fires
            # immediately. Without the flag the redundant inline unload runs
            # on a guessed head_source and retracts the WRONG ACE/slot.
            # Consumed ONE-SHOT here (a staged head has no
            # insert edge to clear it via note_filament_present).
            bg_empty = head in getattr(self, '_bg_left_empty', ())
            empty_head = ((not sensor_present) and (prev_source is None)) or bg_empty

            if empty_head:
                if bg_empty:
                    self._bg_left_empty.discard(head)
                    # GET, not pop: on a slot MATCH the entry must survive
                    # down to the delegated cmd_ACE_LOAD_HEAD, whose
                    # sensor-present early-return otherwise misreads the
                    # staged filament as "already loaded" and does NOTHING -
                    # no feed/grip/prime, no head_source. ACE_LOAD_HEAD
                    # consumes it and runs
                    # the REAL load (feed sensor-stops immediately).
                    staged = getattr(self, '_bg_staged', {}).get(head)
                    if (staged is not None
                            and (int(staged[0]) != int(ace_index)
                                 or int(staged[1]) != int(slot))):
                        # The staged filament belongs to a DIFFERENT slot
                        # than this swap's target - two filaments may not
                        # share the path. Mid-print this is repairable
                        # (unload the head, RESUME replays the swap on an
                        # empty head), so pause instead of cancelling;
                        # idle keeps the plain refusal. The entry stays for
                        # the manual unload's slot routing.
                        if self._is_actively_printing():
                            swap_status = 'staged_mismatch'
                            self._swap_back_to_orig_for_pause(
                                switched_head, orig_ext_name)
                            self._restore_pos_for_pause(saved_pos)
                            _sA, _sS = (self._disp(staged[0]),
                                        self._disp(staged[1]))
                            _lA, _lS = self._disp(ace_index), self._disp(slot)
                            self._pause_for_recovery(
                                gcmd,
                                detail_msg=self._t(
                                    'msg.pause_swap_staged_mismatch',
                                    head=self._disp(head), sa=_sA, ss=_sS,
                                    la=_lA, ls=_lS),
                                recovery_steps=[
                                    'Unload head %d (display/web) - it '
                                    'holds MMU%d / Slot%d staged at the '
                                    'sensor' % (self._disp(head), _sA, _sS),
                                    'RESUME                            '
                                    '(the swap repeats and loads MMU%d / '
                                    'Slot%d)' % (_lA, _lS),
                                ])
                            return
                        raise self._ace_error(gcmd,
                            'head %d has filament of MMU %d / '
                            'Slot %d STAGED at the sensor but the swap '
                            'targets MMU %d / Slot %d - unload the head '
                            'first (display/web), then retry'
                            % (self._disp(head), self._disp(staged[0]),
                               self._disp(staged[1]), self._disp(ace_index),
                               self._disp(slot)),
                            code=204, head=head)
                logging.info(
                    '[multiACE] Swap: head %d is empty '
                    '(sensor=%s, head_source=%s, bg_left_empty=%s) - skipping '
                    'unload, proceeding directly to load'
                    % (head, sensor_present, prev_source is not None, bg_empty))
                unload_start_ts = time.monotonic()
                unload_end_ts = unload_start_ts
            else:

                logging.info('[multiACE] Swap: delegating unload to ACE_UNLOAD_HEAD')
                unload_start_ts = time.monotonic()
                if prev_source:
                    _src_ace = int(prev_source.get('ace_index',
                                                   self._active_device_index))
                    _src_slot = int(prev_source.get('slot', head))
                else:
                    _src_ace = self._active_device_index
                    _src_slot = head
                swap_rl = self.get_swap_retract_length(_src_ace, _src_slot)
                # Tag the failure BEFORE the exception leaves: the terminal
                # swap_failed event in the finally reads swap_status, and an
                # unload that RAISES never reaches the _last_unload_ok check
                # below - so without this it would ship the generic 'error'
                # instead of 'unload_failed' (same gap as the load half). The
                # re-raise keeps the routing exactly as it was.
                try:
                    if swap_rl > 0:
                        self.gcode.run_script_from_command(
                            'ACE_UNLOAD_HEAD HEAD=%d RETRACT_LENGTH=%d KEEP_HEAT=%d' % (
                                head, swap_rl, swap_temp))
                        logging.info('[multiACE] Swap: unload done (retract %dmm, heat held @ %d)' % (
                            swap_rl, swap_temp))
                    else:
                        self.gcode.run_script_from_command(
                            'ACE_UNLOAD_HEAD HEAD=%d KEEP_HEAT=%d' % (head, swap_temp))
                        logging.info('[multiACE] Swap: unload done (per-MMU retract_length, heat held @ %d)' % swap_temp)
                except Exception:
                    swap_status = 'unload_failed'
                    raise
                unload_end_ts = time.monotonic()

                if not self._last_unload_ok:

                    swap_status = 'unload_failed'
                    self._swap_back_to_orig_for_pause(
                        switched_head, orig_ext_name)
                    self._restore_pos_for_pause(saved_pos)
                    _uA, _uS = self._disp(_src_ace), self._disp(_src_slot)
                    _lA, _lS = self._disp(ace_index), self._disp(slot)
                    # Two very different failures share this branch, and they
                    # send the user to opposite ends of the machine: a
                    # tip stuck in the HEAD vs a strand that never
                    # left the TUBE. The unload-first advice is right for the
                    # first and pointless for the second, where the head is
                    # already empty and only the bowden needs clearing.
                    _ureason = getattr(self, '_last_unload_reason', 'toolhead')
                    if _ureason == 'gate_empty':
                        # The strand ran out or broke above the ACE gate: the
                        # head still holds its tail, the ACE cannot pull it.
                        # Unloading the slot is pointless; the user pulls the
                        # rest out of the head by hand, then the replayed
                        # swap sees an empty head and only loads.
                        _detail = self._t('msg.pause_swap_unload_gate_empty',
                            head=self._disp(head), ua=_uA, us=_uS)
                        _steps = [
                            'Heat head %d, open the bowden coupling and pull '
                            'the rest of the filament out by hand'
                            % self._disp(head),
                            'Make sure MMU%d / Slot%d holds a spool' % (_lA, _lS),
                            'RESUME                            (continue the print)',
                        ]
                    elif _ureason == 'bowden_stall':
                        _detail = self._t('msg.pause_swap_unload_bowden',
                            head=self._disp(head), ua=_uA, us=_uS)
                        _steps = [
                            'Clear the tube between head %d and MMU%d / Slot%d'
                            % (self._disp(head), _uA, _uS),
                            'Load MMU%d / Slot%d' % (_lA, _lS),
                            'RESUME                            (continue the print)',
                        ]
                    else:
                        _detail = self._t('msg.pause_swap_unload_jam',
                            head=self._disp(head), ua=_uA, us=_uS, la=_lA, ls=_lS)
                        _steps = [
                            'Unload MMU%d / Slot%d' % (_uA, _uS),
                            'Load MMU%d / Slot%d' % (_lA, _lS),
                            'RESUME                            (continue the print)',
                        ]
                    self._pause_for_recovery(
                        gcmd, detail_msg=_detail, recovery_steps=_steps)
                    return

            if ace_index != self._active_device_index:
                logging.info(self._t('msg.swap_switching_ace',
                    ace=self._raw(ace_index)))
                if not self._switch_ace_for_head_target(ace_index):
                    raise self._ace_error(gcmd,
                        'Failed to connect to MMU %d' % self._disp(ace_index),
                        code=208, head=head)

            if self._await_known_gate(self.gate_status, slot) == GATE_UNKNOWN:
                swap_status = 'gate_unknown'
                self._swap_back_to_orig_for_pause(
                    switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                self._pause_for_recovery(
                    gcmd,
                    detail_msg=self._t('msg.pause_swap_gate_unknown',
                        head=self._disp(head), ace=self._disp(ace_index),
                        slot=self._disp(slot)),
                    recovery_steps=[
                        'Check USB / power of MMU %d' % self._disp(ace_index),
                        'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (load head)'
                            % (head, ace_index, slot),
                        'RESUME                            (continue the print)',
                    ],
                )
                return

            if self.gate_status[slot] != GATE_AVAILABLE:

                swap_status = 'slot_empty'
                self._swap_back_to_orig_for_pause(
                    switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                self._pause_for_recovery(
                    gcmd,
                    detail_msg=self._t('msg.pause_swap_slot_empty_post',
                        head=self._disp(head), ace=self._disp(ace_index),
                        slot=self._disp(slot)),
                    recovery_steps=[
                        'Load filament into MMU %d / Slot %d'
                            % (self._disp(ace_index), self._disp(slot)),
                        'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (load head)'
                            % (head, ace_index, slot),
                        'RESUME                            (continue the print)',
                    ],
                    code=3,
                )
                return

            self._swap_phase = 'load'
            logging.info('[multiACE] Swap: delegating load to ACE_LOAD_HEAD (MMU %d / Slot %d)' % (ace_index, slot))
            load_start_ts = time.monotonic()
            try:
                self.gcode.run_script_from_command(
                    'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace_index, slot))
            except Exception as load_e:
                # A feed timeout ALWAYS lands here, not in the _last_load_ok
                # branch below - so this is the path that ships
                # `swap_failed` to a listening host, and swap_status would
                # still read 'ok' here. The recovery message is classified
                # (_load_slip_details); name the failure for the event too.
                # docs/ENGINE_API.md defines status as the failure tag.
                swap_status = 'load_failed'
                logging.info(
                    '[multiACE] Swap LOAD raised before completion: %s '
                    '(routing to swap_back+pos_restore+pause)' % load_e)
                self._swap_back_to_orig_for_pause(
                    switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                # Surface a clean multiACE recovery message + steps instead of
                # re-raising the raw stock feed error (e.g. "extruder[1]: state:
                # load_feeding, error: timeout! raw msg:logic error!"), which is
                # what reached the display/Fluidd before. The message is
                # CLASSIFIED (no transport vs no flow) via the toolhead sensor
                # (_load_slip_details); the raw error stays in klippy.log
                # (logged above). _pause_for_recovery restores main_state +
                # raises an action='pause' gcmd.error (resumable), no re-raise.
                _detail, _steps = self._load_slip_details(
                    head, ace_index, slot)
                self._pause_for_recovery(
                    gcmd, detail_msg=_detail, recovery_steps=_steps)
                return
            load_end_ts = time.monotonic()

            if not self._last_load_ok:
                swap_status = 'load_failed'
                self._swap_back_to_orig_for_pause(
                    switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                _detail, _steps = self._load_slip_details(
                    head, ace_index, slot)
                self._pause_for_recovery(
                    gcmd, detail_msg=_detail, recovery_steps=_steps)
                return

            logging.info('[multiACE] Swap: load done')
            self._swap_phase = 'flush'

            self._auto_feed_enabled = True
            self._fa_context = fa_prev_context if fa_prev_context in ('print', 'load') else 'print'
            try:
                self._arm_fa_for(ace_index, slot)
                self.wait_ace_ready()
                # Verify FA actually took on the device shortly after; re-arm if
                # the device slot is still not in a running state.
                self._v2_schedule_fa_rearm(
                    ace_index, slot, 'post-load-verify', delay=0.20)
                self._fa_trace('gate RE-OPEN for post-load wipe (context=%s) on MMU %d slot %d' % (
                    self._fa_context, ace_index, slot))
            except Exception as fa_e:
                logging.info('[multiACE] post-load FA re-enable failed: %s' % fa_e)

            wipe_temp = (saved_heater_target
                         if saved_heater_target >= SWAP_MIN_EXTRUDE_TEMP
                         else swap_temp)
            self.gcode.run_script_from_command('M109 S%d' % wipe_temp)
            self.gcode.run_script_from_command('ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD')
            self.toolhead.wait_moves()

            self._fa_log.info(
                '[swap-trace] POST_LOAD last_pos=%.3f delta_from_entry=%+.3f'
                % (gcode_move.last_position[3],
                   gcode_move.last_position[3] - saved_e_last))
            self.gcode.run_script_from_command('G91')
            if anti_ooze > 0:
                self.gcode.run_script_from_command(
                    'G1 E-%.2f F1800' % anti_ooze)
            self.gcode.run_script_from_command('G90')
            self.toolhead.wait_moves()
            self._fa_log.info(
                '[swap-trace] POST_ANTI_OOZE_RETRACT last_pos=%.3f anti_ooze=%.2f'
                % (gcode_move.last_position[3], anti_ooze))

            if wipe_temp != saved_heater_target:
                self.gcode.run_script_from_command('M104 S%d' % saved_heater_target)
                if saved_heater_target >= 190:
                    self.gcode.run_script_from_command('M109 S%d' % saved_heater_target)
            logging.info('[multiACE] Swap: heater target restored=%d (wipe was %d)'
                         % (saved_heater_target, wipe_temp))
            if saved_part_fan > 0. and not initial_swap:
                self.gcode.run_script_from_command(
                    'M106 S%d' % int(round(saved_part_fan * 255.)))
                logging.info('[multiACE] Swap: part fan restored S%d'
                             % int(round(saved_part_fan * 255.)))

            if self.swap_post_retract_wipe:
                self.gcode.run_script_from_command(
                    'INNER_DISCARD_FILAMENT_BASE_DISCARD')
                self.toolhead.wait_moves()

            if switched_head:
                orig_head = 0 if orig_ext_name == 'extruder' else int(
                    orig_ext_name.replace('extruder', ''))
                logging.info('[multiACE] Swap: switching back to %s' % orig_ext_name)
                self.gcode.run_script_from_command('T%d A0' % orig_head)
                self.toolhead.wait_moves()

            e_diff = gcode_move.last_position[3] - saved_e_last
            gcode_move.base_position[3] = saved_e_base + e_diff
            self._fa_log.info(
                '[swap-trace] E_DIFF_ADJUST last_pos=%.3f saved_e_last=%.3f '
                'e_diff=%+.3f new_base=%.3f'
                % (gcode_move.last_position[3], saved_e_last,
                   e_diff, gcode_move.base_position[3]))

            self.gcode.run_script_from_command('G90')
            if initial_swap:
                # Auto-load block: park over the discard chute instead of
                # traveling to the meaningless pre-block position. The head
                # then waits out any bg-partner _wait_bg_op with its ooze
                # falling into the chute, not onto the bed. Z-hop first,
                # then the stock macro (the pick-check's proven pattern).
                self.gcode.run_script_from_command(
                    'G0 Z%.3f F600' % (saved_pos[2] + 3.0))
                try:
                    self.gcode.run_script_from_command(
                        'MOVE_TO_DISCARD_FILAMENT_POSITION')
                except Exception as park_e:
                    logging.info('[multiACE] Swap: discard park failed (%s) '
                                 '- falling back to pos restore' % park_e)
                    self.gcode.run_script_from_command(
                        'G0 Y%.3f F12000' % saved_pos[1])
                    self.gcode.run_script_from_command(
                        'G0 X%.3f F12000' % saved_pos[0])
            else:
                self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 3.0))
                self.gcode.run_script_from_command('G0 Y%.3f F12000' % saved_pos[1])
                self.gcode.run_script_from_command('G0 X%.3f F12000' % saved_pos[0])
                self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 2.0))
            self.toolhead.wait_moves()

            if saved_absolute:
                self.gcode.run_script_from_command('G90')

            self.gcode.run_script_from_command('G1 F%d' % (saved_speed * 60))
            self._fa_log.info(
                '[swap-trace] EXIT last_pos=%.3f base=%.3f '
                'slicer_view_e=%.3f (= last_pos - base)'
                % (gcode_move.last_position[3],
                   gcode_move.base_position[3],
                   gcode_move.last_position[3] - gcode_move.base_position[3]))

            if initial_swap:
                logging.info('[multiACE] Swap: INITIAL=1 - parked at the '
                             'discard position (auto-load block), no pos '
                             'restore')
            else:
                logging.info('[multiACE] Swap: restored pos X=%.2f Y=%.2f Z=%.2f (+2mm travel hop)' % (
                    saved_pos[0], saved_pos[1], saved_pos[2]))

            self._swap_phase = 'done'
            self._last_swap_result = {
                'head': head, 'ace': ace_index, 'slot': slot,
                'status': 'ok', 'ts': self.reactor.monotonic(),
            }
            self._ace_event('slot_ready', head=head, ace=ace_index, slot=slot)
            self._ace_event('swap_done', head=head, ace=ace_index, slot=slot,
                            status='ok')

            self.log_always(self._t('msg.swap_complete',
                head=self._disp(head), ace=self._disp(ace_index), slot=self._disp(slot)))

            # phase3 resistance escalation, consumed at the SAFE point: the
            # swap is COMPLETE (flush/wipe done, pos restored, phase 'done' -
            # the finally below treats this as success), so pausing here
            # keeps the recovery semantics intact. phase3 itself must never
            # raise for a SUCCEEDED load mid-swap (mid-swap pauses skip the
            # pos-restore); it only sets the pending flag.
            _res_pending = getattr(self, '_resistance_pause_pending', None)
            if _res_pending is not None:
                self._resistance_pause_pending = None
                _rp_head = _res_pending[0]
                self._pause_for_recovery(
                    gcmd,
                    detail_msg=self._t('msg.resistance_pause',
                                       head=self._disp(_rp_head)),
                    recovery_steps=[
                        'Check spool / bowden / nozzle of head %d '
                        '(rising back-pressure, extruder may click)'
                            % self._disp(_rp_head),
                        'RESUME                (continue the print)',
                    ],
                )
        finally:
            self._swap_in_progress = False
            self._swap_saved_pos = None
            # Backstop: a raised feed error can escape a dwell-fan window
            # before its OFF ran - restore here (no-op when already off;
            # the OFF path deliberately ignores _swap_in_progress).
            self._dwell_fan(False)

            # Terminal event for any non-success exit (recovery pause, raised
            # error): the success path set swap_phase='done' above; anything
            # else is a failure/abort the host should observe.
            if self._swap_phase != 'done':
                # Normalise ONCE and use it for both channels. The status dict
                # already maps a stale 'ok' to 'error' here, and the event
                # must say the same - 'ok' is not a legal value for this
                # event: docs/ENGINE_API.md defines status as the FAILURE tag
                # (unload_failed, load_failed, slot_empty, ...).
                swap_fail_status = (swap_status
                                    if swap_status != 'ok' else 'error')
                self._last_swap_result = {
                    'head': head, 'ace': ace_index, 'slot': slot,
                    'status': swap_fail_status,
                    'ts': self.reactor.monotonic(),
                }
                self._ace_event('swap_failed', head=head, ace=ace_index,
                                slot=slot, status=swap_fail_status)
            self._swap_phase = 'idle'

            self._auto_feed_enabled = fa_prev_auto
            self._fa_context = fa_prev_context

            if fa_prev_auto:
                try:
                    active_ext = self.toolhead.get_extruder().get_name()
                    active_head = (0 if active_ext == 'extruder'
                                   else int(active_ext.replace('extruder', '')))
                    active_source = self._head_source.get(active_head)
                    if active_source is not None:
                        self._arm_fa_for(
                            active_source['ace_index'], active_source['slot'])
                    else:
                        logging.info(
                            '[multiACE] post-swap FA: active head %d has no head_source, skipping start' % active_head)
                except Exception as e:
                    logging.info('[multiACE] post-swap FA start failed: %s' % e)
            self._fa_trace('gate restored (context=%s auto=%s) after ACE_SWAP_HEAD'
                           % (fa_prev_context, fa_prev_auto))
            self._audit_state('SWAP_HEAD', {'head': head, 'ace': ace_index, 'slot': slot})

            def _dur_ms(start, end):
                if start is None or end is None:
                    return None
                return int((end - start) * 1000)
            swap_end_ts = time.monotonic()
            self._telemetry('SWAP_SUMMARY', {
                'head': head,
                'from_ace': prev_ace_src,
                'from_slot': prev_slot_src,
                'to_ace': ace_index,
                'to_slot': slot,
                'status': swap_status,
                'total_ms': _dur_ms(swap_start_ts, swap_end_ts),
                'unload_ms': _dur_ms(unload_start_ts, unload_end_ts),
                'load_ms': _dur_ms(load_start_ts, load_end_ts),
                'context': fa_prev_context,
            })
