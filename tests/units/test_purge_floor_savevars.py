"""Three settings of 2026-11-03 on stubs (no printer):

- SAVE_VARIABLE is written power-safe (temp file + sync + rename, .bak copy)
  and an empty state file is restored from the .bak at start. HW 2026-11-02:
  a power cycle left ace_vars.cfg empty, head mode came back as normal mode.
- swap_purge_floor is the one minimum: the engine policy and the
  post-processor's per-pair stamps both use it (0 = 40 mm), and a stamp is
  never 0 (LENGTH=0 / PURGE=0 mean the stock 80 mm).
- load_feed_temp_drop replaces the stock 50 C drop during the inline feed.
"""
import ast
import configparser
import importlib
import os
import sys
import types

from . import harness as H

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# -- SAVE_VARIABLE ----------------------------------------------------------

class StockSaveVariables:
    """The stock save_variables loader (2.0.0, identical since 1.4.1)."""
    def __init__(self, filename):
        self.filename = filename
        self.allVariables = {}
        if not os.path.exists(filename):
            open(filename, 'w').close()
        self.loadVariables()

    def loadVariables(self):
        allvars = {}
        varfile = configparser.ConfigParser()
        varfile.read(self.filename)
        if varfile.has_section('Variables'):
            for name, val in varfile.items('Variables'):
                allvars[name] = ast.literal_eval(val)
        self.allVariables = allvars

    def cmd_SAVE_VARIABLE(self, gcmd):
        raise AssertionError('stock writer must be replaced')


class GcodeError(Exception):
    pass


class FakeGcmd:
    error = GcodeError

    def __init__(self, **params):
        self.params = params

    def get(self, name, default=None):
        return self.params.get(name, default)


class FakeGcode:
    def __init__(self):
        self.handlers = {'SAVE_VARIABLE': 'stock'}

    def register_command(self, cmd, func, desc=None):
        if func is None:
            self.handlers.pop(cmd, None)
            return
        assert cmd not in self.handlers
        self.handlers[cmd] = func


class FakePrinter:
    def __init__(self, gcode):
        self.gcode = gcode

    def lookup_object(self, name, default=None):
        return self.gcode if name == 'gcode' else default


def _settings_rig(path):
    H.load_ace()
    S = importlib.import_module(H.PKG + '.ace_settings')

    class Ace(S.SettingsMixin):
        pass
    ace = Ace()
    ace.save_variables = StockSaveVariables(str(path))
    ace.printer = FakePrinter(FakeGcode())
    return ace


def test_save_variable_writes_the_stock_format_and_a_backup(tmp_path):
    f = tmp_path / 'ace_vars.cfg'
    f.write_text("[Variables]\nace__mode = 'head'\n")
    ace = _settings_rig(f)
    ace._install_atomic_save_variables()
    handler = ace.printer.gcode.handlers['SAVE_VARIABLE']
    assert handler == ace._cmd_save_variable_atomic
    handler(FakeGcmd(VARIABLE='ace__revision', VALUE='3'))
    assert ace.save_variables.allVariables == {'ace__mode': 'head',
                                               'ace__revision': 3}
    text = f.read_text()
    assert text == (tmp_path / 'ace_vars.cfg.bak').read_text()
    assert text.startswith('[Variables]\n')
    assert not (tmp_path / 'ace_vars.cfg.tmp').exists()


def test_bad_literal_is_refused_like_stock(tmp_path):
    f = tmp_path / 'ace_vars.cfg'
    f.write_text("[Variables]\nace__mode = 'head'\n")
    ace = _settings_rig(f)
    ace._install_atomic_save_variables()
    try:
        ace._cmd_save_variable_atomic(FakeGcmd(VARIABLE='x', VALUE='abc'))
    except GcodeError as e:
        assert 'Unable to parse' in str(e)
    else:
        raise AssertionError('no error')
    assert "ace__mode = 'head'" in f.read_text()


def test_empty_file_is_restored_from_the_backup(tmp_path):
    f = tmp_path / 'ace_vars.cfg'
    (tmp_path / 'ace_vars.cfg.bak').write_text(
        "[Variables]\nace__mode = 'head'\nace__head_ace = {'0': 1}\n")
    f.write_text('')
    ace = _settings_rig(f)
    assert ace.save_variables.allVariables == {}
    ace._install_atomic_save_variables()
    assert ace.save_variables.allVariables['ace__mode'] == 'head'
    assert "ace__mode = 'head'" in f.read_text()
    assert ace._sv_restored_from.endswith('ace_vars.cfg.bak')


def test_a_filled_file_is_left_alone(tmp_path):
    f = tmp_path / 'ace_vars.cfg'
    f.write_text("[Variables]\nace__mode = 'multi'\n")
    (tmp_path / 'ace_vars.cfg.bak').write_text(
        "[Variables]\nace__mode = 'head'\n")
    ace = _settings_rig(f)
    ace._install_atomic_save_variables()
    assert ace.save_variables.allVariables == {'ace__mode': 'multi'}
    assert ace._sv_restored_from is None


def test_no_filename_no_install(tmp_path):
    ace = _settings_rig(tmp_path / 'x.cfg')
    del ace.save_variables.filename
    ace._install_atomic_save_variables()
    assert ace.printer.gcode.handlers['SAVE_VARIABLE'] == 'stock'


# -- purge floor ------------------------------------------------------------

def _pp():
    tools = os.path.join(ROOT, 'multiace', 'tools')
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module('post_process_virtual_toolheads')


def test_stamp_floor_defaults_to_40_and_follows_the_setting():
    pp = _pp()
    m = [[0., 24.05], [480.99, 0.]]          # 10 mm and 200 mm raw
    try:
        pp.set_purge_floor(0)
        assert pp._matrix_purge_mm(m, 0, 1) == 40
        assert pp._matrix_purge_mm(m, 1, 0) == 90
        pp.set_purge_floor(None)
        assert pp._matrix_purge_mm(m, 0, 1) == 40
        pp.set_purge_floor(2)
        assert pp._matrix_purge_mm(m, 0, 1) == 5      # 10 mm x 0.45
        pp.set_purge_floor(0.4)
        tiny = [[0., 0.5], [0.5, 0.]]
        assert pp._matrix_purge_mm(tiny, 0, 1) == 1  # never a 0 stamp
    finally:
        pp.set_purge_floor(None)


def _swap(floor=0, scale=100, cap=0):
    H.load_ace()
    S = importlib.import_module(H.PKG + '.ace_swap')

    class Ace(object):
        apply_purge_policy = S.SwapMixin.apply_purge_policy
    a = Ace()
    a.swap_purge_floor, a.swap_purge_scale, a.swap_purge_cap = \
        floor, scale, cap
    return a


def test_engine_policy_uses_the_floor_instead_of_the_fixed_40():
    assert _swap().apply_purge_policy(12) == 12          # defaults: untouched
    assert _swap().apply_purge_policy(0) == 0
    assert _swap(scale=50).apply_purge_policy(60) == 40  # floor 0 = 40
    assert _swap(floor=10).apply_purge_policy(12) == 12
    assert _swap(floor=10).apply_purge_policy(5) == 10
    assert _swap(floor=10).apply_purge_policy(0) == 80   # unstamped = stock
    assert _swap(floor=10, cap=8).apply_purge_policy(50) == 10


def test_cost_model_mirrors_the_engine():
    tools = os.path.join(ROOT, 'multiace', 'tools')
    if tools not in sys.path:
        sys.path.insert(0, tools)
    sc = importlib.import_module('swap_cost')
    m = sc.SwapCostModel({'swap_purge_floor': 10})
    assert m.apply_purge_policy(12) == 12
    assert m.apply_purge_policy(5) == 10
    assert m.swap_purge_min == 10
    m0 = sc.SwapCostModel({'swap_purge_scale': 50})
    assert m0.apply_purge_policy(60) == 40
    assert sc.SwapCostModel({}).swap_purge_min == 40


# -- load feed temp drop ----------------------------------------------------

def test_feed_temp_drop_reads_the_ace_setting():
    H.load_ace()
    name = H.PKG + '.pulse_counter'
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    F = importlib.import_module(H.PKG + '.filament_feed_ace')

    class Feed(object):
        _load_feed_temp_drop = F.FilamentFeed._load_feed_temp_drop

    class Ace(object):
        pass
    feed = Feed()
    feed.ace = Ace()
    assert feed._load_feed_temp_drop() == 50          # older ace.py
    feed.ace.load_feed_temp_drop = 0
    assert feed._load_feed_temp_drop() == 0
    feed.ace.load_feed_temp_drop = 15
    assert feed._load_feed_temp_drop() == 15
