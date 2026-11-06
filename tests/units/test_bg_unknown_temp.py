"""[ace_bg_swap] unknown_temp: the heat target of a background swap for a
head whose filament identity is unknown.

The field case (klippy logs of a 270-swap day): a spool with no RFID tag and
no declared override is loaded with an empty identity on purpose (the display
shows "?"), so print_task_config holds an empty type and the stock filament DB
answers with its 250 C default. Every background swap of such a head heated to
250 instead of the 215 a declared PLA gets (45 of 45 blank-type swaps, 144 of
145 PLA swaps at 215). The option is off by default, so the DB default stays
until a user opts in; it never overrides a known material or an
[ace_tipform] temperature.
"""
import importlib
import sys
import types

import pytest

from . import harness as H


def _bg_module():
    name = H.PKG + '.ace_bg_swap'
    if name in sys.modules:
        return sys.modules[name]
    H.load_ace()
    if 'chelper' not in sys.modules:
        sys.modules['chelper'] = types.ModuleType('chelper')
    fm = H.PKG + '.force_move'
    if fm not in sys.modules:
        stub = types.ModuleType(fm)
        stub.calc_move_time = None
        sys.modules[fm] = stub
        setattr(sys.modules[H.PKG], 'force_move', stub)
    return importlib.import_module(name)


class Ptc:
    def __init__(self, types_by_head):
        self.types = list(types_by_head)

    def get_status(self, eventtime=None):
        return {'filament_type': self.types}


class Printer:
    def __init__(self, ptc):
        self.ptc = ptc

    def lookup_object(self, name, default=None):
        return self.ptc if name == 'print_task_config' else default


class Ace:
    """tipform_<which>_temp_for as ace.py answers it: a temperature or None."""

    def __init__(self, load=None, unload=None):
        self.temps = {'load': load, 'unload': unload}

    def tipform_load_temp_for(self, head, soft=False):
        return self.temps['load']

    def tipform_unload_temp_for(self, head, soft=False):
        return self.temps['unload']


def _engine(types_by_head, unknown_temp=0.):
    BG = _bg_module()
    e = BG.AceBgSwap.__new__(BG.AceBgSwap)
    e.printer = Printer(Ptc(types_by_head))
    e.unknown_temp = unknown_temp
    e.said = []
    e._say = e.said.append
    e._dh = lambda idx: idx + 1
    return e


class TestUnknownIdentityTemp:
    @pytest.mark.parametrize('ftype', ['', 'NONE', 'none', '  ', None])
    def test_an_empty_type_is_unknown(self, ftype):
        e = _engine([ftype, 'PLA'], unknown_temp=215.)
        assert e._unknown_identity_temp(Ace(), 0, 'unload') == 215.

    def test_off_by_default_reports_unknown_with_zero(self):
        e = _engine([''])
        assert e._unknown_identity_temp(Ace(), 0, 'unload') == 0.

    @pytest.mark.parametrize('ftype', ['PLA', 'petg', 'ABS'])
    def test_a_known_type_is_left_alone(self, ftype):
        e = _engine([ftype], unknown_temp=215.)
        assert e._unknown_identity_temp(Ace(), 0, 'load') is None

    def test_the_other_heads_types_do_not_matter(self):
        e = _engine(['', 'PLA', '', 'PETG'], unknown_temp=215.)
        assert e._unknown_identity_temp(Ace(), 1, 'load') is None
        assert e._unknown_identity_temp(Ace(), 3, 'load') is None
        assert e._unknown_identity_temp(Ace(), 2, 'load') == 215.

    @pytest.mark.parametrize('which', ['load', 'unload'])
    def test_a_tipform_temperature_wins(self, which):
        e = _engine([''], unknown_temp=215.)
        ace = Ace(**{which: 230})
        assert e._unknown_identity_temp(ace, 0, which) is None

    def test_a_tipform_load_temp_does_not_hide_an_unknown_unload(self):
        e = _engine([''], unknown_temp=215.)
        assert e._unknown_identity_temp(Ace(load=230), 0, 'unload') == 215.

    def test_nothing_to_look_at_means_no_change(self):
        e = _engine([''], unknown_temp=215.)
        e.printer = Printer(None)                # no print_task_config
        assert e._unknown_identity_temp(Ace(), 0, 'load') is None
        e = _engine([], unknown_temp=215.)       # head out of range
        assert e._unknown_identity_temp(Ace(), 0, 'load') is None

    def test_an_ace_without_the_tipform_lookup_still_works(self):
        e = _engine([''], unknown_temp=215.)
        assert e._unknown_identity_temp(object(), 0, 'load') == 215.


class TestExplanation:
    def test_names_the_option_when_it_is_set(self):
        e = _engine([''], unknown_temp=215.)
        e._say_unknown_temp(0, 215., 'load')
        (msg,) = e.said
        assert msg.startswith('head 1:')
        assert 'identity unknown' in msg and 'unknown_temp' in msg
        assert '215' in msg

    def test_explains_the_default_and_the_fix_when_it_is_off(self):
        e = _engine([''])
        e._say_unknown_temp(2, 250., 'unload')
        (msg,) = e.said
        assert msg.startswith('head 3:')
        assert '250' in msg and 'filament DB default' in msg
        assert 'declare the spool' in msg and 'unknown_temp' in msg


class Cfg:
    class error(Exception):
        pass

    def __init__(self, value=None):
        self.value = value

    def getfloat(self, name, default=None, minval=None, maxval=None):
        assert name == 'unknown_temp'
        v = default if self.value is None else self.value
        if minval is not None and v < minval:
            raise self.error('below minval')
        if maxval is not None and v > maxval:
            raise self.error('above maxval')
        return v


class TestOption:
    def read(self, value):
        return _bg_module().AceBgSwap._read_unknown_temp(Cfg(value))

    def test_defaults_to_off(self):
        assert self.read(None) == 0.

    @pytest.mark.parametrize('value', [175., 215., 350.])
    def test_accepts_a_load_temperature(self, value):
        assert self.read(value) == value

    @pytest.mark.parametrize('value', [1., 100., 174.9])
    def test_refuses_a_temperature_too_low_to_move_filament(self, value):
        with pytest.raises(Cfg.error, match='0 \(off\) or 175-350'):
            self.read(value)

    @pytest.mark.parametrize('value', [-1., 351.])
    def test_out_of_range_is_refused_by_the_config_reader(self, value):
        with pytest.raises(Cfg.error):
            self.read(value)
