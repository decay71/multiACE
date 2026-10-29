"""The unload mapping verdict is decided once at the unload entry (head
mode, no head_source, no bg hint, nothing assisting on the unit) and held
in _unload_no_mapping, so the tip-form pre-prime arms no feed assist on a
guessed slot and the retract guards read the same answer.
"""
import logging
from . import harness as H


def _rig(mode='head', source=None, hint=None, armed=None, uses_ace=True):
    A = H.load_ace()

    class Ace(object):
        _note_unload_mapping = A.MultiAce._note_unload_mapping

        def __init__(self):
            self._ace_mode = mode
            self._head_source = {0: source}
            self._unload_slot_hint = {0: hint} if hint else {}
            self._active_device_index = 0
            self._armed = armed

        def head_uses_ace(self, head):
            return uses_ace

        def _armed_slot_for_ace(self, idx):
            return self._armed
    return Ace()


def test_head_mode_without_mapping_holds_the_verdict():
    ace = _rig()
    assert ace._note_unload_mapping(0) is True
    assert ace._unload_no_mapping == {0: True}


def test_head_source_or_hint_or_assist_means_a_mapping():
    assert _rig(source={'ace_index': 0, 'slot': 1})._note_unload_mapping(0) is False
    assert _rig(hint={'ace_index': 0, 'slot': 2})._note_unload_mapping(0) is False
    assert _rig(armed=2)._note_unload_mapping(0) is False
    assert _rig(mode='multi')._note_unload_mapping(0) is False
    assert _rig(uses_ace=False)._note_unload_mapping(0) is False


def test_a_fresh_entry_drops_the_old_verdict():
    ace = _rig()
    ace._note_unload_mapping(0)
    ace._head_source[0] = {'ace_index': 0, 'slot': 1}
    assert ace._note_unload_mapping(0) is False
    assert ace._unload_no_mapping == {}


def test_pre_prime_arms_no_fa_without_mapping():
    A = H.load_ace()

    class Ace(object):
        _tipform_pre_prime = A.MultiAce._tipform_pre_prime

        def __init__(self, no_map):
            self._head_source = {}
            self._active_device_index = 0
            self._unload_no_mapping = {0: True} if no_map else {}
            self.armed = []
            self.gcode = self
            self.run = []
            self._disp = lambda i: i + 1
            self._raw = lambda i: i
            self.toolhead = self

        def tipform_load_temp_for(self, head, soft):
            return 250

        def _ace_slot_for_head(self, head):
            return 0

        def _cap(self, idx, name):
            return True  # a V2: the push would normally need assist

        def run_script_from_command(self, line):
            self.run.append(line)

        def log_always(self, msg):
            pass

        def _tipform_fa_start(self, idx, slot):
            self.armed.append((idx, slot))
            return True

        def _v2_arm_fa_for_unload(self, head):
            self.armed.append(('unload', head))

        def get_extruder(self):
            raise RuntimeError('no heater in the stub')
    ace = Ace(no_map=True)
    ace._tipform_pre_prime(0, (None, 30, 300), 215, 0)
    assert ace.armed == []
    assert any(l.startswith('G1 E30') for l in ace.run)
    ace = Ace(no_map=False)
    ace._tipform_pre_prime(0, (None, 30, 300), 215, 0)
    assert ace.armed == [(0, 0), ('unload', 0)]


def test_feed_module_guard_reads_the_held_verdict():
    import ast
    src = open('multiace/klipper/extras/filament_feed_ace.py').read()
    assert src.count("getattr(self.ace, '_unload_no_mapping', None)") == 2
    assert src.count('self.ace._note_unload_mapping(') == 1
    tree = ast.parse(src)
    assert tree is not None
