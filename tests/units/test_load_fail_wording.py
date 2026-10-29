"""Wording of a failed load: the real helpers of ace_swap.py and
filament_feed_ace.py on stubs (no printer).

HW 2026-10-25 (head mode, head 2 from ACE 2 / slot 0, nozzle without flow):
the swap path said NO TRANSPORT although the feed machine had failed in
load_extruding (the strand had reached the toolhead; phase3's pushes
without filament motion flip the motion helper to "free"), and the direct
feed path showed the raw stock text because its emitters were multi-only.
"""
import importlib
import sys
import types

from . import harness as H


class FakeReactor:
    def monotonic(self):
        return 100.0


class FakeSensor:
    def __init__(self, motion, pin=None):
        self.motion = motion
        if pin is not None:
            self.runout_buttun_state = pin

    def get_status(self, eventtime):
        return {'filament_detected': self.motion}


class FakeFeed:
    def __init__(self, error_state):
        self.channel_error_state = ['none', error_state]


class FakePrinter:
    def __init__(self, objs):
        self.objs = objs

    def lookup_object(self, name, default=None):
        return self.objs.get(name, default)


def _rig(error_state='load_feeding', motion=False, pin=None, feed=True,
         decoder=True):
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    S = importlib.import_module(H.PKG + '.ace_swap')

    class Ace(object):
        EXTRUDER_MAP = A.MultiAce.EXTRUDER_MAP
        LOAD_ARRIVED_STATES = S.SwapMixin.LOAD_ARRIVED_STATES
        _feed_failed_after_arrival = S.SwapMixin._feed_failed_after_arrival
        _load_slip_details = S.SwapMixin._load_slip_details
        _head_filament_present = A.MultiAce._head_filament_present
        unload_gpio = True

        def _t(self, key, **kw):
            return key

        def _disp(self, i):
            return int(i) + 1

        def _cap(self, idx, name, default=None):
            assert name == 'has_decoder'
            return decoder
    ace = Ace()
    ace.reactor = FakeReactor()
    ace._press_zero = {}
    ace._load_feed_error = {}
    objs = {'filament_motion_sensor e3_filament': FakeSensor(motion, pin)}
    if feed:
        objs['filament_feed right'] = FakeFeed(error_state)   # head 3 = right/1
    ace.printer = FakePrinter(objs)
    return ace


def test_failed_in_load_extruding_is_no_flow_whatever_the_motion_helper_says():
    ace = _rig('load_extruding', motion=False, pin=False)
    detail, steps = ace._load_slip_details(3, 2, 0)
    assert detail == 'msg.pause_swap_load_no_flow'
    assert 'clog' in steps[0]


def test_failed_before_arrival_with_a_clear_pin_is_no_transport():
    ace = _rig('load_feeding', motion=False, pin=False)
    detail, steps = ace._load_slip_details(3, 2, 0)
    assert detail == 'msg.pause_swap_load_no_transport'
    assert 'bowden' in steps[0]


def test_presence_pin_beats_the_motion_helper():
    ace = _rig('load_feeding', motion=False, pin=True)
    detail, _steps = ace._load_slip_details(3, 2, 0)
    assert detail == 'msg.pause_swap_load_no_flow'


def test_without_feed_module_or_pin_the_motion_helper_decides():
    ace = _rig(motion=True, feed=False)
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_no_flow'
    ace = _rig(motion=False, feed=False)
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_no_transport'


def test_without_decoder_the_arrived_case_names_both_ends():
    ace = _rig('load_extruding', motion=False, pin=False, decoder=False)
    detail, steps = ace._load_slip_details(3, 2, 0)
    assert detail == 'msg.pause_swap_load_no_extrude'
    assert 'clog' in steps[0] and 'tangle' in steps[1]
    ace = _rig('load_feeding', motion=False, pin=True, decoder=False)
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_no_extrude'
    # transport wording is unit-agnostic
    ace = _rig('load_feeding', motion=False, pin=False, decoder=False)
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_no_transport'


def test_press_zero_still_wins():
    ace = _rig('load_extruding', motion=True, pin=True)
    ace._press_zero[(2, 0)] = 99.0
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_ace_slip'


def test_device_feed_error_names_the_ace_end():
    # HW 2026-10-25: matte PLA the ACE 2 gear could not grip; the device
    # ended every feed with slot_status 'feed_error', the toolhead never
    # saw filament. NO TRANSPORT pointed at the bowden, the spool was it.
    ace = _rig('load_feeding', motion=False, pin=False)
    ace._load_feed_error[(2, 0)] = 99.0
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_ace_slip'
    ace._load_feed_error[(2, 0)] = 100.0 - 1300.0      # stale verdict
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_no_transport'
    ace._load_feed_error = {(1, 0): 99.0}                # another lane
    assert ace._load_slip_details(3, 2, 0)[0] == 'msg.pause_swap_load_no_transport'


# --- the verdict itself: what the feed loop stores per attempt ------------

class _VerdictReactor:
    def __init__(self):
        self.t = 100.0

    def monotonic(self):
        return self.t

    def pause(self, until):
        self.t = max(self.t, until)


class _VerdictAce:
    def __init__(self, statuses, truth=True):
        self._active_device_index = 0
        self._load_feed_error = {}
        self.statuses = list(statuses)   # one per poll, last one sticks
        self.truth = truth

    def _cap(self, idx, name, default=None):
        assert name == 'has_slot_status_truth'
        return self.truth

    def _v2_get_slot_status(self, idx, slot):
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]


def _verdict_feed(ace):
    F = _feed_module()

    class Feed(object):
        FEED_VERDICT_WAIT_S = F.FilamentFeed.FEED_VERDICT_WAIT_S
        _note_feed_verdict = F.FilamentFeed._note_feed_verdict
    feed = Feed()
    feed.ace = ace
    feed.reactor = _VerdictReactor()
    return feed


def test_feed_error_is_stored_and_a_plain_finish_clears_it():
    ace = _VerdictAce(['feeding', 'feeding', 'feed_error'])
    feed = _verdict_feed(ace)
    feed._note_feed_verdict(2, 0, 10.0)
    assert (0, 2) in ace._load_feed_error
    ace.statuses = ['ready']
    feed._note_feed_verdict(2, 1, 26.0)
    assert ace._load_feed_error == {}


def test_verdict_wait_is_bounded_while_the_device_still_says_feeding():
    ace = _VerdictAce(['feeding'])
    feed = _verdict_feed(ace)
    feed._note_feed_verdict(2, 0, 10.0)
    assert feed.reactor.t <= 100.0 + feed.FEED_VERDICT_WAIT_S + 0.2
    assert ace._load_feed_error == {}


def test_unit_without_slot_status_truth_carries_no_verdict():
    ace = _VerdictAce(['feed_error'], truth=False)
    ace._load_feed_error[(0, 2)] = 1.0
    feed = _verdict_feed(ace)
    feed._note_feed_verdict(2, 0, 10.0)
    assert ace._load_feed_error == {}


def test_old_ace_without_the_store_is_a_noop():
    ace = _VerdictAce(['feed_error'])
    del ace._load_feed_error
    feed = _verdict_feed(ace)
    feed._note_feed_verdict(2, 0, 10.0)


# --- direct feed path: which heads get the multiACE wording ---------------

def _feed_module():
    H.load_ace()
    name = H.PKG + '.pulse_counter'
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return importlib.import_module(H.PKG + '.filament_feed_ace')


class FakeAce:
    def __init__(self, mode, uses_ace=True, head_source=None):
        self._ace_mode = mode
        self.uses_ace = uses_ace
        self._head_source = head_source or {}
        self.slip_calls = []

    def head_uses_ace(self, head):
        return self.uses_ace

    def _disp(self, i):
        return int(i) + 1

    def _t(self, key, **kw):
        return '%s head=%s loc=%s' % (key, kw.get('head'), kw.get('loc', ''))

    def _load_slip_details(self, head, a, s):
        self.slip_calls.append((head, a, s))
        return 'detail', ['step']


def _feed(mode, uses_ace=True, head_source=None):
    F = _feed_module()

    class Feed(object):
        _localized_feed_head = F.FilamentFeed._localized_feed_head
        _emit_feed_pause = F.FilamentFeed._emit_feed_pause
        _feed_load_fail_details = F.FilamentFeed._feed_load_fail_details
    feed = Feed()
    feed.ace = FakeAce(mode, uses_ace, head_source)
    feed.filament_ch = [2, 3]
    return feed


def test_head_mode_ace_head_is_classified_like_multi():
    feed = _feed('head', head_source={2: {'ace_index': 2, 'slot': 0}})
    assert feed._feed_load_fail_details(0) == ('detail', ['step'])
    assert feed.ace.slip_calls == [(2, 2, 0)]
    assert feed._emit_feed_pause(0, 'msg.pause_feed_load_jam') == \
        'msg.pause_feed_load_jam head=3 loc= (MMU 3 / Slot 1)'


def test_head_mode_feeder_or_manual_head_keeps_the_stock_text():
    feed = _feed('head', uses_ace=False,
                 head_source={2: {'ace_index': 2, 'slot': 0}})
    assert feed._feed_load_fail_details(0) == (None, None)
    assert feed._emit_feed_pause(0, 'msg.pause_feed_load_jam') is None


def test_multi_mode_unchanged_and_normal_mode_stays_stock():
    feed = _feed('multi')
    assert feed._emit_feed_pause(1, 'msg.pause_feed_load_jam') == \
        'msg.pause_feed_load_jam head=4 loc='
    assert feed._feed_load_fail_details(1) == (None, None)   # no head_source
    feed = _feed('normal')
    assert feed._emit_feed_pause(0, 'msg.pause_feed_load_jam') is None
    assert feed._feed_load_fail_details(0) == (None, None)
