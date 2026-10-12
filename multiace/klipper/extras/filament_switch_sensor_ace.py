import logging, os

POSTFIX_CONFIG_FILE ='_runout_sensor.json'
DEFAULT_CONFIG = {
    'enable': True
}

class RunoutHelper:
    def __init__(self, config):
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.runout_pause = config.getboolean('pause_on_runout', True)
        if self.runout_pause:
            self.printer.load_object(config, 'pause_resume')
        self.runout_gcode = self.insert_gcode = None
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        if self.runout_pause or config.get('runout_gcode', None) is not None:
            self.runout_gcode = gcode_macro.load_template(
                config, 'runout_gcode', '')
        if config.get('insert_gcode', None) is not None:
            self.insert_gcode = gcode_macro.load_template(
                config, 'insert_gcode')
        self.pause_delay = config.getfloat('pause_delay', .5, above=.0)
        self.event_delay = config.getfloat('event_delay', 3., above=0.)

        self.min_event_systime = self.reactor.NEVER
        self.filament_present = False
        self.sensor_enabled = True
        # No present-note counter here on purpose: the sensor pin is a
        # PRESENCE GATE (edges only on tip-arrival/tail-departure), so
        # counting note(True) calls measures nothing while filament is
        # present - a perfect load and a clogged head both read 0.

        self.extruder_index = self._get_extruder_index(config.get('extruder'))
        self.exception_manager = self.printer.lookup_object('exception_manager', None)

        config_dir = self.printer.get_snapmaker_config_dir()
        config_name = self.name + POSTFIX_CONFIG_FILE
        self.config_path = os.path.join(config_dir, config_name)
        self.config = self.printer.load_snapmaker_config_file(self.config_path, DEFAULT_CONFIG)
        self.sensor_enabled = self.config['enable']

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.gcode.register_mux_command(
            "QUERY_FILAMENT_SENSOR", "SENSOR", self.name,
            self.cmd_QUERY_FILAMENT_SENSOR,
            desc=self.cmd_QUERY_FILAMENT_SENSOR_help)
        self.gcode.register_mux_command(
            "SET_FILAMENT_SENSOR", "SENSOR", self.name,
            self.cmd_SET_FILAMENT_SENSOR,
            desc=self.cmd_SET_FILAMENT_SENSOR_help)
        self.gcode.register_mux_command(
            "CHECK_FILAMENT_RUNOUT", "SENSOR", self.name,
            self.cmd_CHECK_FILAMENT_RUNOUT,
            desc=self.cmd_CHECK_FILAMENT_RUNOUT_help)
    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.
        self.print_task_config = self.printer.lookup_object('print_task_config', None)

    def _get_extruder_index(self, extruder_name):
        if extruder_name is not None and extruder_name.startswith('extruder'):
            num_str = extruder_name[8:]
            return int(num_str) if num_str.isdigit() else 0
        return 0

    def _runout_disp(self):
        # Localized multiACE runout message (full text), 1-based via the ACE
        # _disp() with ACE/Slot from head_source when known. Falls back to the
        # sensor name if the ACE module is unavailable. No M117 (invisible on
        # the Snapmaker touchscreen).
        ace = self.printer.lookup_object('ace', None)
        head = self.extruder_index
        if ace is not None and hasattr(ace, '_t'):
            hd = ace._disp(head)
            src = (getattr(ace, '_head_source', None) or {}).get(head) or {}
            a, s = src.get('ace_index'), src.get('slot')
            loc = ' (ACE %d / Slot %d)' % (ace._disp(a), ace._disp(s)) \
                if a is not None and s is not None else ''
            return ace._t('msg.pause_runout', head=hd, loc=loc)
        return ('[multiACE] %s runout - reload filament '
                '(display or web "Reload"), then RESUME' % self.name)

    def _runout_event_handler(self, eventtime):

        ace = self.printer.lookup_object('ace', None)
        if ace is not None and getattr(ace, '_swap_in_progress', False):
            logging.info("[multiACE] filament_switch_sensor: blocking runout during swap")
            return
        if ace is not None and self.extruder_index in getattr(ace, '_runout_suppress_heads', ()):
            logging.info("[multiACE] filament_switch_sensor: runout suppressed for head %d (recovery: empty head awaiting reload)" % self.extruder_index)
            return

        full_msg = self._runout_disp()
        pause_prefix = ""
        if self.runout_pause:
            if self.exception_manager is not None:
                self.printer.send_event("print_stats:update_exception_info",
                                        self.exception_manager.list.MODULE_ID_TOOLHEAD,
                                        self.extruder_index,
                                        self.exception_manager.list.CODE_TOOLHEAD_FILAMENT_RUNOUT,
                                        full_msg,
                                        2)
            pause_resume = self.printer.lookup_object('pause_resume')
            pause_resume.send_pause_command()
            pause_prefix = "PAUSE IS_RUNOUT=1\n"
            self.printer.get_reactor().pause(eventtime + self.pause_delay)
        self._exec_gcode(pause_prefix, self.runout_gcode)
        if self.runout_pause:
            # Quad Replenish - the SLOT tier next to stock's HEAD-twin
            # replenish. Order is configurable ([ace] quad_first /
            # ACE_SET_QUAD_REPLENISH FIRST=): quad-first (default) drains
            # the printing head's OWN lane first, which reaches every spool
            # of the rack instead of stranding the slots of heads that
            # emptied earlier; stock-first switches to a loaded twin head
            # instantly and we only step in when it declines. Returns
            # True when a reload+resume was scheduled (it raises its own
            # message on failure, so the user is never left popup-less).
            # Fail-open: any error or an old ace.py keeps the stock path.
            def _try_quad():
                try:
                    ace = self.printer.lookup_object('ace', None)
                    if ace is not None and hasattr(
                            ace, 'quad_replenish_after_runout'):
                        return bool(ace.quad_replenish_after_runout(
                            self.extruder_index))
                except Exception:
                    logging.exception(
                        '[multiACE] quad-replenish hook failed')
                return False

            quad_first = False
            try:
                _ace = self.printer.lookup_object('ace', None)
                quad_first = bool(getattr(_ace, 'quad_first', False))
            except Exception:
                quad_first = False

            handled = False
            if quad_first:
                handled = _try_quad()
            if not handled:
                # The presence override (feed get_status) is scoped to THIS
                # call: stock's replenish self-check must read the toolhead
                # truth for the ran-out head (else it self-selects T->T off
                # the gate lie), but every OTHER reader of the field -
                # the display exist flag above all - wants the gate. State-
                # scoping (printing/paused + head_source) kept the ran-out
                # or failed-load head at '/' for the WHOLE pause and refused
                # the display load until filament touched the toolhead
                # sensor. run_script is synchronous,
                # so the window is exactly the command's execution.
                _ace = self.printer.lookup_object('ace', None)
                try:
                    if _ace is not None:
                        _ace._replenish_check_active = True
                    self.gcode.run_script(f'\nM400\nINNER_AUTO_REPLENISH_FILAMENT EXTRUDER={self.extruder_index}\n')
                except Exception:
                    logging.exception("Script running error")
                finally:
                    if _ace is not None:
                        _ace._replenish_check_active = False
                        # The command itself may recompute filament_exist
                        # while the override is active and cache '/' for the
                        # tile (a stale exist flag) - recompute with the
                        # window closed so the display reads the gate again.
                        try:
                            _ace._refresh_filament_exist_flags()
                        except Exception:
                            pass
            if not handled and self.print_task_config.perform_auto_replenish == False:
                if not quad_first:
                    handled = _try_quad()
                if not handled and self.exception_manager is not None:
                    self.exception_manager.raise_exception_async(
                        id = self.exception_manager.list.MODULE_ID_TOOLHEAD,
                        index = self.extruder_index,
                        code = self.exception_manager.list.CODE_TOOLHEAD_FILAMENT_RUNOUT,
                        message = full_msg,
                        oneshot = 0,
                        level = 2)

    def _insert_event_handler(self, eventtime):
        self._exec_gcode("", self.insert_gcode)

    def _exec_gcode(self, prefix, template):
        try:
            self.gcode.run_script(prefix + template.render() + "\nM400")
        except Exception:
            logging.exception("Script running error")
        self.min_event_systime = self.reactor.monotonic() + self.event_delay
    def note_filament_present(self, is_filament_present, force=False):
        # force=True is used by the stock filament_motion_sensor
        # (_extruder_pos_update_event runout path, _handle_stop_print_job) to
        # push the note even when the present-state is unchanged. Dropping the
        # param made those stock calls raise TypeError -> Klipper shutdown
        # ("System Anomaly") on runout. Keep the stock signature.
        if is_filament_present == self.filament_present and force == False:
            return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()
        if eventtime < self.min_event_systime or not self.sensor_enabled:

            return

        if self.filament_present:
            logging.info("Filament Sensor %s: insert event detected, Time %.2f" %
                         (self.name, eventtime))
        else:
            logging.info("Filament Sensor %s: remove event detected, Time %.2f" %
                         (self.name, eventtime))

        if self.print_task_config is not None:
            self.print_task_config.backup_filament_info(self.extruder_index)
        self.printer.send_event("filament_switch_sensor:runout",
                                self.extruder_index, is_filament_present)

        print_stats = self.printer.lookup_object('print_stats')
        is_printing = print_stats.state == "printing"

        if is_filament_present:
            # Head got filament -> a suppressed empty head is (re)loaded; lift
            # the recovery runout suppression for it (see ace _runout_suppress_heads).
            ace = self.printer.lookup_object('ace', None)
            if ace is not None and self.extruder_index in getattr(ace, '_runout_suppress_heads', ()):
                ace._runout_suppress_heads.discard(self.extruder_index)
                logging.info("[multiACE] note_filament_present: head %d (re)loaded - clearing runout suppression" % self.extruder_index)
            # A head that had a background-swap FEED-abort was flagged empty; it
            # now has filament again -> clear the flag (see ace._bg_left_empty).
            if ace is not None and self.extruder_index in getattr(ace, '_bg_left_empty', ()):
                ace._bg_left_empty.discard(self.extruder_index)
            if not is_printing and self.insert_gcode is not None:

                self.min_event_systime = self.reactor.NEVER
                self.reactor.register_callback(self._insert_event_handler)
            if self.exception_manager is not None:
                self.exception_manager.clear_exception(
                    id = self.exception_manager.list.MODULE_ID_TOOLHEAD,
                    index = self.extruder_index,
                    code = self.exception_manager.list.CODE_TOOLHEAD_FILAMENT_RUNOUT)
        elif is_printing and self.runout_gcode is not None:

            ace = self.printer.lookup_object('ace', None)
            if ace is not None and getattr(ace, '_swap_in_progress', False):
                logging.info("[multiACE] note_filament_present: blocking runout callback during swap")
                return
            if ace is not None and self.extruder_index in getattr(ace, '_runout_suppress_heads', ()):
                logging.info("[multiACE] note_filament_present: runout suppressed for head %d (recovery: empty head awaiting reload)" % self.extruder_index)
                return

            # Stock 1.4: don't process runout while the print-end action runs.
            if self.print_task_config is not None and \
                    getattr(self.print_task_config, 'is_exec_print_end_action', False):
                return

            self.min_event_systime = self.reactor.NEVER
            logging.info(
                "Filament Sensor %s: runout event detected, Time %.2f" %
                (self.name, eventtime))
            self.reactor.register_callback(self._runout_event_handler)

    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled)}
    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"
    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Filament Sensor %s: filament detected" % (self.name)
        else:
            msg = "Filament Sensor %s: filament not detected" % (self.name)
        gcmd.respond_info(msg)
    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"
    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = gcmd.get_int("ENABLE", 1)
        self.config['enable'] = bool(self.sensor_enabled)
        logging.info("Filament Sensor: set enable/disable -- %d", self.sensor_enabled)

        # Stock 1.4: refresh print_task_config filament flags on enable/disable.
        if self.print_task_config is not None and \
                hasattr(self.print_task_config, 'update_filament_flags'):
            self.print_task_config.update_filament_flags()

        need_save = gcmd.get_int('SAVE', 1, minval=0, maxval=1)
        if (need_save):
            load_config = self.printer.load_snapmaker_config_file(self.config_path, DEFAULT_CONFIG)
            load_config['enable'] = self.config['enable']
            ret = self.printer.update_snapmaker_config_file(self.config_path, load_config, DEFAULT_CONFIG)
            if not ret:
                raise gcmd.error("save startup stay failed!")
    cmd_CHECK_FILAMENT_RUNOUT_help = "Check for filament runout during printing process."
    def cmd_CHECK_FILAMENT_RUNOUT(self, gcmd):
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is not None and print_stats.state in ["printing", "paused"]:
            if bool(self.sensor_enabled) and not bool(self.filament_present):
                ace = self.printer.lookup_object('ace', None)
                # Resume-gate parity with the runout-EVENT suppression: a
                # recovery pause marked this head empty-awaiting-reload
                # (_runout_suppress_heads). The event paths
                # honored the set but this resume-time check (INNER_RESUME ->
                # CHECK_FILAMENT_RUNOUT) did not, so the RESUME was still
                # refused with the runout error the suppression exists for.
                if ace is not None and self.extruder_index in getattr(
                        ace, '_runout_suppress_heads', ()):
                    logging.info(
                        '[multiACE] CHECK_FILAMENT_RUNOUT: suppressed for '
                        'head %d (recovery: empty head awaiting reload)'
                        % self.extruder_index)
                    return
                # A NEVER-loaded ACE-driven head cannot be a real runout when
                # the print's gcode carries multiACE loads (preflight auto-load
                # block / ACE_SWAP_HEAD lines - sniffed once at print start,
                # ace._print_has_gcode_loads): its load is ahead in the file,
                # so a pause that hit BEFORE it ran (e.g. a stock
                # park-malfunction pause inside the auto-load block) must stay
                # resumable. Applies to multi AND head mode - both put every
                # load in the gcode via preflight. A raw Fluidd/slicer upload
                # has no gcode loads -> flag False -> stock refusal stays. A
                # real runout keeps its head_source (loaded head, sensor
                # empty) and still refuses; manual/feeder heads (no gcode
                # loads, head_uses_ace False) unchanged.
                if (ace is not None
                        and getattr(ace, '_print_has_gcode_loads', False)
                        and ace.head_uses_ace(self.extruder_index)
                        and not ace._head_is_loaded(self.extruder_index)):
                    logging.info(
                        '[multiACE] CHECK_FILAMENT_RUNOUT: head %d is an '
                        'unloaded ACE-driven head and this print carries '
                        'multiACE loads - its load is ahead in the gcode, '
                        'allowing RESUME' % self.extruder_index)
                    return
                raise gcmd.error(
                        message = self._runout_disp(),
                        action = 'pause',
                        id = 523,
                        index = self.extruder_index,
                        code = 0,
                        oneshot = 0,
                        level = 2)

class SwitchSensor:
    def __init__(self, config):
        printer = config.get_printer()
        buttons = printer.load_object(config, 'buttons')
        switch_pin = config.get('switch_pin')

        if config.get('analog_range', None) is None:
            buttons.register_buttons([switch_pin], self._button_handler)
        else:
            amin, amax = config.getfloatlist('analog_range', count=2)
            pullup = config.getfloat('analog_pullup_resistor', 4700., above=0.)
            buttons.register_adc_button(switch_pin, amin, amax, pullup, self._button_handler)
        self.runout_helper = RunoutHelper(config)
        self.get_status = self.runout_helper.get_status
    def _button_handler(self, eventtime, state):
        self.runout_helper.note_filament_present(state)

def load_config_prefix(config):
    return SwitchSensor(config)
