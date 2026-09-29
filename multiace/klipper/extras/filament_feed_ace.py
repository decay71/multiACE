import logging, copy, os
from . import pulse_counter

FEED_CHANNEL_NUMS                                   = 2
FEED_CHANNEL_1                                      = 0
FEED_CHANNEL_2                                      = 1

FEED_OK                                             = 'ok'
FEED_ERR                                            = 'general'
FEED_ERR_PARAMETER                                  = 'parameter'
FEED_ERR_TIMEOUT                                    = 'timeout'
FEED_ERR_NO_FILAMENT                                = 'no_filament'
FEED_ERR_RESIDUAL_FILAMENT                          = 'residual_filament'
FEED_ERR_MOTOR_SPEED                                = 'motor_speed'
FEED_ERR_WHEEL_SPEED                                = 'wheel_speed'
FEED_ERR_MOVE                                       = 'move'
FEED_ERR_MOVE_HOME                                  = 'move_home'
FEED_ERR_MOVE_SWITCH                                = 'move_switch'
FEED_ERR_MOVE_EXTRUDE                               = 'move_extrude'
FEED_ERR_CUSTOM_GCODE                               = 'custom_gcode'
FEED_ERR_DISTANCE                                   = 'distance'
FEED_ERR_STATE_MISMATCH                             = 'state_mismatch'
FEED_ERR_HEAT                                       = 'heat'

FEED_ACT_PRELOAD                                    = 'preload'
FEED_ACT_LOAD                                       = 'load'
FEED_ACT_UNLOAD                                     = 'unload'
FEED_ACT_MANUAL_FEED                                = 'manual_feed'
FEED_ACT_UPDATE_AUTO_MODE                           = 'update_auto_mode'
FEED_ACT_REMOVE_FILAMENT                            = 'remove_filament'
FEED_ACT_FILAMENT_RUNOUT                            = 'filament_runout'

FEED_STA_NONE                                       = 'none'
FEED_STA_INITED                                     = 'inited'
FEED_STA_WAIT_INSERT                                = 'wait_insert'
FEED_STA_PRELOAD_PREPARE                            = 'preload_prepare'
FEED_STA_PRELOAD_FEEDING                            = 'preload_feeding'
FEED_STA_PRELOAD_FINISH                             = 'preload_finish'
FEED_STA_PRELOAD_FAIL                               = 'preload_fail'
FEED_STA_LOAD_PREPARE                               = 'load_prepare'
FEED_STA_LOAD_HOMING                                = 'load_homing'
FEED_STA_LOAD_PICKING                               = 'load_picking'
FEED_STA_LOAD_HEATING                               = 'load_heating'
FEED_STA_LOAD_FEEDING                               = 'load_feeding'
FEED_STA_LOAD_EXTRUDING                             = 'load_extruding'
FEED_STA_LOAD_FLUSHING                              = 'load_flushing'
FEED_STA_LOAD_FINISH                                = 'load_finish'
FEED_STA_LOAD_FAIL                                  = 'load_fail'
FEED_STA_UNLOAD_PREPARE                             = 'unload_prepare'
FEED_STA_UNLOAD_HOMING                              = 'unload_homing'
FEED_STA_UNLOAD_PICKING                             = 'unload_picking'
FEED_STA_UNLOAD_HEATING                             = 'unload_heating'
FEED_STA_UNLOAD_HEAT_FINISH                         = 'unload_heat_finish'
FEED_STA_UNLOAD_DOING                               = 'unload_doing'
FEED_STA_UNLOAD_FINISH                              = 'unload_finish'
FEED_STA_UNLOAD_FAIL                                = 'unload_fail'
FEED_STA_MANUAL_PREPARE                             = 'manual_sta_prepare'
FEED_STA_MANUAL_HOMING                              = 'manual_sta_homing'
FEED_STA_MANUAL_PICKING                             = 'manual_sta_picking'
FEED_STA_MANUAL_PREPARE_FINISH                      = 'manual_sta_prepare_finish'
FEED_STA_MANUAL_PREPARE_FAIL                        = 'manual_sta_prepare_fail'
FEED_STA_MANUAL_HEATING                             = 'manual_sta_heating'
FEED_STA_MANUAL_EXTRUDING                           = 'manual_sta_extruding'
FEED_STA_MANUAL_EXTRUDE_FINISH                      = 'manual_sta_extrude_finish'
FEED_STA_MANUAL_EXTRUDE_FAIL                        = 'manual_sta_extrude_fail'
FEED_STA_MANUAL_FLUSHING                            = 'manual_sta_flushing'
FEED_STA_MANUAL_FLUSH_FINISH                        = 'manual_sta_flush_finish'
FEED_STA_MANUAL_FLUSH_FAIL                          = 'manual_sta_flush_fail'
FEED_STA_MANUAL_FINISH                              = 'manual_sta_finish'
FEED_STA_MANUAL_FAIL                                = 'manual_sta_fail'
FEED_STA_TEST                                       = 'test'

FEED_MANUAL_STAGE_PREPARE                           = 'prepare'
FEED_MANUAL_STAGE_EXTRUDE                           = 'extrude'
FEED_MANUAL_STAGE_FLUSH                             = 'flush'
FEED_MANUAL_STAGE_FINISH                            = 'finish'
FEED_MANUAL_STAGE_CANCEL                            = 'cancel'
FEED_UNLOAD_STAGE_PREPARE                           = 'prepare'
FEED_UNLOAD_STAGE_DOING                             = 'doing'
FEED_UNLOAD_STAGE_CANCEL                            = 'cancel'

FEED_LIGHT_PWM_CYCLE_TIME                           = 1
FEED_LIGHT_INDEXS                                   = ['RED', 'WHITE', 'ALL']

FEED_PORT_ADC_SAMPLE_TIME                           = 0.05
FEED_PORT_ADC_SAMPLE_COUNT                          = 4
FEED_PORT_ADC_REPORT_TIME                           = 0.300
FEED_PORT_ADC_VAL_THRESHOLD                         = 0.18
FEED_PORT_ADC_VAL_MODULE_EXIST                      = 0.9
FEED_PORT_ADC_DEBOUNCE_COUNT                        = 2

FEED_MOTOR_DIR_IDLE                                 = 0
FEED_MOTOR_DIR_A                                    = 1
FEED_MOTOR_DIR_B                                    = 2

FEED_MOTOR_HARD_PROTECT_TIME                        = 2.5
FEED_MOTOR_SLIP_RATE                                = 0.7
FEED_MOTOR_REDUCTION_R                              = 33.0
FEED_WHEEL_CIRCUMFERENCE                            = 31.4159

FEED_PRELOAD_LENGTH                                 = 950.0
FEED_PRELOAD_TIMEOUT_TIME                           = 45
FEED_PRELOAD_MOTOR_MIN_SPEED                        = 200
FEED_PRELOAD_WHEEL_ERR_CNT_MAX                      = 3
FEED_PRELOAD_MOTOR_ERR_CNT_MAX                      = 2
FEED_LOAD_POSITION_X                                = 150
FEED_LOAD_POSITION_Y                                = 5
FEED_LOAD_LENGTH_MAX                                = 1100.0
FEED_LOAD_TIMEOUT_TIME                              = 60
FEED_LOAD_MOTOR_ERR_CNT_MAX                         = 20
FEED_LOAD_WHEEL_ERR_CNT_MAX                         = 20
FEED_LOAD_EXTRUDE_TIMES_MAX                         = 20

FEED_MOTOR_SPEED_SLOW_SWITCHING                     = 0.45
FEED_MOTOR_SPEED_PRELOAD                            = 0.7
FEED_MOTOR_SPEED_LOAD                               = 0.7
FEED_MOTOR_SPEED_EXTRUDE                            = 0.50
FEED_MOTOR_SPEED_HANG_NEUTRAL_A                     = 1
FEED_MOTOR_SPEED_HANG_NEUTRAL_B                     = 0.9
FEED_MOTOR_HANG_NEUTRAL_TIME                        = 0.040

FEED_COIL_FREQ_THERSHOLD_SOFT                       = 800

FEED_COIL_FREQ_THERSHOLD_HARD                       = 5000

FEED_MIN_TIME                                       = 0.100

FEED_CONFIG_FILE_POSTFIX                            = '_filament_feed.json'
FEED_DEFAULT_CONFIG = {
    'auto_mode': [True] * FEED_CHANNEL_NUMS,
    'load_finish': [False] * FEED_CHANNEL_NUMS
}

FEED_FILAMENT_TEMP_DEFAULT                          = 250

# Swap forward-probe: how many deg C BELOW the live print temp the cool
# forward-probe runs (floored at swap_probe_temp). Derived from the PLA
# baseline (print ~220, probe 175 -> 45). Keeping it relative to the live
# print target makes the probe extrudable for hotter materials (PETG/ABS)
# instead of the PLA-only absolute 175.
SWAP_PROBE_COOL_DELTA                               = 45

# Heat-soak reset temp (deg C) for the swap unload RETRY: cool the head to this
# before the hot re-unload so it re-heats from a parked/cold-like gradient
# (firm filament column) and INNER can grip+extract the plug instead of
# stripping soft heat-soaked filament. Swap-context only. 0 = disabled.
FEED_SWAP_PRECOOL_TEMP                              = 0

FEED_UNLOAD_TRIGGER_SETTLE                          = 0.5

# Unload: retract only this short length before the forward probe verifies the
# toolhead sensor cleared; the bulk "rest" of the configured retract length is
# pulled back to the ACE only AFTER a verified clear. Full-unload-safe + faster
# (a stuck filament is caught after this much travel instead of the full
# retract) and hardware-agnostic (V1+V2 - the verify is the toolhead motion
# sensor, no ACE device sensor needed).
FEED_UNLOAD_PROBE_RETRACT                           = 150
# Ready-exit guard of the load poll loop (see the feed call in _do_feed):
# the 'busy' send-stamp can be overwritten by an in-flight status report
# before the device shows the motion, so is_ace_ready() may not end the
# loop within this window. The SENSOR exit is not gated by it.
FEED_READY_GUARD_S = 4.0
# [diag] passive: log the V2 ACE decoder (real filament movement) before/after
# each unload retract to multiace_feedlog.log, to see whether the decoder
# tracks the full retract length (candidate for a V2 decoder-based unload
# verify vs the toolhead forward-probe). NO control depends on it - the probe
# stays the verify. Flip to False to silence. V2-only (V1 has no feed_info).
UNLOAD_DECODER_DIAG                                 = True
# Decoder GATE on the bulk rest retract ([ace] unload_decoder_gate, V2 only).
# A rollback whose lane encoder never moves is a buckled/jammed strand that the
# ACE firmware still reports as SUCCESS (it bypasses its own slip comparator
# for mode 1 - Simon-CR's cmd 76 disassembly, pinned at
# ace._retract_with_decoder_span). Deliberately a NEAR-TOTAL stall, not a
# proportional check: healthy spans read 87-101 % of the commanded length, so
# nothing between 2 mm and ~870 mm of a 1000 mm pull can trip this, and the one
# known inline span anomaly ('kind=short len=150 span=1083') errs LARGE -
# the harmless direction for a zero test. MIN_LEN keeps short/leftover rests
# out of it (a few mm of rest is not worth a verdict).
UNLOAD_DECODER_MIN_LEN                              = 30.0
UNLOAD_DECODER_MIN_MOVE                             = 2.0

class FeedLight:
    def __init__(self, printer, reactor, red_pin, white_pin):
        self.reactor = reactor
        ppins = printer.lookup_object('pins')
        self.red_light = ppins.setup_pin('pwm', red_pin)
        self.red_light.setup_max_duration(0.)
        self.red_light.setup_start_value(0, 0)
        self.red_light.setup_cycle_time(FEED_LIGHT_PWM_CYCLE_TIME, False)
        self.white_light = ppins.setup_pin('pwm', white_pin)
        self.white_light.setup_max_duration(0.)
        self.white_light.setup_start_value(0, 0)
        self.white_light.setup_cycle_time(FEED_LIGHT_PWM_CYCLE_TIME, False)

    def get_mcu(self):
        return self.red_light.get_mcu()

    def set_light_state(self, print_time, state, index=None, value=None):
        if state in [FEED_STA_PRELOAD_PREPARE, FEED_STA_LOAD_PREPARE, FEED_STA_UNLOAD_PREPARE,
                     FEED_STA_MANUAL_PREPARE]:
            self.red_light.set_pwm(print_time, 0, FEED_MIN_TIME)
            self.white_light.set_pwm(print_time, 0.2, FEED_MIN_TIME)
        elif state in [FEED_STA_PRELOAD_FEEDING, FEED_STA_LOAD_HOMING, FEED_STA_LOAD_PICKING,
                       FEED_STA_LOAD_HEATING, FEED_STA_LOAD_FEEDING, FEED_STA_LOAD_EXTRUDING,
                       FEED_STA_LOAD_FLUSHING, FEED_STA_UNLOAD_HOMING, FEED_STA_UNLOAD_PICKING,
                       FEED_STA_UNLOAD_HEAT_FINISH,
                       FEED_STA_UNLOAD_HEATING, FEED_STA_UNLOAD_DOING, FEED_STA_MANUAL_HOMING,
                       FEED_STA_MANUAL_PICKING, FEED_STA_MANUAL_PREPARE_FINISH, FEED_STA_MANUAL_HEATING,
                       FEED_STA_MANUAL_EXTRUDING, FEED_STA_MANUAL_EXTRUDE_FINISH, FEED_STA_MANUAL_FLUSHING,
                       FEED_STA_MANUAL_FLUSH_FINISH]:
            self.red_light.set_pwm(print_time, 0, FEED_MIN_TIME)
            self.white_light.set_pwm(print_time, 0.5, FEED_MIN_TIME)
        elif state in [FEED_STA_PRELOAD_FINISH, FEED_STA_LOAD_FINISH, FEED_STA_UNLOAD_FINISH,
                       FEED_STA_MANUAL_FINISH]:
            self.red_light.set_pwm(print_time, 0, FEED_MIN_TIME)
            self.white_light.set_pwm(print_time, 1, FEED_MIN_TIME)
        elif state in [FEED_STA_PRELOAD_FAIL, FEED_STA_LOAD_FAIL, FEED_STA_UNLOAD_FAIL,
                       FEED_STA_MANUAL_PREPARE_FAIL, FEED_STA_MANUAL_EXTRUDE_FAIL,
                       FEED_STA_MANUAL_FLUSH_FAIL, FEED_STA_MANUAL_FAIL]:
            self.red_light.set_pwm(print_time, 1, FEED_MIN_TIME)
            self.white_light.set_pwm(print_time, 0, FEED_MIN_TIME)
        elif state == FEED_STA_TEST:
            if index == 'RED' and value is not None:
                self.red_light.set_pwm(print_time, value, FEED_MIN_TIME)
            elif index == 'WHITE' and value is not None:
                self.white_light.set_pwm(print_time, value, FEED_MIN_TIME)
            elif index == 'ALL' and value is not None:
                self.red_light.set_pwm(print_time, value, FEED_MIN_TIME)
                self.white_light.set_pwm(print_time, value, FEED_MIN_TIME)
            else:
                pass
        else:
            self.red_light.set_pwm(print_time, 0, FEED_MIN_TIME)
            self.white_light.set_pwm(print_time, 0, FEED_MIN_TIME)

class FeedPort:
    def __init__(self, printer, reactor, pin, threshold, index):
        self.reactor = reactor
        self.index = index
        self.ace = None
        ppins = printer.lookup_object('pins')
        self._port = ppins.setup_pin('adc', pin)
        self._port_adc_value = 0
        self._threshold = threshold
        self._filament_detected = True
        self._last_filament_detected = True
        self._port_event_callback = None
        self._pending_state = True
        self._stable_count = 0

        self._port.setup_adc_sample(FEED_PORT_ADC_SAMPLE_TIME, FEED_PORT_ADC_SAMPLE_COUNT)
        self._port.setup_adc_callback(FEED_PORT_ADC_REPORT_TIME, self._adc_callback)

    def get_mcu(self):
        return self._port.get_mcu()

    def register_cb_2_port_event(self, cb):
        try:
            if callable(cb):
                self._port_event_callback = cb
            else:
                raise TypeError()
        except:
            logging.error("[feed][port]: param[cb] is not a callable function!")

    def _adc_callback(self, read_time, read_value):
        self._port_adc_value = read_value
        current_detected = self._port_adc_value < self._threshold

        if current_detected == self._pending_state:
            if self._stable_count < FEED_PORT_ADC_DEBOUNCE_COUNT:
                self._stable_count += 1
        else:
            self._pending_state = current_detected
            self._stable_count = 1

        if (self._stable_count >= FEED_PORT_ADC_DEBOUNCE_COUNT
                and self._pending_state != self._filament_detected):
            self._filament_detected = self._pending_state
            self._stable_count = 0
            if (self._port_event_callback is not None
                    and self._last_filament_detected != self._filament_detected):
                self._last_filament_detected = self._filament_detected
                self._port_event_callback(self._filament_detected)

    def get_adc_value(self):
        return self._port_adc_value

    def add_ace(self, ace):
        self.ace = ace

    def get_filament_detected(self):
        if self.ace is not None:
            # A head-mode feeder head is NOT ACE-driven: its presence is the
            # physical side-feeder port (ADC), not the ACE slot gate. Without
            # this the display's empty/ready follows the ACE slot occupancy
            # (empty slot -> "?" even when the feeder is physically empty)
            # instead of the feeder itself (empty -> "/", loaded -> "?").
            # head_uses_ace is True for every ACE-driven head, so those keep
            # the gate (byte-identical). A feeder head (head mode) AND a
            # manual head (multi) read the port: neither is fed by an ACE,
            # and a manual head reading the ACE gate came up as empty after
            # every restart whenever its slot index held no spool.
            # self.index is the head index (filament_ch[ch]).
            try:
                if not self.ace.head_uses_ace(self.index):
                    return self._filament_detected
            except Exception:
                pass
            # ACE-driven head: presence is the gate of the slot that ACTUALLY
            # feeds this head. With a 4->1 combiner the feeding slot != the head
            # index (head_source), so resolve it via _ace_slot_for_head - never
            # index gate_status by the head index. Fallback =
            # head index, so slot==head (multi/normal) stays byte-identical; only
            # a combiner head (head_source.slot != head) is corrected. Without
            # this, a partially-loaded ACE in head mode reads the wrong slot's
            # gate (head 3 <- slot 0, but it checked gate_status[3]) -> a load is
            # wrongly rejected as no_filament.
            slot = self.ace._ace_slot_for_head(self.index)
            # gate_status is a "currently-selected ACE" context (ace.py:2565),
            # NOT ACE-aware: it reflects whichever unit is active (the printing/
            # ran-out head's ACE, set by _on_extruder_change). A head on a
            # DIFFERENT ACE (head mode always; multi when heads span ACEs) then
            # reads the WRONG unit's gate - the stock auto-replenish candidate
            # scan reads every head at once, so a same-colour twin on another
            # ACE sees the ran-out ACE's emptied slot and is wrongly rejected
            # ('cannot auto replenish'). Read the
            # head's OWN ACE (head_source ace_index) per-ace gate list. Fall back
            # to the flat gate_status for an unloaded head (no head_source) or
            # missing per-ace data - byte-identical when the head IS on the
            # active ACE (the common multi case).
            src = self.ace._head_source.get(self.index)
            ace_idx = None
            if src is not None and isinstance(src.get('ace_index'), int):
                ace_idx = src['ace_index']
            else:
                # UNLOADED head (no head_source): the per-ACE read above
                # covers only loaded heads, so this case would fall through
                # to the flat
                # gate_status below = the ACTIVE unit's context. In head
                # mode the unloaded head's wired ACE is almost never the
                # active one, and every OTHER unit's same-numbered slot is
                # typically LOADED (not AVAILABLE) -> presence read False
                # -> display "/" + display load refused although the spool
                # sits at the wired ACE's gate. Resolve the wired ACE
                # instead - head_ace_for as an unloaded-display fallback
                # inside a head-mode gate.
                # Multi/normal fall through flat = byte-identical.
                try:
                    if (getattr(self.ace, '_ace_mode', 'multi') == 'head'
                            and self.ace.head_uses_ace(self.index)):
                        ace_idx = self.ace.head_ace_for(self.index)
                except Exception:
                    ace_idx = None
            if ace_idx is not None:
                gates = self.ace._gate_status_per_ace.get(ace_idx)
                if gates is not None and slot < len(gates):
                    return gates[slot] == 1
            return self.ace.gate_status[slot] == 1
        else:
            return self._filament_detected

    def get_filament_detected_local(self):
        return self._filament_detected

class FeedTachometer:
    def __init__(self, printer, pin, ppr, sample_time, poll_time):
        self.frequence = pulse_counter.FrequencyCounter(printer, pin, sample_time, poll_time)
        self.ppr = ppr

    def get_rpm(self):
        rpm = self.frequence.get_frequency()  * 30. / self.ppr
        return rpm

    def get_counts(self):
        return self.frequence.get_count()

    def get_last_report_time(self):
        return self.frequence.get_last_report_time()

class FeedMotorPwmCfg:
    def __init__(self):
        self.a_pin = None
        self.b_pin = None
        self.cycle_time = 0.010
        self.max_value = 1.0

class FeedMotor:
    def __init__(self, printer, reactor, cfg:FeedMotorPwmCfg):
        self.reactor = reactor
        ppins = printer.lookup_object('pins')
        self.max_value = cfg.max_value
        self._motor_a = ppins.setup_pin('pwm', cfg.a_pin)
        self._motor_a.setup_max_duration(0)
        self._motor_a.setup_cycle_time(cfg.cycle_time, False)
        self._motor_a.setup_start_value(0, 0)
        self._motor_b = ppins.setup_pin('pwm', cfg.b_pin)
        self._motor_b.setup_max_duration(0)
        self._motor_b.setup_cycle_time(cfg.cycle_time, False)
        self._motor_b.setup_start_value(0, 0)
        self._mutex_lock = False
        self._dir = FEED_MOTOR_DIR_IDLE

    def get_mcu(self):
        return self._motor_a.get_mcu()

    def _run(self, dir, value):
        systime = self.reactor.monotonic()
        systime += FEED_MIN_TIME
        print_time = self._motor_a.get_mcu().estimated_print_time(systime)
        if FEED_MOTOR_DIR_A == dir:
            self._motor_b.set_pwm(print_time, 0)
            self._motor_a.set_pwm(print_time, value)
        elif FEED_MOTOR_DIR_B == dir:
            self._motor_a.set_pwm(print_time, 0)
            self._motor_b.set_pwm(print_time, value)
        else:
            self._motor_b.set_pwm(print_time, 0)
            self._motor_a.set_pwm(print_time, 0)
        self._last_print_time = print_time = print_time

    def _run_one_cycle(self, dir, value, time):
        systime = self.reactor.monotonic()
        systime += FEED_MIN_TIME
        print_time = self._motor_a.get_mcu().estimated_print_time(systime)
        delta = time
        if FEED_MOTOR_DIR_A == dir:
            self._motor_b.set_pwm(print_time, 0)
            self._motor_a.set_pwm(print_time, value)
            self._motor_a.set_pwm(print_time + delta, 0)
        elif FEED_MOTOR_DIR_B == dir:
            self._motor_a.set_pwm(print_time, 0)
            self._motor_b.set_pwm(print_time, value)
            self._motor_b.set_pwm(print_time + delta, 0)
        self._last_print_time = print_time + delta

    def run(self, dir, value):
        while self._mutex_lock:
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        self._mutex_lock = True

        val = max(0, min(self.max_value, value))
        if val == 0:
            dir = FEED_MOTOR_DIR_IDLE

        while 1:
            if FEED_MOTOR_DIR_IDLE == self._dir:
                if FEED_MOTOR_DIR_IDLE == dir:
                    break
                self._dir = dir
                self._run(dir, val)
                self.reactor.pause(self.reactor.monotonic() + 1.05 * FEED_MIN_TIME)
            else:
                if dir == self._dir:
                    self._run(dir, val)
                    self.reactor.pause(self.reactor.monotonic() + 1.05 * FEED_MIN_TIME)
                else:
                    self._run(FEED_MOTOR_DIR_IDLE, 0)
                    self.reactor.pause(self.reactor.monotonic() + FEED_MOTOR_HARD_PROTECT_TIME)
                    self._dir = FEED_MOTOR_DIR_IDLE
                    if FEED_MOTOR_DIR_IDLE != dir:
                        self._dir = dir
                        self._run(dir, val)
                        self.reactor.pause(self.reactor.monotonic() + 1.05 * FEED_MIN_TIME)
            break
        self._mutex_lock = False

    def run_one_cycle(self, dir, value, time):
        while self._mutex_lock:
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        self._mutex_lock = True

        val = max(0, min(self.max_value, value))
        if val == 0:
            dir = FEED_MOTOR_DIR_IDLE

        while 1:
            if FEED_MOTOR_DIR_IDLE == self._dir:
                if FEED_MOTOR_DIR_IDLE == dir:
                    break
                self._dir = dir
                self._run_one_cycle(dir, val, time)
                self.reactor.pause(self.reactor.monotonic() + 1.05 * (FEED_MIN_TIME + time))
                self._dir = FEED_MOTOR_DIR_IDLE
            else:
                self._run(FEED_MOTOR_DIR_IDLE, 0)
                self.reactor.pause(self.reactor.monotonic() + FEED_MOTOR_HARD_PROTECT_TIME)
                self._dir = FEED_MOTOR_DIR_IDLE
                if FEED_MOTOR_DIR_IDLE != dir:
                    self._dir = dir
                    self._run_one_cycle(dir, val, time)
                    self.reactor.pause(self.reactor.monotonic() + 1.05 * (FEED_MIN_TIME + time))
                    self._dir = FEED_MOTOR_DIR_IDLE
            break
        self._mutex_lock = False

class FilamentFeed:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.module_name = config.get_name().split()[1]

        self.channel_active = None
        self.channel_state = [FEED_STA_NONE] * FEED_CHANNEL_NUMS
        self.channel_action_state = [FEED_STA_NONE] * FEED_CHANNEL_NUMS
        self.channel_error_state = [FEED_STA_NONE] * FEED_CHANNEL_NUMS
        self.channel_error = [FEED_OK] * FEED_CHANNEL_NUMS
        self.module_exist = [False] * FEED_CHANNEL_NUMS
        self.manual_feeding = [False] * FEED_CHANNEL_NUMS
        self.exception_code = [0] * FEED_CHANNEL_NUMS

        config_dir = self.printer.get_snapmaker_config_dir()
        config_name = self.module_name + FEED_CONFIG_FILE_POSTFIX
        self.config_path = os.path.join(config_dir, config_name)
        self.config = self.printer.load_snapmaker_config_file(self.config_path, FEED_DEFAULT_CONFIG)

        self.filament_ch = []
        self.filament_ch.append(config.getint('filament_ch_1'))
        self.filament_ch.append(config.getint('filament_ch_2'))

        self.runout_sensor = []
        tmp_obj = self.printer.lookup_object('filament_motion_sensor e%d_filament' % (self.filament_ch[FEED_CHANNEL_1]), None)
        self.runout_sensor.append(tmp_obj)
        tmp_obj = self.printer.lookup_object('filament_motion_sensor e%d_filament' % (self.filament_ch[FEED_CHANNEL_2]), None)
        self.runout_sensor.append(tmp_obj)
        self.filament_detect = self.printer.lookup_object('filament_detect', None)

        self.light = []
        white_pin = config.get('light_ch_1_white')
        red_pin = config.get('light_ch_1_red')
        tmp_obj = FeedLight(self.printer, self.reactor, white_pin, red_pin)
        self.light.append(tmp_obj)
        white_pin = config.get('light_ch_2_white')
        red_pin = config.get('light_ch_2_red')
        tmp_obj = FeedLight(self.printer, self.reactor, white_pin, red_pin)
        self.light.append(tmp_obj)
        self.gcode.register_mux_command("FEED_LIGHT", "MODULE",
                                self.module_name,
                                self.cmd_FEED_LIGHT)

        self._port = []
        tmp_pin = config.get('port_ch_1_pin')
        threshold = config.getfloat('port_ch_1_threshold')
        tmp_obj = FeedPort(self.printer, self.reactor, tmp_pin, threshold, self.filament_ch[0])
        tmp_obj.register_cb_2_port_event(self._port_ch1_event_handler)
        self._port.append(tmp_obj)
        tmp_pin = config.get('port_ch_2_pin')
        threshold = config.getfloat('port_ch_2_threshold')
        tmp_obj = FeedPort(self.printer, self.reactor, tmp_pin, threshold, self.filament_ch[1])
        tmp_obj.register_cb_2_port_event(self._port_ch2_event_handler)
        self._port.append(tmp_obj)
        self.gcode.register_mux_command("FEED_PORT", "MODULE",
                                self.module_name,
                                self.cmd_FEED_PORT)

        self.wheel = []
        self.wheel_2 = []
        tmp_pin = config.get('wheel_tach_ch_1_1_pin')
        wheel_tach_ppr = config.getint('wheel_tach_ppr', 6, minval=1)
        poll_time = config.getfloat('wheel_tach_poll_interval', 0.0005, above=0.)
        tmp_obj = FeedTachometer(
                                self.printer,
                                tmp_pin,
                                wheel_tach_ppr,
                                0.100,
                                poll_time)
        self.wheel.append(tmp_obj)

        tmp_pin = config.get('wheel_tach_ch_2_1_pin')
        tmp_obj = FeedTachometer(
                                self.printer,
                                tmp_pin,
                                wheel_tach_ppr,
                                0.100,
                                poll_time)
        self.wheel.append(tmp_obj)

        tmp_pin = config.get('wheel_tach_ch_1_2_pin')
        tmp_obj = FeedTachometer(
                                self.printer,
                                tmp_pin,
                                wheel_tach_ppr,
                                0.100,
                                poll_time)
        self.wheel_2.append(tmp_obj)

        tmp_pin = config.get('wheel_tach_ch_2_2_pin')
        tmp_obj = FeedTachometer(
                                self.printer,
                                tmp_pin,
                                wheel_tach_ppr,
                                0.100,
                                poll_time)
        self.wheel_2.append(tmp_obj)

        self.gcode.register_mux_command("FEED_WHEEL_TACH", "MODULE",
                                self.module_name,
                                self.cmd_FEED_WHEEL_TACH)

        motor_cfg = FeedMotorPwmCfg()
        motor_cfg.a_pin = config.get('motor_ch_1_pin')
        motor_cfg.b_pin = config.get('motor_ch_2_pin')
        motor_cfg.cycle_time = config.getfloat('motor_cycle_time')
        motor_cfg.max_value = config.getfloat('motor_max_value', maxval=1.0)
        self.motor = FeedMotor(self.printer, self.reactor, motor_cfg)
        self.gcode.register_mux_command("FEED_MOTOR", "MODULE",
                                self.module_name,
                                self.cmd_FEED_MOTOR)
        self.gcode.register_mux_command("FEED_MOTOR_ONE_CYCLE", "MODULE",
                                self.module_name,
                                self.cmd_FEED_MOTOR_ONE_CYCLE)

        tmp_pin = config.get('motor_tach_pin')
        motor_tach_ppr = config.getint('motor_tach_ppr', 2, minval=1)
        poll_time = config.getfloat('motor_tach_poll_interval', 0.0015, above=0.)
        self.motor_tachometer = FeedTachometer(
                                self.printer,
                                tmp_pin,
                                motor_tach_ppr,
                                0.100,
                                poll_time)
        self.gcode.register_mux_command("FEED_MOTOR_TACH", "MODULE",
                                self.module_name,
                                self.cmd_FEED_MOTOR_TACH)

        self._feed_load_position_x = config.getfloat('load_position_x', FEED_LOAD_POSITION_X, minval=2, maxval=265)
        self._feed_load_position_y = config.getfloat('load_position_y', FEED_LOAD_POSITION_Y, minval=2, maxval=250)
        self._feed_load_extrude_max_times = config.getint('load_extrude_max_times', FEED_LOAD_EXTRUDE_TIMES_MAX, minval=3, maxval=50)
        preload_length = config.getfloat('preload_length', FEED_PRELOAD_LENGTH, minval=600.0, maxval=1500.0)
        self.coil_freq_threshold_soft = config.getint('coil_freq_thershold_soft', FEED_COIL_FREQ_THERSHOLD_SOFT, minval=100)
        self.coil_freq_threshold_hard = config.getint('coil_freq_thershold_hard', FEED_COIL_FREQ_THERSHOLD_HARD, minval=100)

        self.check_wheel_data = config.getint('check_wheel_data', 0)
        self.check_coil_freq = config.getint('check_coil_freq', 1)
        if self.check_coil_freq == 0 and self.check_wheel_data == 0:
            raise Exception("check_wheel_data and check_coil_freq can not be both 0")

        self.gcode.register_mux_command("FEED_AUTO", "MODULE",
                        self.module_name,
                        self.cmd_FEED_AUTO)
        self.gcode.register_mux_command("FEED_MANUAL", "MODULE",
                        self.module_name,
                        self.cmd_FEED_MANUAL)
        self.gcode.register_mux_command("FEED_RUNOUT_EVENT_HANDLE", "MODULE",
                        self.module_name,
                        self.cmd_FEED_RUNOUT_EVENT_HANDLE)

        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("filament_switch_sensor:runout", self._runout_evt_handle)
        self._check_init_state_timer = self.reactor.register_timer(self._check_init_state_timer_handler)

        self._feed_preload_counts = int(preload_length / FEED_WHEEL_CIRCUMFERENCE * 2)
        self._feed_load_counts_max = int(FEED_LOAD_LENGTH_MAX / FEED_WHEEL_CIRCUMFERENCE * 2)

        self.motor_speed_slow_switching = FEED_MOTOR_SPEED_SLOW_SWITCHING
        self.motor_speed_preload = FEED_MOTOR_SPEED_PRELOAD
        self.motor_speed_load = FEED_MOTOR_SPEED_LOAD
        self.motor_speed_extrude = FEED_MOTOR_SPEED_EXTRUDE
        self.motor_speed_hang_neutral_a = FEED_MOTOR_SPEED_HANG_NEUTRAL_A
        self.motor_speed_hang_neutral_b = FEED_MOTOR_SPEED_HANG_NEUTRAL_B
        self.motor_hang_neutral_time = FEED_MOTOR_HANG_NEUTRAL_TIME

        self._last_print_time = 0
        for ch in range(FEED_CHANNEL_NUMS):
            self.channel_state[ch] = FEED_STA_INITED

    def _ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.gcode_move = self.printer.lookup_object('gcode_move')
        self.ace = self.printer.lookup_object('ace')
        for i in self._port:
            i.add_ace(self.ace)
        self.exception_manager = self.printer.lookup_object('exception_manager', None)
        self.reactor.update_timer(self._check_init_state_timer,
                                  self.reactor.monotonic() + 2 * FEED_PORT_ADC_REPORT_TIME)

    def _runout_evt_handle(self, extruder, present):
        if present == True:
            return

        if self.ace is not None and getattr(self.ace, '_swap_in_progress', False):
            return

        for ch in range(FEED_CHANNEL_NUMS):
            if extruder == self.filament_ch[ch]:
                # Stock 1.4: don't trigger a runout-reload while this channel
                # is actively loading (feeding/extruding/flushing) - the
                # transient sensor drop during load is not a real runout.
                if self.channel_state[ch] in (FEED_STA_LOAD_FEEDING,
                                              FEED_STA_LOAD_EXTRUDING,
                                              FEED_STA_LOAD_FLUSHING):
                    return
                self.reactor.register_async_callback(
                    (lambda et, c=self._do_feed, ch=ch, action=FEED_ACT_FILAMENT_RUNOUT: c(ch, action)))
                break

    def _check_init_state_timer_handler(self, eventtime):
        self.reactor.unregister_timer(self._check_init_state_timer)

        for ch in range(FEED_CHANNEL_NUMS):
            if self._port[ch].get_adc_value() < FEED_PORT_ADC_VAL_MODULE_EXIST or self.ace is not None:
                self.module_exist[ch] = True
            else:
                self.module_exist[ch] = False

            if self.config['auto_mode'][ch] == True and self.module_exist[ch] == True:
                if self.filament_detect.is_startup_stay() == False:
                    self.printer.send_event("filament_feed:port", self.filament_ch[ch],
                                            self._port[ch].get_filament_detected())
                if self._port[ch].get_filament_detected() == False:
                    self._set_channel_state(ch, FEED_STA_WAIT_INSERT)
                else:
                    if self.config['load_finish'][ch] == True:
                        self._set_channel_state(ch, FEED_STA_LOAD_FINISH)
                    else:
                        self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH)

        return self.reactor.NEVER

    def _set_channel_state(self, channel, state, save=False):
        prev_state = self.channel_state[channel]
        if prev_state != state:
            import traceback as _tb
            stack = _tb.extract_stack(limit=6)[:-1]
            caller = ' <- '.join(
                '%s:%d:%s' % (f.filename.rsplit('/', 1)[-1], f.lineno, f.name)
                for f in reversed(stack))
            logging.info(
                "[feed][state] channel[%d]: %s -> %s save=%s | %s",
                channel, prev_state, state, save, caller)
        systime = self.reactor.monotonic()
        systime += FEED_MIN_TIME
        print_time = self.light[channel].get_mcu().estimated_print_time(systime)
        if print_time - self._last_print_time < FEED_MIN_TIME:
            print_time = self._last_print_time + FEED_MIN_TIME

        if self.config['auto_mode'][channel] == False:
            self.light[channel].set_light_state(print_time, FEED_STA_NONE)
        else:
            self.light[channel].set_light_state(print_time, state)
        self.channel_state[channel] = state
        self._last_print_time = print_time

        if state not in [FEED_STA_INITED, FEED_STA_WAIT_INSERT, FEED_STA_TEST] and \
                not state.startswith('preload_'):
            self.channel_action_state[channel] = state

        if save == True:
            if state == FEED_STA_LOAD_FINISH:
                self.config['load_finish'][channel] = True
            else:
                self.config['load_finish'][channel] = False
            if not self.printer.update_snapmaker_config_file(self.config_path, self.config, FEED_DEFAULT_CONFIG):
                logging.error("[feed] save config failed!")

    def _set_light_state(self, channel, state):
        systime = self.reactor.monotonic()
        systime += FEED_MIN_TIME
        print_time = self.light[channel].get_mcu().estimated_print_time(systime)
        if print_time - self._last_print_time < FEED_MIN_TIME:
            print_time = self._last_print_time + FEED_MIN_TIME

        self.light[channel].set_light_state(print_time, state)
        self._last_print_time = print_time

    def _port_ch1_event_handler(self, detected):
        self._port_event_handler(detected, FEED_CHANNEL_1)

    def _port_ch2_event_handler(self, detected):
        self._port_event_handler(detected, FEED_CHANNEL_2)

    def _port_event_handler(self, detected, channel):
        if self.config['auto_mode'][channel] == False or \
                self.module_exist[channel] == False:
            return

        self.printer.send_event("filament_feed:port", self.filament_ch[channel], detected)

        if self.runout_sensor[channel] is None or \
                self.runout_sensor[channel].get_status(0)['enabled'] == False:
            return

        if self.manual_feeding[channel]:
            return

        if detected:
            if self.channel_state[channel] == FEED_STA_PRELOAD_PREPARE:
                self._set_light_state(channel, FEED_STA_PRELOAD_PREPARE)
                return
            else:
                self._set_channel_state(channel, FEED_STA_PRELOAD_PREPARE)
                self.reactor.register_async_callback(
                    (lambda et, c=self._do_feed, ch=channel, action=FEED_ACT_PRELOAD: c(ch, action)))
        else:
            if self.channel_active != channel:
                self._set_light_state(channel, FEED_STA_WAIT_INSERT)
            self.reactor.register_async_callback(
                (lambda et, c=self._do_feed, ch=channel, action=FEED_ACT_REMOVE_FILAMENT: c(ch, action)))

    def _check_homing_xy(self):
        curtime = self.reactor.monotonic()
        homed_axes_list = self.toolhead.get_status(curtime)['homed_axes']
        return ('x' in homed_axes_list and 'y' in homed_axes_list)

    def _db_nozzle_args(self, channel):
        # 1.6.0 made the filament DB nozzle-aware: get_load_temp/get_is_soft
        # take (nozzle_diameter, nozzle_volume_type) and select one of five
        # per-nozzle tables (standard 02/04/06/08 + high_flow_04). The shipped
        # temps are identical across tables today, but the selection exists
        # now - so ask with the HEAD's real nozzle, not the 0.4-standard
        # default the omitted args silently pick. Returns () when the DB
        # does not take them; the callers then use the legacy 3-arg form,
        # byte-identical to before.
        #
        # GATE ON THE DB FUNCTION, NOT ON THE EXTRUDER: asking whether the
        # extruder has nozzle_volume_type is wrong - the extruder is OUR
        # extruder_ace.py, which carries the attribute on every firmware,
        # so the nozzle args were passed to a 1.4.1..1.5.2 DB whose
        # get_load_temp takes three -> TypeError on every load AND in the
        # FEED_ACT_REMOVE_FILAMENT timer at boot = full Klipper shutdown.
        # Only the stock DB knows what it accepts; ask its signature once.
        if not self._db_takes_nozzle_args():
            return ()
        try:
            head = self.filament_ch[channel]
            name = 'extruder' if head == 0 else 'extruder%d' % head
            ext = self.printer.lookup_object(name, None)
            vt = getattr(ext, 'nozzle_volume_type', None)
            dia = getattr(ext, 'nozzle_diameter', None)
            if ext is None or vt is None or dia is None:
                return ()
            return (float(dia), str(vt))
        except Exception:
            return ()

    def _db_takes_nozzle_args(self):
        # True when the stock filament DB's get_load_temp accepts
        # nozzle_diameter (1.6.0+). Cached per process; a missing DB or an
        # unreadable signature counts as "no" (the 3-arg form is valid on
        # every firmware, the 5-arg form only on 1.6.0).
        cached = getattr(self, '_db_nozzle_ok', None)
        if cached is not None:
            return cached
        ok = False
        try:
            import inspect
            fp = self.printer.lookup_object('filament_parameters', None)
            fn = getattr(fp, 'get_load_temp', None)
            if fn is not None:
                params = inspect.signature(fn).parameters
                ok = ('nozzle_diameter' in params
                      or any(p.kind == p.VAR_POSITIONAL
                             for p in params.values()))
        except Exception as e:
            logging.info("[feed] filament DB signature probe failed: %s" % e)
            ok = False
        self._db_nozzle_ok = ok
        logging.info("[feed] filament DB nozzle-aware (1.6.0 signature): %s" % ok)
        return ok

    def _get_filament_temp_db(self, channel):
        # The RAW filament-DB load temp (Generic PLA: 250). Kept as its
        # own getter because the stuck-tip RETRY escalates to THIS value
        # even when a tipform 'loadtemp:' runs the normal load cooler
        # (the freeing pull keeps full power).
        print_task_config = self.printer.lookup_object('print_task_config', None)
        filament_parameters = self.printer.lookup_object('filament_parameters', None)
        if print_task_config is None or filament_parameters is None:
            return FEED_FILAMENT_TEMP_DEFAULT

        status = print_task_config.get_status()
        return filament_parameters.get_load_temp(
                status['filament_vendor'][self.filament_ch[channel]],
                status['filament_type'][self.filament_ch[channel]],
                status['filament_sub_type'][self.filament_ch[channel]],
                *self._db_nozzle_args(channel))

    def _get_filament_temp(self, channel):
        # Effective LOAD temp: the [ace_tipform] 'loadtemp:' parameter
        # (the soak lever - with print-temp PLA the swap turns isothermal
        # and the reheat to 250 disappears) or the DB load temp.
        # Chain: tipform -> DB get_load_temp -> 250; floor 175
        # (phase3 extrudes, MIN_MOVE_TEMP class).
        if self.ace is not None:
            try:
                _ov_fn = getattr(self.ace, 'tipform_load_temp_for', None)
                _ov = (_ov_fn(self.filament_ch[channel],
                              soft=self._get_filament_soft(channel))
                       if _ov_fn else None)
            except Exception:
                _ov = None
            if _ov:
                return max(int(_ov), 175)
        return self._get_filament_temp_db(channel)
    def _get_filament_unload_temp(self, channel):
        # Unload/tip-form temp: the [ace_tipform] 'unloadtemp:' PARAMETER
        # (per material/vendor via the table keys, ace.cfg merge-safe) or
        # plainly the load temp. The stock
        # DB's per-material 'unload_temp' field (+ get_unload_temp
        # accessor, present since 1.4.1, zero callers firmware-wide, every
        # row == load temp) is deliberately NOT read:
        # a dormant stock field could be repurposed by any future
        # Snapmaker update and would then steer our unloads uninvited -
        # re-add only when Snapmaker actually wires it themselves.
        # Floor 175 = MIN_MOVE_TEMP class (the pull's extruder moves need
        # > min_extrude_temp 170).
        if self.ace is not None:
            try:
                _ov_fn = getattr(self.ace, 'tipform_unload_temp_for', None)
                _ov = (_ov_fn(self.filament_ch[channel],
                              soft=self._get_filament_soft(channel))
                       if _ov_fn else None)
            except Exception:
                _ov = None
            if _ov:
                return max(int(_ov), 175)
        return self._get_filament_temp(channel)

    def _get_filament_soft(self, channel):
        print_task_config = self.printer.lookup_object('print_task_config', None)
        filament_parameters = self.printer.lookup_object('filament_parameters', None)
        if print_task_config is None or filament_parameters is None:
            return False

        status = print_task_config.get_status()
        return filament_parameters.get_is_soft(
                status['filament_vendor'][self.filament_ch[channel]],
                status['filament_type'][self.filament_ch[channel]],
                status['filament_sub_type'][self.filament_ch[channel]],
                *self._db_nozzle_args(channel))

    def _ms_after_feed_op(self):
        # Return the machine to IDLE after a feed op, but keep it resumable
        # (main_state=PRINTING) if a print is PAUSED - a feed op during a pause
        # is a recovery reload (runout/manual), and leaving IDLE makes the
        # stock RESUME guard refuse. Delegates to the ACE helper; bare IDLE if
        # no ACE. (Fallback split across lines so the IDLE-setter sweep that
        # routes callers here does not rewrite this line.)
        if self.ace is not None:
            self.ace._machine_state_after_feed_op()
        else:
            self.gcode.run_script_from_command(
                "SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")

    def _hang_neutral(self, channel):
        self.reactor.pause(self.reactor.monotonic() + 0.105)
        motor_cnt_1 = self.motor_tachometer.get_counts()
        for retry in range(2):
            if channel == FEED_CHANNEL_1:
                self.motor.run_one_cycle(FEED_MOTOR_DIR_B,
                        self.motor_speed_hang_neutral_b,
                        self.motor_hang_neutral_time)
            else:
                self.motor.run_one_cycle(FEED_MOTOR_DIR_A,
                                        self.motor_speed_hang_neutral_a,
                                        self.motor_hang_neutral_time)
            self.reactor.pause(self.reactor.monotonic() + 0.105)
            motor_cnt_2 = self.motor_tachometer.get_counts()
            logging.info("[feed] extruder[%d] hanging neutral, try: %d, cnt1:%d, cnt2: %d\r\n",
                         self.filament_ch[channel], retry, motor_cnt_1, motor_cnt_2)
            if motor_cnt_2 - motor_cnt_1 > 5:
                break

    def _put_into_drive(self, channel):
        logging.info("[feed] extruder[%d] putting into drive", self.filament_ch[channel])
        if channel == FEED_CHANNEL_1:
            self.motor.run_one_cycle(FEED_MOTOR_DIR_A,
                                     self.motor_speed_hang_neutral_a,
                                     self.motor_hang_neutral_time)
        else:
            self.motor.run_one_cycle(FEED_MOTOR_DIR_B,
                                     self.motor_speed_hang_neutral_b,
                                     self.motor_hang_neutral_time)

    def _is_keep_raw_error_info(self, error=None):
        if error in [FEED_ERR_MOVE, FEED_ERR_MOVE_HOME,
                     FEED_ERR_MOVE_SWITCH, FEED_ERR_HEAT]:
            return True
        else:
            return False

    def _snapshot_inner_resume_state(self):

        try:
            cur_extruder = self.toolhead.get_extruder()
            extruder_index = getattr(cur_extruder, 'extruder_index', None)
            if extruder_index is None:
                return
            temps = []
            for i in range(4):
                name = 'extruder' if i == 0 else ('extruder%d' % i)
                ext = self.printer.lookup_object(name, None)
                if ext is None:
                    temps.append(0)
                    continue
                try:
                    t = int(ext.get_heater().target_temp)
                except Exception:
                    t = 0
                temps.append(t)
            cur_temp = temps[extruder_index] if 0 <= extruder_index < 4 else 0
            cmds = (
                "SET_GCODE_VARIABLE MACRO=INNER_RESUME VARIABLE=last_extruder_index VALUE=%d\n"
                "SET_GCODE_VARIABLE MACRO=INNER_RESUME VARIABLE=last_extruder_temp VALUE=%d\n"
                "SET_GCODE_VARIABLE MACRO=INNER_RESUME VARIABLE=extruder0_temp VALUE=%d\n"
                "SET_GCODE_VARIABLE MACRO=INNER_RESUME VARIABLE=extruder1_temp VALUE=%d\n"
                "SET_GCODE_VARIABLE MACRO=INNER_RESUME VARIABLE=extruder2_temp VALUE=%d\n"
                "SET_GCODE_VARIABLE MACRO=INNER_RESUME VARIABLE=extruder3_temp VALUE=%d\n"
                "SET_GCODE_VARIABLE MACRO=INNER_RESUME VARIABLE=is_pause_on_err VALUE=True\n"
            ) % (extruder_index, cur_temp, temps[0], temps[1], temps[2], temps[3])
            self.gcode.run_script_from_command(cmds)
            logging.info("[feed] snapshot INNER_RESUME: idx=%d, cur_temp=%d, temps=%s",
                         extruder_index, cur_temp, temps)
        except Exception as e:
            logging.error("[feed] snapshot INNER_RESUME failed: %s", str(e))

    def _swap_probe_temp(self, cool_probe, filament_feed_temp):
        # Forward-probe temperature after the INNER tip-pull. Non-swap (or
        # cool_probe off): probe at the material feed temp (always
        # extrudable). Swap cool_probe: aim BELOW the live print temp so the
        # G1 E push does not re-melt the just-formed tip, but stay
        # extrudable for the material - take the swap reference temp
        # (= _get_swap_temp, the live print target captured at swap start,
        # exposed on the ace as _swap_probe_ref_temp) minus
        # SWAP_PROBE_COOL_DELTA, floored at swap_probe_temp. PLA print ~220
        # -> 220-45=175 (= the old absolute default, no regression);
        # PETG/ABS scale up so the probe still extrudes. No live print
        # reference (idle/test) -> the absolute floor. Material-agnostic.
        if not cool_probe:
            return filament_feed_temp
        floor = getattr(self.ace, 'swap_probe_temp', 175)
        ref = getattr(self.ace, '_swap_probe_ref_temp', 0) or 0
        if ref >= 170:
            return max(floor, int(ref) - SWAP_PROBE_COOL_DELTA)
        return floor

    def _unload_dec_log(self, head, slot, kind, length, span, attempt):
        """[diag] one decoder-SPAN line per unload retract to the ace feed log.
        span ~ length = the ACE really pulled the full retract; span << length
        = under-retract/stall (a genuinely stuck tip the ACE cannot move).
        `span` is the (span, n, min, max) tuple from
        ace._retract_with_decoder_span - the SPAN (max-min sampled DURING the
        retract) is base-agnostic, so it is NOT fooled by the decoder's
        per-command reset/hold the way a before/after delta is (it can read a
        full-movement retry as '0'). See UNLOAD_DECODER_DIAG."""
        if not UNLOAD_DECODER_DIAG:
            return
        fl = getattr(self.ace, '_feedlog', None) if self.ace else None
        if fl is None:
            return
        try:
            sp, n, mn, mx = span
            fl.info('unload-dec head=%d slot=%d kind=%s len=%d span=%s n=%s '
                    'min=%s max=%s attempt=%s'
                    % (head, slot, kind, int(length), sp, n, mn, mx, attempt))
        except Exception:
            pass

    def _unload_dec_stalled(self, head, slot, length, span):
        """True when the bulk rest retract demonstrably moved NO filament.

        Reads the (span, n, min, max) tuple of ace._retract_with_decoder_span.
        ABSTAINS (False) whenever there is no verdict to give: gate off, no ace,
        span None (V1, or a firmware that never reports `decoder` - the absent
        vs zero split lives in that function), or a rest too short to judge.
        Only a real reading below UNLOAD_DECODER_MIN_MOVE over a commanded
        UNLOAD_DECODER_MIN_LEN or more counts as a stall, so the failure mode
        of this gate is missing a partial stall, never inventing one."""
        ace = self.ace
        if ace is None or not getattr(ace, 'unload_decoder_gate', True):
            return False
        try:
            sp, n, _mn, _mx = span
        except Exception:
            return False
        if sp is None or not n or length < UNLOAD_DECODER_MIN_LEN:
            return False
        if abs(sp) >= UNLOAD_DECODER_MIN_MOVE:
            return False
        logging.warning(
            '[multiACE] [feed][unload] head %d ACE slot %d: bulk retract of '
            '%dmm moved the lane encoder %s (n=%s) - the filament did not '
            'move. The ACE reports rollbacks as successful even when nothing '
            'feeds, so this unload is NOT verified.'
            % (head, slot, int(length), sp, n))
        return True

    def _do_feed(self, ch, action=None, stage=None, auto_mode=None):
        if ch < 0 or ch >= FEED_CHANNEL_NUMS or action == None:
            logging.error("[feed] parameter error!")
            return

        if action == FEED_ACT_UPDATE_AUTO_MODE and auto_mode is None:
            logging.error("[feed] parameter error!")
            return

        if action in [FEED_ACT_PRELOAD, FEED_ACT_LOAD] and \
                (self.config['auto_mode'][ch] == False or self.module_exist[ch] == False):
            return

        wheel_cnt_a_1 = 0
        wheel_cnt_b_1 = 0
        motor_cnt_1 = 0
        wheel_cnt_a_2 = 0
        wheel_cnt_b_2 = 0
        motor_cnt_2 = 0

        if action == FEED_ACT_PRELOAD:
            wheel_cnt_a_1 = self.wheel[ch].get_counts()
            wheel_cnt_b_1 = self.wheel_2[ch].get_counts()

        while self.channel_active != None:
            self.reactor.pause(self.reactor.monotonic() + 0.1)
        self.channel_active = ch
        self.channel_error[ch] = FEED_OK
        self.exception_code[ch] = 0

        filament_feed_temp = self._get_filament_temp(ch)
        filament_unload_temp = self._get_filament_unload_temp(ch)
        # RETRY escalation temp: the RAW DB load temp, NOT the tipform
        # loadtemp - a cooled normal path must not cool the stuck-tip
        # freeing pull with it.
        filament_feed_temp_db = self._get_filament_temp_db(ch)
        filament_soft = self._get_filament_soft(ch)

        motor_dir = FEED_MOTOR_DIR_A
        if ch == FEED_CHANNEL_2:
            motor_dir = FEED_MOTOR_DIR_B

        try:

            if action == FEED_ACT_UPDATE_AUTO_MODE:
                self.config['auto_mode'][ch] = bool(auto_mode)
                if self.config['auto_mode'][ch] == True:
                    if self.module_exist[ch]:
                        if self._port[ch].get_filament_detected() == False:
                            self._set_channel_state(ch, FEED_STA_WAIT_INSERT, True)
                        else:
                            self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH, True)
                else:
                    self._set_channel_state(ch, FEED_STA_NONE, True)

            elif action == FEED_ACT_REMOVE_FILAMENT:
                if self._port[ch].get_filament_detected() == False:
                    self._set_channel_state(ch, FEED_STA_WAIT_INSERT, True)
                else:
                    self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH, True)

            elif action == FEED_ACT_FILAMENT_RUNOUT:

                if self.ace is not None and getattr(self.ace, '_swap_in_progress', False):
                    logging.info("[multiACE] _do_feed: blocking FILAMENT_RUNOUT during swap")
                    return
                if (self.channel_state[ch] == FEED_STA_LOAD_FINISH
                        and self.runout_sensor[ch] is not None
                        and self.runout_sensor[ch].get_status(0).get('filament_detected')):
                    logging.info(
                        "[feed][runout] channel[%d] flicker ignored - motion sensor back True, "
                        "state LOAD_FINISH preserved", ch)
                    return
                if self._port[ch].get_filament_detected() == True:
                    self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH, True)
                else:
                    self._set_channel_state(ch, FEED_STA_WAIT_INSERT, True)

            elif action == FEED_ACT_PRELOAD:
                has_put_into_drive = False
                try:
                    self.exception_code[ch] = 10
                    self.channel_error_state[ch] = FEED_STA_NONE
                    self._set_channel_state(ch, FEED_STA_PRELOAD_PREPARE, True)

                    if self._port[ch].get_filament_detected() == False:
                        self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                        self.exception_code[ch] = 13
                        raise

                    if self.runout_sensor[ch].get_status(0)['filament_detected']:
                        self.channel_error[ch] = FEED_ERR_RESIDUAL_FILAMENT
                        self.exception_code[ch] = 15
                        raise

                    self.reactor.pause(self.reactor.monotonic() + 1)

                    motor_cnt_1 = self.motor_tachometer.get_counts()
                    logging.info("[feed_preload] extruder[%d], start, wheel_cnt_a: %d, wheel_cnt_b: %d, motor_cnt: %d",
                                  self.filament_ch[ch], wheel_cnt_a_1, wheel_cnt_b_1, motor_cnt_1)

                    self._set_channel_state(ch, FEED_STA_PRELOAD_FEEDING)
                    systime_1 = self.reactor.monotonic()
                    self.motor.run(motor_dir, self.motor_speed_slow_switching)
                    has_put_into_drive = True
                    arrive_runout_sensor = False
                    logging.info("[feed] extruder[%d] putting into drive", self.filament_ch[ch])
                    self.reactor.pause(self.reactor.monotonic() + 0.5)

                    preload_duty = self.motor_speed_preload
                    if arrive_runout_sensor == False:
                        for i in range(3):
                            self.motor.run(motor_dir, preload_duty)
                            self.reactor.pause(self.reactor.monotonic() + 0.35)
                            arrive_runout_sensor = self.runout_sensor[ch].get_status(0)['filament_detected']
                            if arrive_runout_sensor == True or preload_duty >= 1.0:
                                break
                            preload_duty = min(1.0, preload_duty + 0.1)
                        if arrive_runout_sensor == False and preload_duty < 1.0:
                            self.motor.run(motor_dir, 1.0)
                            self.reactor.pause(self.reactor.monotonic() + 0.2)
                            arrive_runout_sensor = self.runout_sensor[ch].get_status(0)['filament_detected']
                    logging.info("[feed_preload] extruder[%d], duty:%f, ", self.filament_ch[ch], preload_duty)

                    motor_speed = 0
                    wheel_speed_a = 0
                    wheel_speed_b = 0
                    wheel_speed_err_max = FEED_PRELOAD_WHEEL_ERR_CNT_MAX
                    motor_speed_err_max = FEED_PRELOAD_MOTOR_ERR_CNT_MAX
                    if arrive_runout_sensor == False:
                        while 1:
                            wheel_cnt_a_2 = self.wheel[ch].get_counts()
                            wheel_cnt_b_2 = self.wheel_2[ch].get_counts()
                            systime_2 = self.reactor.monotonic()
                            motor_speed = self.motor_tachometer.get_rpm()
                            wheel_speed_a = self.wheel[ch].get_rpm()
                            wheel_speed_b = self.wheel_2[ch].get_rpm()
                            port_detect = self._port[ch].get_filament_detected()
                            runout_detect = self.runout_sensor[ch].get_status(0)['filament_detected']

                            if runout_detect == True:
                                self.channel_error[ch] = FEED_OK
                                break
                            if port_detect == False:
                                self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                                self.exception_code[ch] = 13
                                break
                            if (wheel_cnt_a_2 - wheel_cnt_a_1) / self.wheel[ch].ppr > self._feed_preload_counts or \
                                    (wheel_cnt_b_2 - wheel_cnt_b_1) / self.wheel_2[ch].ppr > self._feed_preload_counts:
                                self.channel_error[ch] = FEED_OK
                                break
                            if motor_speed < FEED_PRELOAD_MOTOR_MIN_SPEED:
                                logging.info("[feed_preload] extruder[%d], motor speed error, motor_speed:%d",
                                             self.filament_ch[ch], motor_speed)
                                if motor_speed_err_max > 0:
                                    motor_speed_err_max -= 1
                                else:
                                    self.channel_error[ch] = FEED_ERR_MOTOR_SPEED
                                    self.exception_code[ch] = 11
                                    break
                            else:
                                motor_speed_err_max = FEED_PRELOAD_MOTOR_ERR_CNT_MAX
                            if wheel_speed_a * FEED_MOTOR_REDUCTION_R < motor_speed * (1 - FEED_MOTOR_SLIP_RATE) and \
                                wheel_speed_b * FEED_MOTOR_REDUCTION_R < motor_speed * (1 - FEED_MOTOR_SLIP_RATE):
                                logging.info("[feed_preload] extruder[%d], wheel speed error, wheel_speed_a:%d, wheel_speed_b:%d, motor_speed:%d",
                                             self.filament_ch[ch], wheel_speed_a, wheel_speed_b, motor_speed)
                                if wheel_speed_err_max > 0:
                                    wheel_speed_err_max -= 1
                                else:
                                    self.channel_error[ch] = FEED_ERR_WHEEL_SPEED
                                    self.exception_code[ch] = 12
                                    break
                            else:
                                wheel_speed_err_max = FEED_PRELOAD_WHEEL_ERR_CNT_MAX
                            if systime_2 - systime_1 > FEED_PRELOAD_TIMEOUT_TIME:
                                self.channel_error[ch] = FEED_ERR_TIMEOUT
                                self.exception_code[ch] = 14
                                break

                            self.reactor.pause(self.reactor.monotonic() + 0.05)

                    self.motor.run(FEED_MOTOR_DIR_IDLE, 0)
                    wheel_cnt_a_2 = self.wheel[ch].get_counts()
                    wheel_cnt_b_2 = self.wheel_2[ch].get_counts()
                    motor_cnt_2 = self.motor_tachometer.get_counts()

                    logging.info("[feed_preloading] extruder[%d], wheel, cnt_a_1:%d, cnt_b_1:%d, cnt_a_2:%d, cnt_b_2:%d, wheel_speed_a:%d, wheel_speed_b: %d, "
                                 "motor, motor_cnt_1:%d, motor_cnt_2:%d, motor_speed:%d",
                                 self.filament_ch[ch], wheel_cnt_a_1, wheel_cnt_b_1, wheel_cnt_a_2, wheel_cnt_b_2, wheel_speed_a, wheel_speed_b,
                                 motor_cnt_1, motor_cnt_2, motor_speed)
                    if self.channel_error[ch] != FEED_OK:
                        raise

                    self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH)

                except:
                    if self.channel_error[ch] == FEED_OK:
                        self.channel_error[ch] = FEED_ERR
                        self.exception_code[ch] = 10
                    self._set_channel_state(ch, FEED_STA_PRELOAD_FAIL)
                    self.channel_error_state[ch] = self.channel_state[ch]
                    if self.exception_manager is not None:
                        self.exception_manager.raise_exception_async(
                            id = self.exception_manager.list.MODULE_ID_FEEDING,
                            index = self.filament_ch[ch],
                            code = self.exception_code[ch],
                            message = "preload fail: %s" % (self.channel_error[ch]),
                            oneshot = 1,
                            level = 1)

                finally:
                    if has_put_into_drive:
                        self._hang_neutral(ch)

            elif action == FEED_ACT_LOAD:

                fa_gate_opened = False
                if self.ace is not None:
                    # Head mode: point the active device at this head's wired ACE
                    # (head_ace_for) BEFORE feeding - the whole feed path uses
                    # _active_device_index, so without this a display load on a
                    # head would pull from whichever ACE was globally active.
                    # No-op in multi/normal.
                    self.ace._ensure_active_ace_for_head(self.filament_ch[ch])
                    # Calibration cancel check point (both unload blocks).
                    # No-op unless a calibration PREPARATION unload is running;
                    # then it raises so the web abort actually takes effect at
                    # the next wait instead of after the full sequence.
                    self.ace._check_calibration_unload_cancel()
                    self.ace._fa_trace('FEED_ACT_LOAD enter: ch=%d head=%d active_ace=%d'
                                       % (ch, self.filament_ch[ch], self.ace._active_device_index))
                    self.ace._auto_feed_enabled = True
                    self.ace._fa_context = 'load'
                    self.ace._fa_trace('gate OPEN (context=load) via FEED_ACT_LOAD')
                    fa_gate_opened = True
                try:

                    self.exception_code[ch] = 30
                    self.manual_feeding[ch] = False
                    self.channel_error_state[ch] = FEED_STA_NONE
                    # Stock 1.5 pre-heat: warm the hotend progressively during
                    # home/pick so it is ready by feed time (temp-70 while
                    # homing, then temp/temp-50 depending on whether the last
                    # preload finished normally). All in the shared pre-branch
                    # section -> runs once for ACE/feeder/manual heads alike.
                    is_last_preload_normal = bool(
                        self.channel_state[ch] == FEED_STA_PRELOAD_FINISH)
                    self._set_channel_state(ch, FEED_STA_LOAD_PREPARE, True)

                    if self._port[ch].get_filament_detected() == False:
                        self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                        self.exception_code[ch] = 33
                        raise ValueError('logic error!')

                    self.gcode.run_script_from_command(
                        "M104 S%d\r\n" % (filament_feed_temp - 70))

                    try:
                        self._set_channel_state(ch, FEED_STA_LOAD_HOMING)
                        if self._check_homing_xy() != True:
                            self.gcode.run_script_from_command("G28 X Y\r\n")
                            self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_MOVE_HOME
                        raise

                    try:
                        self._set_channel_state(ch, FEED_STA_LOAD_PICKING)
                        self.gcode.run_script_from_command("T%d A0\r\n" % (self.filament_ch[ch]))
                        self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_MOVE_SWITCH
                        raise

                    if is_last_preload_normal:
                        self.gcode.run_script_from_command(
                            "M104 S%d\r\n" % (filament_feed_temp))
                    else:
                        self.gcode.run_script_from_command(
                            "M104 S%d\r\n" % (filament_feed_temp - 50))

                    self._set_channel_state(ch, FEED_STA_LOAD_FEEDING)
                    self._put_into_drive(ch)
                    self.toolhead.wait_moves()

                    if self.runout_sensor[ch].get_status(0)['filament_detected'] == False:

                        try:
                            self.toolhead.wait_moves()
                            self.gcode.run_script_from_command( \
                                f"G90\nG0 Y{self._feed_load_position_y} F18000\r\n")
                            self.gcode.run_script_from_command( \
                                f"G90\nG0 X{self._feed_load_position_x} F18000\r\n")
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE
                            raise

                        self.reactor.pause(self.reactor.monotonic() + 0.105)
                        wheel_cnt_a_0 = self.wheel[ch].get_counts()
                        wheel_cnt_b_0 = self.wheel_2[ch].get_counts()
                        systime_0 = self.reactor.monotonic()
                        duty = self.motor_speed_load
                        period = 0.09
                        motor_err_max_cnt = FEED_LOAD_MOTOR_ERR_CNT_MAX
                        wheel_err_max_cnt = FEED_LOAD_WHEEL_ERR_CNT_MAX
                        one_step_cnt = self.wheel[ch].ppr * 2.0 * 10.0 / FEED_WHEEL_CIRCUMFERENCE

                        if self.ace is not None \
                                and not self.ace.head_uses_ace(self.filament_ch[ch]):
                            # FEEDER head (head mode) or MANUAL head (multi):
                            # push filament to the toolhead sensor with the native
                            # stock side feeder - NO ACE. The display's normal
                            # Load transports like stock, its Manual load stays
                            # the hand routine. The ACE fork replaced
                            # this native loop with the ACE feed in the elif below.
                            # motor_dir + the duty/period/wheel-count setup above
                            # are already in scope; the shared post-feed check
                            # (channel_error != FEED_OK -> _hang_neutral) below
                            # handles failure for both branches.
                            # === SYNC MARKER ===========================================
                            # The while-loop below is VERBATIM from stock
                            # u1_firmware:1.4.1/extras/filament_feed.py
                            # _do_feed FEED_ACT_LOAD native feed loop (~lines 998-1057).
                            # It is a copy (Klipper loads one module per section, the
                            # stock _do_feed is monolithic, and the .pre_multiace backup
                            # is not always present - so it can't be imported/subclassed
                            # cleanly). If Snapmaker changes the feed loop in a firmware
                            # bump, RE-SYNC this copy against that source.
                            # ===========================================================
                            logging.info(
                                "[feed_loading] feeder head %d: native side-feed (no ACE)"
                                % self.filament_ch[ch])
                            while 1:
                                wheel_cnt_a_1 = self.wheel[ch].get_counts()
                                wheel_cnt_b_1 = self.wheel_2[ch].get_counts()
                                motor_cnt_1 = self.motor_tachometer.get_counts()
                                self.motor.run_one_cycle(motor_dir, duty, period)
                                self.reactor.pause(self.reactor.monotonic() + 0.105)
                                systime_2 = self.reactor.monotonic()
                                motor_cnt_2 = self.motor_tachometer.get_counts()
                                wheel_cnt_a_2 = self.wheel[ch].get_counts()
                                wheel_cnt_b_2 = self.wheel_2[ch].get_counts()
                                port_detect = self._port[ch].get_filament_detected()
                                runout_detect = self.runout_sensor[ch].get_status(0)['filament_detected']
                                if runout_detect == True:
                                    self.channel_error[ch] = FEED_OK
                                    break
                                if port_detect == False:
                                    self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                                    self.exception_code[ch] = 33
                                    break
                                if systime_2 - systime_0 > FEED_LOAD_TIMEOUT_TIME:
                                    self.channel_error[ch] = FEED_ERR_TIMEOUT
                                    self.exception_code[ch] = 34
                                    break
                                if (wheel_cnt_a_2 - wheel_cnt_a_0) / self.wheel[ch].ppr > self._feed_load_counts_max or \
                                        (wheel_cnt_b_2 - wheel_cnt_b_0) / self.wheel_2[ch].ppr > self._feed_load_counts_max:
                                    self.channel_error[ch] = FEED_ERR_DISTANCE
                                    self.exception_code[ch] = 35
                                    break
                                if wheel_cnt_a_2 - wheel_cnt_a_1 < 1 and wheel_cnt_b_2 - wheel_cnt_b_1 < 1:
                                    wheel_err_max_cnt -= 1
                                    if wheel_err_max_cnt <= 0:
                                        self.channel_error[ch] = FEED_ERR_WHEEL_SPEED
                                        self.exception_code[ch] = 32
                                        break
                                else:
                                    wheel_err_max_cnt = FEED_LOAD_WHEEL_ERR_CNT_MAX
                                if motor_cnt_2 - motor_cnt_1 < 1:
                                    motor_err_max_cnt -= 1
                                    if motor_err_max_cnt <= 0:
                                        self.channel_error[ch] = FEED_ERR_MOTOR_SPEED
                                        self.exception_code[ch] = 31
                                        break
                                else:
                                    motor_err_max_cnt = FEED_LOAD_MOTOR_ERR_CNT_MAX
                                if wheel_cnt_a_2 - wheel_cnt_a_1 > one_step_cnt or wheel_cnt_b_2 - wheel_cnt_b_1 > one_step_cnt:
                                    if duty > 0.7:
                                        duty = max(0.7, duty - 0.1)
                                    period = max(0.09, period - 0.01)
                                elif wheel_cnt_a_2 - wheel_cnt_a_1 < one_step_cnt and wheel_cnt_b_2 - wheel_cnt_b_1 < one_step_cnt:
                                    if duty < 1.0:
                                        duty = min(1.0, duty + 0.1)
                                    else:
                                        period = min(0.120, period + 0.01)
                        elif self.ace is not None:
                            ace_idx = self.ace._active_device_index
                            self.ace._fa_trace('feed phase enter: ace=%d ch=%d head=%d'
                                               % (ace_idx, ch, self.filament_ch[ch]))

                            # (A manual head never reaches this branch any
                            # more: it loads through the native feeder loop
                            # above, like a head-mode feeder head.)
                            if ace_idx in self.ace._fa_load_disable:
                                logging.info(
                                    '[multiACE] FEED_AUTO LOAD: ACE %d in fa_load_disable, skipping feed+FA' % ace_idx)
                                self.channel_error[ch] = FEED_OK
                            else:

                                self.ace._disable_feed_assist_all()
                                self.ace.wait_ace_ready()
                                _head_idx = self.filament_ch[ch]
                                _ace_slot = self.ace._ace_slot_for_head(_head_idx)
                                if _ace_slot != _head_idx:
                                    logging.info(
                                        "[feed_loading] head %d loads from ACE slot %d (slot!=head)",
                                        _head_idx, _ace_slot)
                                load_retries = self.ace.head_load_retry[_head_idx]
                                load_retry_retract = self.ace.head_load_retry_retract[_head_idx]

                                # Bowden transport = passive dock wait ->
                                # dwell-fan window (swap-gated inside the
                                # helper). Covers the feed poll AND retries;
                                # OFF before the heat/seat/phase3 sequence,
                                # backstopped in cmd_ACE_SWAP_HEAD's finally.
                                self.ace._dwell_fan(True)
                                for load_attempt in range(load_retries + 1):
                                    if load_attempt > 0:
                                        logging.info("[feed_loading] retry %d/%d: retracting %dmm",
                                                     load_attempt, load_retries, load_retry_retract)
                                        self.ace._retract(_ace_slot, load_retry_retract, self.ace.retract_speed, head=_head_idx)
                                        self.ace.wait_ace_ready()

                                    _ll = self.ace.get_load_length(self.ace._active_device_index, _ace_slot)
                                    self.ace._feed(_ace_slot, _ll, self.ace.feed_speed, 0)
                                    # No fixed pause before the poll loop any
                                    # more. The old 4.0 s wait existed for
                                    # ONE reason: send_request_to stamps
                                    # 'busy' at the feed send, but a status
                                    # report already in flight can write
                                    # 'ready' back before the device reports
                                    # the motion (heartbeat 1 Hz) - so
                                    # is_ace_ready() would end the loop with
                                    # the feed still running. That guard now
                                    # sits on the ready-exit ONLY. Polling
                                    # the sensor during those 4 s matters on
                                    # a RETRY (50 mm retract, filament back
                                    # at the sensor in <1 s) and on a reload
                                    # after a fault: the sensor fired at
                                    # ~0.9 s and the feed ran on for ~3.3 s
                                    # = up to 262 mm at 80 mm/s into a cold,
                                    # stationary extruder (reporter timing,
                                    # 2026-09: crushed/chewed filament, 3x
                                    # in a week). A normal load reaches the
                                    # sensor ~17 s in, so it is unchanged.
                                    _feed_start = self.reactor.monotonic()

                                    # Feed-time budget: without it a comms
                                    # loss whose stale 'busy' never clears
                                    # spins this wait loop forever (the comms
                                    # give-up PAUSE fires elsewhere but the
                                    # load op would hang). Expiry acts like
                                    # "sensor not reached" -> retry path.
                                    _feed_deadline = (self.reactor.monotonic()
                                        + _ll / max(self.ace.feed_speed, 1)
                                        + 30.0)
                                    _gate_skip_said = False
                                    load_found = False
                                    while 1:
                                        self.reactor.pause(self.reactor.monotonic() + 0.105)
                                        port_detect = self._port[ch].get_filament_detected()
                                        runout_detect = self.runout_sensor[ch].get_status(0)['filament_detected']

                                        if self.reactor.monotonic() > _feed_deadline:
                                            logging.info(
                                                "[feed_loading] attempt %d: feed wait deadline hit"
                                                " - treating as sensor-not-reached",
                                                load_attempt + 1)
                                            break
                                        if runout_detect == True:
                                            self.ace._stop_feeding(_ace_slot)
                                            self.channel_error[ch] = FEED_OK
                                            load_found = True
                                            break
                                        if (self.ace.is_ace_ready()
                                                and self.reactor.monotonic()
                                                    - _feed_start > FEED_READY_GUARD_S):
                                            break
                                        if port_detect == False:
                                            # Trust the empty-gate verdict only
                                            # when it is DEFINITIVE: during an
                                            # ACE reconnect the gate reads
                                            # UNKNOWN/stale -> False, and that
                                            # aborted the load as no_filament
                                            # WITHOUT any retry although the
                                            # reconnect was done 1s later (a
                                            # USB blip mid-feed).
                                            # Definitive = ACE connected, no
                                            # reconnect in flight, raw gate ==
                                            # GATE_EMPTY (0); UNKNOWN (-1) or a
                                            # comms gap keeps waiting - the
                                            # deadline/retry path recovers.
                                            _src_ace = self.ace._active_device_index
                                            _raw_gate = -1
                                            try:
                                                _gates = self.ace._gate_status_per_ace.get(_src_ace)
                                                if _gates is not None and _ace_slot < len(_gates):
                                                    _raw_gate = _gates[_ace_slot]
                                            except Exception:
                                                pass
                                            _definitive = (
                                                self.ace._connected_per_ace.get(_src_ace, False)
                                                and not self.ace._reconnecting_per_ace.get(_src_ace, False)
                                                and _raw_gate == 0)
                                            if _definitive:
                                                self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                                                self.exception_code[ch] = 33
                                                break
                                            if not _gate_skip_said:
                                                logging.info(
                                                    "[feed_loading] gate reads empty but not definitive "
                                                    "(ace=%d connected=%s reconnecting=%s raw_gate=%s)"
                                                    " - transient, keep waiting",
                                                    _src_ace,
                                                    self.ace._connected_per_ace.get(_src_ace, False),
                                                    self.ace._reconnecting_per_ace.get(_src_ace, False),
                                                    _raw_gate)
                                                _gate_skip_said = True

                                    if load_found:
                                        break

                                    if self.channel_error[ch] == FEED_ERR_NO_FILAMENT:
                                        break

                                    logging.info("[feed_loading] attempt %d: sensor not reached", load_attempt + 1)

                                if not load_found and self.channel_error[ch] != FEED_ERR_NO_FILAMENT:
                                    self.channel_error[ch] = FEED_ERR_TIMEOUT
                                    self.exception_code[ch] = 34
                                self.ace._dwell_fan(False)

                        if self.channel_error[ch] != FEED_OK:
                            self._hang_neutral(ch)
                            raise ValueError('logic error!')
                    if self.ace is not None \
                            and self.ace.head_uses_ace(self.filament_ch[ch]) \
                            and self.ace._active_device_index not in self.ace._fa_load_disable:

                        self.ace.wait_ace_ready()
                        head_idx = self.filament_ch[ch]
                        fa_slot = self.ace._ace_slot_for_head(head_idx)
                        logging.info('[multiACE] FEED_AUTO LOAD: about to call _arm_fa_for idx=%d slot=%d auto_feed=%s fa_context=%s' % (
                            self.ace._active_device_index, fa_slot, self.ace._auto_feed_enabled, self.ace._fa_context))
                        try:
                            self.ace._arm_fa_for(
                                self.ace._active_device_index, fa_slot)
                        except Exception as fa_e:
                            logging.info(
                                '[multiACE] FEED_AUTO LOAD: _arm_fa_for failed: %s'
                                % fa_e)
                    self.gcode.run_script_from_command("M104 S%d\r\n" % (filament_feed_temp))
                    try:
                        self.toolhead.wait_moves()
                        self.gcode.run_script_from_command("MOVE_TO_DISCARD_FILAMENT_POSITION\r\n")
                        self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_MOVE
                        raise

                    self._set_channel_state(ch, FEED_STA_LOAD_HEATING)
                    try:
                        self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
                    except:
                        self.channel_error[ch] = FEED_ERR_HEAT
                        raise

                    # Hot seat press (seat_overshoot_length, shipped 20):
                    # the sensor-stopped feed leaves the tip ~10-20mm ABOVE
                    # the gear nip. Press it in AFTER the heat (hot hotend -
                    # the old 05-05 cold-ram concern is void) with the bg
                    # press semantics: slow (20mm/s), bounded, busy-retried.
                    # V2 self-stops at resistance, V1 = the knob bounds the
                    # open-loop push. phase3 below stays the verify.
                    # _seat_pressed drives the phase3 retry-0 coil check: with
                    # the tip pressed into the nip, retry-0's flow reading is
                    # trustworthy, so phase3 can pass at retry 0 instead of
                    # always burning the no-check refill pass (see phase3).
                    _seat_pressed = False
                    _press = (int(getattr(self.ace, 'seat_overshoot_length', 0))
                              if self.ace is not None else 0)
                    if _press > 0 and self.ace.head_uses_ace(self.filament_ch[ch]):
                        _p_head = self.filament_ch[ch]
                        _p_slot = self.ace._ace_slot_for_head(_p_head)
                        _p_idx = self.ace._active_device_index
                        try:
                            self.ace._feed_assist_per_ace[_p_idx] = -1
                            self.ace._tipform_send(_p_idx, {
                                'method': 'stop_feed_assist',
                                'params': {'index': _p_slot}})
                            _p_ok = False
                            for _pa in range(3):
                                _presp = self.ace._tipform_send(_p_idx, {
                                    'method': 'feed_filament',
                                    'params': {'index': _p_slot,
                                               'length': _press,
                                               'speed': 20}})
                                if not self.ace._tipform_rejected(_presp):
                                    _p_ok = True
                                    break
                                if _pa < 2:
                                    self.reactor.pause(
                                        self.reactor.monotonic() + 2.0)
                            if _p_ok:
                                # [diag] the commanded length is an UPPER
                                # BOUND - the ACE stops by itself at
                                # resistance. Sample the decoder span so a
                                # failed load tells us whether the press
                                # ran out its length (tip far from the nip
                                # -> a longer press would help) or stopped
                                # early (a thin/long taper already trips
                                # the stop -> more length is never used).
                                # Same 'unload-dec' line as the bg press.
                                def _do_press():
                                    # COUPLED press: the
                                    # extruder turns WITH the ACE push. An
                                    # energized, holding extruder is a locked
                                    # wall - the V2's resistance self-stop
                                    # fires AT that wall (tip at the nip, not
                                    # in it), which is why a hand-push worked
                                    # where the press read moved 0: after a
                                    # failed load the stepper is disabled and
                                    # the gears freewheel. Turning gears take
                                    # the tip instead. E length = press bound,
                                    # F300 = the re-grip's proven pairing
                                    # with the 20mm/s ACE feed;
                                    # position is the discard chute, hot, so
                                    # the pull-in is purged by phase3/flush.
                                    _t_end = (self.reactor.monotonic()
                                              + _press / 20. + 1.0)
                                    try:
                                        self.gcode.run_script_from_command(
                                            "M83\r\n")
                                        self.gcode.run_script_from_command(
                                            "G1 E%d F300\r\n" % _press)
                                        self.toolhead.wait_moves()
                                    except Exception as _ge:
                                        logging.info(
                                            '[feed_loading] press extruder '
                                            'couple failed (%s) - ACE-only '
                                            'window' % _ge)
                                    _rem = _t_end - self.reactor.monotonic()
                                    if _rem > 0:
                                        self.reactor.pause(
                                            self.reactor.monotonic() + _rem)
                                    self.ace._tipform_send(_p_idx, {
                                        'method': 'stop_feed_filament',
                                        'params': {'index': _p_slot}})
                                _psp = (None, 0, None, None)
                                try:
                                    _psp = self.ace._retract_with_decoder_span(
                                        _p_idx, _p_slot, _do_press)
                                except Exception:
                                    _do_press()
                                try:
                                    _fl = getattr(self.ace, '_feedlog', None)
                                    if _fl is not None:
                                        _fl.info(
                                            'unload-dec head=%d slot=%d '
                                            'kind=seat-press len=%d span=%s '
                                            'n=%s min=%s max=%s'
                                            % (_p_head, _p_slot, _press,
                                               _psp[0], _psp[1], _psp[2],
                                               _psp[3]))
                                except Exception:
                                    pass
                                _seat_pressed = True
                                logging.info(
                                    "[feed_loading] hot seat press %dmm done "
                                    "(ace %d slot %d, attempt %d, moved %s)",
                                    _press, _p_idx, _p_slot, _pa + 1,
                                    _psp[0] if _psp[0] is not None
                                    else 'n/a (V1)')
                                # Zero span with turning gears = slip at the
                                # ACE / blockage before it; the load-slip
                                # pause names that end (note_seat_press_span).
                                try:
                                    self.ace.note_seat_press_span(
                                        _p_idx, _p_slot, _psp[0])
                                except Exception:
                                    pass
                            else:
                                logging.info(
                                    "[feed_loading] hot seat press rejected "
                                    "(busy) 3x - continuing without press")
                        except Exception as _pe:
                            logging.info("[feed_loading] hot seat press failed "
                                         "(continuing): %s" % _pe)
                        try:
                            self.ace._arm_fa_for(_p_idx, _p_slot)
                        except Exception:
                            pass

                    self.exception_code[ch] = 50
                    self._set_channel_state(ch, FEED_STA_LOAD_EXTRUDING)
                    inductance_coil = None
                    try:
                        inductance_coil = self.toolhead.get_extruder().binding_probe.sensor
                    except:
                        logging.info("[feed_loading] inductance_coil not found")
                        inductance_coil = None

                    extruded = False
                    phase3_wiggles_used = ''
                    try:
                        duty = 0.8
                        period = 0.100
                        self.gcode.run_script_from_command("M83\r\n")

                        if self.ace is not None:
                            extrude_max = self.ace.extrusion_retry + 1
                        else:
                            extrude_max = self._feed_load_extrude_max_times

                        ace_idx_p3 = None
                        slot_p3 = None
                        # Feeder head: leave ace_idx_p3/slot_p3 None so every
                        # phase3 ACE action (gated on them) is skipped and the
                        # extruder-only/native retry path runs instead.
                        if self.ace is not None and self.ace.head_uses_ace(self.filament_ch[ch]):
                            head_idx_p3 = self.filament_ch[ch]
                            src_p3 = self.ace._head_source.get(head_idx_p3)
                            if src_p3 is not None:
                                ace_idx_p3 = src_p3['ace_index']
                                slot_p3 = src_p3['slot']
                            else:
                                ace_idx_p3 = self.ace._active_device_index
                                slot_p3 = head_idx_p3

                        for retry in range(extrude_max):
                            self.toolhead.wait_moves()
                            self.reactor.pause(self.reactor.monotonic() + 0.105)
                            wheel_cnt_a_1 = self.wheel[ch].get_counts()
                            wheel_cnt_b_1 = self.wheel_2[ch].get_counts()

                            prev_a_p3 = wheel_cnt_a_1
                            prev_b_p3 = wheel_cnt_b_1
                            coil_freq_start = 0
                            coil_freq_end_min = 0
                            coil_freq_end_max = 0
                            coil_freq_threshold = 1500
                            coil_freq_sample_times = 5
                            coil_freq_time_interval = 0.1

                            extrude_length = 20
                            extrude_speed = 400
                            retry_extrude_times = 2
                            if filament_soft == True:
                                extrude_length = 30
                                extrude_speed = 200
                                coil_freq_time_interval = 1.0
                                coil_freq_sample_times = 8
                                coil_freq_threshold = self.coil_freq_threshold_soft
                                retry_extrude_times = 3
                            else:
                                extrude_length = 20
                                extrude_speed = 400
                                coil_freq_time_interval = 0.5
                                coil_freq_sample_times = 5
                                coil_freq_threshold = self.coil_freq_threshold_hard
                                retry_extrude_times = 2

                            for retry_extrude in range(retry_extrude_times):
                                if inductance_coil is not None:
                                    coil_freq_start = inductance_coil.get_coil_freq()
                                    coil_freq_end_min = coil_freq_end_max = coil_freq_start
                                self.gcode.run_script_from_command(f"G1 E{extrude_length} F{extrude_speed}\r\n")
                                self.reactor.pause(self.reactor.monotonic() + 0.5)
                                if inductance_coil is not None:
                                    for i in range(coil_freq_sample_times):
                                        tmp_coil_frep = inductance_coil.get_coil_freq()
                                        if tmp_coil_frep > coil_freq_end_max:
                                            coil_freq_end_max = tmp_coil_frep
                                        elif tmp_coil_frep < coil_freq_end_min:
                                            coil_freq_end_min = tmp_coil_frep
                                        self.reactor.pause(self.reactor.monotonic() + coil_freq_time_interval)
                                self.toolhead.wait_moves()
                                self.reactor.pause(self.reactor.monotonic() + 0.105)
                                wheel_cnt_a_2 = self.wheel[ch].get_counts()
                                wheel_cnt_b_2 = self.wheel_2[ch].get_counts()
                                logging.info("[feed_loading] phase3: extrude[%d] retry:%d, retry_extrude:%d, "
                                             "coil_freq_start:%d, coil_freq_end_min:%d, coil_freq_end_max:%d, coil_freq_delta:%d",
                                                self.filament_ch[ch], retry, retry_extrude, coil_freq_start,
                                                coil_freq_end_min, coil_freq_end_max, coil_freq_end_max - coil_freq_end_min)
                                logging.info("[feed_loading] phase3: wheel, cnt_a_1:%d, cnt_a_2:%d, cnt_b_1:%d, cnt_b_2:%d",
                                         wheel_cnt_a_1, wheel_cnt_a_2, wheel_cnt_b_1, wheel_cnt_b_2)
                                _wlog_push = (getattr(self.ace, '_wiggle_log', None)
                                              if self.ace is not None else None)
                                if _wlog_push is not None:
                                    _wlog_push.info(
                                        'phase3 head=%d ace=%s slot=%s retry=%d r_e=%d '
                                        'coil_start=%d coil_min=%d coil_max=%d coil_delta=%d '
                                        'wheel_a=%d wheel_b=%d',
                                        self.filament_ch[ch], ace_idx_p3, slot_p3,
                                        retry, retry_extrude, coil_freq_start,
                                        coil_freq_end_min, coil_freq_end_max,
                                        coil_freq_end_max - coil_freq_end_min,
                                        wheel_cnt_a_2 - wheel_cnt_a_1,
                                        wheel_cnt_b_2 - wheel_cnt_b_1)

                                step_delta_a = wheel_cnt_a_2 - prev_a_p3
                                step_delta_b = wheel_cnt_b_2 - prev_b_p3

                                coil_high = (abs(coil_freq_end_min - coil_freq_start) >= 5000
                                             or abs(coil_freq_end_max - coil_freq_start) >= 5000)
                                step_ok = (retry_extrude == 0
                                           or step_delta_a >= 2 or step_delta_b >= 2
                                           or coil_high)
                                if self.check_wheel_data != 0 and self.check_coil_freq == 0:
                                    if ((wheel_cnt_a_2 - wheel_cnt_a_1 >= 5 or wheel_cnt_b_2 - wheel_cnt_b_1 >= 5)
                                            and step_ok):
                                        extruded = True
                                        break
                                elif self.check_wheel_data == 0 and self.check_coil_freq != 0:
                                    # Coil check normally starts at retry 1
                                    # (retry 0 is a no-check refill pass: the
                                    # tip may not be at the melt zone yet). A
                                    # hot seat press SEATS the tip in the nip,
                                    # so retry-0 flow IS trustworthy -> let it
                                    # pass at retry 0, saving the wasted refill
                                    # probe.
                                    if (retry > 0 or _seat_pressed) and inductance_coil is not None:
                                        if abs(coil_freq_end_min - coil_freq_start) >= coil_freq_threshold or \
                                                abs(coil_freq_end_max - coil_freq_start) >= coil_freq_threshold:
                                            # LOW-PASS MIRROR (ace.py
                                            # COIL_LOWPASS_FRAC note): a
                                            # retry-0 shortcut pass far below
                                            # the lane's own clean baseline is
                                            # a marginal grip (white 91-92mm
                                            # thin band: 1974 vs ~5600), not
                                            # proof of flow. Deny the shortcut
                                            # and verify at retry 1 - a
                                            # DEMOTION only, retry>=1 passes
                                            # are never touched.
                                            _lp_ok = True
                                            if (retry == 0 and self.ace is not None
                                                    and ace_idx_p3 is not None):
                                                try:
                                                    _lp_ok, _lp_b, _lp_r = \
                                                        self.ace.coil_lowpass_check(
                                                            self.filament_ch[ch],
                                                            ace_idx_p3, slot_p3,
                                                            coil_freq_end_max
                                                            - coil_freq_end_min)
                                                except Exception:
                                                    _lp_ok = True
                                                if not _lp_ok:
                                                    # _lp_b is None = the
                                                    # SEED deny (no clean
                                                    # baseline yet) - its own
                                                    # wording/format: the
                                                    # %d demote format
                                                    # would crash on None,
                                                    # and log analyses key
                                                    # on the demote line
                                                    # carrying a baseline.
                                                    if _lp_b is None:
                                                        _lp_msg = (
                                                            '[feed_loading] phase3: '
                                                            'retry-0 pass %d has '
                                                            'no clean lane '
                                                            'baseline yet - '
                                                            'shortcut denied, '
                                                            'seeding at retry 1'
                                                            % (coil_freq_end_max
                                                               - coil_freq_end_min))
                                                    else:
                                                        _lp_msg = (
                                                            '[feed_loading] phase3: '
                                                            'retry-0 pass %d well '
                                                            'below lane baseline %d '
                                                            '(ratio %.2f) - shortcut '
                                                            'denied, verifying at '
                                                            'retry 1'
                                                            % (coil_freq_end_max
                                                               - coil_freq_end_min,
                                                               _lp_b, _lp_r))
                                                    logging.info(_lp_msg)
                                                    if _wlog_push is not None:
                                                        if _lp_b is None:
                                                            _wlog_push.info(
                                                                'phase3 head=%d ace=%s '
                                                                'slot=%s LOWPASS '
                                                                'seed-deny delta=%d',
                                                                self.filament_ch[ch],
                                                                ace_idx_p3, slot_p3,
                                                                coil_freq_end_max
                                                                - coil_freq_end_min)
                                                        else:
                                                            _wlog_push.info(
                                                                'phase3 head=%d ace=%s '
                                                                'slot=%s LOWPASS demote '
                                                                'delta=%d baseline=%d '
                                                                'ratio=%.2f',
                                                                self.filament_ch[ch],
                                                                ace_idx_p3, slot_p3,
                                                                coil_freq_end_max
                                                                - coil_freq_end_min,
                                                                _lp_b, _lp_r)
                                            if _lp_ok:
                                                extruded = True
                                                break
                                else:

                                    wheel_ok = (wheel_cnt_a_2 - wheel_cnt_a_1 >= 5 or
                                                wheel_cnt_b_2 - wheel_cnt_b_1 >= 5)
                                    coil_ok = True
                                    if retry > 0 and inductance_coil is not None:
                                        coil_ok = (abs(coil_freq_end_min - coil_freq_start) >= coil_freq_threshold or
                                                   abs(coil_freq_end_max - coil_freq_start) >= coil_freq_threshold)
                                    if wheel_ok and coil_ok and step_ok:
                                        extruded = True
                                        break

                                prev_a_p3 = wheel_cnt_a_2
                                prev_b_p3 = wheel_cnt_b_2

                            # RESCUE VERIFY (ace.py RESCUE_VERIFY_FRAC note):
                            # a pass fought out of the wiggle ladder that
                            # still reads weak vs the lane's clean baseline
                            # can print a visible air band - the ladder
                            # saves the load, not the
                            # print. Purge ~25mm to rebuild pressure and
                            # push out the thin melt, then ONE re-measure
                            # with the standard probe; still weak -> fall
                            # through to the regular phase3 FAIL path (the
                            # pickcheck two-strike pattern, no third try).
                            # ACE heads only; the coil vars are overwritten
                            # with the re-measure so the SUCCESS line and
                            # the resistance/baseline note reflect the
                            # VERIFIED measurement.
                            if (extruded and retry >= 2
                                    and inductance_coil is not None
                                    and self.ace is not None
                                    and ace_idx_p3 is not None):
                                try:
                                    _rv_weak, _rv_b, _rv_r = \
                                        self.ace.rescue_verify_check(
                                            self.filament_ch[ch], ace_idx_p3,
                                            slot_p3,
                                            coil_freq_end_max
                                            - coil_freq_end_min)
                                except Exception:
                                    _rv_weak, _rv_b, _rv_r = (False, None, None)
                                if _rv_weak:
                                    logging.info(
                                        '[feed_loading] phase3: RESCUE pass '
                                        '%d at retry %d is weak vs baseline '
                                        '%s (ratio %s) - purge + re-measure',
                                        coil_freq_end_max - coil_freq_end_min,
                                        retry,
                                        '%d' % _rv_b if _rv_b else 'none',
                                        '%.2f' % _rv_r if _rv_r else '-')
                                    self.gcode.run_script_from_command(
                                        "G1 E25 F400\r\n")
                                    self.toolhead.wait_moves()
                                    self.reactor.pause(
                                        self.reactor.monotonic() + 0.5)
                                    _c0 = inductance_coil.get_coil_freq()
                                    _cmin = _cmax = _c0
                                    self.gcode.run_script_from_command(
                                        f"G1 E{extrude_length} "
                                        f"F{extrude_speed}\r\n")
                                    self.reactor.pause(
                                        self.reactor.monotonic() + 0.5)
                                    for _i in range(coil_freq_sample_times):
                                        _cf = inductance_coil.get_coil_freq()
                                        if _cf > _cmax:
                                            _cmax = _cf
                                        elif _cf < _cmin:
                                            _cmin = _cf
                                        self.reactor.pause(
                                            self.reactor.monotonic()
                                            + coil_freq_time_interval)
                                    self.toolhead.wait_moves()
                                    _d2 = _cmax - _cmin
                                    _abs_ok = (
                                        abs(_cmin - _c0) >= coil_freq_threshold
                                        or abs(_cmax - _c0)
                                        >= coil_freq_threshold)
                                    _rel_ok = (_rv_b is None
                                               or _d2 >= _rv_b * 0.7)
                                    _verdict = _abs_ok and _rel_ok
                                    logging.info(
                                        '[feed_loading] phase3: RESCUE '
                                        're-measure delta %d (was %d) -> %s',
                                        _d2,
                                        coil_freq_end_max - coil_freq_end_min,
                                        'ok' if _verdict else
                                        'STILL WEAK - failing the load')
                                    _wlog_rv = (getattr(self.ace,
                                                        '_wiggle_log', None))
                                    if _wlog_rv is not None:
                                        _wlog_rv.info(
                                            'phase3 head=%d ace=%s slot=%s '
                                            'RESCUE_VERIFY first=%d '
                                            're=%d baseline=%s verdict=%s',
                                            self.filament_ch[ch], ace_idx_p3,
                                            slot_p3,
                                            coil_freq_end_max
                                            - coil_freq_end_min,
                                            _d2,
                                            '%d' % _rv_b if _rv_b else '-',
                                            'ok' if _verdict else 'weak')
                                    if _verdict:
                                        coil_freq_start = _c0
                                        coil_freq_end_min = _cmin
                                        coil_freq_end_max = _cmax
                                    else:
                                        extruded = False

                            if extruded == True:
                                _wlog = (getattr(self.ace, '_wiggle_log', None)
                                         if self.ace else None)
                                if _wlog is not None:
                                    _wlog.info(
                                        'phase3 head=%d ace=%s slot=%s SUCCESS at retry=%d wiggles_used=%s '
                                        'coil_start=%d coil_min=%d coil_max=%d coil_delta=%d',
                                        self.filament_ch[ch], ace_idx_p3, slot_p3,
                                        retry, phase3_wiggles_used or '(none)',
                                        coil_freq_start, coil_freq_end_min, coil_freq_end_max,
                                        coil_freq_end_max - coil_freq_end_min)
                                # Resistance watch (ace.py RESISTANCE_* block):
                                # phase3's coil test is one-sided, so a HUGE
                                # passing delta is back-pressure, not health.
                                # Judge SUCCESS deltas against the lane's own
                                # baseline; a pause escalation is only STAGED
                                # here (_resistance_pause_pending) - the swap
                                # consumes it AFTER flush/wipe/pos-restore
                                # (never raise for a succeeded load mid-swap).
                                #
                                # RETRY 0 IS A NOISY MEASUREMENT POINT: it
                                # probes a FRESH melt zone - right after
                                # load+seat press, before anything was ever
                                # extruded - so the coil reads the tip's
                                # melt/press state, not the lane's
                                # resistance, and spreads far wider than
                                # retry>=1 on the same lane. It is NOT pure
                                # noise though: a real resistance episode
                                # can show up ONLY there. So it is passed
                                # with
                                # noisy=True - own baseline (never blurs the
                                # clean retry>=1 reference) + the much
                                # higher RESISTANCE_WARN_ABS_NOISY (see the
                                # ace.py const block).
                                if (self.ace is not None
                                        and ace_idx_p3 is not None
                                        and inductance_coil is not None):
                                    try:
                                        _resv, _, _ = self.ace._resistance_note(
                                            'phase3', self.filament_ch[ch],
                                            ace_idx_p3, slot_p3,
                                            coil_freq_end_max - coil_freq_end_min,
                                            noisy=(retry == 0))
                                        if _resv == 'pause_due':
                                            self.ace._resistance_pause_pending = (
                                                self.filament_ch[ch],
                                                ace_idx_p3, slot_p3)
                                    except Exception:
                                        pass
                                break

                            if retry == 0:
                                logging.info(
                                    '[feed_loading] phase3: retry:0 - skip cleanup, advance to retry 1')
                                continue

                            scheme = (getattr(self.ace, 'wiggle_scheme', 'EEEEE')
                                      if self.ace is not None else 'EEEEE')
                            wiggle_idx = retry - 1
                            if 0 <= wiggle_idx < len(scheme):
                                wiggle_char = scheme[wiggle_idx]
                            elif scheme:
                                wiggle_char = scheme[-1]
                            else:
                                wiggle_char = 'E'
                            phase3_wiggles_used += wiggle_char

                            self.gcode.run_script_from_command("ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD\r\n")
                            self.toolhead.wait_moves()

                            if wiggle_char == 'A':
                                retract_mm = self.ace.extrusion_retry_retract_a if self.ace is not None else 50
                            else:
                                retract_mm = self.ace.extrusion_retry_retract if self.ace is not None else 30
                            push_mm = retract_mm + (20 if filament_soft else 10)

                            wiggle_log = getattr(self.ace, '_wiggle_log', None) if self.ace else None
                            head_for_log = self.filament_ch[ch]
                            if wiggle_log is not None:
                                wiggle_log.info(
                                    'phase3 head=%d ace=%s slot=%s scheme=%s retry=%d type=%s retract=%d push=%d START',
                                    head_for_log, ace_idx_p3, slot_p3,
                                    scheme, retry, wiggle_char,
                                    retract_mm, push_mm)

                            if (self.ace is not None and ace_idx_p3 is not None
                                    and slot_p3 is not None):
                                try:
                                    def _phase3_stop_cb(self, response):
                                        pass
                                    self.ace.send_request_to(ace_idx_p3,
                                        {"method": "stop_feed_assist",
                                         "params": {"index": slot_p3}},
                                        _phase3_stop_cb)
                                    self.ace._feed_assist_per_ace[ace_idx_p3] = -1
                                    if ace_idx_p3 == self.ace._active_device_index:
                                        self.ace._feed_assist_index = -1
                                    self.ace.wait_ace_ready()
                                    self.ace._fa_trace(
                                        'phase3 wiggle %s: stop FA slot=%d on ACE %d'
                                        % (wiggle_char, slot_p3, ace_idx_p3))
                                except Exception as e:
                                    logging.info(
                                        '[multiACE] phase3 stop_feed_assist failed: %s' % e)

                            if wiggle_char == 'A':

                                if (self.ace is not None and ace_idx_p3 is not None
                                        and slot_p3 is not None):
                                    head_idx_for_ext = self.filament_ch[ch]
                                    extruder_name = ('extruder' if head_idx_for_ext == 0
                                                     else 'extruder%d' % head_idx_for_ext)
                                    extruder_disabled = False
                                    try:
                                        self.gcode.run_script_from_command(
                                            "SET_STEPPER_ENABLE STEPPER=%s ENABLE=0\r\n" % extruder_name)
                                        self.toolhead.wait_moves()
                                        extruder_disabled = True
                                        self.ace._fa_trace(
                                            'phase3 A wiggle: disabled %s for free-pull'
                                            % extruder_name)
                                    except Exception as e:
                                        logging.info(
                                            '[multiACE] phase3 extruder disable failed: %s' % e)
                                    try:
                                        retract_speed = getattr(self.ace, 'retract_speed', 25)
                                        feed_speed = getattr(self.ace, 'feed_speed', 25)
                                        def _phase3_a_cb(self, response):
                                            pass
                                        self.ace.send_request_to(ace_idx_p3,
                                            {"method": "unwind_filament",
                                             "params": {"index": slot_p3,
                                                        "length": retract_mm,
                                                        "speed": retract_speed}},
                                            _phase3_a_cb)
                                        self.reactor.pause(self.reactor.monotonic()
                                            + (retract_mm / max(1, retract_speed)) + 0.5)
                                        self.ace.wait_ace_ready()
                                        self.ace.send_request_to(ace_idx_p3,
                                            {"method": "feed_filament",
                                             "params": {"index": slot_p3,
                                                        "length": push_mm,
                                                        "speed": feed_speed}},
                                            _phase3_a_cb)
                                        self.reactor.pause(self.reactor.monotonic()
                                            + (push_mm / max(1, feed_speed)) + 0.5)
                                        self.ace.wait_ace_ready()
                                        self.ace._fa_trace(
                                            'phase3 ACE wiggle done slot=%d on ACE %d (retract=%d push=%d)'
                                            % (slot_p3, ace_idx_p3, retract_mm, push_mm))
                                    except Exception as e:
                                        logging.info(
                                            '[multiACE] phase3 ACE wiggle failed: %s' % e)
                                    finally:
                                        if extruder_disabled:
                                            try:
                                                self.gcode.run_script_from_command(
                                                    "SET_STEPPER_ENABLE STEPPER=%s ENABLE=1\r\n" % extruder_name)
                                                self.toolhead.wait_moves()
                                                self.ace._fa_trace(
                                                    'phase3 A wiggle: re-enabled %s'
                                                    % extruder_name)
                                            except Exception as e:
                                                logging.warning(
                                                    '[multiACE] phase3 extruder re-enable failed: %s' % e)
                            else:

                                self.gcode.run_script_from_command(
                                    "G1 E-%d F600\r\n" % retract_mm)
                                self.toolhead.wait_moves()
                                self.reactor.pause(self.reactor.monotonic() + 0.2)

                                if (self.ace is not None and ace_idx_p3 is not None
                                        and slot_p3 is not None):
                                    try:
                                        self.ace._arm_fa_for(ace_idx_p3, slot_p3)
                                        self.ace._fa_trace(
                                            'phase3 E wiggle: restart FA slot=%d on ACE %d before push'
                                            % (slot_p3, ace_idx_p3))
                                    except Exception as e:
                                        logging.info(
                                            '[multiACE] phase3 FA restart failed: %s' % e)

                                if filament_soft:
                                    self.gcode.run_script_from_command(
                                        "G1 E%d F200\r\n" % push_mm)
                                else:
                                    self.gcode.run_script_from_command(
                                        "G1 E%d F480\r\n" % push_mm)
                                self.toolhead.get_last_move_time()
                                self.reactor.pause(self.reactor.monotonic() + 0.7)

                            if retry < 5:
                                duty = max(1.0, duty + 0.05)
                            else:
                                period = max(0.12, period + 0.01)
                            self.motor.run_one_cycle(motor_dir, duty, period)
                            self.toolhead.wait_moves()

                            logging.info(f"[feed_loading] phase3: retry:{retry}, duty:{duty}, period:{period}, wiggle={wiggle_char}")
                            if wiggle_log is not None:
                                wiggle_log.info(
                                    'phase3 head=%d ace=%s slot=%s retry=%d type=%s DONE',
                                    head_for_log, ace_idx_p3, slot_p3,
                                    retry, wiggle_char)
                    except Exception as e:
                        self.channel_error[ch] = FEED_ERR_MOVE_EXTRUDE
                        self.exception_code[ch] = 51
                        logging.error("[feed_loading] phase3: except rawinfo: %s", str(e))
                        _wlog = (getattr(self.ace, '_wiggle_log', None)
                                 if self.ace else None)
                        if _wlog is not None:
                            _wlog.info(
                                'phase3 head=%d ace=%s slot=%s FAILED (exception) wiggles_used=%s err=%s',
                                self.filament_ch[ch], ace_idx_p3, slot_p3,
                                phase3_wiggles_used or '(none)', str(e))
                        self._snapshot_inner_resume_state()
                        raise ValueError('logic error!')
                    finally:
                        self._hang_neutral(ch)

                    if extruded == False:
                        self.channel_error[ch] = FEED_ERR_MOVE_EXTRUDE
                        self.exception_code[ch] = 51
                        _wlog = (getattr(self.ace, '_wiggle_log', None)
                                 if self.ace else None)
                        if _wlog is not None:
                            _wlog.info(
                                'phase3 head=%d ace=%s slot=%s FAILED (no movement) wiggles_used=%s '
                                'last_coil_delta=%d',
                                self.filament_ch[ch], ace_idx_p3, slot_p3,
                                phase3_wiggles_used or '(none)',
                                coil_freq_end_max - coil_freq_end_min)
                        self._snapshot_inner_resume_state()
                        raise ValueError('logic error!')

                    self._set_channel_state(ch, FEED_STA_LOAD_FLUSHING)
                    try:
                        self.toolhead.wait_moves()
                        # Pass LENGTH= only when a purge length is configured
                        # (swap_purge_length / Pro override); 0 omits it so the
                        # stock macro uses its default (80mm).
                        _purge_len = self.ace.get_purge_length() if self.ace else 0
                        _flush_cmd = ("INNER_FLUSH_FILAMENT TEMP=%d SOFT=%d NOZZLE_DIAMETER=%f" %
                                      (filament_feed_temp, int(filament_soft),
                                       self.toolhead.get_extruder().nozzle_diameter))
                        if _purge_len and _purge_len > 0:
                            _flush_cmd += " LENGTH=%d" % _purge_len
                        self.gcode.run_script_from_command(_flush_cmd + "\r\n")
                        self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                        raise ValueError('custom gcode error!')

                    self.channel_error[ch] = FEED_OK
                    self._set_channel_state(ch, FEED_STA_LOAD_FINISH, True)

                    if self.ace is not None and self.ace.head_uses_ace(self.filament_ch[ch]):
                        head_idx = self.filament_ch[ch]
                        # Record the ACE/slot the head was ACTUALLY loaded from.
                        # PRECEDENCE: the explicit head_source ace/slot (set by
                        # ACE_LOAD_HEAD/SWAP with ACE=n/SLOT=s) WINS - the gcode's
                        # per-swap ACE argument is the truth. Stamping
                        # head_ace_for (config wiring) here overwrote an
                        # ACE_LOAD_HEAD's explicit ACE with the config default,
                        # so a head loaded from ACE 0 got head_source=ACE 1 and
                        # FA was armed on the wrong ACE (split-brain). Fall back
                        # to head_ace_for / _ace_slot_for_head only for a display
                        # load with no head_source. Multi/normal: byte-identical.
                        # Fallback (display load, no head_source) = the ACTIVE
                        # device (already repointed by _ensure_active_ace_for_head
                        # to the fed ACE in both modes); NOT head_ace_for, which
                        # returns the head index in multi (!= active, a regression).
                        _src = self.ace._head_source.get(head_idx)
                        if _src is not None and isinstance(_src.get('ace_index'), int):
                            ace_idx = int(_src['ace_index'])
                        else:
                            ace_idx = self.ace._active_device_index
                        if _src is not None and isinstance(_src.get('slot'), int):
                            ace_slot = int(_src['slot'])
                        else:
                            ace_slot = self.ace._ace_slot_for_head(head_idx)
                        slot_info = (self.ace._info_per_ace.get(ace_idx, {}) or {}
                                     ).get('slots', [{}] * 4)
                        if ace_slot < len(slot_info):
                            si = slot_info[ace_slot]
                        else:
                            si = {}
                        _ident = {
                            'ace_index': ace_idx,
                            'slot': ace_slot,
                            'type': si.get('type', 'PLA'),
                            'color': self.ace.rgb2hex(*si.get('color', (0, 0, 0))),
                            'brand': si.get('brand', 'Generic'),
                        }
                        # V2 identity snapshot: this stamp fires on
                        # EVERY FEED_AUTO load (incl. the quad reload). Resolve
                        # the override in (declared truth) and inherit the lane
                        # like the others; getattr-guarded because ace.py is
                        # NOT in the bundle sha and can be older than this file.
                        _ovl = getattr(self.ace, '_overlay_override', None)
                        if _ovl is not None:
                            _ident = _ovl(ace_idx, ace_slot, _ident)
                        _inh = getattr(self.ace, '_inherit_prev_capture', None)
                        if _inh is not None:
                            _ident = _inh(head_idx, ace_idx, ace_slot, _ident)
                        self.ace._head_source[head_idx] = _ident
                        self.ace._save_head_source()
                        self.ace._ghost_heads.discard(head_idx)
                        logging.info('[multiACE] FEED_AUTO LOAD: head_source[%d] -> ACE %d / Slot %d' % (
                            head_idx, ace_idx, ace_slot))

                    self.gcode.respond_raw('ok')

                except:
                    self.toolhead.wait_moves()
                    self.channel_error_state[ch] = self.channel_state[ch]
                    if self.channel_error[ch] == FEED_OK:
                        self.channel_error[ch] = FEED_ERR
                    self._set_channel_state(ch, FEED_STA_LOAD_FAIL)
                    raise

                finally:
                    self.gcode.run_script_from_command("M107\r\n")
                    self.gcode.run_script_from_command("M104 S0\r\n")

                    if fa_gate_opened and self.ace is not None:
                        self.ace._auto_feed_enabled = False
                        self.ace._fa_context = 'idle'
                        self.ace._fa_trace('gate CLOSE (context=idle) via FEED_ACT_LOAD finally')
                        try:
                            self.ace._disable_feed_assist_all()
                        except Exception as fa_e:
                            logging.info(
                                '[multiACE] FA disable after load failed: %s' % fa_e)

            elif action == FEED_ACT_UNLOAD:

                # Head mode: retract goes to this head's wired ACE - repoint the
                # active device before the unwind/retract (the retract path uses
                # _active_device_index). No-op in multi/normal.
                if self.ace is not None:
                    self.ace._ensure_active_ace_for_head(self.filament_ch[ch])
                    # Calibration cancel check point (both unload blocks).
                    # No-op unless a calibration PREPARATION unload is running;
                    # then it raises so the web abort actually takes effect at
                    # the next wait instead of after the full sequence.
                    self.ace._check_calibration_unload_cancel()

                # Manual/TPU head: do NOT arm the V2 rollback-assist - it spins
                # the ACE motor (the rollback pull during unload). A manual or
                # feeder head has no ACE filament path, so this would move the
                # ACE for nothing. The extruder INNER tip-pull below still runs.
                if self.ace is not None \
                        and self.ace.head_uses_ace(self.filament_ch[ch]):
                    try:
                        self.ace._v2_arm_fa_for_unload(self.filament_ch[ch])
                    except Exception as fa_e:
                        logging.info(
                            '[multiACE] V2 arm FA for FEED_ACT_UNLOAD failed: %s' % fa_e)

                if self.ace is not None:
                    self.ace._disable_feed_assist_all()

                self.exception_code[ch] = 70
                if stage not in [None, FEED_UNLOAD_STAGE_PREPARE, FEED_UNLOAD_STAGE_DOING,
                                 FEED_UNLOAD_STAGE_CANCEL]:
                    logging.error("[feed][unload] stage parameter error!\r\n")
                    self.toolhead.wait_moves()
                    self.channel_error[ch] = FEED_ERR_PARAMETER
                    self._set_channel_state(ch, FEED_STA_UNLOAD_FAIL)
                    raise ValueError('parameter error!')

                self.manual_feeding[ch] = False
                self.channel_error_state[ch] = FEED_STA_NONE
                if stage == FEED_UNLOAD_STAGE_PREPARE:
                    try:

                        self._set_channel_state(ch, FEED_STA_UNLOAD_PREPARE, True)

                        # Heat-soak reset: start cooling the swap head NOW with a
                        # non-blocking M104 so it cools DURING homing/T-switch/
                        # move-to-discard; the blocking TEMPERATURE_WAIT happens
                        # just before the re-heat below, so little extra time is
                        # spent. Mid-print the melt zone is heat-soaked - a firm
                        # column lets INNER grip+extract the plug and gives the
                        # probe an honest signal (a stuck filament otherwise reads
                        # falsely "free"). Swap-context only; 0 = off.
                        _precool = (FEED_SWAP_PRECOOL_TEMP
                                    if (self.ace is not None and
                                        getattr(self.ace, '_swap_in_progress', False))
                                    else 0)
                        if _precool > 0:
                            self.gcode.run_script_from_command("M104 S%d\r\n" % _precool)
                            logging.info(
                                "[feed][unload] pre-cool start: M104 S%d "
                                "(cools during homing/move)", _precool)

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HOMING)
                            if self._check_homing_xy() != True:
                                self.gcode.run_script_from_command("G28 X Y\r\n")
                                self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_HOME
                            raise

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_PICKING)
                            self.gcode.run_script_from_command("T%d A0\r\n" % (self.filament_ch[ch]))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_SWITCH
                            raise

                        try:
                            self.gcode.run_script_from_command("MOVE_TO_DISCARD_FILAMENT_POSITION\r\n")
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE
                            raise

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HEATING)
                            if _precool > 0:
                                # Block here (not earlier) so cooling overlapped the
                                # homing/T-switch/move above; now finish cooling,
                                # then re-heat for the tip-form pull.
                                self.gcode.run_script_from_command(
                                    'TEMPERATURE_WAIT SENSOR="%s" MAXIMUM=%d\r\n'
                                    % (self.toolhead.get_extruder().get_name(), _precool))
                                logging.info(
                                    "[feed][unload] pre-cool reached <=%d C, re-heating "
                                    "for tip-form", _precool)
                            self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_unload_temp))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_HEAT
                            raise

                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_UNLOAD_HEAT_FINISH)

                    except:
                        self.toolhead.wait_moves()
                        self.channel_error_state[ch] = self.channel_state[ch]
                        if self.channel_error[ch] == FEED_OK:
                            self.channel_error[ch] = FEED_ERR
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FAIL)
                        raise

                elif stage == FEED_UNLOAD_STAGE_DOING:
                    try:

                        _stable_states = [
                            FEED_STA_UNLOAD_HEAT_FINISH,
                            FEED_STA_UNLOAD_FINISH,
                            FEED_STA_LOAD_FINISH,
                            FEED_STA_LOAD_FAIL,
                            FEED_STA_PRELOAD_FINISH,
                            FEED_STA_PRELOAD_FAIL,
                            FEED_STA_NONE,
                            FEED_STA_INITED,
                            FEED_STA_WAIT_INSERT,
                        ]
                        if self.channel_state[ch] not in _stable_states:
                            self.channel_error[ch] = FEED_ERR_STATE_MISMATCH
                            raise ValueError('state mismatch!')

                        if self.channel_state[ch] != FEED_STA_UNLOAD_HEAT_FINISH:
                            logging.info("[feed][unload] ignoring STAGE=doing, no prepare phase (state: %s)" % self.channel_state[ch])
                            self.channel_error[ch] = FEED_OK
                            return

                        sensor_name = "e%d_filament" % self.filament_ch[ch]
                        self.gcode.run_script_from_command(
                            "SET_FILAMENT_SENSOR SENSOR=%s ENABLE=1\r\n" % sensor_name)
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_DOING)
                            self.toolhead.wait_moves()
                            self.ace._run_tipform(
                                self.filament_ch[ch], filament_unload_temp,
                                int(filament_soft),
                                self.toolhead.get_extruder().nozzle_diameter)
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                            raise ValueError('custom gcode error!')

                        # Manual/TPU head: INNER above already pulled the tip out
                        # of the hotend. Skip the ACE retract loop (no ACE motion)
                        # - the user removes the filament by hand (there is no
                        # separate manual-unload routine). The finish below still
                        # runs (M104 S0, state, head_source clear).
                        if self.ace is not None \
                                and self.ace.head_uses_ace(self.filament_ch[ch]):
                            # Swap unload tip handling. The main INNER pull just
                            # ran hot. With swap_cool_probe the FORWARD-PROBE runs
                            # cool (swap_probe_temp) so the G1 E10 push does not
                            # re-melt/swell the just-formed tip. On a still-detected
                            # retry the RE-UNLOAD goes hot again (full feed temp,
                            # like the first unload) so INNER can free a stuck tip,
                            # then drops back to the cool probe temp - the next
                            # retract is the free cooldown. The probe push is always
                            # pulled back (a little extra) when filament is still
                            # present, so molten material never piles up forward
                            # across retries.
                            cool_probe = (getattr(self.ace, 'swap_cool_probe', False)
                                          and getattr(self.ace, '_swap_in_progress', False))
                            probe_temp = self._swap_probe_temp(
                                cool_probe, filament_unload_temp)
                            probe_push = getattr(self.ace, 'swap_probe_push', 5)
                            probe_pull = (probe_push * 3) // 2   # push +50%
                            # Heat-soak reset temp for the retry re-unload (0=off).
                            precool_temp = (FEED_SWAP_PRECOOL_TEMP
                                            if getattr(self.ace, '_swap_in_progress', False) else 0)
                            if getattr(self.ace, '_swap_in_progress', False):
                                self.gcode.run_script_from_command(
                                    "M104 S%d\r\n" % probe_temp)
                                logging.info(
                                    "[feed][unload] swap forward-probe at %d C "
                                    "(cool_probe=%s, unload_temp=%d)",
                                    probe_temp, cool_probe, filament_unload_temp)
                            else:
                                # Non-swap: INNER leaves the heater at 0 and the
                                # slow ACE retract below cools the head under
                                # min_extrude_temp, so the forward probe G1 E threw
                                # ("forward probe failed") and forced an extra
                                # re-unload. Hold the material temp through the
                                # retract so attempt 1 can probe (verified: head
                                # had cooled to ~140 C at the probe).
                                self.gcode.run_script_from_command(
                                    "M104 S%d\r\n" % filament_unload_temp)
                                logging.info(
                                    "[feed][unload] forward-probe pre-warm to %d C "
                                    "(non-swap)", filament_unload_temp)
                            head_idx = self.filament_ch[ch]
                            source = self.ace._head_source.get(head_idx)
                            if source and source['ace_index'] != self.ace._active_device_index:
                                logging.info('[multiACE] FEED_AUTO UNLOAD: switching to ACE %d for retract' % source['ace_index'])
                                self.ace._switch_ace_for_head_target(source['ace_index'])

                            unload_max = self.ace.unload_retry if self.ace is not None else 3
                            unload_ok = False
                            unload_reason = 'toolhead'
                            _ace_slot = self.ace._ace_slot_for_head(head_idx)
                            if _ace_slot != head_idx:
                                logging.info(
                                    "[feed][unload] head %d unloads to ACE slot %d (slot!=head)",
                                    head_idx, _ace_slot)
                            # Full-unload-safe + faster: retract only a short
                            # probe length first and verify the toolhead sensor
                            # cleared; pull the bulk "rest" back to the ACE only
                            # once verified (see post-loop). A stuck filament is
                            # caught after ~FEED_UNLOAD_PROBE_RETRACT mm instead
                            # of the full retract, and each retry re-pulls short.
                            # Hardware-agnostic (V1+V2): the verify is the
                            # toolhead motion sensor, no ACE device sensor.
                            _full_retract = self.ace._resolve_retract_length(_ace_slot)
                            _short_retract = min(FEED_UNLOAD_PROBE_RETRACT, _full_retract)
                            _retract_speed = self.ace.get_retract_speed(
                                self.ace._active_device_index)
                            # Filament ran out / broke ABOVE the ACE gate: the
                            # source slot reads EMPTY while the toolhead still
                            # holds filament. The tail is out of the ACE gears,
                            # so NO retract can ever move it - skip the
                            # retract/probe
                            # retries, tell the user to extract manually. Reads
                            # the ACE INPUT gate of the head's OWN ACE (per-ACE
                            # list, == 0 is GATE_EMPTY; GATE_UNKNOWN=-1 never
                            # trips this). NOT a probe gate:
                            # the motion-sensor probe verify of the normal path
                            # is untouched, this only skips work that is
                            # physically impossible with no filament at the ACE.
                            _gate_empty = False
                            try:
                                _gate_ace = (source['ace_index'] if source
                                             else self.ace._active_device_index)
                                _gates = self.ace._gate_status_per_ace.get(
                                    _gate_ace) or []
                                _gate_empty = (_ace_slot < len(_gates)
                                               and _gates[_ace_slot] == 0)
                            except Exception:
                                _gate_empty = False
                            if _gate_empty:
                                unload_max = 0
                                self.ace.log_error(self.ace._t(
                                    'msg.unload_gate_empty',
                                    head=self.ace._disp(head_idx),
                                    ace=self.ace._disp(_gate_ace),
                                    slot=self.ace._disp(_ace_slot)))
                            for unload_attempt in range(unload_max):
                                self.ace._check_calibration_unload_cancel()
                                # Wait ready BEFORE the span wrapper: its
                                # sampling timer must never hammer get_feed_info
                                # through a wait_ace_ready stall (the 0.1s tick
                                # saturated the V2 writer queue, the status
                                # cache froze at 'busy' and the wait ran its
                                # full 60s into a needless reconnect - and the
                                # span window covered the whole wait).
                                # _retract's internal wait is then instant.
                                self.ace.wait_ace_ready()
                                _usp = self.ace._retract_with_decoder_span(
                                    self.ace._active_device_index, _ace_slot,
                                    lambda: self.ace._retract(
                                        _ace_slot, _short_retract,
                                        _retract_speed, head=head_idx))
                                self.ace.wait_ace_ready()
                                self.ace._check_calibration_unload_cancel()
                                self._unload_dec_log(
                                    head_idx, _ace_slot, 'short', _short_retract,
                                    _usp, unload_attempt + 1)

                                # Pin-first verify (unload_gpio default on):
                                # the raw presence pin reads the short
                                # retract's outcome directly (True=stuck,
                                # False=gone, transition-accurate) - the probe
                                # and
                                # its reheat to extrude temp (~10-25s/unload)
                                # are SKIPPED whenever the pin delivers a
                                # verdict. The probe still runs on the LAST
                                # attempt (the recovery-PAUSE verdict stays a
                                # real probe), when the pin is unreadable, and
                                # with unload_gpio: False (defective/dirty-pin
                                # escape hatch = pre-pin behaviour). A pin-
                                # verified clear syncs the motion helper via
                                # the OFFICIAL path (note_filament_present,
                                # like the bg engine) - never write
                                # filament_present directly.
                                self.reactor.pause(self.reactor.monotonic() + FEED_UNLOAD_TRIGGER_SETTLE)
                                _pin = None
                                if bool(getattr(self.ace, 'unload_gpio', True)):
                                    try:
                                        _pin = getattr(self.runout_sensor[ch],
                                                       'runout_buttun_state', None)
                                    except Exception:
                                        _pin = None
                                _last_attempt = (unload_attempt + 1 >= unload_max)
                                pushed = False
                                if _pin is False:
                                    logging.info(
                                        "[feed][gpio] head %d pin CLEAR after short "
                                        "retract - unload verified without probe "
                                        "(attempt %d/%d)",
                                        self.filament_ch[ch],
                                        unload_attempt + 1, unload_max)
                                    try:
                                        self.runout_sensor[ch].runout_helper.note_filament_present(False, True)
                                    except Exception:
                                        logging.info("[feed][gpio] motion-helper sync failed")
                                    unload_ok = True
                                    break
                                elif _pin is True and not _last_attempt:
                                    logging.info(
                                        "[feed][gpio] head %d pin still PRESENT after "
                                        "short retract - stuck, skipping probe, "
                                        "retry %d/%d - hot re-unload",
                                        self.filament_ch[ch],
                                        unload_attempt + 1, unload_max)
                                else:
                                    # Forward probe (pin unreadable / unload_gpio
                                    # off / last attempt): advances the extruder
                                    # to flip the motion sensor to "free" - the
                                    # classic unload verify.
                                    try:
                                        if not getattr(self.ace, '_swap_in_progress', False):
                                            # Guarantee extrudability before the probe
                                            # (> min_extrude_temp); the pre-warm above
                                            # is non-blocking, this waits if the retract
                                            # was faster than the reheat.
                                            self.gcode.run_script_from_command(
                                                'TEMPERATURE_WAIT SENSOR="%s" MINIMUM=%d\r\n'
                                                % (self.toolhead.get_extruder().get_name(),
                                                   filament_unload_temp))
                                        self.gcode.run_script_from_command("M83\r\n")
                                        self.gcode.run_script_from_command("G1 E%d F400\r\n" % probe_push)
                                        self.toolhead.wait_moves()
                                        pushed = True
                                    except:
                                        logging.info("[feed][unload] forward probe failed")
                                    self.reactor.pause(self.reactor.monotonic() + FEED_UNLOAD_TRIGGER_SETTLE)
                                    try:
                                        _pin = getattr(self.runout_sensor[ch],
                                                       'runout_buttun_state', None)
                                    except Exception:
                                        _pin = None
                                    logging.info(
                                        "[feed][gpio] head %d probe verify: "
                                        "runout_buttun_state=%s",
                                        self.filament_ch[ch], _pin)
                                    _cleared = not self.runout_sensor[ch].get_status(0)['filament_detected']
                                    if (_cleared and _pin is True
                                            and bool(getattr(self.ace, 'unload_gpio', True))):
                                        # Veto: motion says "free" but the pin
                                        # still sees filament = a false-
                                        # positive clear (gear slip reads like
                                        # gone). Treat as still detected.
                                        logging.info(
                                            "[feed][gpio] head %d VETO: probe cleared "
                                            "but pin still PRESENT - false-positive "
                                            "clear (gear slip?), treating as stuck",
                                            self.filament_ch[ch])
                                        _cleared = False
                                    if _cleared:
                                        logging.info("[feed][unload] sensor cleared (attempt %d/%d)",
                                                     unload_attempt + 1, unload_max)
                                        unload_ok = True
                                        break
                                    if not _last_attempt:
                                        logging.info("[feed][unload] sensor still detected, "
                                                     "retry %d/%d - pull-back + hot re-unload",
                                                     unload_attempt + 1, unload_max)

                                if _last_attempt:
                                    break
                                # If we pushed (false-clear check turned up
                                # filament), pull it back +50% before re-unloading
                                # so nothing accumulates forward in the melt zone.
                                if pushed:
                                    try:
                                        self.gcode.run_script_from_command("M83\r\n")
                                        self.gcode.run_script_from_command("G1 E-%d F400\r\n" % probe_pull)
                                        self.toolhead.wait_moves()
                                        logging.info("[feed][unload] probe push %dmm pulled back %dmm (attempt %d/%d)",
                                                     probe_push, probe_pull, unload_attempt + 1, unload_max)
                                    except:
                                        logging.info("[feed][unload] probe pull-back failed")
                                try:
                                    # Heat-soak reset before the hot re-unload: cool the
                                    # head first, then re-heat, so INNER pulls from a
                                    # parked/cold-like gradient (firm column) and grips
                                    # the plug instead of stripping soft heat-soaked
                                    # filament. Swap-context only; no-op at 0.
                                    if precool_temp > 0:
                                        self.gcode.run_script_from_command("M104 S%d\r\n" % precool_temp)
                                        self.gcode.run_script_from_command(
                                            'TEMPERATURE_WAIT SENSOR="%s" MAXIMUM=%d\r\n'
                                            % (self.toolhead.get_extruder().get_name(), precool_temp))
                                        logging.info("[feed][unload] retry %d/%d: pre-cool to <=%d C (heat-soak reset)",
                                                     unload_attempt + 1, unload_max, precool_temp)
                                    # Re-unload HOT so INNER/tip-form can free a
                                    # stuck tip - max(load, unload): the freeing
                                    # pull keeps full power even when the normal
                                    # tip-form temp is set cooler.
                                    self.gcode.run_script_from_command("M109 S%d\r\n"
                                        % (max(filament_feed_temp_db, filament_unload_temp)))
                                    self.toolhead.wait_moves()
                                    self.ace._run_tipform(
                                        self.filament_ch[ch],
                                        max(filament_feed_temp_db, filament_unload_temp),
                                        int(filament_soft),
                                        self.toolhead.get_extruder().nozzle_diameter)
                                    self.toolhead.wait_moves()
                                    # Re-warm after INNER (it zeroes the heater) so the next
                                    # attempt's forward-probe TEMPERATURE_WAIT MINIMUM cannot
                                    # hang on a target=0 heater. Unconditional: probe_temp ==
                                    # filament_feed_temp on the non-swap path (was gated on
                                    # cool_probe -> non-swap retries stuck at 0, gcode hung).
                                    self.gcode.run_script_from_command("M104 S%d\r\n" % probe_temp)
                                except:
                                    logging.info("[feed][unload] toolhead unload retry failed")
                            # Verified clear of the toolhead via the short
                            # probe-retract: now pull the rest of the configured
                            # retract length back to the ACE (full-unload). Not
                            # re-probed - the filament is confirmed out.
                            if unload_ok:
                                _rest = _full_retract - _short_retract
                                if _rest > 0:
                                    # Passive dock wait -> dwell-fan window
                                    # (swap-gated inside the helper; OFF is
                                    # backstopped in cmd_ACE_SWAP_HEAD's
                                    # finally if the retract raises).
                                    self.ace._dwell_fan(True)
                                    # Wait ready BEFORE the span wrapper (see
                                    # the short-retract note above - this rest
                                    # site is where the 60s stall actually hit).
                                    self.ace.wait_ace_ready()
                                    _rsp = self.ace._retract_with_decoder_span(
                                        self.ace._active_device_index, _ace_slot,
                                        lambda: self.ace._retract(
                                            _ace_slot, _rest,
                                            _retract_speed, head=head_idx))
                                    self.ace.wait_ace_ready()
                                    self.ace._check_calibration_unload_cancel()
                                    self.ace._dwell_fan(False)
                                    self._unload_dec_log(
                                        head_idx, _ace_slot, 'rest', _rest,
                                        _rsp, '-')
                                    # The bulk is otherwise UNVERIFIED:
                                    # the toolhead pin proved the head is
                                    # clear, nothing proves the strand ever
                                    # reached the ACE. A flat decoder span is
                                    # the only signal that contradicts the
                                    # device's own 'success' - see the helper.
                                    if self._unload_dec_stalled(
                                            head_idx, _ace_slot, _rest, _rsp):
                                        unload_ok = False
                                        unload_reason = 'bowden_stall'
                            if not unload_ok:
                                logging.info("[feed][unload] filament genuinely stuck after %d unload attempts (sensor never cleared)", unload_max)

                            if self.ace is not None:
                                self.ace._last_unload_ok = unload_ok
                                self.ace._last_unload_reason = unload_reason
                                # Tip-form rollback-assist window is over: release
                                # the global _v2_active_rev_assist flag NOW. It was
                                # only cleared at the next _arm_fa_for, but in a
                                # swap the new head starts assisting before that
                                # clear -> a print/swap retract dispatched mode=3
                                # rollback on the printing slot -> ACE 2 Pro
                                # assist_error -> solid light / air print (field report).
                                self.ace._v2_active_rev_assist = False
                        self.gcode.run_script_from_command("M104 S0\r\n")
                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FINISH, True)

                        if self.ace is not None and self.ace.head_uses_ace(self.filament_ch[ch]):
                            head_idx = self.filament_ch[ch]
                            # Clear the mapping ONLY on a VERIFIED unload
                            # (_last_unload_ok = the probe's sensor truth, set
                            # right above). A genuinely-stuck unload also
                            # reaches UNLOAD_FINISH - wiping head_source then
                            # makes the retry fall back to a first-loaded-slot
                            # guess and retract the WRONG slot.
                            if not getattr(self.ace, '_last_unload_ok', True):
                                logging.info('[multiACE] FEED_AUTO UNLOAD: unload '
                                             'NOT verified (stuck) - keeping '
                                             'head_source[%d] for the retry' % head_idx)
                            elif self.ace._head_source.get(head_idx) is not None:
                                self.ace._head_source[head_idx] = None
                                self.ace._save_head_source()
                                logging.info('[multiACE] FEED_AUTO UNLOAD: cleared head_source[%d]' % head_idx)

                            try:
                                self.ace._push_slot_rfid_to_extruder(head_idx)
                            except Exception:
                                pass

                    except:
                        self.toolhead.wait_moves()
                        self.channel_error_state[ch] = self.channel_state[ch]
                        self.gcode.run_script_from_command("M104 S0\r\n")
                        if self.channel_error[ch] == FEED_OK:
                            self.channel_error[ch] = FEED_ERR
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FAIL)
                        raise

                elif stage == FEED_UNLOAD_STAGE_CANCEL:
                    self.toolhead.wait_moves()
                    self.channel_error[ch] = FEED_OK
                    self._set_channel_state(ch, FEED_STA_UNLOAD_FAIL, True)
                    self.gcode.run_script_from_command("M104 S0\r\n")
                    if self.module_exist[ch] == True and self.config['auto_mode'][ch] == True:
                        if self._port[ch].get_filament_detected() == False:
                            self._set_channel_state(ch, FEED_STA_WAIT_INSERT)
                        else:
                            self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH)

                else:
                    try:

                        self._set_channel_state(ch, FEED_STA_UNLOAD_PREPARE, True)

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HOMING)
                            if self._check_homing_xy() != True:
                                self.gcode.run_script_from_command("G28 X Y\r\n")
                                self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_HOME
                            raise

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_PICKING)
                            self.gcode.run_script_from_command("T%d A0\r\n" % (self.filament_ch[ch]))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_SWITCH
                            raise

                        try:
                            self.gcode.run_script_from_command("MOVE_TO_DISCARD_FILAMENT_POSITION\r\n")
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE
                            raise

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HEATING)
                            self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_unload_temp))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_HEAT
                            raise

                        self.gcode.run_script_from_command(
                            "SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1\r\n"
                            % self.filament_ch[ch])

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_DOING)
                            self.toolhead.wait_moves()
                            self.ace._run_tipform(
                                self.filament_ch[ch], filament_unload_temp,
                                int(filament_soft),
                                self.toolhead.get_extruder().nozzle_diameter)
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                            raise ValueError('custom gcode error!')

                        # Manual/TPU head: INNER above already pulled the tip out
                        # of the hotend. Skip the ACE retract loop (no ACE motion)
                        # - the user removes the filament by hand (there is no
                        # separate manual-unload routine). The finish below still
                        # runs (M104 S0, state, head_source clear).
                        if self.ace is not None \
                                and self.ace.head_uses_ace(self.filament_ch[ch]):
                            # Swap unload tip handling. The main INNER pull just
                            # ran hot. With swap_cool_probe the FORWARD-PROBE runs
                            # cool (swap_probe_temp) so the G1 E10 push does not
                            # re-melt/swell the just-formed tip. On a still-detected
                            # retry the RE-UNLOAD goes hot again (full feed temp,
                            # like the first unload) so INNER can free a stuck tip,
                            # then drops back to the cool probe temp - the next
                            # retract is the free cooldown. The probe push is always
                            # pulled back (a little extra) when filament is still
                            # present, so molten material never piles up forward
                            # across retries.
                            cool_probe = (getattr(self.ace, 'swap_cool_probe', False)
                                          and getattr(self.ace, '_swap_in_progress', False))
                            probe_temp = self._swap_probe_temp(
                                cool_probe, filament_unload_temp)
                            probe_push = getattr(self.ace, 'swap_probe_push', 5)
                            probe_pull = (probe_push * 3) // 2   # push +50%
                            # Heat-soak reset temp for the retry re-unload (0=off).
                            precool_temp = (FEED_SWAP_PRECOOL_TEMP
                                            if getattr(self.ace, '_swap_in_progress', False) else 0)
                            if getattr(self.ace, '_swap_in_progress', False):
                                self.gcode.run_script_from_command(
                                    "M104 S%d\r\n" % probe_temp)
                                logging.info(
                                    "[feed][unload] swap forward-probe at %d C "
                                    "(cool_probe=%s, unload_temp=%d)",
                                    probe_temp, cool_probe, filament_unload_temp)
                            else:
                                # Non-swap: INNER leaves the heater at 0 and the
                                # slow ACE retract below cools the head under
                                # min_extrude_temp, so the forward probe G1 E threw
                                # ("forward probe failed") and forced an extra
                                # re-unload. Hold the material temp through the
                                # retract so attempt 1 can probe (verified: head
                                # had cooled to ~140 C at the probe).
                                self.gcode.run_script_from_command(
                                    "M104 S%d\r\n" % filament_unload_temp)
                                logging.info(
                                    "[feed][unload] forward-probe pre-warm to %d C "
                                    "(non-swap)", filament_unload_temp)
                            head_idx = self.filament_ch[ch]
                            source = self.ace._head_source.get(head_idx)
                            if source and source['ace_index'] != self.ace._active_device_index:
                                logging.info('[multiACE] FEED_AUTO UNLOAD: switching to ACE %d for retract' % source['ace_index'])
                                self.ace._switch_ace_for_head_target(source['ace_index'])

                            unload_max = self.ace.unload_retry if self.ace is not None else 3
                            unload_ok = False
                            unload_reason = 'toolhead'
                            _ace_slot = self.ace._ace_slot_for_head(head_idx)
                            if _ace_slot != head_idx:
                                logging.info(
                                    "[feed][unload] head %d unloads to ACE slot %d (slot!=head)",
                                    head_idx, _ace_slot)
                            # Full-unload-safe + faster: retract only a short
                            # probe length first and verify the toolhead sensor
                            # cleared; pull the bulk "rest" back to the ACE only
                            # once verified (see post-loop). A stuck filament is
                            # caught after ~FEED_UNLOAD_PROBE_RETRACT mm instead
                            # of the full retract, and each retry re-pulls short.
                            # Hardware-agnostic (V1+V2): the verify is the
                            # toolhead motion sensor, no ACE device sensor.
                            _full_retract = self.ace._resolve_retract_length(_ace_slot)
                            _short_retract = min(FEED_UNLOAD_PROBE_RETRACT, _full_retract)
                            _retract_speed = self.ace.get_retract_speed(
                                self.ace._active_device_index)
                            # Filament ran out / broke ABOVE the ACE gate: the
                            # source slot reads EMPTY while the toolhead still
                            # holds filament. The tail is out of the ACE gears,
                            # so NO retract can ever move it - skip the
                            # retract/probe
                            # retries, tell the user to extract manually. Reads
                            # the ACE INPUT gate of the head's OWN ACE (per-ACE
                            # list, == 0 is GATE_EMPTY; GATE_UNKNOWN=-1 never
                            # trips this). NOT a probe gate:
                            # the motion-sensor probe verify of the normal path
                            # is untouched, this only skips work that is
                            # physically impossible with no filament at the ACE.
                            _gate_empty = False
                            try:
                                _gate_ace = (source['ace_index'] if source
                                             else self.ace._active_device_index)
                                _gates = self.ace._gate_status_per_ace.get(
                                    _gate_ace) or []
                                _gate_empty = (_ace_slot < len(_gates)
                                               and _gates[_ace_slot] == 0)
                            except Exception:
                                _gate_empty = False
                            if _gate_empty:
                                unload_max = 0
                                self.ace.log_error(self.ace._t(
                                    'msg.unload_gate_empty',
                                    head=self.ace._disp(head_idx),
                                    ace=self.ace._disp(_gate_ace),
                                    slot=self.ace._disp(_ace_slot)))
                            for unload_attempt in range(unload_max):
                                self.ace._check_calibration_unload_cancel()
                                # Wait ready BEFORE the span wrapper: its
                                # sampling timer must never hammer get_feed_info
                                # through a wait_ace_ready stall (the 0.1s tick
                                # saturated the V2 writer queue, the status
                                # cache froze at 'busy' and the wait ran its
                                # full 60s into a needless reconnect - and the
                                # span window covered the whole wait).
                                # _retract's internal wait is then instant.
                                self.ace.wait_ace_ready()
                                _usp = self.ace._retract_with_decoder_span(
                                    self.ace._active_device_index, _ace_slot,
                                    lambda: self.ace._retract(
                                        _ace_slot, _short_retract,
                                        _retract_speed, head=head_idx))
                                self.ace.wait_ace_ready()
                                self.ace._check_calibration_unload_cancel()
                                self._unload_dec_log(
                                    head_idx, _ace_slot, 'short', _short_retract,
                                    _usp, unload_attempt + 1)

                                # Pin-first verify (unload_gpio default on):
                                # the raw presence pin reads the short
                                # retract's outcome directly (True=stuck,
                                # False=gone, transition-accurate) - the probe
                                # and
                                # its reheat to extrude temp (~10-25s/unload)
                                # are SKIPPED whenever the pin delivers a
                                # verdict. The probe still runs on the LAST
                                # attempt (the recovery-PAUSE verdict stays a
                                # real probe), when the pin is unreadable, and
                                # with unload_gpio: False (defective/dirty-pin
                                # escape hatch = pre-pin behaviour). A pin-
                                # verified clear syncs the motion helper via
                                # the OFFICIAL path (note_filament_present,
                                # like the bg engine) - never write
                                # filament_present directly.
                                self.reactor.pause(self.reactor.monotonic() + FEED_UNLOAD_TRIGGER_SETTLE)
                                _pin = None
                                if bool(getattr(self.ace, 'unload_gpio', True)):
                                    try:
                                        _pin = getattr(self.runout_sensor[ch],
                                                       'runout_buttun_state', None)
                                    except Exception:
                                        _pin = None
                                _last_attempt = (unload_attempt + 1 >= unload_max)
                                pushed = False
                                if _pin is False:
                                    logging.info(
                                        "[feed][gpio] head %d pin CLEAR after short "
                                        "retract - unload verified without probe "
                                        "(attempt %d/%d)",
                                        self.filament_ch[ch],
                                        unload_attempt + 1, unload_max)
                                    try:
                                        self.runout_sensor[ch].runout_helper.note_filament_present(False, True)
                                    except Exception:
                                        logging.info("[feed][gpio] motion-helper sync failed")
                                    unload_ok = True
                                    break
                                elif _pin is True and not _last_attempt:
                                    logging.info(
                                        "[feed][gpio] head %d pin still PRESENT after "
                                        "short retract - stuck, skipping probe, "
                                        "retry %d/%d - hot re-unload",
                                        self.filament_ch[ch],
                                        unload_attempt + 1, unload_max)
                                else:
                                    # Forward probe (pin unreadable / unload_gpio
                                    # off / last attempt): advances the extruder
                                    # to flip the motion sensor to "free" - the
                                    # classic unload verify.
                                    try:
                                        if not getattr(self.ace, '_swap_in_progress', False):
                                            # Guarantee extrudability before the probe
                                            # (> min_extrude_temp); the pre-warm above
                                            # is non-blocking, this waits if the retract
                                            # was faster than the reheat.
                                            self.gcode.run_script_from_command(
                                                'TEMPERATURE_WAIT SENSOR="%s" MINIMUM=%d\r\n'
                                                % (self.toolhead.get_extruder().get_name(),
                                                   filament_unload_temp))
                                        self.gcode.run_script_from_command("M83\r\n")
                                        self.gcode.run_script_from_command("G1 E%d F400\r\n" % probe_push)
                                        self.toolhead.wait_moves()
                                        pushed = True
                                    except:
                                        logging.info("[feed][unload] forward probe failed")
                                    self.reactor.pause(self.reactor.monotonic() + FEED_UNLOAD_TRIGGER_SETTLE)
                                    try:
                                        _pin = getattr(self.runout_sensor[ch],
                                                       'runout_buttun_state', None)
                                    except Exception:
                                        _pin = None
                                    logging.info(
                                        "[feed][gpio] head %d probe verify: "
                                        "runout_buttun_state=%s",
                                        self.filament_ch[ch], _pin)
                                    _cleared = not self.runout_sensor[ch].get_status(0)['filament_detected']
                                    if (_cleared and _pin is True
                                            and bool(getattr(self.ace, 'unload_gpio', True))):
                                        # Veto: motion says "free" but the pin
                                        # still sees filament = a false-
                                        # positive clear (gear slip reads like
                                        # gone). Treat as still detected.
                                        logging.info(
                                            "[feed][gpio] head %d VETO: probe cleared "
                                            "but pin still PRESENT - false-positive "
                                            "clear (gear slip?), treating as stuck",
                                            self.filament_ch[ch])
                                        _cleared = False
                                    if _cleared:
                                        logging.info("[feed][unload] sensor cleared (attempt %d/%d)",
                                                     unload_attempt + 1, unload_max)
                                        unload_ok = True
                                        break
                                    if not _last_attempt:
                                        logging.info("[feed][unload] sensor still detected, "
                                                     "retry %d/%d - pull-back + hot re-unload",
                                                     unload_attempt + 1, unload_max)

                                if _last_attempt:
                                    break
                                # If we pushed (false-clear check turned up
                                # filament), pull it back +50% before re-unloading
                                # so nothing accumulates forward in the melt zone.
                                if pushed:
                                    try:
                                        self.gcode.run_script_from_command("M83\r\n")
                                        self.gcode.run_script_from_command("G1 E-%d F400\r\n" % probe_pull)
                                        self.toolhead.wait_moves()
                                        logging.info("[feed][unload] probe push %dmm pulled back %dmm (attempt %d/%d)",
                                                     probe_push, probe_pull, unload_attempt + 1, unload_max)
                                    except:
                                        logging.info("[feed][unload] probe pull-back failed")
                                try:
                                    # Heat-soak reset before the hot re-unload: cool the
                                    # head first, then re-heat, so INNER pulls from a
                                    # parked/cold-like gradient (firm column) and grips
                                    # the plug instead of stripping soft heat-soaked
                                    # filament. Swap-context only; no-op at 0.
                                    if precool_temp > 0:
                                        self.gcode.run_script_from_command("M104 S%d\r\n" % precool_temp)
                                        self.gcode.run_script_from_command(
                                            'TEMPERATURE_WAIT SENSOR="%s" MAXIMUM=%d\r\n'
                                            % (self.toolhead.get_extruder().get_name(), precool_temp))
                                        logging.info("[feed][unload] retry %d/%d: pre-cool to <=%d C (heat-soak reset)",
                                                     unload_attempt + 1, unload_max, precool_temp)
                                    # Re-unload HOT so INNER/tip-form can free a
                                    # stuck tip - max(load, unload): the freeing
                                    # pull keeps full power even when the normal
                                    # tip-form temp is set cooler.
                                    self.gcode.run_script_from_command("M109 S%d\r\n"
                                        % (max(filament_feed_temp_db, filament_unload_temp)))
                                    self.toolhead.wait_moves()
                                    self.ace._run_tipform(
                                        self.filament_ch[ch],
                                        max(filament_feed_temp_db, filament_unload_temp),
                                        int(filament_soft),
                                        self.toolhead.get_extruder().nozzle_diameter)
                                    self.toolhead.wait_moves()
                                    # Re-warm after INNER (it zeroes the heater) so the next
                                    # attempt's forward-probe TEMPERATURE_WAIT MINIMUM cannot
                                    # hang on a target=0 heater. Unconditional: probe_temp ==
                                    # filament_feed_temp on the non-swap path (was gated on
                                    # cool_probe -> non-swap retries stuck at 0, gcode hung).
                                    self.gcode.run_script_from_command("M104 S%d\r\n" % probe_temp)
                                except:
                                    logging.info("[feed][unload] toolhead unload retry failed")
                            # Verified clear of the toolhead via the short
                            # probe-retract: now pull the rest of the configured
                            # retract length back to the ACE (full-unload). Not
                            # re-probed - the filament is confirmed out.
                            if unload_ok:
                                _rest = _full_retract - _short_retract
                                if _rest > 0:
                                    # Passive dock wait -> dwell-fan window
                                    # (swap-gated inside the helper; OFF is
                                    # backstopped in cmd_ACE_SWAP_HEAD's
                                    # finally if the retract raises).
                                    self.ace._dwell_fan(True)
                                    # Wait ready BEFORE the span wrapper (see
                                    # the short-retract note above - this rest
                                    # site is where the 60s stall actually hit).
                                    self.ace.wait_ace_ready()
                                    _rsp = self.ace._retract_with_decoder_span(
                                        self.ace._active_device_index, _ace_slot,
                                        lambda: self.ace._retract(
                                            _ace_slot, _rest,
                                            _retract_speed, head=head_idx))
                                    self.ace.wait_ace_ready()
                                    self.ace._check_calibration_unload_cancel()
                                    self.ace._dwell_fan(False)
                                    self._unload_dec_log(
                                        head_idx, _ace_slot, 'rest', _rest,
                                        _rsp, '-')
                                    # The bulk is otherwise UNVERIFIED:
                                    # the toolhead pin proved the head is
                                    # clear, nothing proves the strand ever
                                    # reached the ACE. A flat decoder span is
                                    # the only signal that contradicts the
                                    # device's own 'success' - see the helper.
                                    if self._unload_dec_stalled(
                                            head_idx, _ace_slot, _rest, _rsp):
                                        unload_ok = False
                                        unload_reason = 'bowden_stall'
                            if not unload_ok:
                                logging.info("[feed][unload] filament genuinely stuck after %d unload attempts (sensor never cleared)", unload_max)
                            if self.ace is not None:
                                self.ace._last_unload_ok = unload_ok
                                self.ace._last_unload_reason = unload_reason
                                # Tip-form rollback-assist window is over: release
                                # the global _v2_active_rev_assist flag NOW. It was
                                # only cleared at the next _arm_fa_for, but in a
                                # swap the new head starts assisting before that
                                # clear -> a print/swap retract dispatched mode=3
                                # rollback on the printing slot -> ACE 2 Pro
                                # assist_error -> solid light / air print (field report).
                                self.ace._v2_active_rev_assist = False
                        self.gcode.run_script_from_command("M104 S0\r\n")
                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FINISH, True)

                        if self.ace is not None and self.ace.head_uses_ace(self.filament_ch[ch]):
                            head_idx = self.filament_ch[ch]
                            # Clear the mapping ONLY on a VERIFIED unload
                            # (_last_unload_ok = the probe's sensor truth, set
                            # right above). A genuinely-stuck unload also
                            # reaches UNLOAD_FINISH - wiping head_source then
                            # makes the retry fall back to a first-loaded-slot
                            # guess and retract the WRONG slot.
                            if not getattr(self.ace, '_last_unload_ok', True):
                                logging.info('[multiACE] FEED_AUTO UNLOAD: unload '
                                             'NOT verified (stuck) - keeping '
                                             'head_source[%d] for the retry' % head_idx)
                            elif self.ace._head_source.get(head_idx) is not None:
                                self.ace._head_source[head_idx] = None
                                self.ace._save_head_source()
                                logging.info('[multiACE] FEED_AUTO UNLOAD: cleared head_source[%d]' % head_idx)

                            try:
                                self.ace._push_slot_rfid_to_extruder(head_idx)
                            except Exception:
                                pass

                    except:
                        self.toolhead.wait_moves()
                        self.channel_error_state[ch] = self.channel_state[ch]
                        self.gcode.run_script_from_command("M104 S0\r\n")
                        if self.channel_error[ch] == FEED_OK:
                            self.channel_error[ch] = FEED_ERR
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FAIL)
                        raise

            elif action == FEED_ACT_MANUAL_FEED:
                self.exception_code[ch] = 90
                if stage not in [FEED_MANUAL_STAGE_PREPARE, FEED_MANUAL_STAGE_EXTRUDE,
                                 FEED_MANUAL_STAGE_FLUSH, FEED_MANUAL_STAGE_FINISH,
                                 FEED_MANUAL_STAGE_CANCEL]:
                    logging.error("[feed][manual] stage parameter error!\r\n")
                    self.toolhead.wait_moves()
                    self.channel_error[ch] = FEED_ERR_PARAMETER
                    self._set_channel_state(ch, FEED_STA_MANUAL_FAIL)
                    raise ValueError('parameter error!')

                self.channel_error_state[ch] = FEED_STA_NONE
                if stage == FEED_MANUAL_STAGE_PREPARE:
                    try:
                        self._set_channel_state(ch, FEED_STA_MANUAL_PREPARE, True)
                        self.manual_feeding[ch] = True

                        try:
                            self._set_channel_state(ch, FEED_STA_MANUAL_HOMING)
                            if self._check_homing_xy() != True:
                                self.gcode.run_script_from_command("G28 X Y\r\n")
                                self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_HOME
                            raise

                        try:
                            self._set_channel_state(ch, FEED_STA_MANUAL_PICKING)
                            self.gcode.run_script_from_command("T%d A0\r\n" % (self.filament_ch[ch]))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_SWITCH
                            raise

                        try:
                            self.toolhead.wait_moves()
                            self.gcode.run_script_from_command("INNER_MANUAL_FEED_STAGE_PREPARE\r\n")
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                            raise ValueError('custom gcode error!')

                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_MANUAL_PREPARE_FINISH)

                    except:
                        self.manual_feeding[ch] = False
                        self.toolhead.wait_moves()
                        self.channel_error_state[ch] = self.channel_state[ch]
                        if self.channel_error[ch] == FEED_OK:
                            self.channel_error[ch] = FEED_ERR
                        self._set_channel_state(ch, FEED_STA_MANUAL_PREPARE_FAIL)
                        raise

                elif stage == FEED_MANUAL_STAGE_EXTRUDE:
                    try:

                        try:
                            self._set_channel_state(ch, FEED_STA_MANUAL_HEATING, True)
                            self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_HEAT
                            raise

                        try:
                            self._set_channel_state(ch, FEED_STA_MANUAL_EXTRUDING)
                            self.toolhead.wait_moves()
                            self.gcode.run_script_from_command("INNER_MANUAL_FEED_STAGE_EXTRUDE TEMP=%d SOFT=%d NOZZLE_DIAMETER=%f\r\n" %
                                                               (filament_feed_temp, int(filament_soft), self.toolhead.get_extruder().nozzle_diameter))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                            raise ValueError('custom gcode error!')

                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_MANUAL_EXTRUDE_FINISH)

                    except:
                        self.toolhead.wait_moves()
                        self.manual_feeding[ch] = False
                        self.channel_error_state[ch] = self.channel_state[ch]
                        if self.channel_error[ch] == FEED_OK:
                            self.channel_error[ch] = FEED_ERR
                        self._set_channel_state(ch, FEED_STA_MANUAL_EXTRUDE_FAIL)
                        raise

                elif stage == FEED_MANUAL_STAGE_FLUSH:
                    try:

                        try:
                            self.toolhead.wait_moves()
                            self._set_channel_state(ch, FEED_STA_MANUAL_FLUSHING, True)
                            self.gcode.run_script_from_command("INNER_MANUAL_FEED_STAGE_FLUSH TEMP=%d SOFT=%d NOZZLE_DIAMETER=%f\r\n" %
                                                (filament_feed_temp, int(filament_soft), self.toolhead.get_extruder().nozzle_diameter))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                            raise ValueError('custom gcode error!')

                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_MANUAL_FLUSH_FINISH)

                    except:
                        self.toolhead.wait_moves()
                        self.manual_feeding[ch] = False
                        self.channel_error_state[ch] = self.channel_state[ch]
                        if self.channel_error[ch] == FEED_OK:
                            self.channel_error[ch] = FEED_ERR
                        self._set_channel_state(ch, FEED_STA_MANUAL_FLUSH_FAIL)
                        raise

                elif stage == FEED_MANUAL_STAGE_FINISH:
                    self.manual_feeding[ch] = False
                    try:
                        self.toolhead.wait_moves()
                        self.gcode.run_script_from_command("INNER_MANUAL_FEED_STAGE_FINISH\r\n")
                        self.toolhead.wait_moves()
                    except:
                        logging.error("[feed][manual] stage: finish, gcode error\r\n")
                        self._set_channel_state(ch, FEED_STA_MANUAL_FAIL)
                        self.channel_error_state[ch] = self.channel_state[ch]
                        raise
                    self._set_channel_state(ch, FEED_STA_MANUAL_FINISH, True)

                    self.reactor.pause(self.reactor.monotonic() + 0.26)
                    self._set_channel_state(ch, FEED_STA_LOAD_FINISH, True)

                    if self.module_exist[ch] == True and self.config['auto_mode'][ch] == True:
                        if self._port[ch].get_filament_detected() == False:
                            self._set_channel_state(ch, FEED_STA_WAIT_INSERT)
                        else:
                            if self.runout_sensor[ch] is not None and \
                                    self.runout_sensor[ch].get_status(0)['enabled'] == True and \
                                    self.runout_sensor[ch].get_status(0)['filament_detected'] == True:
                                self._set_channel_state(ch, FEED_STA_LOAD_FINISH, True)
                            else:
                                self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH)

                elif stage == FEED_MANUAL_STAGE_CANCEL:
                    self.manual_feeding[ch] = False
                    try:
                        self.toolhead.wait_moves()
                        self.gcode.run_script_from_command("INNER_MANUAL_FEED_STAGE_CANCEL\r\n")
                        self.toolhead.wait_moves()
                    except:
                        logging.error("[feed][manual] stage: cancel, gcode error!\r\n")
                    self._set_channel_state(ch, FEED_STA_MANUAL_FAIL, True)
                    self.channel_error_state[ch] = self.channel_state[ch]

                    if self.module_exist[ch] == True and self.config['auto_mode'][ch] == True:
                        if self._port[ch].get_filament_detected() == False:
                            self._set_channel_state(ch, FEED_STA_WAIT_INSERT)
                        else:
                            self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH)
                else:
                    logging.error("[feed][manual] stage parameter error!\r\n")

        except:
            raise

        finally:
            self.channel_active = None

    def _emit_feed_pause(self, channel, key):
        # Localized multiACE pause message for a feed fault, rendered via the
        # ACE _t() catalog (1-based indices, ACE/Slot from head_source when
        # known). Returns the [multiACE]-prefixed text for the gcmd.error raise,
        # or None when not in multi mode / no ACE (caller falls back to the
        # technical text). No M117 (invisible on the Snapmaker touchscreen) and
        # no RESPOND (the raised gcmd.error already reaches popup + Fluidd).
        if self.ace is None or getattr(self.ace, '_ace_mode', '') != 'multi':
            return None
        head = self.filament_ch[channel]
        hd = self.ace._disp(head)
        src = (getattr(self.ace, '_head_source', None) or {}).get(head) or {}
        a, s = src.get('ace_index'), src.get('slot')
        loc = ' (ACE %d / Slot %d)' % (self.ace._disp(a), self.ace._disp(s)) \
            if a is not None and s is not None else ''
        return self.ace._t(key, head=hd, loc=loc)

    def _feed_load_fail_details(self, channel):
        # (detail, steps) for a failed LOAD, classified the way the SWAP path
        # classifies it: filament never reached the toolhead (no transport ->
        # spool/bowden/ACE) vs arrived but the nozzle will not extrude (no
        # flow -> clog) vs a dead ACE-side feed. This path used to answer ALL
        # of them with the flat 'Load jam ... reload via display/web', which
        # is wrong advice for a clog and is exactly the complaint the swap
        # split was built to fix - so both paths now share
        # ace._load_slip_details and word the same failure identically.
        #
        # (None, None) = cannot classify, caller keeps the flat message:
        # no ace object, not multi (the localized feed message is multi-only
        # by design, see _emit_feed_pause), no head_source to name an
        # ACE/slot, or an ace.py that predates the helper - ace.py is NOT
        # part of the bundle sha, so it can be older than this file.
        if self.ace is None or getattr(self.ace, '_ace_mode', '') != 'multi':
            return None, None
        detail_fn = getattr(self.ace, '_load_slip_details', None)
        if detail_fn is None:
            return None, None
        head = self.filament_ch[channel]
        src = (getattr(self.ace, '_head_source', None) or {}).get(head) or {}
        a, s = src.get('ace_index'), src.get('slot')
        if a is None or s is None:
            return None, None
        try:
            return detail_fn(head, int(a), int(s))
        except Exception as e:
            logging.info('[feed][load] classify failed, using flat message: %s' % e)
            return None, None

    def get_status(self, eventtime=None):
        filament_detected = []
        filament_detected.append(self._port[FEED_CHANNEL_1].get_filament_detected())
        filament_detected.append(self._port[FEED_CHANNEL_2].get_filament_detected())

        def _runout(ch):
            s = self.runout_sensor[ch]
            if s is None:
                return None
            try:
                return s.get_status(0).get('filament_detected')
            except Exception:
                return None

        # Stock auto-replenish SELF-check trap: it reads e_obj['filament_detected']
        # to decide "is the ran-out head still fed" and self-selects T->T when
        # True (print_task_config.py:959). For an ACE head filament_detected is
        # the ACE INPUT gate (FeedPort.get_filament_detected -> gate_status, a
        # multiACE mapping), a whole bowden upstream of the toolhead: on a break
        # between the gate and the toolhead the gate stays present while the
        # toolhead is empty -> stock self-selects the empty head and never rolls
        # to the twin ("auto replenish T1 -> T1" -> runout pause). Report
        # presence as the TOOLHEAD for the replenish self-check: force False only when THIS head is ACE-driven AND its own
        # toolhead sensor is explicitly not-present (_runout is False = the tail
        # physically left the toolhead; None/True never override). The replenish
        # DONOR scan still sees the gate for a healthy twin (toolhead present ->
        # no override), so only the ran-out head flips. Healthy print /
        # feeder / manual byte-identical.
        #
        # CALL-SITE GATE. The getter feeds THREE consumers: the replenish
        # SELF-check, the replenish DONOR gate, and the display exist flag. Only the self-check must see the
        # toolhead truth, and it reads EXACTLY during stock's
        # INNER_AUTO_REPLENISH_FILAMENT - which has ONE caller system-wide:
        # our own runout handler (filament_switch_sensor_ace), which sets
        # ace._replenish_check_active around the synchronous call. So the
        # override now lives in that window and nowhere else (broader
        # scopes left idle or paused heads at '/' and refused the display
        # load). Outside the window the field is the plain gate; the resume
        # gate CHECK_FILAMENT_RUNOUT reads the toolhead sensor directly,
        # never this field (verified against u1_firmware).
        # getattr on SELF too, not just on the ace: self.ace is created in
        # _ready(), and stock's print_task_config timer
        # (_update_filament_flags_timer_handle -> update_filament_exist_flag)
        # polls this get_status BEFORE that ready callback runs. A direct
        # self.ace read therefore raises AttributeError inside a reactor
        # timer = "Unhandled exception during run" -> printer shutdown at
        # startup - which is why every get_status field uses getattr; the
        # gate reads below dodge it via self._port[ch].ace, which IS set in
        # FeedPort.__init__.
        _ace = getattr(self, 'ace', None)
        _replenish = bool(getattr(_ace, '_replenish_check_active', False))
        # ... for EVERY ACE head in that window, not only the one that ran
        # out. The window covers two stock consumers, and both need the same
        # answer: the SELF-check asks "did the ran-out head really lose its
        # filament", the CANDIDATE scan asks "can this other head take the
        # print over" - a head with an empty toolhead must fail both.
        # No head_source condition: a head that ran out earlier and lost its
        # stale head_source would otherwise read the ACE input GATE, and
        # stock would take that EMPTY head as its donor.
        # Cost of the wider scope: while the window is open a deliberately
        # unloaded head reports no filament, so a display-exist poll landing
        # inside it shows '/' instead of '?' for a moment. Cosmetic and
        # self-healing (the flag is recomputed on the next event), and
        # the window is one synchronous call in our own runout handler.
        for _ch in (FEED_CHANNEL_1, FEED_CHANNEL_2):
            if _replenish and filament_detected[_ch] and _runout(_ch) is False:
                try:
                    if _ace.head_uses_ace(self.filament_ch[_ch]):
                        filament_detected[_ch] = False
                except Exception:
                    pass

        # in_ace = the gate of the slot that actually feeds this channel's head.
        # filament_ch[ch] is the head index; with a combiner the feeding
        # slot != head, so resolve via _ace_slot_for_head. Fallback = head index
        # -> slot==head byte-identical. (Display-status only, not a load gate.)
        in_ace_1 = (self.ace.gate_status[self.ace._ace_slot_for_head(self.filament_ch[FEED_CHANNEL_1])] == 1
                    if self._port[FEED_CHANNEL_1].ace is not None else None)
        in_ace_2 = (self.ace.gate_status[self.ace._ace_slot_for_head(self.filament_ch[FEED_CHANNEL_2])] == 1
                    if self._port[FEED_CHANNEL_2].ace is not None else None)

        channel_1_dist = {
            'module_exist': self.module_exist[FEED_CHANNEL_1],
            'filament_detected': filament_detected[FEED_CHANNEL_1],
            'filament_in_ace':      in_ace_1,
            'filament_in_toolhead': self._port[FEED_CHANNEL_1].get_filament_detected_local(),
            'filament_at_extruder': _runout(FEED_CHANNEL_1),
            'disable_auto': not self.config['auto_mode'][FEED_CHANNEL_1],
            'channel_state':self.channel_state[FEED_CHANNEL_1],
            'channel_error':self.channel_error[FEED_CHANNEL_1],
            'channel_error_state': self.channel_error_state[FEED_CHANNEL_1],
            'channel_action_state': self.channel_action_state[FEED_CHANNEL_1]
        }
        channel_2_dist = {
            'module_exist': self.module_exist[FEED_CHANNEL_2],
            'filament_detected': filament_detected[FEED_CHANNEL_2],
            'filament_in_ace':      in_ace_2,
            'filament_in_toolhead': self._port[FEED_CHANNEL_2].get_filament_detected_local(),
            'filament_at_extruder': _runout(FEED_CHANNEL_2),
            'disable_auto': not self.config['auto_mode'][FEED_CHANNEL_2],
            'channel_state':self.channel_state[FEED_CHANNEL_2],
            'channel_error':self.channel_error[FEED_CHANNEL_2],
            'channel_error_state': self.channel_error_state[FEED_CHANNEL_2],
            'channel_action_state': self.channel_action_state[FEED_CHANNEL_2]
        }

        return {
            f'extruder{self.filament_ch[FEED_CHANNEL_1]}': channel_1_dist,
            f'extruder{self.filament_ch[FEED_CHANNEL_2]}': channel_2_dist}

    def cmd_FEED_LIGHT(self, gcmd):
        channel = gcmd.get_int('CHANNEL')
        index = gcmd.get('INDEX').upper()
        value = gcmd.get_int('VALUE', minval=0, maxval=1)

        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))

        if not index in FEED_LIGHT_INDEXS:
            raise gcmd.error("[feed] light index[%s] is error" % (index))

        systime = self.reactor.monotonic()
        systime += FEED_MIN_TIME
        print_time = self.light[channel].get_mcu().estimated_print_time(systime)
        self.light[channel].set_light_state(print_time, FEED_STA_TEST, index, value)
        self._last_print_time = print_time

    def cmd_FEED_PORT(self, gcmd):
        channel = gcmd.get_int('CHANNEL')

        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))

        adc_value = self._port[channel].get_adc_value()
        if self._port[channel].get_filament_detected():
            present = "detected"
        else:
            present = "not detected"

        msg = ("port[%d]: adc value = %f, filament: %s\n" % (
                channel, adc_value, present))

        gcmd.respond_info(msg, log=False)

    def cmd_FEED_WHEEL_TACH(self, gcmd):
        channel = gcmd.get_int('CHANNEL')

        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))

        msg = ( "rpm: %d\n"
                "cnt: %d\n"
                "rpm2: %d\n"
                "cnt2: %d\n"
                % ( self.wheel[channel].get_rpm(),
                    self.wheel[channel].get_counts(),
                    self.wheel_2[channel].get_rpm(),
                    self.wheel_2[channel].get_counts()))
        gcmd.respond_info(msg, log=False)

    def cmd_FEED_MOTOR(self, gcmd):
        channel = gcmd.get_int('CHANNEL')
        value = gcmd.get_float('VALUE')

        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))

        if channel == FEED_CHANNEL_1:
            self.motor.run(FEED_MOTOR_DIR_A, value)
        else:
            self.motor.run(FEED_MOTOR_DIR_B, value)

    def cmd_FEED_MOTOR_ONE_CYCLE(self, gcmd):
        channel = gcmd.get_int('CHANNEL')
        value = gcmd.get_float('VALUE')
        time = gcmd.get_float('TIME', self.motor_hang_neutral_time)

        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))

        if channel == FEED_CHANNEL_1:
            self.motor.run_one_cycle(FEED_MOTOR_DIR_A, value, time)
        else:
            self.motor.run_one_cycle(FEED_MOTOR_DIR_B, value, time)

    def cmd_FEED_MOTOR_TACH(self, gcmd):
        msg = ( "rpm: %d\n"
                "cnt: %d\n"
                % ( self.motor_tachometer.get_rpm(),
                    self.motor_tachometer.get_counts()))
        gcmd.respond_info(msg, log=False)

    def cmd_FEED_AUTO(self, gcmd):
        channel = gcmd.get_int('CHANNEL')
        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))
        auto_mode = gcmd.get_int('AUTO', None)
        if auto_mode is not None:
            auto_mode = bool(auto_mode)
        need_to_load = gcmd.get_int('LOAD', None)
        if need_to_load is not None:
            need_to_load = bool(need_to_load)
        need_to_unload = gcmd.get_int('UNLOAD', None)
        if need_to_unload is not None:
            need_to_unload = bool(need_to_unload)
        stage = gcmd.get('STAGE', None)
        if stage is not None:
            stage = stage.lower()
        is_printing = gcmd.get_int('PRINTING', 0, minval=0, maxval=1)
        need_save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)

        raw_msg = None
        msg = None

        logging.info("[feed] FEED_AUTO %s", gcmd.get_raw_command_parameters())
        machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
        if machine_state_manager is not None:
            machine_sta = machine_state_manager.get_status()
            if str(machine_sta["main_state"]) not in ["IDLE", "PRINTING", "AUTO_LOAD", "AUTO_UNLOAD" ]:
                raise gcmd.error('[feed] channel[%d] machine main state error: %s\n'
                                 % (channel, str(machine_sta["main_state"])))

        if auto_mode is not None:
            try:
                self._do_feed(channel, FEED_ACT_UPDATE_AUTO_MODE, auto_mode=auto_mode)
            except:
                raise gcmd.error(
                        message = '[feed] channel[%d]: set auto mode error \n' % (channel),
                        action = 'none',
                        id = 525,
                        index = self.filament_ch[channel],
                        code = 0,
                        oneshot = 1,
                        level = 2)

            if need_save:
                load_config = self.printer.load_snapmaker_config_file(self.config_path, FEED_DEFAULT_CONFIG)
                load_config['auto_mode'] = self.config['auto_mode']
                ret = self.printer.update_snapmaker_config_file(self.config_path, load_config, FEED_DEFAULT_CONFIG)
                if not ret:
                    logging.error("[feed] save auto_mode failed!")
            return

        if need_to_load == True:
            if self.channel_state[channel] == FEED_STA_LOAD_FINISH and self.channel_error[channel] == FEED_OK:
                logging.info("[feed] FEED_AUTO LOAD skipped: channel[%d] already FEED_STA_LOAD_FINISH", channel)
                return

            if is_printing == 1:
                logging.info("[feed] FEED_AUTO LOAD skipped: channel[%d] is_printing=1", channel)
                return

            if self.config['auto_mode'][channel] == False:

                if self.ace is not None and getattr(self.ace, '_ace_mode', '') == 'multi':
                    logging.info("[feed] FEED_AUTO LOAD: ACE bypass auto_mode gate (channel[%d] auto_mode=False ignored)", channel)
                else:
                    logging.info("[feed] FEED_AUTO LOAD skipped: channel[%d] auto_mode=False", channel)
                    return

            if self.runout_sensor[channel] is None or self.runout_sensor[channel].get_status(0)['enabled'] == False:

                if self.ace is not None and getattr(self.ace, '_ace_mode', '') == 'multi':
                    logging.info("[feed] FEED_AUTO LOAD: ACE bypass runout_sensor gate (channel[%d] sensor None or disabled)", channel)
                else:
                    logging.info("[feed] FEED_AUTO LOAD skipped: channel[%d] runout_sensor disabled or None", channel)
                    return

            try:
                if machine_state_manager is not None:
                    machine_sta = machine_state_manager.get_status()
                    if str(machine_sta["main_state"]) == "PRINTING":
                        if str(machine_sta["action_code"]) != "PRINT_RESUMING" and str(machine_sta["action_code"]) != "PRINT_REPLENISHING":
                            self.gcode.run_script_from_command("SET_ACTION_CODE ACTION=PRINT_AUTO_FEEDING")
                    else:
                        self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=AUTO_LOAD ACTION=AUTO_LOADING")
                    self.toolhead.wait_moves()
                self._do_feed(channel, FEED_ACT_LOAD)
                logging.info(
                    "[feed][load] channel[%d] _do_feed returned: state=%s error=%s error_state=%s sensor=%s",
                    channel, self.channel_state[channel], self.channel_error[channel],
                    self.channel_error_state[channel],
                    self.runout_sensor[channel].get_status(0)['filament_detected']
                        if self.runout_sensor[channel] is not None else 'no-sensor')
            except Exception as e:
                raw_msg =  self.printer.extract_coded_message_field(str(e))
                logging.error("[feed][load] channel[%d] auto load error: %s", channel, raw_msg)
                logging.info(
                    "[feed][load] channel[%d] post-exception: state=%s error=%s error_state=%s sensor=%s",
                    channel, self.channel_state[channel], self.channel_error[channel],
                    self.channel_error_state[channel],
                    self.runout_sensor[channel].get_status(0)['filament_detected']
                        if self.runout_sensor[channel] is not None else 'no-sensor')
                if self._is_keep_raw_error_info(self.channel_error[channel]):
                    raise
            finally:
                if machine_state_manager is not None:
                    machine_sta = machine_state_manager.get_status()
                    if str(machine_sta["main_state"]) == "PRINTING":
                        if str(machine_sta["action_code"]) != "PRINT_RESUMING" and str(machine_sta["action_code"]) != "PRINT_REPLENISHING":
                            self.gcode.run_script_from_command("SET_ACTION_CODE ACTION=IDLE")
                    else:
                        self._ms_after_feed_op()
                    self.toolhead.wait_moves()
                logging.info(
                    "[feed][load] channel[%d] post-finally: state=%s error=%s error_state=%s sensor=%s",
                    channel, self.channel_state[channel], self.channel_error[channel],
                    self.channel_error_state[channel],
                    self.runout_sensor[channel].get_status(0)['filament_detected']
                        if self.runout_sensor[channel] is not None else 'no-sensor')

            if self.channel_state[channel] != FEED_STA_LOAD_FINISH or self.channel_error[channel] != FEED_OK:
                self.gcode.respond_raw(f'{self.channel_state[channel] != FEED_STA_LOAD_FINISH} {self.channel_error[channel] != FEED_OK}')
                tech_msg = 'extruder[%d]: state: %s, error: %s!' % (
                        self.filament_ch[channel],
                        self.channel_error_state[channel],
                        self.channel_error[channel])
                if raw_msg is not None:
                    tech_msg = tech_msg + "raw msg:" + raw_msg

                feed_msg, feed_steps = self._feed_load_fail_details(channel)
                if feed_msg is None:
                    feed_msg = self._emit_feed_pause(channel, 'msg.pause_feed_load_jam')
                if feed_msg is not None:
                    head_idx = self.filament_ch[channel]
                    head_disp = self.ace._disp(head_idx)
                    # Recovery hints stay English (they embed gcode-ish steps);
                    # only the primary message above is localized.
                    if feed_steps is None:
                        feed_steps = (
                            'Reload Head %s filament (display load menu or web "Reload")' % head_disp,
                            'Verify filament is in the toolhead',
                            'Press RESUME on display or in fluidd to continue',
                        )
                    for step in feed_steps:
                        try:
                            self.gcode.run_script_from_command(
                                'RESPOND TYPE=echo MSG="  - %s"' % step)
                        except Exception:
                            pass
                    try:
                        self.ace._audit_state('PAUSE_FEED_LOAD_FAIL', {
                            'channel': channel,
                            'head': head_idx,
                            'tech_msg': tech_msg,
                        })
                    except Exception:
                        pass
                    msg = feed_msg
                else:
                    msg = tech_msg

                raise gcmd.error(
                        message = msg,
                        action = 'pause',
                        id = 525,
                        index = self.filament_ch[channel],
                        # 210 = multiACE resumable-pause band: an
                        # unknown (id,code) pair makes the screen render OUR
                        # message as the popup's top line instead of the
                        # canned stock baustein. exception_code[channel]
                        # bookkeeping stays for the log/status trail.
                        code = 210,
                        oneshot = 1,
                        level = 2)

            try:
                ace = self.printer.lookup_object('ace', None)
                if ace is not None and hasattr(ace, 'notify_external_load'):
                    ace.notify_external_load(
                        module=self.module_name, channel=channel,
                        head=self.filament_ch[channel])
            except Exception as e:
                logging.info('[feed][auto] notify_external_load err: %s' % e)
            return

        if need_to_unload == True:
            try:
                if machine_state_manager is not None:
                    machine_sta = machine_state_manager.get_status()
                    if str(machine_sta["main_state"]) == "PRINTING":
                        self.gcode.run_script_from_command("SET_ACTION_CODE ACTION=PRINT_AUTO_UNLOADING")
                    else:
                        self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=AUTO_UNLOAD ACTION=AUTO_UNLOADING")
                self._do_feed(channel, FEED_ACT_UNLOAD, stage=stage)
            except Exception as e:
                if machine_state_manager is not None:
                    machine_sta = machine_state_manager.get_status()
                    if str(machine_sta["main_state"]) == "PRINTING":
                        self.gcode.run_script_from_command("SET_ACTION_CODE ACTION=IDLE")
                    else:
                        self._ms_after_feed_op()
                raw_msg =  self.printer.extract_coded_message_field(str(e))
                logging.error("[feed][unload] channel[%d]: auto unload error: %s", channel, raw_msg)
                if self._is_keep_raw_error_info(self.channel_error[channel]):
                    raise
            else:

                if stage in [FEED_UNLOAD_STAGE_DOING, FEED_UNLOAD_STAGE_CANCEL]:
                    if machine_state_manager is not None:
                        machine_sta = machine_state_manager.get_status()
                        if str(machine_sta["main_state"]) == "PRINTING":
                            self.gcode.run_script_from_command("SET_ACTION_CODE ACTION=IDLE")
                        else:
                            self._ms_after_feed_op()
            if self.channel_error[channel] != FEED_OK:
                tech = 'extruder[%d]: state: %s, error: %s!' % (
                        self.filament_ch[channel],
                        self.channel_error_state[channel],
                        self.channel_error[channel])
                if raw_msg is not None:
                    tech = tech + "raw msg:" + raw_msg
                msg = self._emit_feed_pause(channel, 'msg.pause_feed_error') or tech

                raise gcmd.error(
                        message = msg,
                        action = 'pause',
                        id = 525,
                        index = self.filament_ch[channel],
                        # 210 = multiACE resumable-pause band: an
                        # unknown (id,code) pair makes the screen render OUR
                        # message as the popup's top line instead of the
                        # canned stock baustein. exception_code[channel]
                        # bookkeeping stays for the log/status trail.
                        code = 210,
                        oneshot = 1,
                        level = 2)

            return

    def cmd_FEED_MANUAL(self, gcmd):
        channel = gcmd.get_int('CHANNEL')
        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed][manual_load] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))
        stage = gcmd.get('STAGE').lower()
        if stage not in [FEED_MANUAL_STAGE_PREPARE, FEED_MANUAL_STAGE_EXTRUDE,
                         FEED_MANUAL_STAGE_FLUSH, FEED_MANUAL_STAGE_FINISH,
                         FEED_MANUAL_STAGE_CANCEL]:
            raise gcmd.error('[feed][manual_load] stage error: %s\n' % (stage))

        raw_msg = None
        msg = None

        logging.info("[feed] FEED_MANUAL %s", gcmd.get_raw_command_parameters())

        machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
        if machine_state_manager is not None:
            machine_sta = machine_state_manager.get_status()
            if str(machine_sta["main_state"]) not in ["IDLE", "PRINTING", "MANUAL_LOAD"]:
                raise gcmd.error('[feed][manual] channel[%d] machine main state error: %s\n'
                                 % (channel, str(machine_sta["main_state"])))

        try:
            if machine_state_manager is not None:
                machine_sta = machine_state_manager.get_status()
                if str(machine_sta["main_state"]) != "PRINTING":
                    self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=MANUAL_LOAD ACTION=MANUAL_LOADING")
            self._do_feed(channel, FEED_ACT_MANUAL_FEED, stage)
        except Exception as e:
            if machine_state_manager is not None:
                machine_sta = machine_state_manager.get_status()
                if str(machine_sta["main_state"]) != "PRINTING":
                    self._ms_after_feed_op()
            raw_msg =  self.printer.extract_coded_message_field(str(e))
            logging.error("[feed][manual] channel[%d]: manual load error: %s", channel, raw_msg)
            if self._is_keep_raw_error_info(self.channel_error[channel]):
                raise
        else:

            if stage in [FEED_MANUAL_STAGE_FINISH, FEED_MANUAL_STAGE_CANCEL]:
                if machine_state_manager is not None:
                    machine_sta = machine_state_manager.get_status()
                    if str(machine_sta["main_state"]) != "PRINTING":
                        self._ms_after_feed_op()

        if self.channel_error[channel] != FEED_OK:
            tech = 'extruder[%d]: state: %s, error: %s!' % (
                    self.filament_ch[channel],
                    self.channel_error_state[channel],
                    self.channel_error[channel])
            if raw_msg is not None:
                tech = tech + "raw msg:" + raw_msg
            msg = self._emit_feed_pause(channel, 'msg.pause_manual_feed_error') or tech

            raise gcmd.error(
                    message = msg,
                    action = 'pause',
                    id = 525,
                    index = self.filament_ch[channel],
                    # 210 = multiACE resumable-pause band, see above.
                    code = 210,
                    oneshot = 1,
                    level = 2)
        elif stage == FEED_MANUAL_STAGE_FINISH:

            try:
                ace = self.printer.lookup_object('ace', None)
                if ace is not None and hasattr(ace, 'notify_external_load'):
                    ace.notify_external_load(
                        module=self.module_name, channel=channel,
                        head=self.filament_ch[channel])
            except Exception as e:
                logging.info('[feed][manual] notify_external_load err: %s' % e)

    def cmd_FEED_RUNOUT_EVENT_HANDLE(self, gcmd):

        if self.ace is not None and getattr(self.ace, '_swap_in_progress', False):
            logging.info("[multiACE] FEED_RUNOUT_EVENT_HANDLE: blocking during swap")
            return
        channel = gcmd.get_int('CHANNEL')
        if channel < 0 or channel >= FEED_CHANNEL_NUMS:
            raise gcmd.error('[feed] channel[%d] is out of range[0,%d]\n' % (channel, FEED_CHANNEL_NUMS - 1))

        self.toolhead.wait_moves()
        try:
            self._do_feed(channel, FEED_ACT_FILAMENT_RUNOUT)
        except:
            logging.error("[feed] channel[%d]: runout event handle error!", channel)

def load_config_prefix(config):
    return FilamentFeed(config)
