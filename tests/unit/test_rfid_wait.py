"""LOAD_HEAD's wait for the slot's RFID read (ace.py _wait_for_slot_identity).

An untagged spool never reports a read, so the 3 s wait ran in full on every
load of such a slot (234 times in one day of logs). When the slot's override
already names material, colour and brand, the tag can add nothing (the
override wins field by field, _overlay_override), so the load does not wait.
A partial override still waits: the tag would fill the missing field.
"""
import importlib
import os
import sys
import types

import pytest

EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                      'multiace', 'klipper', 'extras')
PKG = 'multiace_rfid_wait_extras'


def _ace():
    name = PKG + '.ace'
    if name in sys.modules:
        return sys.modules[name]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [EXTRAS]
    sys.modules[PKG] = pkg
    return importlib.import_module(name)


class Clock:
    """A reactor whose pause() advances time, optionally flipping the slot's
    rfid field once `rfid_at` seconds have passed."""

    def __init__(self, slot_info=None, rfid_at=None):
        self.now = 100.0
        self.start = self.now
        self.pauses = 0
        self.slot_info = slot_info
        self.rfid_at = rfid_at

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.pauses += 1
        self.now = max(self.now, until)
        if (self.rfid_at is not None
                and self.now - self.start >= self.rfid_at):
            self.slot_info['rfid'] = 2

    def waited(self):
        return self.now - self.start


FULL = {'material': 'PLA', 'color': '#FF0000', 'brand': 'Generic'}


def _make(override=None, rfid=0, rfid_at=None):
    mod = _ace()
    a = mod.MultiAce.__new__(mod.MultiAce)
    slot_info = {'rfid': rfid}
    a._info = {'slots': [slot_info] * 4}
    a.reactor = Clock(slot_info, rfid_at)
    a._slot_overrides = {'0_1': override} if override is not None else {}
    return a


class TestIdentityFullyDeclared:
    def test_material_colour_and_brand_is_enough(self):
        assert _make(FULL)._identity_fully_declared(0, 1) is True

    @pytest.mark.parametrize('missing', ['material', 'color', 'brand'])
    def test_a_missing_field_is_not(self, missing):
        ov = dict(FULL)
        ov.pop(missing)
        assert _make(ov)._identity_fully_declared(0, 1) is False

    @pytest.mark.parametrize('field,value', [
        ('material', '  '), ('color', '#'), ('color', ' '), ('brand', ' ')])
    def test_blank_fields_do_not_count(self, field, value):
        ov = dict(FULL, **{field: value})
        assert _make(ov)._identity_fully_declared(0, 1) is False

    def test_no_override_is_not(self):
        assert _make()._identity_fully_declared(0, 1) is False

    def test_another_slots_override_is_not(self):
        assert _make(FULL)._identity_fully_declared(0, 2) is False

    def test_a_broken_override_store_never_raises(self):
        a = _make(FULL)
        a._slot_overrides = None
        assert a._identity_fully_declared(0, 1) is False


class TestWait:
    def test_a_fully_declared_slot_does_not_wait(self):
        a = _make(FULL)
        a._wait_for_slot_identity(0, 1)
        assert a.reactor.pauses == 0
        assert a.reactor.waited() == 0

    def test_a_partial_override_still_waits_the_full_3_seconds(self):
        a = _make({'material': 'PLA', 'color': '#FF0000'})
        a._wait_for_slot_identity(0, 1)
        assert a.reactor.waited() == pytest.approx(3.0, abs=0.2)

    def test_an_untagged_undeclared_slot_still_waits(self):
        a = _make()
        a._wait_for_slot_identity(0, 1)
        assert a.reactor.waited() == pytest.approx(3.0, abs=0.2)

    def test_a_tag_that_arrives_ends_the_wait_early(self):
        a = _make(rfid_at=0.5)
        a._wait_for_slot_identity(0, 1)
        assert 0.4 < a.reactor.waited() < 1.0

    def test_an_already_read_tag_does_not_wait(self):
        a = _make(rfid=2)
        a._wait_for_slot_identity(0, 1)
        assert a.reactor.pauses == 0
