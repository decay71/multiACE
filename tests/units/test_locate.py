"""ACE_LOCATE helpers on stubs: slot choice for the ACE Pro jog and the
FLASH_LED payload of the ACE 2 codec.

The jog must never touch a slot that feeds a loaded head, is staged by
the background engine or carries the unit's armed feed assist; the LED
request carries six uint32 fields (components, loop, quick1, slow1,
quick2, slow2) in that order.
"""
from . import harness as H
from multiace.klipper.extras import ace_protocol_v2 as P2
from multiace.klipper.extras.ace_protocol import (
    GATE_AVAILABLE, GATE_EMPTY, GATE_UNKNOWN)


def _rig(gates, loaded=(), staged=None, armed=None):
    A = H.load_ace()

    class Ace(object):
        _locate_pick_slot = A.MultiAce._locate_pick_slot
        _locate_slot_feeds = A.MultiAce._locate_slot_feeds

        def _head_is_loaded(self, head):
            return head in self._loaded

        def _armed_slot_for_ace(self, idx):
            return self._armed
    ace = Ace()
    ace._gate_status_per_ace = {0: list(gates)}
    ace._head_source = {h: {'ace_index': 0, 'slot': s}
                        for h, s in loaded}
    ace._loaded = {h for h, _ in loaded}
    ace._bg_staged = staged or {}
    ace._armed = armed
    return ace


def test_first_occupied_free_slot_wins():
    ace = _rig([GATE_EMPTY, GATE_AVAILABLE, GATE_AVAILABLE, GATE_UNKNOWN])
    assert ace._locate_pick_slot(0) == 1


def test_loaded_slot_is_skipped():
    ace = _rig([GATE_AVAILABLE, GATE_AVAILABLE, GATE_EMPTY, GATE_EMPTY],
               loaded=[(0, 0)])
    assert ace._locate_pick_slot(0) == 1
    assert ace._locate_slot_feeds(0, 0)


def test_staged_and_armed_slots_are_skipped():
    ace = _rig([GATE_AVAILABLE] * 4, staged={2: (0, 0)}, armed=1)
    assert ace._locate_pick_slot(0) == 2


def test_empty_slot_is_the_fallback():
    ace = _rig([GATE_EMPTY, GATE_UNKNOWN, GATE_AVAILABLE, GATE_EMPTY],
               loaded=[(2, 2)])
    assert ace._locate_pick_slot(0) == 0


def test_no_candidate_gives_none():
    ace = _rig([GATE_AVAILABLE] * 4,
               loaded=[(0, 0), (1, 1), (2, 2), (3, 3)])
    assert ace._locate_pick_slot(0) is None


def test_flash_led_payload_fields():
    proto = P2.AceProtocolV2.__new__(P2.AceProtocolV2)
    cmd, payload = proto._method_to_v2('flash_led', {
        'components': 15, 'loop': 3, 'quick1': 200, 'slow1': 500,
        'quick2': 7, 'slow2': 9})
    assert cmd == P2.Cmd.FLASH_LED == 70
    fields = P2.pb_decode(payload)
    got = [P2._fval(fields, n) for n in range(1, 7)]
    assert got == [15, 3, 200, 500, 7, 9]


class _Reactor:
    NEVER = float('inf')

    def __init__(self):
        self.timers = {}
        self.n = 0
        self.now = 100.0

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = max(self.now, until)

    def register_timer(self, fn, when):
        self.n += 1
        self.timers[self.n] = (fn, when)
        return self.n

    def unregister_timer(self, t):
        del self.timers[t]


def _led_rig():
    A = H.load_ace()

    class Ace(object):
        LOCATE_LED_DEFAULT = A.MultiAce.LOCATE_LED_DEFAULT
        _locate_led_running = A.MultiAce._locate_led_running
        _locate_led_cancel = A.MultiAce._locate_led_cancel
        _locate_led_arm_stop = A.MultiAce._locate_led_arm_stop

        def __init__(self):
            self.reactor = _Reactor()
            self._connected_per_ace = {0: True}
            self.sent = []
            self._disp = lambda i: i + 1

        def _locate_led_send(self, idx, params):
            self.sent.append((idx, dict(params)))
            return {'code': 0, 'msg': 'success'}
    return Ace()


def test_led_stop_timer_sends_components_zero():
    ace = _led_rig()
    ace._locate_led_cancel(0)
    assert not ace._locate_led_running(0)
    ace._locate_led_arm_stop(0, 15.0)
    assert ace._locate_led_running(0)
    (fn, when), = ace.reactor.timers.values()
    assert when == 115.0
    fn(when)
    assert ace.sent == [(0, dict(ace.LOCATE_LED_DEFAULT, components=0))]
    assert not ace._locate_led_running(0)


def test_led_cancel_drops_pending_timer():
    ace = _led_rig()
    ace._locate_led_cancel(0)
    ace._locate_led_arm_stop(0, 15.0)
    ace._locate_led_cancel(0)
    assert ace.reactor.timers == {}
    assert not ace._locate_led_running(0)


# --- 2026-10-26: jog pairing, blink fields, armed slot ------------------

def test_led_default_frame_is_the_full_blinking_frame():
    # HW 2026-10-26: loop=0 or a frame without the timing fields is
    # accepted but shows nothing; loop=3 with 200/500 blinks and ends.
    A = H.load_ace()
    assert A.MultiAce.LOCATE_LED_DEFAULT['loop'] > 0
    proto = P2.AceProtocolV2.__new__(P2.AceProtocolV2)
    cmd, payload = proto._method_to_v2('flash_led',
                                       dict(A.MultiAce.LOCATE_LED_DEFAULT))
    fields = P2.pb_decode(payload)
    assert [P2._fval(fields, n) for n in range(1, 7)] == [15, 3, 200, 500, 200, 500]


def test_led_codec_leaves_missing_fields_out():
    proto = P2.AceProtocolV2.__new__(P2.AceProtocolV2)
    cmd, payload = proto._method_to_v2('flash_led', {'components': 0, 'loop': 0})
    assert set(P2.pb_decode(payload)) == {1, 2}


def _jog_rig(moves, slot_status_truth=False):
    """moves: list of results in call order, popped per _locate_move."""
    A = H.load_ace()

    class Ace(object):
        LOCATE_JOG_REPEAT = A.MultiAce.LOCATE_JOG_REPEAT
        LOCATE_V1_SETTLE_S = A.MultiAce.LOCATE_V1_SETTLE_S
        _locate_jog = A.MultiAce._locate_jog

        def __init__(self):
            self.reactor = _Reactor()
            self.calls = []
            self.said = []
            self.warned = []
            self._locate_busy = True
            self._disp = lambda i: i + 1

        def _locate_unit_idle(self, idx):
            return True

        def _cap(self, idx, name):
            return slot_status_truth

        def _locate_move(self, idx, slot, mm, method):
            self.calls.append(method)
            return moves.pop(0)

        def _t(self, key, **kw):
            return key + ' ' + ' '.join('%s=%s' % kv for kv in sorted(kw.items()))

        def log_always(self, msg):
            self.said.append(msg)

        def log_warn(self, msg):
            self.warned.append(msg)
    return Ace()


def test_jog_rejected_feed_skips_the_unwind():
    ace = _jog_rig(['rejected'])
    ace._locate_jog(0, 0, 20)
    assert ace.calls == ['feed_filament']
    assert 'result=rejected/-' in ace.said[0]
    assert ace.warned == []
    assert ace._locate_busy is False


def test_jog_pairs_feed_and_unwind_and_repeats():
    ace = _jog_rig(['ok', 'ok', 'ok', 'ok'])
    ace._locate_jog(0, 0, 20)
    assert ace.calls == ['feed_filament', 'unwind_filament'] * 2
    assert 'result=ok/ok ok/ok' in ace.said[0]
    assert ace.warned == []


def test_jog_rejected_unwind_stops_and_says_forward():
    ace = _jog_rig(['ok', 'rejected', 'ok', 'ok'])
    ace._locate_jog(0, 1, 20)
    assert ace.calls == ['feed_filament', 'unwind_filament']
    assert 'result=ok/rejected' in ace.said[0]
    assert len(ace.warned) == 1
    assert 'msg.locate_jog_forward_left' in ace.warned[0]
    assert 'mm=20' in ace.warned[0] and 'slot=2' in ace.warned[0]


def test_jog_waits_between_rounds_without_slot_status():
    ace = _jog_rig(['ok', 'ok', 'ok', 'ok'], slot_status_truth=False)
    t0 = ace.reactor.now
    ace._locate_jog(0, 0, 20)
    assert ace.reactor.now - t0 == ace.LOCATE_V1_SETTLE_S
    ace = _jog_rig(['ok', 'ok', 'ok', 'ok'], slot_status_truth=True)
    t0 = ace.reactor.now
    ace._locate_jog(0, 0, 20)
    assert ace.reactor.now == t0


def test_armed_slot_counts_only_a_real_assist():
    A = H.load_ace()

    class Ace(object):
        _armed_slot_for_ace = A.MultiAce._armed_slot_for_ace

        def __init__(self, states, cache=-1):
            self._feed_assist_per_ace = {0: cache}
            self.states = states

        def _cap(self, idx, name):
            return True

        def _v2_get_slot_status(self, idx, s):
            return self.states[s]
    assert Ace(['ready', 'ready', 'preloading', 'ready'])._armed_slot_for_ace(0) is None
    assert Ace(['ready', 'feeding', 'rollback', 'ready'])._armed_slot_for_ace(0) is None
    assert Ace(['ready', 'ready', 'assisting', 'ready'])._armed_slot_for_ace(0) == 2
    assert Ace(['ready'] * 4, cache=3)._armed_slot_for_ace(0) == 3
