"""ACE_SET_DRIVER on stubs: the last active driver stays on, a driver whose
unit feeds a head with filament stays on, no driver goes off while a head
holds filament of unknown source (the toolhead sensor decides, not the
bookkeeping), a switch writes its enable_<driver> config line and only
changes the configured state (the running discovery keeps its drivers
until the firmware restart).
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


def _rig(cfg=None, printing=False, write_err=None, mode='multi',
         sensors=None, sources=None, staged=None, protocols=None,
         uses_ace=(0, 1, 2, 3), wiring=None):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    D = importlib.import_module(H.PKG + '.ace_drivers')
    V1 = importlib.import_module(H.PKG + '.ace_protocol_v1').AceProtocolV1
    V2 = importlib.import_module(H.PKG + '.ace_protocol_v2').AceProtocolV2
    kinds = {'v1': V1, 'v2': V2}

    class Ace(object):
        cmd_ACE_SET_DRIVER = A.MultiAce.cmd_ACE_SET_DRIVER
        _driver_switches_status = A.MultiAce._driver_switches_status
        _driver_off_blocker = A.MultiAce._driver_off_blocker

        def _is_actively_printing(self):
            return printing

        def _driver_places(self, drv):
            return []

        def _head_sensor_present(self, head):
            return (sensors or {}).get(head, False)

        def head_uses_ace(self, head):
            return head in uses_ace

        def head_ace_for(self, head):
            return (wiring or {}).get(head, head)

        def _cfg_write_ace_option(self, option, value):
            self.written.append((option, value))
            return write_err

        def _ace_error(self, gcmd, text, code):
            return Refused(text)

        def _t(self, key, **kw):
            self.params = kw
            return key

        def _disp(self, idx):
            return idx + 1

        def log_always(self, msg):
            self.said.append(msg)

    ace = Ace()
    ace._ace_mode = mode
    ace._drivers_enabled = {'ace_v1': True, 'ace_v2': True}
    ace._drivers_cfg = dict(cfg or ace._drivers_enabled)
    ace._head_source = dict(sources or {})
    ace._bg_staged = dict(staged or {})
    ace._protocols = {i: kinds[k]() for i, k in (protocols or {}).items()}
    ace.written = []
    ace.said = []
    return ace, D


def _set(ace, name, enable):
    ace.cmd_ACE_SET_DRIVER(FakeGcmd(NAME=name, ENABLE=enable))


def test_switch_off_writes_line_and_keeps_running_state():
    ace, _ = _rig()
    _set(ace, 'ace_v1', 0)
    assert ace.written == [('enable_ace_v1', 'false')]
    assert ace._drivers_cfg['ace_v1'] is False
    assert ace._drivers_enabled['ace_v1'] is True
    row = [d for d in ace._driver_switches_status() if d['name'] == 'ace_v1'][0]
    assert row['enabled'] is False and row['active'] is True
    assert ace.said == ['msg.driver_disabled']


def test_last_active_driver_stays_on():
    ace, _ = _rig(cfg={'ace_v1': True, 'ace_v2': False})
    with pytest.raises(Refused, match='driver_last'):
        _set(ace, 'ace_v1', 0)
    assert ace.written == [] and ace._drivers_cfg['ace_v1'] is True


def test_head_with_filament_from_its_unit_blocks_only_that_driver():
    rig = dict(sensors={2: True}, sources={2: {'ace_index': 2, 'slot': 0}},
               protocols={0: 'v2', 1: 'v2', 2: 'v1', 3: 'v1'})
    ace, _ = _rig(**rig)
    with pytest.raises(Refused, match='driver_feeding'):
        _set(ace, 'ace_v1', 0)
    assert ace.params == {'driver': 'AceV1', 'head': 3, 'ace': 3}
    assert ace.written == []
    ace, _ = _rig(**rig)
    _set(ace, 'ace_v2', 0)
    assert ace._drivers_cfg['ace_v2'] is False


def test_stale_staged_entry_on_an_empty_head_blocks_nothing():
    # HW 2026-10-27: STAGED head 1 -> MMU 1 was restored at every start
    # while e1 reported no filament; the first guard refused AceV2 for it.
    ace, _ = _rig(sensors={1: False}, staged={1: (1, 2)},
                  protocols={1: 'v2'})
    _set(ace, 'ace_v2', 0)
    assert ace._drivers_cfg['ace_v2'] is False


def test_staged_head_with_filament_blocks_its_driver():
    ace, _ = _rig(sensors={1: True}, staged={1: (1, 2)},
                  protocols={1: 'v2'})
    with pytest.raises(Refused, match='driver_feeding'):
        _set(ace, 'ace_v2', 0)


def test_stale_head_source_on_an_empty_head_blocks_nothing():
    ace, _ = _rig(sensors={2: False}, sources={2: {'ace_index': 2}},
                  protocols={2: 'v1'})
    _set(ace, 'ace_v1', 0)
    assert ace._drivers_cfg['ace_v1'] is False


def test_unknown_source_blocks_every_driver_in_multi():
    for name in ('ace_v1', 'ace_v2'):
        ace, _ = _rig(sensors={0: True}, protocols={0: 'v2', 2: 'v1'})
        with pytest.raises(Refused, match='driver_unknown_source$'):
            _set(ace, name, 0)
        assert ace.params['head'] == 1 and 'ace' not in ace.params


def test_unknown_source_names_the_wired_unit_in_head_mode():
    ace, _ = _rig(mode='head', sensors={0: True}, wiring={0: 2},
                  protocols={2: 'v1'})
    with pytest.raises(Refused, match='driver_unknown_source_wired'):
        _set(ace, 'ace_v2', 0)
    assert ace.params['head'] == 1 and ace.params['ace'] == 3


def test_unknown_source_wins_over_a_known_one():
    ace, _ = _rig(sensors={0: True, 2: True},
                  sources={2: {'ace_index': 2}}, protocols={2: 'v1'})
    with pytest.raises(Refused, match='driver_unknown_source'):
        _set(ace, 'ace_v1', 0)


def test_feeder_head_with_filament_does_not_count():
    ace, _ = _rig(mode='head', sensors={3: True}, uses_ace=(0, 1, 2))
    _set(ace, 'ace_v1', 0)
    assert ace._drivers_cfg['ace_v1'] is False


def test_switch_on_is_never_blocked_by_the_guards():
    ace, _ = _rig(cfg={'ace_v1': True, 'ace_v2': False}, sensors={0: True})
    _set(ace, 'ace_v2', 1)
    assert ace.written == [('enable_ace_v2', 'true')]
    assert ace._drivers_cfg['ace_v2'] is True


def test_refused_while_printing():
    ace, _ = _rig(printing=True)
    with pytest.raises(Refused, match='driver_printing'):
        _set(ace, 'ace_v2', 1)


def test_unknown_name_and_failed_write():
    ace, _ = _rig()
    with pytest.raises(Refused, match='NAME must be one of'):
        _set(ace, 'ace3', 1)
    ace, _ = _rig(write_err='cannot write')
    with pytest.raises(Refused, match='not written'):
        _set(ace, 'ace_v1', 0)
    assert ace._drivers_cfg['ace_v1'] is True


def test_every_driver_has_a_label():
    _, D = _rig()
    for d in D.DRIVERS:
        assert d.label
