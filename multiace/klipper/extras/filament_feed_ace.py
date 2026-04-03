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
FEED_COIL_FREQ_THERSHOLD_HARD                       = 1500

FEED_MIN_TIME                                       = 0.100

FEED_CONFIG_FILE_POSTFIX                            = '_filament_feed.json'
FEED_DEFAULT_CONFIG = {
    'auto_mode': [True] * FEED_CHANNEL_NUMS,
    'load_finish': [False] * FEED_CHANNEL_NUMS
}

FEED_FILAMENT_TEMP_DEFAULT                          = 250

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
            self.red_light.set_pwm(print_time, 0)
            self.white_light.set_pwm(print_time, 0.2)
        elif state in [FEED_STA_PRELOAD_FEEDING, FEED_STA_LOAD_HOMING, FEED_STA_LOAD_PICKING,
                       FEED_STA_LOAD_HEATING, FEED_STA_LOAD_FEEDING, FEED_STA_LOAD_EXTRUDING,
                       FEED_STA_LOAD_FLUSHING, FEED_STA_UNLOAD_HOMING, FEED_STA_UNLOAD_PICKING,
                       FEED_STA_UNLOAD_HEAT_FINISH,
                       FEED_STA_UNLOAD_HEATING, FEED_STA_UNLOAD_DOING, FEED_STA_MANUAL_HOMING,
                       FEED_STA_MANUAL_PICKING, FEED_STA_MANUAL_PREPARE_FINISH, FEED_STA_MANUAL_HEATING,
                       FEED_STA_MANUAL_EXTRUDING, FEED_STA_MANUAL_EXTRUDE_FINISH, FEED_STA_MANUAL_FLUSHING,
                       FEED_STA_MANUAL_FLUSH_FINISH]:
            self.red_light.set_pwm(print_time, 0)
            self.white_light.set_pwm(print_time, 0.5)
        elif state in [FEED_STA_PRELOAD_FINISH, FEED_STA_LOAD_FINISH, FEED_STA_UNLOAD_FINISH,
                       FEED_STA_MANUAL_FINISH]:
            self.red_light.set_pwm(print_time, 0)
            self.white_light.set_pwm(print_time, 1)
        elif state in [FEED_STA_PRELOAD_FAIL, FEED_STA_LOAD_FAIL, FEED_STA_UNLOAD_FAIL,
                       FEED_STA_MANUAL_PREPARE_FAIL, FEED_STA_MANUAL_EXTRUDE_FAIL,
                       FEED_STA_MANUAL_FLUSH_FAIL, FEED_STA_MANUAL_FAIL]:
            self.red_light.set_pwm(print_time, 1)
            self.white_light.set_pwm(print_time, 0)
        elif state == FEED_STA_TEST:
            if index == 'RED' and value is not None:
                self.red_light.set_pwm(print_time, value)
            elif index == 'WHITE' and value is not None:
                self.white_light.set_pwm(print_time, value)
            elif index == 'ALL' and value is not None:
                self.red_light.set_pwm(print_time, value)
                self.white_light.set_pwm(print_time, value)
            else:
                pass
        else:
            self.red_light.set_pwm(print_time, 0)
            self.white_light.set_pwm(print_time, 0)

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
        if (self._port_adc_value < self._threshold):
            self._filament_detected = True
        else:
            self._filament_detected = False

        if (None != self._port_event_callback and self._last_filament_detected != self._filament_detected):
            self._last_filament_detected = self._filament_detected
            self._port_event_callback(self._filament_detected)

    def get_adc_value(self):
        return self._port_adc_value
    
    def add_ace(self, ace):
        self.ace = ace

    def get_filament_detected(self):
        if self.ace is not None:
            return self.ace.gate_status[self.index] == 1
        else:
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

        # extruder channel / filament channel
        self.filament_ch = []
        self.filament_ch.append(config.getint('filament_ch_1'))
        self.filament_ch.append(config.getint('filament_ch_2'))

        # runout sensor
        self.runout_sensor = []
        tmp_obj = self.printer.lookup_object('filament_motion_sensor e%d_filament' % (self.filament_ch[FEED_CHANNEL_1]), None)
        self.runout_sensor.append(tmp_obj)
        tmp_obj = self.printer.lookup_object('filament_motion_sensor e%d_filament' % (self.filament_ch[FEED_CHANNEL_2]), None)
        self.runout_sensor.append(tmp_obj)
        self.filament_detect = self.printer.lookup_object('filament_detect', None)

        # light
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

        # port
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

        # wheel
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

        # motor
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

        # motor tachometer
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

        # other config
        self._feed_load_position_x = config.getfloat('load_position_x', FEED_LOAD_POSITION_X, minval=2, maxval=265)
        self._feed_load_position_y = config.getfloat('load_position_y', FEED_LOAD_POSITION_Y, minval=2, maxval=250)
        self._feed_load_extrude_max_times = config.getint('load_extrude_max_times', FEED_LOAD_EXTRUDE_TIMES_MAX, minval=3, maxval=50)
        preload_length = config.getfloat('preload_length', FEED_PRELOAD_LENGTH, minval=600.0, maxval=1500.0)
        self.coil_freq_threshold_soft = config.getint('coil_freq_thershold_soft', FEED_COIL_FREQ_THERSHOLD_SOFT, minval=100)
        self.coil_freq_threshold_hard = config.getint('coil_freq_thershold_hard', FEED_COIL_FREQ_THERSHOLD_HARD, minval=100)
        self.check_wheel_data = config.getint('check_wheel_data', 1)
        self.check_coil_freq = config.getint('check_coil_freq', 1)
        if self.check_coil_freq == 0 and self.check_wheel_data == 0:
            raise Exception("check_wheel_data and check_coil_freq can not be both 0")

        # other gcode cmd
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

        # Block runout during mid-print swap
        if self.ace is not None and getattr(self.ace, '_swap_in_progress', False):
            return

        for ch in range(FEED_CHANNEL_NUMS):
            if extruder == self.filament_ch[ch]:
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

    def _get_filament_temp(self, channel):
        print_task_config = self.printer.lookup_object('print_task_config', None)
        filament_parameters = self.printer.lookup_object('filament_parameters', None)
        if print_task_config is None or filament_parameters is None:
            return FEED_FILAMENT_TEMP_DEFAULT

        status = print_task_config.get_status()
        return filament_parameters.get_load_temp(
                status['filament_vendor'][self.filament_ch[channel]],
                status['filament_type'][self.filament_ch[channel]],
                status['filament_sub_type'][self.filament_ch[channel]])
    def _get_filament_soft(self, channel):
        print_task_config = self.printer.lookup_object('print_task_config', None)
        filament_parameters = self.printer.lookup_object('filament_parameters', None)
        if print_task_config is None or filament_parameters is None:
            return False

        status = print_task_config.get_status()
        return filament_parameters.get_is_soft(
                status['filament_vendor'][self.filament_ch[channel]],
                status['filament_type'][self.filament_ch[channel]],
                status['filament_sub_type'][self.filament_ch[channel]])

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
        filament_soft = self._get_filament_soft(ch)

        motor_dir = FEED_MOTOR_DIR_A
        if ch == FEED_CHANNEL_2:
            motor_dir = FEED_MOTOR_DIR_B

        try:
            # update auto-mode
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

            # remove filament
            elif action == FEED_ACT_REMOVE_FILAMENT:
                if self._port[ch].get_filament_detected() == False:
                    self._set_channel_state(ch, FEED_STA_WAIT_INSERT, True)
                else:
                    self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH, True)

            # filament runout
            elif action == FEED_ACT_FILAMENT_RUNOUT:
                # Block runout during mid-print swap
                if self.ace is not None and getattr(self.ace, '_swap_in_progress', False):
                    logging.info("_do_feed: blocking FILAMENT_RUNOUT during swap")
                    return
                if self._port[ch].get_filament_detected() == True:
                    self._set_channel_state(ch, FEED_STA_PRELOAD_FINISH, True)
                else:
                    self._set_channel_state(ch, FEED_STA_WAIT_INSERT, True)

            # preload
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

                    # feed
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

                            # Please do not adjust the order arbitrarily
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

            # load
            elif action == FEED_ACT_LOAD:
                try:
                    # prepare
                    self.exception_code[ch] = 30
                    self.manual_feeding[ch] = False
                    self.channel_error_state[ch] = FEED_STA_NONE
                    self._set_channel_state(ch, FEED_STA_LOAD_PREPARE, True)

                    if self._port[ch].get_filament_detected() == False:
                        self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                        self.exception_code[ch] = 33
                        raise ValueError('logic error!')

                    # home
                    try:
                        self._set_channel_state(ch, FEED_STA_LOAD_HOMING)
                        if self._check_homing_xy() != True:
                            self.gcode.run_script_from_command("G28 X Y\r\n")
                            self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_MOVE_HOME
                        raise

                    # switch extruder
                    try:
                        self._set_channel_state(ch, FEED_STA_LOAD_PICKING)
                        self.gcode.run_script_from_command("T%d A0\r\n" % (self.filament_ch[ch]))
                        self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_MOVE_SWITCH
                        raise

                    # feed filament
                    self._set_channel_state(ch, FEED_STA_LOAD_FEEDING)
                    self._put_into_drive(ch)
                    self.toolhead.wait_moves()

                    # Please do not adjust the order arbitrarily
                    if self.runout_sensor[ch].get_status(0)['filament_detected'] == False:
                        # move to dest position
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
                        # Rewritten: Original sent a fixed 100mm feed at speed 20.
                        # Now uses configurable load_length and feed_speed from ace.cfg,
                        # with per-toolhead overrides. Adds retry logic: if sensor not
                        # reached after full feed, mini-retract and retry up to load_retry times.
                        # Also disables feed_assist before feeding to prevent conflicts
                        # when loading a different toolhead than the last one.
                        # 4.0s pause after feed command gives ACE time to start motors
                        # before the polling loop checks is_ace_ready().
                        if self.ace is not None:
                            self.ace._disable_feed_assist()    # Prevents conflict with previous toolhead's feed_assist
                            self.ace.wait_ace_ready()
                            load_retries = self.ace.head_load_retry[self.filament_ch[ch]]
                            load_retry_retract = self.ace.head_load_retry_retract[self.filament_ch[ch]]

                            for load_attempt in range(load_retries + 1):
                                if load_attempt > 0:
                                    logging.info("[feed_loading] retry %d/%d: retracting %dmm",
                                                 load_attempt, load_retries, load_retry_retract)
                                    self.ace._retract(self.filament_ch[ch], load_retry_retract, self.ace.retract_speed)
                                    self.ace.wait_ace_ready()

                                self.ace._feed(self.filament_ch[ch], self.ace.head_load_length[self.filament_ch[ch]], self.ace.feed_speed, 0)
                                self.reactor.pause(self.reactor.monotonic() + 4.0)  # Wait for ACE to start motors

                                load_found = False
                                while 1:
                                    self.reactor.pause(self.reactor.monotonic() + 0.105)
                                    port_detect = self._port[ch].get_filament_detected()
                                    runout_detect = self.runout_sensor[ch].get_status(0)['filament_detected']

                                    if runout_detect == True:
                                        self.ace._stop_feeding(self.filament_ch[ch])
                                        self.channel_error[ch] = FEED_OK
                                        load_found = True
                                        break
                                    if self.ace.is_ace_ready():
                                        break
                                    if port_detect == False:
                                        self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                                        self.exception_code[ch] = 33
                                        break

                                if load_found:
                                    break

                                if self.channel_error[ch] == FEED_ERR_NO_FILAMENT:
                                    break

                                logging.info("[feed_loading] attempt %d: sensor not reached", load_attempt + 1)

                            if not load_found and self.channel_error[ch] != FEED_ERR_NO_FILAMENT:
                                self.channel_error[ch] = FEED_ERR_TIMEOUT
                                self.exception_code[ch] = 34
                            # if systime_2 - systime_0 > FEED_LOAD_TIMEOUT_TIME:
                            #     self.channel_error[ch] = FEED_ERR_TIMEOUT
                            #     self.exception_code[ch] = 34
                            # #     break
                            # if (wheel_cnt_a_2 - wheel_cnt_a_0) / self.wheel[ch].ppr > self._feed_load_counts_max or \
                            #         (wheel_cnt_b_2 - wheel_cnt_b_0) / self.wheel_2[ch].ppr > self._feed_load_counts_max:
                            #     self.channel_error[ch] = FEED_ERR_DISTANCE
                            #     self.exception_code[ch] = 35
                            #     break
                            # if wheel_cnt_a_2 - wheel_cnt_a_1 < 1 and wheel_cnt_b_2 - wheel_cnt_b_1 < 1:
                            #     wheel_err_max_cnt -= 1
                            #     if wheel_err_max_cnt <= 0:
                            #         self.channel_error[ch] = FEED_ERR_WHEEL_SPEED
                            #         self.exception_code[ch] = 32
                            #         break
                            # else:
                            #     wheel_err_max_cnt = FEED_LOAD_WHEEL_ERR_CNT_MAX
                            # if motor_cnt_2 - motor_cnt_1 < 1:
                            #     motor_err_max_cnt -= 1
                            #     if motor_err_max_cnt <= 0:
                            #         self.channel_error[ch] = FEED_ERR_MOTOR_SPEED
                            #         self.exception_code[ch] = 31
                            #         break
                            # else:
                            #     motor_err_max_cnt = FEED_LOAD_MOTOR_ERR_CNT_MAX

                            # if wheel_cnt_a_2 - wheel_cnt_a_1 > one_step_cnt or wheel_cnt_b_2 - wheel_cnt_b_1 > one_step_cnt:
                            #     if duty > 0.7:
                            #         duty = max(0.7, duty - 0.1)
                            #     period = max(0.09, period - 0.01)
                            # elif wheel_cnt_a_2 - wheel_cnt_a_1 < one_step_cnt and wheel_cnt_b_2 - wheel_cnt_b_1 < one_step_cnt:
                            #     if duty < 1.0:
                            #         duty = min(1.0, duty + 0.1)
                            #     else:
                            #         period = min(0.120, period + 0.01)

                        if self.channel_error[ch] != FEED_OK:
                            self._hang_neutral(ch)
                            raise ValueError('logic error!')
                    if self.ace is not None:
                        self.ace._enable_feed_assist(self.filament_ch[ch])
                    self.gcode.run_script_from_command("M104 S%d\r\n" % (filament_feed_temp))
                    try:
                        self.toolhead.wait_moves()
                        self.gcode.run_script_from_command("MOVE_TO_DISCARD_FILAMENT_POSITION\r\n")
                        self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_MOVE
                        raise

                    # heating
                    self._set_channel_state(ch, FEED_STA_LOAD_HEATING)
                    try:
                        self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
                    except:
                        self.channel_error[ch] = FEED_ERR_HEAT
                        raise

                    # extruding
                    self.exception_code[ch] = 50
                    self._set_channel_state(ch, FEED_STA_LOAD_EXTRUDING)
                    inductance_coil = None
                    try:
                        inductance_coil = self.toolhead.get_extruder().binding_probe.sensor
                    except:
                        logging.info("[feed_loading] inductance_coil not found")
                        inductance_coil = None

                    extruded = False
                    try:
                        duty = 0.8
                        period = 0.100
                        self.gcode.run_script_from_command("M83\r\n")
                        for retry in range(self._feed_load_extrude_max_times):
                            self.toolhead.wait_moves()
                            self.reactor.pause(self.reactor.monotonic() + 0.105)
                            wheel_cnt_a_1 = self.wheel[ch].get_counts()
                            wheel_cnt_b_1 = self.wheel_2[ch].get_counts()
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
                                if self.check_wheel_data != 0 and self.check_coil_freq == 0:
                                    if wheel_cnt_a_2 - wheel_cnt_a_1 >= 2 or wheel_cnt_b_2 - wheel_cnt_b_1 >= 2:
                                        extruded = True
                                        break
                                elif self.check_wheel_data == 0 and self.check_coil_freq != 0:
                                    if retry > 0 and inductance_coil is not None:
                                        if abs(coil_freq_end_min - coil_freq_start) >= coil_freq_threshold or \
                                                abs(coil_freq_end_max - coil_freq_start) >= coil_freq_threshold:
                                            extruded = True
                                            break
                                else:
                                    if wheel_cnt_a_2 - wheel_cnt_a_1 >= 2 or wheel_cnt_b_2 - wheel_cnt_b_1 >= 2:
                                        extruded = True
                                        break
                                    if retry > 0 and inductance_coil is not None:
                                        if abs(coil_freq_end_min - coil_freq_start) >= coil_freq_threshold or \
                                                abs(coil_freq_end_max - coil_freq_start) >= coil_freq_threshold:
                                            extruded = True
                                            break

                            if extruded == True:
                                break

                            self.gcode.run_script_from_command("ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD\r\n")
                            self.toolhead.wait_moves()

                            if filament_soft:
                                self.gcode.run_script_from_command("G1 E50 F200\r\n")
                            else:
                                self.gcode.run_script_from_command("G1 E40 F480\r\n")
                            self.toolhead.get_last_move_time()
                            self.reactor.pause(self.reactor.monotonic() + 0.7)

                            if retry < 5:
                                duty = max(1.0, duty + 0.05)
                            else:
                                period = max(0.12, period + 0.01)
                            self.motor.run_one_cycle(motor_dir, duty, period)
                            self.toolhead.wait_moves()
                            logging.info(f"[feed_loading] phase3: retry:{retry}, duty:{duty}, period:{period}")
                    except Exception as e:
                        self.channel_error[ch] = FEED_ERR_MOVE_EXTRUDE
                        self.exception_code[ch] = 51
                        logging.error("[feed_loading] phase3: except rawinfo: %s", str(e))
                        raise ValueError('logic error!')
                    finally:
                        self._hang_neutral(ch)

                    if extruded == False:
                        self.channel_error[ch] = FEED_ERR_MOVE_EXTRUDE
                        self.exception_code[ch] = 51
                        raise ValueError('logic error!')

                    # flush filaments
                    self._set_channel_state(ch, FEED_STA_LOAD_FLUSHING)
                    try:
                        self.toolhead.wait_moves()
                        self.gcode.run_script_from_command("INNER_FLUSH_FILAMENT TEMP=%d SOFT=%d\r\n" %
                                            (filament_feed_temp, int(filament_soft)))
                        self.toolhead.wait_moves()
                    except:
                        self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                        raise ValueError('custom gcode error!')

                    # Extra purge extrusion after flush to clear residual color
                    extra_purge = getattr(self.ace, 'extra_purge_length', 0) if self.ace else 0
                    if extra_purge > 0:
                        try:
                            self.gcode.run_script_from_command(
                                "G1 E%.1f F300\r\n" % extra_purge)
                            self.toolhead.wait_moves()
                        except Exception as e:
                            logging.warning('extra_purge_length extrusion failed: %s' % str(e))

                    # not need!
                    # if self._port[ch].get_filament_detected() == False:
                    #     self.channel_error[ch] = FEED_ERR_NO_FILAMENT
                    #     raise

                    self.channel_error[ch] = FEED_OK
                    self._set_channel_state(ch, FEED_STA_LOAD_FINISH, True)

                    # Set head_source so _on_extruder_change can auto-switch ACE.
                    # FEED_AUTO always loads head N from the ACE slot matching the feeder
                    # channel (slot = filament_ch[ch]). For ACE_SWAP_HEAD loads (which can
                    # load from arbitrary slots), head_source is set in ace.py directly.
                    if self.ace is not None and getattr(self.ace, '_ace_mode', '') == 'multi':
                        head_idx = self.filament_ch[ch]
                        ace_slot = head_idx  # FEED_AUTO: slot always matches head index
                        ace_idx = self.ace._active_device_index
                        slot_info = self.ace._info.get('slots', [{}] * 4)
                        if ace_slot < len(slot_info):
                            si = slot_info[ace_slot]
                        else:
                            si = {}
                        self.ace._head_source[head_idx] = {
                            'ace_index': ace_idx,
                            'slot': ace_slot,
                            'type': si.get('type', 'PLA'),
                            'color': self.ace.rgb2hex(*si.get('color', (0, 0, 0))),
                            'brand': si.get('brand', 'Generic'),
                        }
                        self.ace._save_head_source()
                        logging.info('FEED_AUTO LOAD: head_source[%d] -> ACE %d / Slot %d' % (
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

            # unload
            elif action == FEED_ACT_UNLOAD:
                # Added: Disable feed_assist before unloading to prevent
                # ACE from continuing to push filament while we're trying to retract.
                if self.ace is not None:
                    self.ace._disable_feed_assist()
                
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
                        # prepare
                        self._set_channel_state(ch, FEED_STA_UNLOAD_PREPARE, True)

                        # home
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HOMING)
                            if self._check_homing_xy() != True:
                                self.gcode.run_script_from_command("G28 X Y\r\n")
                                self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_HOME
                            raise

                        # switch extruder
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_PICKING)
                            self.gcode.run_script_from_command("T%d A0\r\n" % (self.filament_ch[ch]))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_SWITCH
                            raise

                        # move to dest position
                        try:
                            self.gcode.run_script_from_command("MOVE_TO_DISCARD_FILAMENT_POSITION\r\n")
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE
                            raise

                        # heat
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HEATING)
                            self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
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
                        # Changed: Original only accepted UNLOAD_HEAT_FINISH state.
                        # ACE_SWITCH sends UNLOAD STAGE=doing for all channels, but only
                        # channels that went through STAGE=prepare will be in UNLOAD_HEAT_FINISH.
                        # All other channels may be in any stable state (finished, failed, idle).
                        # We accept these and return silently instead of throwing state_mismatch.
                        _stable_states = [
                            FEED_STA_UNLOAD_HEAT_FINISH,
                            FEED_STA_UNLOAD_FINISH,
                            FEED_STA_LOAD_FINISH,
                            FEED_STA_LOAD_FAIL,
                            FEED_STA_PRELOAD_FINISH,
                            FEED_STA_PRELOAD_FAIL,
                            FEED_STA_NONE,
                            FEED_STA_INITED,
                            FEED_STA_WAIT_INSERT,  # After unload, channel goes to wait_insert
                        ]
                        if self.channel_state[ch] not in _stable_states:
                            self.channel_error[ch] = FEED_ERR_STATE_MISMATCH
                            raise ValueError('state mismatch!')

                        # Only UNLOAD_HEAT_FINISH means prepare ran — proceed with unload.
                        # All other states: prepare was skipped, ignore silently.
                        if self.channel_state[ch] != FEED_STA_UNLOAD_HEAT_FINISH:
                            logging.info("[feed][unload] ignoring STAGE=doing, no prepare phase (state: %s)" % self.channel_state[ch])
                            self.channel_error[ch] = FEED_OK
                            return

                        # unloading
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_DOING)
                            self.toolhead.wait_moves()
                            self.gcode.run_script_from_command("INNER_FILAMENT_UNLOAD TEMP=%d SOFT=%d\r\n"
                                                            % (filament_feed_temp, int(filament_soft)))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                            raise ValueError('custom gcode error!')

                        # Rewritten: Original did a single retract_fil() with no verification.
                        # Now checks toolhead sensor after retract. If filament still detected,
                        # re-heats the nozzle and runs INNER_FILAMENT_UNLOAD again before retrying.
                        # Up to 3 full unload cycles. Heater turned off only after all attempts.
                        self.toolhead.wait_moves()
                        if self.ace is not None:
                            # Switch to correct ACE for retract if needed
                            head_idx = self.filament_ch[ch]
                            source = self.ace._head_source.get(head_idx)
                            if source and source['ace_index'] != self.ace._active_device_index:
                                logging.info('FEED_AUTO UNLOAD: switching to ACE %d for retract' % source['ace_index'])
                                self.ace._switch_ace_for_head_target(source['ace_index'])

                            for unload_attempt in range(3):
                                self.ace.retract_fil(self.filament_ch[ch])
                                self.ace.wait_ace_ready()
                                if not self.runout_sensor[ch].get_status(0)['filament_detected']:
                                    break
                                logging.info("[feed][unload] filament still detected after retract, retry %d - re-running toolhead unload", unload_attempt + 1)
                                try:
                                    self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
                                    self.toolhead.wait_moves()
                                    self.gcode.run_script_from_command("INNER_FILAMENT_UNLOAD TEMP=%d SOFT=%d\r\n"
                                                                    % (filament_feed_temp, int(filament_soft)))
                                    self.toolhead.wait_moves()
                                except:
                                    logging.info("[feed][unload] toolhead unload retry failed")
                            if self.runout_sensor[ch].get_status(0)['filament_detected']:
                                logging.info("[feed][unload] filament still stuck after 3 full unload attempts")
                        self.gcode.run_script_from_command("M104 S0\r\n")
                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FINISH)

                        # Clear head_source after successful unload
                        if self.ace is not None and getattr(self.ace, '_ace_mode', '') == 'multi':
                            head_idx = self.filament_ch[ch]
                            if self.ace._head_source.get(head_idx) is not None:
                                self.ace._head_source[head_idx] = None
                                self.ace._save_head_source()
                                logging.info('FEED_AUTO UNLOAD: cleared head_source[%d]' % head_idx)

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
                        # prepare
                        self._set_channel_state(ch, FEED_STA_UNLOAD_PREPARE, True)

                        # home
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HOMING)
                            if self._check_homing_xy() != True:
                                self.gcode.run_script_from_command("G28 X Y\r\n")
                                self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_HOME
                            raise

                        # switch extruder
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_PICKING)
                            self.gcode.run_script_from_command("T%d A0\r\n" % (self.filament_ch[ch]))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_SWITCH
                            raise

                        # move to dest position
                        try:
                            self.gcode.run_script_from_command("MOVE_TO_DISCARD_FILAMENT_POSITION\r\n")
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE
                            raise

                        # heat
                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_HEATING)
                            self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_HEAT
                            raise

                        try:
                            self._set_channel_state(ch, FEED_STA_UNLOAD_DOING)
                            self.toolhead.wait_moves()
                            self.gcode.run_script_from_command("INNER_FILAMENT_UNLOAD TEMP=%d SOFT=%d\r\n"
                                                            % (filament_feed_temp, int(filament_soft)))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_CUSTOM_GCODE
                            raise ValueError('custom gcode error!')

                        # Rewritten: Same retry logic as the first unload path.
                        # Original had no ACE retract in this code path at all.
                        self.toolhead.wait_moves()
                        if self.ace is not None:
                            # Switch to correct ACE for retract if needed
                            head_idx = self.filament_ch[ch]
                            source = self.ace._head_source.get(head_idx)
                            if source and source['ace_index'] != self.ace._active_device_index:
                                logging.info('FEED_AUTO UNLOAD: switching to ACE %d for retract' % source['ace_index'])
                                self.ace._switch_ace_for_head_target(source['ace_index'])

                            for unload_attempt in range(3):
                                self.ace.retract_fil(self.filament_ch[ch])
                                self.ace.wait_ace_ready()
                                if not self.runout_sensor[ch].get_status(0)['filament_detected']:
                                    break
                                logging.info("[feed][unload] filament still detected after retract, retry %d - re-running toolhead unload", unload_attempt + 1)
                                try:
                                    self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
                                    self.toolhead.wait_moves()
                                    self.gcode.run_script_from_command("INNER_FILAMENT_UNLOAD TEMP=%d SOFT=%d\r\n"
                                                                    % (filament_feed_temp, int(filament_soft)))
                                    self.toolhead.wait_moves()
                                except:
                                    logging.info("[feed][unload] toolhead unload retry failed")
                            if self.runout_sensor[ch].get_status(0)['filament_detected']:
                                logging.info("[feed][unload] filament still stuck after 3 full unload attempts")
                        self.gcode.run_script_from_command("M104 S0\r\n")
                        self.channel_error[ch] = FEED_OK
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FINISH)

                        # Clear head_source after successful unload
                        if self.ace is not None and getattr(self.ace, '_ace_mode', '') == 'multi':
                            head_idx = self.filament_ch[ch]
                            if self.ace._head_source.get(head_idx) is not None:
                                self.ace._head_source[head_idx] = None
                                self.ace._save_head_source()
                                logging.info('FEED_AUTO UNLOAD: cleared head_source[%d]' % head_idx)

                    except:
                        self.toolhead.wait_moves()
                        self.channel_error_state[ch] = self.channel_state[ch]
                        self.gcode.run_script_from_command("M104 S0\r\n")
                        if self.channel_error[ch] == FEED_OK:
                            self.channel_error[ch] = FEED_ERR
                        self._set_channel_state(ch, FEED_STA_UNLOAD_FAIL)
                        raise

            # manually feed
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

                        # home
                        try:
                            self._set_channel_state(ch, FEED_STA_MANUAL_HOMING)
                            if self._check_homing_xy() != True:
                                self.gcode.run_script_from_command("G28 X Y\r\n")
                                self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_MOVE_HOME
                            raise

                        # switch extruder
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
                        # heat
                        try:
                            self._set_channel_state(ch, FEED_STA_MANUAL_HEATING, True)
                            self.gcode.run_script_from_command("M109 S%d\r\n" % (filament_feed_temp))
                            self.toolhead.wait_moves()
                        except:
                            self.channel_error[ch] = FEED_ERR_HEAT
                            raise

                        # extrude
                        try:
                            self._set_channel_state(ch, FEED_STA_MANUAL_EXTRUDING)
                            self.toolhead.wait_moves()
                            self.gcode.run_script_from_command("INNER_MANUAL_FEED_STAGE_EXTRUDE TEMP=%d SOFT=%d\r\n" %
                                                               (filament_feed_temp, int(filament_soft)))
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
                        # flush
                        try:
                            self.toolhead.wait_moves()
                            self._set_channel_state(ch, FEED_STA_MANUAL_FLUSHING, True)
                            self.gcode.run_script_from_command("INNER_MANUAL_FEED_STAGE_FLUSH TEMP=%d SOFT=%d\r\n" %
                                                (filament_feed_temp, int(filament_soft)))
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
                    # The delay here is for the convenience of updating the status to the client
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

    def get_status(self, eventtime=None):
        filament_detected = []
        filament_detected.append(self._port[FEED_CHANNEL_1].get_filament_detected())
        filament_detected.append(self._port[FEED_CHANNEL_2].get_filament_detected())

        channel_1_dist = {
            'module_exist': self.module_exist[FEED_CHANNEL_1],
            'filament_detected': filament_detected[FEED_CHANNEL_1],
            'disable_auto': not self.config['auto_mode'][FEED_CHANNEL_1],
            'channel_state':self.channel_state[FEED_CHANNEL_1],
            'channel_error':self.channel_error[FEED_CHANNEL_1],
            'channel_error_state': self.channel_error_state[FEED_CHANNEL_1],
            'channel_action_state': self.channel_action_state[FEED_CHANNEL_1]
        }
        channel_2_dist = {
            'module_exist': self.module_exist[FEED_CHANNEL_2],
            'filament_detected': filament_detected[FEED_CHANNEL_2],
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
        present = None
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
        filament_entangle_detect = self.printer.lookup_object(
                f'filament_entangle_detect e{self.filament_ch[channel]}_filament', None)
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
                return

            if is_printing == 1:
                return

            if self.config['auto_mode'][channel] == False:
                return

            if self.runout_sensor[channel] is None or self.runout_sensor[channel].get_status(0)['enabled'] == False:
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
                if filament_entangle_detect is not None:
                    filament_entangle_detect.skip_entangle_check(True)
                self._do_feed(channel, FEED_ACT_LOAD)
            except Exception as e:
                raw_msg =  self.printer.extract_coded_message_field(str(e))
                logging.error("[feed][load] channel[%d] auto load error: %s", channel, raw_msg)
                if self._is_keep_raw_error_info(self.channel_error[channel]):
                    raise
            finally:
                if filament_entangle_detect is not None:
                    filament_entangle_detect.skip_entangle_check(False)
                if machine_state_manager is not None:
                    machine_sta = machine_state_manager.get_status()
                    if str(machine_sta["main_state"]) == "PRINTING":
                        if str(machine_sta["action_code"]) != "PRINT_RESUMING" and str(machine_sta["action_code"]) != "PRINT_REPLENISHING":
                            self.gcode.run_script_from_command("SET_ACTION_CODE ACTION=IDLE")
                    else:
                        self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")
                    self.toolhead.wait_moves()

            if self.channel_state[channel] != FEED_STA_LOAD_FINISH or self.channel_error[channel] != FEED_OK:
                self.gcode.respond_raw(f'{self.channel_state[channel] != FEED_STA_LOAD_FINISH} {self.channel_error[channel] != FEED_OK}')
                msg = 'extruder[%d]: state: %s, error: %s!' % (
                        self.filament_ch[channel],
                        self.channel_error_state[channel],
                        self.channel_error[channel])
                if raw_msg is not None:
                    msg = msg + "raw msg:" + raw_msg

                raise gcmd.error(
                        message = msg,
                        action = 'pause',
                        id = 525,
                        index = self.filament_ch[channel],
                        code = self.exception_code[channel],
                        oneshot = 1,
                        level = 2)

            return

        if need_to_unload == True:
            try:
                if filament_entangle_detect is not None:
                    filament_entangle_detect.skip_entangle_check(True)
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
                        self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")
                raw_msg =  self.printer.extract_coded_message_field(str(e))
                logging.error("[feed][unload] channel[%d]: auto unload error: %s", channel, raw_msg)
                if self._is_keep_raw_error_info(self.channel_error[channel]):
                    raise
            else:
                # cancel or finish
                if stage in [FEED_UNLOAD_STAGE_DOING, FEED_UNLOAD_STAGE_CANCEL]:
                    if machine_state_manager is not None:
                        machine_sta = machine_state_manager.get_status()
                        if str(machine_sta["main_state"]) == "PRINTING":
                            self.gcode.run_script_from_command("SET_ACTION_CODE ACTION=IDLE")
                        else:
                            self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")
            finally:
                if filament_entangle_detect is not None:
                    filament_entangle_detect.skip_entangle_check(False)

            if self.channel_error[channel] != FEED_OK:
                msg = 'extruder[%d]: state: %s, error: %s!' % (
                        self.filament_ch[channel],
                        self.channel_error_state[channel],
                        self.channel_error[channel])
                if raw_msg is not None:
                    msg = msg + "raw msg:" + raw_msg

                raise gcmd.error(
                        message = msg,
                        action = 'pause',
                        id = 525,
                        index = self.filament_ch[channel],
                        code = self.exception_code[channel],
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

        filament_entangle_detect = self.printer.lookup_object(
                f'filament_entangle_detect e{self.filament_ch[channel]}_filament', None)
        machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
        if machine_state_manager is not None:
            machine_sta = machine_state_manager.get_status()
            if str(machine_sta["main_state"]) not in ["IDLE", "PRINTING", "MANUAL_LOAD"]:
                raise gcmd.error('[feed][manual] channel[%d] machine main state error: %s\n'
                                 % (channel, str(machine_sta["main_state"])))

        try:
            if filament_entangle_detect is not None:
                filament_entangle_detect.skip_entangle_check(True)
            if machine_state_manager is not None:
                machine_sta = machine_state_manager.get_status()
                if str(machine_sta["main_state"]) != "PRINTING":
                    self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=MANUAL_LOAD ACTION=MANUAL_LOADING")
            self._do_feed(channel, FEED_ACT_MANUAL_FEED, stage)
        except Exception as e:
            if machine_state_manager is not None:
                machine_sta = machine_state_manager.get_status()
                if str(machine_sta["main_state"]) != "PRINTING":
                    self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")
            raw_msg =  self.printer.extract_coded_message_field(str(e))
            logging.error("[feed][manual] channel[%d]: manual load error: %s", channel, raw_msg)
            if self._is_keep_raw_error_info(self.channel_error[channel]):
                raise
        else:
            # cancel or finish
            if stage in [FEED_MANUAL_STAGE_FINISH, FEED_MANUAL_STAGE_CANCEL]:
                if machine_state_manager is not None:
                    machine_sta = machine_state_manager.get_status()
                    if str(machine_sta["main_state"]) != "PRINTING":
                        self.gcode.run_script_from_command("SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE")
        finally:
            if filament_entangle_detect is not None:
                filament_entangle_detect.skip_entangle_check(False)

        if self.channel_error[channel] != FEED_OK:
            msg = 'extruder[%d]: state: %s, error: %s!' % (
                    self.filament_ch[channel],
                    self.channel_error_state[channel],
                    self.channel_error[channel])
            if raw_msg is not None:
                msg = msg + "raw msg:" + raw_msg

            raise gcmd.error(
                    message = msg,
                    action = 'pause',
                    id = 525,
                    index = self.filament_ch[channel],
                    code = self.exception_code[channel],
                    oneshot = 1,
                    level = 2)
    def cmd_FEED_RUNOUT_EVENT_HANDLE(self, gcmd):
        # Block runout during mid-print swap
        if self.ace is not None and getattr(self.ace, '_swap_in_progress', False):
            logging.info("FEED_RUNOUT_EVENT_HANDLE: blocking during swap")
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

