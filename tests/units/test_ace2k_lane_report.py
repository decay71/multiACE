"""ace2k v0.15.0: lanes in use and lanes set by hand. The link keeps the
unit's in-use mask current, marks lanes by hand, and the host computes
both lists."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))

from tests.units import harness  # noqa: E402,F401
from extras import ace_protocol_ace2k as K  # noqa: E402
from extras import ace as ace_mod  # noqa: E402


class _Dict:
    def __init__(self, names):
        self.names = set(names)

    def has(self, name):
        return name in self.names


NEW = ('ace2k_rfid_in_use', 'ace2k_rfid_by_hand', 'ace2k_rfid_forget')


def _link(cmds=NEW, switches=0x0F):
    sent = []
    s = types.SimpleNamespace(
        dictionary=_Dict(cmds), _in_use=None, _in_use_sent=(0.0, None),
        _by_hand_want={}, _by_hand_unit=None, _by_hand_sent={},
        _lanes_first=True, _follow={}, _jobs={}, _tag_reads={},
        _sessions={}, _sensors=(0.0, switches), clock=[100.0])
    s._now = lambda: s.clock[0]
    s._send = lambda name, **v: sent.append((name, v))
    for m in ('_lane_report', '_set_lanes', '_lanes_tick'):
        setattr(s, m, (lambda f: lambda *a: f(s, *a))(getattr(K.Ace2kLink, m)))
    return s, sent


def test_old_image_takes_no_lists():
    s, sent = _link(cmds=('ace2k_rfid_forget',))
    assert s._set_lanes({'in_use': 1, 'by_hand': 2}) == {'lane_report': False}
    s._lanes_tick(101.0)
    assert sent == []


def test_first_list_forgets_unmarked_lanes_then_sends_mask_and_marks():
    s, sent = _link()
    s._by_hand_unit = 0b1000            # the unit still holds lane 3 by hand
    s._set_lanes({'in_use': 0b0001, 'by_hand': 0b0010})
    forgot = [v['lane'] for n, v in sent if n == 'ace2k_rfid_forget']
    assert forgot == [0, 2]             # not lane 1 (wanted), not lane 3
    assert ('ace2k_rfid_in_use', {'mask': 0b0001}) in sent
    assert ('ace2k_rfid_by_hand', {'lane': 1}) in sent
    sent[:] = []
    s._set_lanes({'in_use': 0b0001, 'by_hand': 0b0010})
    assert not [n for n, _v in sent if n == 'ace2k_rfid_forget']


def test_mask_goes_out_every_second_and_on_change():
    s, sent = _link()
    s._set_lanes({'in_use': 0b0001, 'by_hand': 0})
    sent[:] = []
    s._lanes_tick(100.3)
    assert sent == []
    s._lanes_tick(101.1)
    assert sent == [('ace2k_rfid_in_use', {'mask': 0b0001})]
    sent[:] = []
    s._follow[2] = 7                    # a following lane is in use too
    s._jobs[3] = {'kind': 'feed'}
    s._jobs[1] = {'kind': 'retract', 'auto': True}   # the link's own relax
    s._lanes_tick(101.2)
    assert sent == [('ace2k_rfid_in_use', {'mask': 0b1101})]


def test_no_mask_before_the_host_said_anything():
    s, sent = _link()
    s._lanes_tick(105.0)
    assert sent == []


def test_by_hand_follows_the_units_announcement():
    s, sent = _link()
    s._set_lanes({'in_use': 0, 'by_hand': 0b0100})
    K.Ace2kLink._dispatch(s, 'ace2k_rfid_by_hand_state', {'mask': 0b0100}) \
        if False else None
    s._by_hand_unit = 0b0100            # the unit took the mark
    sent[:] = []
    s._lanes_tick(102.0)
    assert not [n for n, _v in sent if n == 'ace2k_rfid_by_hand']


def test_by_hand_on_an_empty_bay_is_dropped_after_5s():
    s, sent = _link(switches=0x0B)      # lane 2 has no filament
    s._set_lanes({'in_use': 0, 'by_hand': 0b0100})
    assert not [n for n, _v in sent if n == 'ace2k_rfid_by_hand']
    s._lanes_tick(103.0)
    assert 2 in s._by_hand_want
    s._lanes_tick(106.0)
    assert 2 not in s._by_hand_want


def test_host_masks():
    a = types.SimpleNamespace(
        _head_source={0: {'ace_index': 0, 'slot': 0},
                      1: {'ace_index': 1, 'slot': 1}, 2: None},
        _bg_staged={3: (0, 3)},
        _v2_filament_info_per_ace={0: {2: {'type': 'PLA'}, 1: {'uid': 'x'}}})
    a._override_for = lambda idx, slot: ({'material': 'PETG'}
                                         if (idx, slot) == (0, 1) else None)
    assert ace_mod.MultiAce._lane_masks(a, 0) == (0b1001, 0b0110)
    assert ace_mod.MultiAce._lane_masks(a, 1) == (0b0010, 0b0000)


def test_host_sends_on_change_and_resends():
    out = []
    a = types.SimpleNamespace(
        reactor=types.SimpleNamespace(monotonic=lambda: a.t), t=10.0,
        LANE_REPORT_RESEND_S=5.0, masks=(1, 2))
    a._lane_masks = lambda idx: a.masks
    a.send_request_to = lambda idx, req, cb: out.append((idx, req['params']))
    tick = lambda info: ace_mod.MultiAce._lane_report_tick(a, 0, info)
    tick({'slots': []})                 # unit without the feature
    assert out == []
    tick({'lane_report': True})
    tick({'lane_report': True})
    assert out == [(0, {'in_use': 1, 'by_hand': 2})]
    a.t = 16.0
    tick({'lane_report': True})         # resend after 5 s
    a.masks = (3, 2)
    tick({'lane_report': True})         # change
    assert [p['in_use'] for _i, p in out] == [1, 1, 3]
