"""ACE_FW_SET_DRIVER and the ACE_FW_RELEASE gate, the Klipper half of the
ace2k flasher in the web: the real methods of ace.py on stubs.

An ace2k unit has the USB id of a stock ACE 2, so its port is named in
[ace] ace2k_ports. After a flash that changes the unit between stock and
ace2k the web writes that line (and enable_ace2k) and restarts Klipper.
ACE_FW_RELEASE hands the port to any unit with a flash engine, ace2k
included.
"""
import importlib
import os

import pytest

from . import harness as H


class Refused(Exception):
    pass


class _Gcmd(object):
    def __init__(self, **params):
        self.params = params

    def get_int(self, name, default=None, minval=None, maxval=None):
        return int(self.params.get(name, default))

    def get(self, name, default=None):
        return self.params.get(name, default)


def _rig(ports=(), engine='ace2k', printing=False):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    M = A.MultiAce

    class Ace(object):
        cmd_ACE_FW_SET_DRIVER = M.cmd_ACE_FW_SET_DRIVER
        cmd_ACE_FW_RELEASE = M.cmd_ACE_FW_RELEASE

        def __init__(self):
            self._ace_devices = ['/dev/serial/by-path/u0', 'absent:uid:x']
            self._ace2k_ports_cfg = list(ports)
            self.writes = []
            self.logs = []
            self._fw_update_hold = set()
            self._auto_dry_started = {}
            self._connected_per_ace = {0: True}
            self._swap_in_progress = False
            self.disconnected = []

        def _disp(self, idx):
            return idx + 1

        def _is_unit_sentinel(self, path):
            return path.startswith(('absent:', 'free:'))

        def _is_actively_printing(self):
            return printing

        def _cfg_write_ace_option(self, option, value, section='ace'):
            self.writes.append((option, value))
            return None

        def _cap(self, idx, name, default=None):
            assert name == 'flash_engine'
            return engine

        def _disconnect_from(self, idx):
            self.disconnected.append(idx)

        def log_always(self, msg):
            self.logs.append(msg)

        def _ace_error(self, gcmd, text, code=0):
            return Refused(text)
    return Ace()


def test_ace2k_adds_the_port_and_enables_the_driver():
    ace = _rig(ports=['/dev/serial/by-path/other'])
    ace.cmd_ACE_FW_SET_DRIVER(_Gcmd(ACE=0, DRIVER='ace2k'))
    assert ace.writes == [
        ('ace2k_ports',
         '/dev/serial/by-path/other, /dev/serial/by-path/u0'),
        ('enable_ace2k', 'True')]
    assert ace._ace2k_ports_cfg[-1] == '/dev/serial/by-path/u0'


def test_ace2k_does_not_list_a_port_twice(tmp_path):
    real = tmp_path / 'ttyACM0'
    real.write_text('')
    link = tmp_path / 'by-path-u0'
    os.symlink(str(real), str(link))
    ace = _rig(ports=[str(real)])
    ace._ace_devices[0] = str(link)
    ace.cmd_ACE_FW_SET_DRIVER(_Gcmd(ACE=0, DRIVER='ace2k'))
    assert ace.writes[0] == ('ace2k_ports', str(link))


def test_stock_removes_the_port():
    ace = _rig(ports=['/dev/serial/by-path/u0', '/dev/serial/by-path/b'])
    ace.cmd_ACE_FW_SET_DRIVER(_Gcmd(ACE=0, DRIVER='stock'))
    assert ace.writes == [('ace2k_ports', '/dev/serial/by-path/b')]


def test_set_driver_refusals():
    ace = _rig()
    with pytest.raises(Refused):
        ace.cmd_ACE_FW_SET_DRIVER(_Gcmd(ACE=0, DRIVER='ace2'))
    with pytest.raises(Refused):
        ace.cmd_ACE_FW_SET_DRIVER(_Gcmd(ACE=1, DRIVER='ace2k'))
    with pytest.raises(Refused):
        ace.cmd_ACE_FW_SET_DRIVER(_Gcmd(ACE=3, DRIVER='ace2k'))
    with pytest.raises(Refused):
        _rig(printing=True).cmd_ACE_FW_SET_DRIVER(
            _Gcmd(ACE=0, DRIVER='ace2k'))
    assert ace.writes == []


@pytest.mark.parametrize('engine', ['iap_v1', 'ota_v2', 'ace2k'])
def test_release_takes_every_flash_engine(engine):
    ace = _rig(engine=engine)
    ace.cmd_ACE_FW_RELEASE(_Gcmd(ACE=0))
    assert ace._fw_update_hold == {0} and ace.disconnected == [0]


@pytest.mark.parametrize('engine', ['none', None])
def test_release_refuses_without_flash_engine(engine):
    ace = _rig(engine=engine)
    with pytest.raises(Refused):
        ace.cmd_ACE_FW_RELEASE(_Gcmd(ACE=0))
    assert ace._fw_update_hold == set()
