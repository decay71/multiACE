"""ACE_SET_V2_CHAIN on stubs: the switch writes its v2_chain config line and
only changes the configured state (the running buses keep their setting
until the firmware restart); it is refused while printing, switched ON
without the ACE 2 driver, and switched OFF while a cable still carries
more than one unit (without the chain every unit on a shared cable acts
on every command).
"""
import importlib

import pytest

from . import harness as H


class Refused(Exception):
    pass


class FakeGcmd:
    def __init__(self, **params):
        self.params = params

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))


def _rig(monkeypatch, running=True, configured=None, printing=False,
         driver=True, chained=(), write_err=None):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    B = importlib.import_module(H.PKG + '.ace_bus_v2')
    monkeypatch.setattr(B, 'chained_cables', lambda: list(chained))

    class Ace(object):
        cmd_ACE_SET_V2_CHAIN = A.MultiAce.cmd_ACE_SET_V2_CHAIN
        _chain_switch_status = A.MultiAce._chain_switch_status

        def _is_actively_printing(self):
            return printing

        def _cfg_write_ace_option(self, option, value):
            self.written.append((option, value))
            return write_err

        def _ace_error(self, gcmd, text, code):
            return Refused(text)

        def _t(self, key, **kw):
            self.params = kw
            return key

        def log_always(self, msg):
            self.said.append(msg)

    ace = Ace()
    ace._v2_chain = running
    ace._v2_chain_cfg = running if configured is None else configured
    ace._drivers_cfg = {'ace_v1': True, 'ace_v2': driver}
    ace.written = []
    ace.said = []
    return ace


def _set(ace, enable):
    ace.cmd_ACE_SET_V2_CHAIN(FakeGcmd(ENABLE=enable))


def test_switch_off_writes_line_and_keeps_running_state(monkeypatch):
    ace = _rig(monkeypatch, running=True)
    _set(ace, 0)
    assert ace.written == [('v2_chain', 'false')]
    st = ace._chain_switch_status()
    assert st['enabled'] is False and st['active'] is True
    assert ace.said == ['msg.chain_disabled']


def test_switch_on_writes_line(monkeypatch):
    ace = _rig(monkeypatch, running=False)
    _set(ace, 1)
    assert ace.written == [('v2_chain', 'true')]
    st = ace._chain_switch_status()
    assert st['enabled'] is True and st['active'] is False
    assert ace.said == ['msg.chain_enabled']


def test_off_refused_while_a_cable_carries_more_than_one_unit(monkeypatch):
    ace = _rig(monkeypatch, running=True, chained=['/dev/serial/by-path/x'])
    with pytest.raises(Refused) as e:
        _set(ace, 0)
    assert str(e.value) == 'msg.chain_off_chained'
    assert ace.params == {'count': 1}
    assert ace.written == []
    assert ace._chain_switch_status()['chained_cables'] == 1


def test_on_is_not_blocked_by_chained_cables(monkeypatch):
    ace = _rig(monkeypatch, running=False, chained=['/dev/serial/by-path/x'])
    _set(ace, 1)
    assert ace.written == [('v2_chain', 'true')]


def test_on_refused_without_the_ace2_driver(monkeypatch):
    ace = _rig(monkeypatch, running=False, driver=False)
    with pytest.raises(Refused) as e:
        _set(ace, 1)
    assert str(e.value) == 'msg.chain_needs_driver'
    assert ace.written == []
    assert ace._chain_switch_status()['available'] is False


def test_off_allowed_without_the_ace2_driver(monkeypatch):
    ace = _rig(monkeypatch, running=True, driver=False)
    _set(ace, 0)
    assert ace.written == [('v2_chain', 'false')]


def test_refused_while_printing(monkeypatch):
    for enable in (0, 1):
        ace = _rig(monkeypatch, running=not enable, printing=True)
        with pytest.raises(Refused) as e:
            _set(ace, enable)
        assert str(e.value) == 'msg.chain_printing'
        assert ace.written == []


def test_failed_write_is_reported_and_keeps_the_state(monkeypatch):
    ace = _rig(monkeypatch, running=True, write_err='cannot write')
    with pytest.raises(Refused):
        _set(ace, 0)
    assert ace._chain_switch_status()['enabled'] is True


def test_every_message_exists_in_every_language():
    import json, pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / 'multiace' / 'i18n'
    keys = ('chain_enabled', 'chain_disabled', 'chain_printing',
            'chain_needs_driver', 'chain_off_chained')
    ui = ('chain_label', 'chain_note_chained', 'chain_note_no_driver',
          'acefw_chained', 'acefw_chain_on', 'acefw_chained_hint',
          'acefw_chain_on_hint')
    for lang in ('en', 'de', 'zh'):
        d = json.loads((root / ('%s.json' % lang)).read_text('utf-8'))
        for k in keys:
            assert d['msg'][k], (lang, k)
        for k in ui:
            assert d['ui']['config'][k], (lang, k)
