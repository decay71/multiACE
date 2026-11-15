"""A bulk retract cut off by a restart is finished before the head loads."""
import types

from .harness import load_ace

A = load_ace()
M = A.MultiAce


class Gcmd:
    def __init__(self, **p):
        self.p = p

    def get_int(self, name, default=None):
        return self.p.get(name, default)


def rig(stops=True, sensor=False, connected=True, full=2424):
    s = types.SimpleNamespace()
    s.saved, s.warned, s.retracts = [], [], []
    s._retract_pending = {}
    s._ace_mode = 'multi'
    s.VARS_ACE_RETRACT_PENDING = M.VARS_ACE_RETRACT_PENDING
    proto = types.SimpleNamespace(MOTION_STOPS_WITH_HOST=stops)
    s._protocols = {0: proto, 1: proto}
    s._connected_per_ace = {0: connected, 1: connected}
    s.save_variable = lambda name, data, write=True: s.saved.append(dict(data))
    s._head_sensor_present = lambda head: sensor
    s.get_retract_length = lambda idx, slot: full
    s.get_retract_speed = lambda idx: 80
    s._t = lambda key, **kw: key
    s._disp = lambda n: n + 1
    s.log_warn = s.warned.append
    s._retract = lambda slot, length, speed, head=None, ace=None: (
        s.retracts.append((ace, slot, length)))
    for name in ('_retract_stops_with_host', '_retract_pending_mark',
                 '_retract_pending_clear', '_save_retract_pending',
                 '_finish_interrupted_retract'):
        setattr(s, name, types.MethodType(getattr(M, name), s))
    return s


def test_bulk_retract_is_noted_and_cleared():
    s = rig()
    key = s._retract_pending_mark(1, 2, 940, head=2)
    assert s._retract_pending == {
        '1_2': {'ace': 1, 'slot': 2, 'len': 940, 'head': 2}}
    assert s.saved[-1] == s._retract_pending
    s._retract_pending_clear(key)
    assert s._retract_pending == {} and s.saved[-1] == {}


def test_short_moves_and_self_running_units_are_not_noted():
    assert rig()._retract_pending_mark(1, 2, 150, head=2) is None
    assert rig(stops=False)._retract_pending_mark(1, 2, 940, head=2) is None


def test_rest_is_pulled_back_before_another_slot_loads():
    s = rig()
    s._retract_pending_mark(1, 2, 940, head=2)
    s._finish_interrupted_retract(Gcmd(ACE=0, SLOT=2), 2)
    assert s.retracts == [(1, 2, 940)]
    assert s.warned == ['msg.retract_interrupted_finish']
    assert s._retract_pending == {}


def test_redo_is_capped_by_the_full_unload_length():
    s = rig(full=1950)
    s._retract_pending_mark(1, 2, 1000, head=2)
    s._finish_interrupted_retract(Gcmd(ACE=0, SLOT=2), 2)
    assert s.retracts == [(1, 2, 800)]          # 1950 - 1000 - 150


def test_no_room_left_asks_for_a_manual_pull():
    s = rig(full=1950)
    s._retract_pending_mark(1, 2, 1800, head=2)
    s._finish_interrupted_retract(Gcmd(ACE=0, SLOT=2), 2)
    assert s.retracts == []
    assert s.warned == ['msg.retract_interrupted_manual']


def test_same_strand_loading_again_needs_no_redo():
    s = rig()
    s._retract_pending_mark(1, 2, 940, head=2)
    s._finish_interrupted_retract(Gcmd(ACE=1, SLOT=2), 2)
    assert s.retracts == [] and s._retract_pending == {}


def test_other_head_and_filament_still_in_the_head_are_left_alone():
    s = rig()
    s._retract_pending_mark(1, 2, 940, head=2)
    s._finish_interrupted_retract(Gcmd(ACE=0, SLOT=1), 1)
    assert s.retracts == [] and '1_2' in s._retract_pending
    s2 = rig(sensor=True)
    s2._retract_pending_mark(1, 2, 940, head=2)
    s2._finish_interrupted_retract(Gcmd(ACE=0, SLOT=2), 2)
    assert s2.retracts == [] and '1_2' in s2._retract_pending
