"""SM_PRINT_FLOW_CALIBRATE in the start gcode, for an ACE head the
auto-load block loads later: the real wrapper and replay of ace.py on
stubs (no printer).

Two review findings drive these cases. The deferral must honour stock's
own gates (head used, flow calibration on for the job and the head, not
yet measured), else "deferred"/"replaying" show up with flow off. And the
replay must give the active head back: stock leaves T<h> selected, and
inside the auto-load block that would send the wrong head into the
prime line.
"""
from . import harness as H


class FakeExtruder:
    def __init__(self, idx):
        self.extruder_index = idx


class FakeToolhead:
    def __init__(self, idx):
        self.ext = FakeExtruder(idx)
        self.waits = 0

    def get_extruder(self):
        return self.ext

    def wait_moves(self):
        self.waits += 1


class FakePtc:
    def __init__(self, **st):
        self.st = {'extruders_used': [True] * 4, 'flow_calibrate': True,
                   'flow_calib_extruders': [True] * 4,
                   'extruder_map_table': [0, 1, 2, 3],
                   'auto_replenish_filament': False,
                   'extruders_replenished': [0, 1, 2, 3]}
        self.st.update(st)

    def get_status(self, eventtime=None):
        return self.st


class FakePrintStats:
    def __init__(self, done=None):
        self._config = {'print_job': {'flow_calibrate':
                                      done or [False] * 4}}


class FakePrinter:
    def __init__(self, objects):
        self.objects = objects

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class FakeGcmd:
    def __init__(self, raw, **params):
        self.raw = raw
        self.params = params

    def get_int(self, name, default=None):
        v = self.params.get(name)
        return default if v is None else int(v)

    def get_raw_command_parameters(self):
        return self.raw


class FakeGcode:
    def __init__(self, toolhead):
        self.toolhead = toolhead
        self.scripts = []

    def run_script_from_command(self, script):
        self.scripts.append(script)
        word = script.split()[0]
        if word == 'SM_PRINT_FLOW_CALIBRATE':
            # Stock picks the head and leaves it selected.
            for tok in script.split()[1:]:
                if tok.startswith('EXTRUDER='):
                    self.toolhead.ext.extruder_index = int(tok[9:])
        elif word.startswith('T') and word[1:].isdigit():
            self.toolhead.ext.extruder_index = int(word[1:])


def _rig(active=0, loaded=False, ptc=None, done=None):
    A = H.load_ace()

    class Ace(object):
        _flow_calibrate_head = A.MultiAce._flow_calibrate_head
        _flow_calibrate_would_run = A.MultiAce._flow_calibrate_would_run
        _wrap_sm_flow_calibrate = A.MultiAce._wrap_sm_flow_calibrate
        _flow_calibrate_replay = A.MultiAce._flow_calibrate_replay

        def head_uses_ace(self, head):
            return True

        def _head_is_loaded(self, head):
            return self.loaded
    ace = Ace()
    ace.loaded = loaded
    ace.toolhead = FakeToolhead(active)
    ace.gcode = FakeGcode(ace.toolhead)
    ace.printer = FakePrinter({'print_task_config': ptc or FakePtc(),
                               'print_stats': FakePrintStats(done)})
    ace._ace_mode = 'head'
    ace._print_has_gcode_loads = True
    ace._flow_calib_deferred = {}
    ace.stock = []
    ace._orig_sm_flow_calibrate = lambda g: ace.stock.append(g.raw)
    return ace


def test_empty_ace_head_is_deferred():
    ace = _rig()
    ace._wrap_sm_flow_calibrate(FakeGcmd('EXTRUDER=3', EXTRUDER=3))
    assert ace._flow_calib_deferred == {3: 'EXTRUDER=3'}
    assert ace.stock == []


def test_flow_off_runs_stock_and_defers_nothing():
    for ptc, done in ((FakePtc(flow_calibrate=False), None),
                      (FakePtc(flow_calib_extruders=[True, True, True,
                                                     False]), None),
                      (FakePtc(extruders_used=[True, True, True, False]),
                       None),
                      (None, [False, False, False, True])):
        ace = _rig(ptc=ptc, done=done)
        ace._wrap_sm_flow_calibrate(FakeGcmd('EXTRUDER=3', EXTRUDER=3))
        assert ace._flow_calib_deferred == {}
        assert ace.stock == ['EXTRUDER=3']


def test_replenish_mapping_picks_the_stock_head():
    ptc = FakePtc(auto_replenish_filament=True,
                  extruders_replenished=[0, 1, 2, 1])
    ace = _rig(ptc=ptc)
    ace._wrap_sm_flow_calibrate(FakeGcmd('EXTRUDER=3', EXTRUDER=3))
    assert list(ace._flow_calib_deferred) == [1]


def test_unreadable_gates_fail_open():
    ace = _rig()
    ace.printer = FakePrinter({})
    assert ace._flow_calibrate_would_run(3) is True


def test_replay_returns_to_the_previous_head():
    ace = _rig(active=0, loaded=True)
    ace._flow_calib_deferred = {3: 'EXTRUDER=3'}
    ace._flow_calibrate_replay(3)
    assert ace.gcode.scripts == ['SM_PRINT_FLOW_CALIBRATE EXTRUDER=3',
                                 'T0 A0']
    assert ace.toolhead.ext.extruder_index == 0


def test_replay_on_the_active_head_does_not_switch():
    ace = _rig(active=3, loaded=True)
    ace._flow_calib_deferred = {3: 'EXTRUDER=3'}
    ace._flow_calibrate_replay(3)
    assert ace.gcode.scripts == ['SM_PRINT_FLOW_CALIBRATE EXTRUDER=3']


def test_replay_drops_a_still_empty_head():
    ace = _rig(active=0, loaded=False)
    ace._flow_calib_deferred = {3: 'EXTRUDER=3'}
    ace._flow_calibrate_replay(3)
    assert ace.gcode.scripts == []
    assert ace._flow_calib_deferred == {}
