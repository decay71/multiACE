"""Unload temp chain of filament_feed_ace: tipform unloadtemp -> the DB
unload_temp when it differs from the DB load temp (firmware 2.1+) -> the
effective load temp. Real methods on a stub feed object."""
import importlib
import sys
import types

from . import harness as H


def _feed_module():
    H.load_ace()
    name = H.PKG + '.pulse_counter'
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return importlib.import_module(H.PKG + '.filament_feed_ace')


class PTC:
    def get_status(self, eventtime=None):
        return {'filament_vendor': ['Generic'] * 4,
                'filament_type': ['PA-CF'] * 4,
                'filament_sub_type': [''] * 4}


class DB:
    def __init__(self, load, unload, has_unload=True):
        self.load, self.unload = load, unload
        if not has_unload:
            self.get_unload_temp = None

    def get_load_temp(self, v, t, s, nozzle_diameter=0.4,
                      nozzle_volume_type='standard'):
        return self.load

    def get_unload_temp(self, v, t, s, nozzle_diameter=0.4,
                        nozzle_volume_type='standard'):
        return self.unload


class Printer:
    def __init__(self, db):
        self.objs = {'print_task_config': PTC(), 'filament_parameters': db}

    def lookup_object(self, name, default=None):
        return self.objs.get(name, default)


class Ace:
    def __init__(self, unload=None, load=None):
        self.u, self.l = unload, load

    def tipform_unload_temp_for(self, head, soft=False):
        return self.u

    def tipform_load_temp_for(self, head, soft=False):
        return self.l


def _feed(db, ace):
    FF = _feed_module().FilamentFeed
    f = FF.__new__(FF)
    f.printer = Printer(db)
    f.ace = ace
    f.filament_ch = [0, 1]
    f._db_nozzle_ok = False
    f._get_filament_soft = lambda ch: False
    return f


def test_tipform_unloadtemp_wins():
    assert _feed(DB(300, 280), Ace(unload=250))._get_filament_unload_temp(0) == 250


def test_db_unload_used_when_it_differs():
    assert _feed(DB(300, 280), Ace())._get_filament_unload_temp(0) == 280


def test_equal_db_pair_keeps_tipform_loadtemp():
    # Pre-2.1 DB: unload == load. The tipform loadtemp stays in force.
    assert _feed(DB(250, 250), Ace(load=215))._get_filament_unload_temp(0) == 215
    assert _feed(DB(250, 250), Ace())._get_filament_unload_temp(0) == 250


def test_db_without_unload_accessor_falls_back():
    assert _feed(DB(260, 0, has_unload=False), Ace())._get_filament_unload_temp(0) == 260


def test_floor_175():
    assert _feed(DB(250, 150), Ace())._get_filament_unload_temp(0) == 175
