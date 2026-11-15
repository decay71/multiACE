"""ACE_BG_SET_MODE: the background swap mode as a live setting.

Driven with the real method on a stub engine: the three modes map onto
the engine's load_mode and the [ace_bg_swap] load_enabled config line,
a print refuses the change, and a running op keeps the mode it started
with (_run_swap snapshots it).
"""
import pytest

from .test_bg_pick_notice import _bg_module


class Gcmd(object):
    def __init__(self, **params):
        self.params = params

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))

    def error(self, msg):
        return RuntimeError(msg)


class Ace(object):
    def __init__(self):
        self.writes = []

    def _wt_persist(self, gcmd, option, value, legacy, section='ace'):
        self.writes.append((section, option, value))
        return ''


def _engine(printing=False, ace=None):
    BG = _bg_module()

    class Printer(object):
        def lookup_object(self, name, default=None):
            return ace if name == 'ace' else default

    class Bg(object):
        cmd_ACE_BG_SET_MODE = BG.AceBgSwap.cmd_ACE_BG_SET_MODE
        _bg_error = BG.AceBgSwap._bg_error
        MODE_CONFIG = BG.AceBgSwap.MODE_CONFIG
        MODE_INTERNAL = BG.AceBgSwap.MODE_INTERNAL

        def __init__(self):
            self.printer = Printer()
            self.load_mode = 'full'
            self.load_enabled = True
            self.said = []

        def _print_active(self):
            return printing

        def _say(self, msg):
            self.said.append(msg)
    return Bg()


@pytest.mark.parametrize('mode,internal,cfg', [
    ('transport', 'transport', 'transport'),
    ('unload', 'none', 'False'),
    ('full', 'full', 'True'),
])
def test_mode_applies_and_writes_config(mode, internal, cfg):
    ace = Ace()
    bg = _engine(ace=ace)
    bg.cmd_ACE_BG_SET_MODE(Gcmd(MODE=mode.upper()))
    assert bg.load_mode == internal
    assert bg.load_enabled == (internal != 'none')
    assert ace.writes == [('ace_bg_swap', 'load_enabled', cfg)]


def test_refused_while_printing():
    ace = Ace()
    bg = _engine(printing=True, ace=ace)
    with pytest.raises(RuntimeError) as ei:
        bg.cmd_ACE_BG_SET_MODE(Gcmd(MODE='unload'))
    assert 'while a print runs' in str(ei.value)
    assert bg.load_mode == 'full'
    assert ace.writes == []


def test_unknown_mode_refused():
    bg = _engine(ace=Ace())
    with pytest.raises(RuntimeError):
        bg.cmd_ACE_BG_SET_MODE(Gcmd(MODE='half'))
    assert bg.load_mode == 'full'


def test_without_write_through_still_applies():
    bg = _engine(ace=None)
    bg.cmd_ACE_BG_SET_MODE(Gcmd(MODE='transport'))
    assert bg.load_mode == 'transport'
    assert 'not saved' in bg.said[-1]


def test_run_swap_reads_the_mode_once():
    import inspect
    src = inspect.getsource(_bg_module().AceBgSwap._run_swap)
    assert src.count('self.load_mode') == 1
    assert 'self.load_enabled' not in src
