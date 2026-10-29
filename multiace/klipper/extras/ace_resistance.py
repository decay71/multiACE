"""Resistance watch and air-print detection (mixin of MultiAce).

The coil-delta upper-bound watch on passing load-time measurements
(`_resistance_note`), the print-time airlog sampler (`_airlog_tick`) and
the chew detector riding it (`_airlog_chew_eval`). State lives on the
MultiAce instance (initialised in ace.py); this module holds the code and
its constants. First module of the R1 split (UNIT_DRIVER_DESIGN.md §11):
every method here is byte-identical to its former place in ace.py, proven
by tests/units/tools/move_check.py.
"""
import logging

# [diag, LOG-ONLY] Air-print baseline sampler. Passively samples
# the ACTIVE extruder's eddy-coil freq + live extruder velocity while a print
# runs and emits one compact wiggle-log line per window. Purpose: learn
# whether printing flow and air separate in coil data at print speeds, and
# timestamp a flow death between swaps, where nothing else watches. Costs
# one coil + motion_report read per AIRLOG_SAMPLE_S while the extruder
# MOVES (the coil is also the bed probe, see _airlog_tick); nothing gates,
# nothing pauses. TOGGLE: [ace] airlog (default False, read unconditionally).
AIRLOG_SAMPLE_S = 2.0
AIRLOG_EMIT_S = 10.0
# --- Airlog CHEW detector (rides the sampler, so it needs BOTH [ace]
# airlog: true AND [ace] resistance_pause: true to pause - with
# resistance_pause off it warns only). Trigger: >= AIRLOG_CHEW_WINDOWS
# consecutive emit-windows of the ACTIVE head with coil_delta >=
# AIRLOG_CHEW_DELTA and moving >= AIRLOG_CHEW_MIN_MOVING. Confirmed chew
# windows read well above the largest legitimate printing window, so the
# threshold separates alone. There is deliberately no velocity gate: chew
# windows carry travel moves too, a velocity cap would blind the detector.
# Suppression: never evaluate during a swap or the bg pick-check, whose own
# push reads as turbulence. Pause latch: once per head per print, reset at
# print start/resume (a resume into a still-chewing lane re-pauses after
# fresh windows - that is protection, not nagging).
AIRLOG_CHEW_DELTA = 25000.
AIRLOG_CHEW_WINDOWS = 2
AIRLOG_CHEW_MIN_MOVING = 3
# --- Resistance watch -------------------------------------------------------
# The coil measures the binding lever = BACK-PRESSURE, so a load-time delta
# far ABOVE a lane's own baseline is not "excellent flow" but the extruder
# hammering against resistance (audible as gear clicking) - the chew phase
# that precedes an airprint, and which a one-sided delta>threshold test
# passes as FLOW. _resistance_note() keeps a per-(site,head,ace,slot,push)
# EMA baseline learned ONLY from unsuspicious reads (a ramp must not drag
# its own baseline up) and flags a measurement as SUSPECT when delta >=
# RESISTANCE_WARN_ABS *and* (no baseline yet or delta >= RESISTANCE_WARN_RATIO
# x baseline). The AND kills both false modes: abs alone would flag
# high-baseline lanes (noise), ratio alone would flag absolutely-healthy
# jumps on low-baseline lanes. Always log-only WARN + strike bookkeeping;
# with [ace] resistance_pause >= RESISTANCE_PAUSE_STRIKES suspects on the
# SAME (ace,slot) lane within one print (not necessarily consecutive, see
# STRIKE DECAY below) escalate ONCE per lane per print to a resumable pause.
# NOISY MEASUREMENT POINTS: phase3's retry-0 probe reads a FRESH melt zone
# (post load+seat press, nothing ever extruded) = the tip's melt/press
# state, not lane resistance, and spreads far wider than the same lane at
# retry >= 1. It is not pure noise though (a real episode can show only in
# retry-0 data), so such a point is passed with noisy=True: it keeps its OWN
# baseline (a noisy source must never blur the clean reference) and must
# clear the much higher RESISTANCE_WARN_ABS_NOISY. Pickchecks have no retry
# concept and are always judged as clean. Long version at the phase3 call
# site in filament_feed_ace.py.
# STRIKE DECAY: strikes are cleared by SUCCESSFUL READS, not time, so a
# lane that loads rarely keeps stacking while a stale artefact cannot
# combine with a fresh one hours later. RESISTANCE_STRIKE_CLEAR_READS
# consecutive healthy _resistance_note calls on a lane clear that lane's
# strikes; same per head (any lane of the head counts, which keeps the
# head-wide aggregation intact). A suspect resets the counters.
RESISTANCE_WARN_ABS = 15000.
RESISTANCE_WARN_ABS_NOISY = 25000.
RESISTANCE_WARN_RATIO = 2.0
RESISTANCE_BASELINE_ALPHA = 0.3
RESISTANCE_PAUSE_STRIKES = 2
RESISTANCE_STRIKE_CLEAR_READS = 5


class ResistanceMixin:
    """Methods mixed into MultiAce; `self` is the MultiAce instance."""

    def _airlog_tick(self, eventtime):
        # [diag, LOG-ONLY] see the AIRLOG_* const note. Window-accumulates
        # coil min/max + |extruder vel| stats for the ACTIVE extruder while
        # printing; one wiggle-log line per emit window. A head switch resets
        # the window (per-head baselines must not mix). Never raises, never
        # gates - pure data collection for the future air-print detector.
        if getattr(self, '_fa_context', 'idle') != 'print':
            self._airlog_state = None
            return eventtime + AIRLOG_EMIT_S
        try:
            ext = self.toolhead.get_extruder()
            name = ext.get_name()
            head = 0 if name == 'extruder' else int(
                name.replace('extruder', '') or 0)
            # Velocity FIRST - it gates the coil read below.
            vel = 0.0
            try:
                mr = self.printer.lookup_object('motion_report', None)
                if mr is not None:
                    vel = abs(float(mr.get_status(eventtime).get(
                        'live_extruder_velocity', 0.0) or 0.0))
            except Exception:
                pass
            # Coil read ONLY while the extruder is actually moving. The eddy
            # coil is ALSO the bed probe: Z-homing / BED_MESH_CALIBRATE query
            # the same MCU oid via the same get_response path, and serialhdl
            # assumes ONE outstanding query per oid - the airlog firing a
            # get_coil_freq() while a probe query is in flight double-frees
            # the response handler -> KeyError('inductance_coil_info') ->
            # SHUTDOWN. Every probe/
            # homing path runs with a STATIONARY extruder, so gating on
            # extruder motion structurally excludes them; it also matches the
            # airlog's purpose (flow only exists during extrusion). Idle
            # windows just carry no coil data.
            freq = None
            if vel >= 0.3:
                try:
                    freq = float(ext.binding_probe.sensor.get_coil_freq())
                except Exception:
                    freq = None
            st = self._airlog_state
            if st is None or st['head'] != head:
                st = self._airlog_state = {
                    'head': head, 'start': eventtime, 'n': 0,
                    'cmin': None, 'cmax': None,
                    'vsum': 0.0, 'vmax': 0.0, 'nmove': 0}
            st['n'] += 1
            if freq is not None:
                st['cmin'] = (freq if st['cmin'] is None
                              else min(st['cmin'], freq))
                st['cmax'] = (freq if st['cmax'] is None
                              else max(st['cmax'], freq))
            st['vsum'] += vel
            st['vmax'] = max(st['vmax'], vel)
            if vel >= 0.3:
                st['nmove'] += 1
            if eventtime - st['start'] >= AIRLOG_EMIT_S and st['n'] > 0:
                delta = (st['cmax'] - st['cmin']
                         if st['cmin'] is not None else None)
                self._wiggle_log.info(
                    'airlog head=%d win=%.0fs n=%d moving=%d vel_mean=%.1f '
                    'vel_max=%.1f coil_min=%s coil_max=%s coil_delta=%s',
                    st['head'], eventtime - st['start'], st['n'],
                    st['nmove'], st['vsum'] / st['n'], st['vmax'],
                    ('%.0f' % st['cmin']) if st['cmin'] is not None else '-',
                    ('%.0f' % st['cmax']) if st['cmax'] is not None else '-',
                    ('%.0f' % delta) if delta is not None else '-')
                self._airlog_chew_eval(head, delta, st['nmove'])
                self._airlog_state = None
        except Exception:
            logging.exception('[multiACE] airlog tick failed')
            self._airlog_state = None
        return eventtime + AIRLOG_SAMPLE_S

    def _airlog_chew_eval(self, head, delta, nmove):
        """CHEW detector on a closed airlog window (AIRLOG_CHEW_* const
        note). >= AIRLOG_CHEW_WINDOWS consecutive windows with a delta >=
        AIRLOG_CHEW_DELTA while the head genuinely extrudes = the lever
        hammering against a bind (white stripe #2 read 7x 40-48k while the
        stripe printed). WARN always; resumable PAUSE only with [ace]
        resistance_pause on. Never raises."""
        try:
            if (self._swap_in_progress or self._pickcheck_active
                    or getattr(self, '_swap_phase', 'idle')
                    not in ('idle', 'done')):
                # A swap flush / pick-check push reads as turbulence (3x
                # 29-47k windows DURING the 14:51 NO_FLOW check) - those
                # phases have their own verdicts. Reset, don't count.
                self._airlog_chew_run.pop(head, None)
                return
            if (delta is None or nmove < AIRLOG_CHEW_MIN_MOVING
                    or delta < AIRLOG_CHEW_DELTA):
                self._airlog_chew_run.pop(head, None)
                return
            run = self._airlog_chew_run.get(head, 0) + 1
            self._airlog_chew_run[head] = run
            logging.warning(
                '[multiACE] [airlog] CHEW window head=%d delta=%.0f '
                'run=%d/%d' % (head, delta, run, AIRLOG_CHEW_WINDOWS))
            if run < AIRLOG_CHEW_WINDOWS or head in self._airlog_chew_latched:
                return
            self._airlog_chew_latched.add(head)
            self._ace_event('airlog_chew', head=head, delta=int(delta),
                            run=run)
            if not self.resistance_pause:
                # NO web/console notice at all - not even amber:
                # notifications are for real errors only. With Air-Print
                # Detection off
                # this is data collection: klippy.log keeps the trail (the
                # per-window CHEW lines above + this latched summary), the
                # airlog_chew engine event stays for API hosts, and the
                # web notification strip stays empty.
                logging.warning(
                    '[multiACE] [airlog] chew episode latched head=%d '
                    'delta=%.0f (Air-Print Detection off - log only)'
                    % (head, delta))
                return
            detail = self._t('msg.airlog_chew_pause',
                             head=self._disp(head), delta=int(delta))
            # Whose pause: the resume clears only this head's strikes/latches
            # (per-head reset in _on_print_start).
            self._resistance_pause_source_head = head
            # Same resumable-pause pattern as the FA-exhaust / comms-loss
            # give-up (RESPOND + structured exception + PAUSE, marshalled
            # to a reactor timer - we are inside the airlog timer here).
            def _do_pause(eventtime):
                try:
                    self.gcode.run_script(
                        'RESPOND TYPE=error MSG="%s"'
                        % detail.replace('"', "'"))
                except Exception:
                    pass
                try:
                    em = self.printer.lookup_object(
                        'exception_manager', None)
                    if em is not None:
                        em.raise_exception_async(
                            id=em.list.MODULE_ID_FEEDING, index=head,
                            code=210, message=detail, oneshot=1, level=2)
                except Exception:
                    pass
                try:
                    self.gcode.run_script('PAUSE')
                except Exception as pe:
                    logging.info(
                        '[multiACE] airlog-chew PAUSE failed: %s' % str(pe))
                return self.reactor.NEVER
            self.reactor.register_timer(_do_pause, self.reactor.NOW)
        except Exception:
            logging.exception('[multiACE] airlog chew eval failed')

    def _resistance_note(self, site, head, ace_idx, slot, delta, push=0,
                         noisy=False):
        """Upper-bound watch on a PASSING load-time coil delta (see the
        RESISTANCE_* const block). Call ONLY with measurements that passed
        their own flow gate (pickcheck FLOW / phase3 SUCCESS) - low/failed
        deltas have their own paths and must not poison the baseline.

        `noisy=True` marks a measurement point known to be unstable (the
        phase3 retry-0 probe: fresh melt zone, nothing ever extruded). Such
        a point keeps its OWN baseline (never mixed into the clean one) and
        must clear the much higher RESISTANCE_WARN_ABS_NOISY before it may
        flag anything - see the const block for the field data behind it.

        Returns (verdict, baseline, ratio): verdict is 'ok', 'suspect', or
        'pause_due' (= the caller should escalate to a resumable pause at
        its next safe point; only ever returned once per lane per print,
        only with [ace] resistance_pause on, only while a print is active).
        Never raises - a watch must not break a working load path."""
        try:
            delta = float(delta)
            if delta <= 0:
                return ('ok', None, None)
            key = (str(site), int(head),
                   -1 if ace_idx is None else int(ace_idx),
                   -1 if slot is None else int(slot),
                   int(round(float(push or 0))),
                   bool(noisy))
            lane = (key[2], key[3])
            baseline = self._coil_baseline.get(key)
            ratio = (delta / baseline) if baseline else None
            abs_thr = (RESISTANCE_WARN_ABS_NOISY if noisy
                       else RESISTANCE_WARN_ABS)
            suspect = (delta >= abs_thr
                       and (baseline is None
                            or delta >= RESISTANCE_WARN_RATIO * baseline))
            if not suspect:
                # Learn only from unsuspicious reads - a chew ramp must not
                # drag its own baseline up until it reads "normal".
                if baseline is None:
                    self._coil_baseline[key] = delta
                else:
                    self._coil_baseline[key] = (
                        (1. - RESISTANCE_BASELINE_ALPHA) * baseline
                        + RESISTANCE_BASELINE_ALPHA * delta)
                # Strikes are NOT cleared by a single healthy read (the blue
                # ramp alternated suspect(push20)/normal(push100) - episodic
                # chew; a reset-on-healthy rule would have delayed the pause
                # from 12:32 to 13:03, 100s before the air). But they DECAY
                # after RESISTANCE_STRIKE_CLEAR_READS consecutive healthy
                # reads (see the const block: every confirmed episode had
                # <=2 healthy reads between its strikes, the 07-30 false
                # alarm had 13 across 80 min). Counted in READS, not time -
                # a sparse lane with 0 healthy reads between suspects keeps
                # stacking regardless of the clock.
                ok_l = self._resistance_lane_ok.get(lane, 0) + 1
                self._resistance_lane_ok[lane] = ok_l
                ok_h = self._resistance_head_ok.get(head, 0) + 1
                self._resistance_head_ok[head] = ok_h
                if (ok_l >= RESISTANCE_STRIKE_CLEAR_READS
                        and self._resistance_strikes.get(lane)):
                    logging.info(
                        '[multiACE] [resistance] lane MMU %d/Slot %d: %d '
                        'strike(s) cleared after %d consecutive healthy reads'
                        % (self._raw(lane[0]), self._raw(lane[1]),
                           self._resistance_strikes.get(lane, 0), ok_l))
                    self._resistance_strikes.pop(lane, None)
                if (ok_h >= RESISTANCE_STRIKE_CLEAR_READS
                        and self._resistance_head_strikes.get(head)):
                    logging.info(
                        '[multiACE] [resistance] head %d: %d head strike(s) '
                        'cleared after %d consecutive healthy reads'
                        % (self._raw(head),
                           self._resistance_head_strikes.get(head, 0), ok_h))
                    self._resistance_head_strikes.pop(head, None)
                if (self._resistance_strikes.get(lane)
                        or self._resistance_head_strikes.get(head)):
                    logging.info(
                        '[multiACE] [resistance] lane MMU %d/Slot %d read '
                        'normal (delta=%.0f baseline=%.0f) - lane strikes=%d '
                        'head strikes=%d kept (%d/%d healthy reads toward '
                        'clear)'
                        % (self._raw(lane[0]), self._raw(lane[1]), delta,
                           baseline if baseline else 0.,
                           self._resistance_strikes.get(lane, 0),
                           self._resistance_head_strikes.get(head, 0),
                           min(ok_l, ok_h), RESISTANCE_STRIKE_CLEAR_READS))
                return ('ok', baseline, ratio)
            # A suspect resets the healthy-read decay counters: the suspect
            # lane's own AND the head's (a head-wide episode must not inherit
            # decay progress another lane accumulated before it started).
            self._resistance_lane_ok[lane] = 0
            self._resistance_head_ok[head] = 0
            self._resistance_lane_head[lane] = head
            strikes = self._resistance_strikes.get(lane, 0) + 1
            self._resistance_strikes[lane] = strikes
            # HEAD-level aggregation: a
            # head-wide episode distributed its suspects 1+1 across the
            # head's TWO lanes (pickcheck on slot A, phase3 on slot B
            # within 3 min) and the per-lane counter never paused. Both
            # slots share the head's path, so suspects on ANY lane of one
            # head also count toward that head.
            h_strikes = self._resistance_head_strikes.get(head, 0) + 1
            self._resistance_head_strikes[head] = h_strikes
            self.log_always(self._t('msg.resistance_warn',
                ace=self._disp(lane[0]), slot=self._disp(lane[1]),
                head=self._disp(head), delta=int(delta),
                baseline=(int(baseline) if baseline else 0),
                strikes=strikes))
            logging.warning(
                '[multiACE] [resistance] SUSPECT site=%s%s head=%d lane=MMU '
                '%d/Slot %d delta=%.0f baseline=%s ratio=%s thr=%d strike=%d '
                'head_strikes=%d'
                % (site, ' (noisy pt)' if noisy else '', head,
                   self._raw(lane[0]), self._raw(lane[1]),
                   delta, ('%.0f' % baseline) if baseline else '-',
                   ('%.2f' % ratio) if ratio else '-', int(abs_thr), strikes,
                   h_strikes))
            self._ace_event('resistance_suspect', head=head, ace=lane[0],
                            slot=lane[1], site=site, delta=int(delta),
                            baseline=(int(baseline) if baseline else 0),
                            strikes=strikes, head_strikes=h_strikes)
            verdict = 'suspect'
            lane_due = (strikes >= RESISTANCE_PAUSE_STRIKES
                        and lane not in self._resistance_paused_lanes)
            head_due = (h_strikes >= RESISTANCE_PAUSE_STRIKES
                        and head not in self._resistance_paused_heads)
            if self.resistance_pause and (lane_due or head_due):
                printing = False
                try:
                    ps = self.printer.lookup_object('print_stats', None)
                    printing = (ps is not None and ps.get_status(
                        self.reactor.monotonic()).get('state') == 'printing')
                except Exception:
                    printing = False
                if printing:
                    # Latch BOTH scopes so one episode never double-pauses
                    # (lane trigger then head trigger on the next suspect).
                    self._resistance_paused_lanes.add(lane)
                    self._resistance_paused_heads.add(head)
                    # Remember whose pause this is: the resume clears ONLY
                    # this head's strikes (per-head reset, _on_print_start).
                    self._resistance_pause_source_head = head
                    verdict = 'pause_due'
            return (verdict, baseline, ratio)
        except Exception as e:
            logging.info('[multiACE] [resistance] note failed (ignored): %s'
                         % e)
            return ('ok', None, None)
