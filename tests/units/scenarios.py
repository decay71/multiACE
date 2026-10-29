"""H0 scenarios: each builds a Harness, drives it, returns the trace.

A scenario is a function `name(h)` that receives the built harness after
`ready()` was NOT yet called; it decides when ready happens and how long
the virtual clock runs. `SCENARIOS` maps golden names to builders.
"""
from __future__ import annotations

import sys

from . import harness as H
from .fake_v1 import FakeV1
from .fake_v2 import FakeV2
from .fake_cfs import FakeCFS

V1_IDS = ('28e9', '018a')
V2_IDS = ('1a86', '55d3')
PATH_V1_A = '/dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1.1:1.0'
PATH_V1_B = '/dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1.2:1.0'
PATH_V2_A = '/dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1.3:1.0'
PATH_V2_B = '/dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1.4:1.0'
CFS_IDS = ('1a86', '7523')
PATH_CFS = '/dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1.5:1.0-port0'


def unit_v1(clock, label, path, **kw):
    return H.Unit(label, 'v1', path, FakeV1(clock, label, **kw), V1_IDS)


def unit_v2(clock, label, path, **kw):
    return H.Unit(label, 'v2', path, FakeV2(clock, label, **kw), V2_IDS)


def unit_cfs(clock, label, path=PATH_CFS, **kw):
    return H.Unit(label, 'cfs', path, FakeCFS(clock, label, **kw), CFS_IDS)


def _harness(make_units, count, overrides=None, save_vars=None,
             extra_sections=None):
    clock = H.VirtualClock()
    units = make_units(clock)
    cfg = {'ace_device_count': str(count)}
    cfg.update(overrides or {})
    h = H.Harness(units, config_overrides=cfg, save_vars=save_vars,
                  extra_sections=extra_sections)
    h.clock = clock
    return h


def connect_v1(**kw):
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A,
                        slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA',
                                   'brand': 'Anycubic', 'color': (20, 20, 20)}})]
    h = _harness(make, 1, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def connect_v2(**kw):
    def make(clock):
        return [unit_v2(clock, 'v2a', PATH_V2_A,
                        slots={1: {'sku': 'AHPLBK-101', 'type': 'PLA',
                                   'color': (200, 30, 30)}})]
    h = _harness(make, 1, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def connect_mixed(**kw):
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A,
                        slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA',
                                   'brand': 'Anycubic'}}),
                unit_v2(clock, 'v2a', PATH_V2_A,
                        slots={2: {'sku': 'AHPEWH-102', 'type': 'PETG',
                                   'color': (240, 240, 240)}},
                        uid=(0x8f3b9387, 0x07b09108, 0x3131334a))]
    h = _harness(make, 2, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def heartbeat_60s_mixed(**kw):
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A),
                unit_v2(clock, 'v2a', PATH_V2_A)]
    h = _harness(make, 2, **kw)
    h.build()
    h.ready()
    h.run(60.0)
    h.snapshot()
    return h.finish()


def _mixed(clock):
    return [unit_v1(clock, 'v1a', PATH_V1_A,
                    slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA',
                               'brand': 'Anycubic'}}),
            unit_v2(clock, 'v2a', PATH_V2_A,
                    slots={2: {'sku': 'AHPEWH-102', 'type': 'PETG',
                               'color': (240, 240, 240)}},
                    uid=(0x8f3b9387, 0x07b09108, 0x3131334a))]


def unplug_v2_replug(**kw):
    """USB pull on the ACE 2 while idle, cable back 2 s later: the reader
    thread fails, the reconnect ladder (0.3/0.8/1.6 s) finds the port
    again on its third attempt, the handshake runs anew."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(5.0, h.unplug, 'v2a')
    h.at(7.0, h.replug, 'v2a')
    h.run(20.0)
    h.snapshot()
    return h.finish()


def unplug_v2_gone(**kw):
    """USB pull on the ACE 2 while idle, cable stays out: the ladder gives
    up, the idle reconnect keeps looking every 5 s, the ACE Pro keeps its
    heartbeat."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(5.0, h.unplug, 'v2a')
    h.run(25.0)
    h.snapshot()
    return h.finish()


def unplug_v1_replug(**kw):
    """USB pull on the ACE Pro (reactor fd path), cable back 2 s later."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(5.0, h.unplug, 'v1a')
    h.at(7.0, h.replug, 'v1a')
    h.run(20.0)
    h.snapshot()
    return h.finish()


def v1_read_eio_once(**kw):
    """A single EIO on the ACE Pro read: the fd is dropped, the recovery
    reopens the same port at the first ladder step."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(5.0, h.inject_read_error, 'v1a')
    h.run(10.0)
    h.snapshot()
    return h.finish()


def silence_v2(**kw):
    """The ACE 2 stops answering for 12 s (port open, no bytes): the
    heartbeat's silence watchdog (8 s) triggers the reconnect."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(5.0, h.mute, 'v2a', True)
    h.at(17.0, h.mute, 'v2a', False)
    h.run(25.0)
    h.snapshot()
    return h.finish()


def head_mode_connect(**kw):
    """Head mode (one ACE per head, Dirk's wiring): two units, head 0 on
    ACE 0, head 1 on ACE 1."""
    kw = dict(kw)
    sv = {'ace__mode': 'head', 'ace__head_ace': {'0': 0, '1': 1}}
    sv.update(kw.pop('save_vars', None) or {})
    h = _harness(_mixed, 2, save_vars=sv, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def startup_wait_late_unit(**kw):
    """ace_device_count 2 but the ACE 2 shows up 3 s after klippy:ready:
    the startup wait loop rescans every second and finds it."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h._unit('v2a').plugged = False
    h.at(3.0, h.replug, 'v2a')
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def startup_softfail(**kw):
    """ace_device_count 2, only the ACE Pro present, startup_wait 3 s,
    no table yet (first boot): D11 connects what is there after the wait
    with a warning (found 1 of 2); the soft-fail path is for 'nothing
    found' only. The ACE Pro gets place 0."""
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A)]
    h = _harness(make, 2, overrides={'startup_wait': '3.0'}, **kw)
    h.build()
    h.ready()
    h.run(12.0)
    h.snapshot()
    return h.finish()


# ---------------------------------------------------------------------------
# round 2: ordering, print lifecycle with feed assist, write faults, id wrap,
# callback exceptions, manual moves, hangs, late join, insert

# multi mode: slot == head is an invariant _on_print_start enforces (a
# head mapped to another slot aborts the print as corrupt), so the print
# fleet holds its spools in the slots of the heads they feed.
HEAD0_ON_V2 = {'0': {'ace_index': 0, 'slot': 0, 'type': 'PETG',
                     'color': 'F0F0F0', 'brand': ''}}
HEAD1_ON_V1 = {'1': {'ace_index': 1, 'slot': 1, 'type': 'PLA',
                     'color': '141414', 'brand': 'Anycubic'}}


def _mixed_print_fleet(clock):
    return [unit_v1(clock, 'v1a', PATH_V1_A,
                    slots={1: {'sku': 'AHPLBK-101', 'type': 'PLA',
                               'brand': 'Anycubic', 'color': (20, 20, 20)}}),
            unit_v2(clock, 'v2a', PATH_V2_A,
                    slots={0: {'sku': 'AHPEWH-102', 'type': 'PETG',
                               'color': (240, 240, 240)}},
                    uid=(0x8f3b9387, 0x07b09108, 0x3131334a))]


def _mixed_printing(save_vars, **kw):
    """Print fleet (ACE 2 = index 0 with v2_order first, ACE Pro = 1),
    head_source from save_vars, connected and settled after 3 s."""
    sv = {'ace__head_source': save_vars}
    sv.update(kw.pop('save_vars', None) or {})
    # fa_debug only moves the multiace_fa logger level (SEND/RESP and the
    # [fa-recover] lines); on for the print scenarios so the goldens carry
    # the feed-assist ladder in words, not only as bytes.
    ov = {'fa_debug': 'true'}
    ov.update(kw.pop('overrides', None) or {})
    h = _harness(_mixed_print_fleet, 2, overrides=ov, save_vars=sv, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    return h


def connect_mixed_v2_order_usb(**kw):
    """v2_order usb: the ACE Pro on the lower port becomes index 0."""
    kw = dict(kw)
    ov = {'v2_order': 'usb'}
    ov.update(kw.pop('overrides', None) or {})
    h = _harness(_mixed, 2, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.snapshot()
    return h.finish()


def connect_mixed_v2_order_last(**kw):
    kw = dict(kw)
    ov = {'v2_order': 'last'}
    ov.update(kw.pop('overrides', None) or {})
    h = _harness(_mixed, 2, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.snapshot()
    return h.finish()


def print_fa_v2(**kw):
    """Print start with head 0 fed by the ACE 2: FA arms on its slot after
    the tool change, the velocity tick sees 'assisting', print end stops
    the assist."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.run(10.0)
    h.snapshot()
    h.extruding(0.0)
    h.print_end()
    h.run(2.0)
    return h.finish()


def print_fa_v1_forbidden(**kw):
    """Print start with head 1 fed by the ACE Pro while that unit is in a
    motion of its own: start_feed_assist answers code=0 FORBIDDEN, the
    retry ladder re-sends until the unit is free."""
    h = _mixed_printing(HEAD1_ON_V1, **kw)
    h.at(0.0, h._unit('v1a').device.busy_for, 1.5)
    h.print_start()
    h.tool_change(1)
    h.extruding(2.0)
    h.run(10.0)
    h.snapshot()
    h.extruding(0.0)
    h.print_end()
    h.run(2.0)
    return h.finish()


def print_fa_v2_dropped_arm(**kw):
    """The ACE 2 swallows the reply to the first start_feed_assist AND
    does not arm (paragraph 33): no RESP, slot never 'assisting'; the
    velocity tick's dropped-arm verify resends after settle + margin."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    dev = h._unit('v2a').device
    P2 = dev.P2

    def _lose_first_arm():
        dev.drop_next(P2.Cmd.FEED_OR_ROLLBACK, 1)
        dev.ignore_next_assist = 1
    h.at(0.0, _lose_first_arm)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.run(15.0)
    h.snapshot()
    h.extruding(0.0)
    h.print_end()
    h.run(2.0)
    return h.finish()


def print_comms_loss_pauses(**kw):
    """Printing from the ACE 2, cable pulled and not back: the ladder
    gives up and the print is PAUSED (the unit feeds a loaded head)."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.at(5.0, h.unplug, 'v2a')
    h.run(15.0)
    h.snapshot()
    return h.finish()


def print_comms_loss_other_unit_no_pause(**kw):
    """Printing from the ACE 2, the ACE Pro (feeding nothing) is pulled:
    reconnect ladder, no pause."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.at(5.0, h.unplug, 'v1a')
    h.run(15.0)
    h.snapshot()
    return h.finish()


def print_reconnect_rearms_fa(**kw):
    """Printing from the ACE 2, a USB blip of 0.5 s: the ladder reopens
    on its second step, on_ready re-arms feed assist."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.at(5.0, h.unplug, 'v2a')
    h.at(5.5, h.replug, 'v2a')
    h.run(15.0)
    h.snapshot()
    return h.finish()


def print_comms_loss_restored_before_pause(**kw):
    """Printing from the ACE 2, cable pulled mid-swap (a command holds the
    gcode mutex for 12 s): the ladder gives up and schedules the pause,
    the pause waits for the mutex, the cable comes back, the idle
    reconnect reopens the unit meanwhile. The stale pause is dropped,
    nothing is spoken, the latch is released."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.at(4.9, h.hold_gcode, 12.0)
    h.at(5.0, h.unplug, 'v2a')
    h.at(9.0, h.replug, 'v2a')
    h.run(20.0)
    h.snapshot()
    return h.finish()


def print_comms_loss_second_unit_stays_down(**kw):
    """Both units feed a loaded head, a command holds the gcode mutex. The
    ACE 2 cable is pulled first: its ladder gives up and schedules the
    pause (one-pause latch). Then the ACE Pro cable goes: its give-up sees
    the latch and schedules nothing. The ACE 2 comes back while the pause
    waits for the mutex, the ACE Pro stays out. The pause must not be
    dropped: it goes ahead for the ACE Pro and its head."""
    both = dict(HEAD0_ON_V2)
    both.update(HEAD1_ON_V1)
    h = _mixed_printing(both, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.at(3.9, h.hold_gcode, 14.0)
    h.at(4.0, h.unplug, 'v2a')
    h.at(7.5, h.unplug, 'v1a')
    h.at(8.0, h.replug, 'v2a')
    h.run(22.0)
    h.snapshot()
    return h.finish()


def print_unit_unpowered_pauses(**kw):
    """Printing from the ACE 2, the unit loses power behind a live USB link
    (port opens, nothing answers, also after a reopen). The silence
    watchdog starts the ladder, no reopen gets a reply, so the ladder gives
    up and the comms pause comes instead of an endless reconnect loop.
    Power returns, the idle reconnect brings the unit back once it
    answers."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.at(5.0, h.power, 'v2a', False)
    h.at(40.0, h.power, 'v2a', True)
    h.run(55.0)
    h.snapshot()
    return h.finish()


def print_comms_loss_during_pause_pauses_on_resume(**kw):
    """Printing from the ACE 2, the print is paused (a runout), the unit
    is unplugged while paused and does not come back. The failure handler
    sees no running print and does not pause; the RESUME reopens, fails,
    and must pause instead of printing from the dead unit."""
    h = _mixed_printing(HEAD0_ON_V2, **kw)
    h.print_start()
    h.tool_change(0)
    h.extruding(2.0)
    h.at(5.0, h.print_pause)
    h.at(5.5, h.extruding, 0.0)
    h.at(6.0, h.unplug, 'v2a')
    h.at(20.0, h.print_start)
    h.at(20.5, h.extruding, 2.0)
    h.run(30.0)
    h.snapshot()
    return h.finish()


def v1_write_eio(**kw):
    """The ACE Pro write (inline on the reactor) fails once with EIO just
    before the heartbeat: the sync write path runs the recovery inline."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(4.99, h.inject_write_error, 'v1a')
    h.run(10.0)
    h.snapshot()
    return h.finish()


def v2_writer_eio(**kw):
    """The ACE 2 writer thread meets one EIO: thread exits, recovery
    reopens the port."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(4.99, h.inject_write_error, 'v2a')
    h.run(10.0)
    h.snapshot()
    return h.finish()


def id_wrap_v2(**kw):
    """The V2 sequence number crosses 65535 (paragraph 61): the counter is
    set close to the limit, the heartbeat carries it over, every reply
    still finds its callback."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()

    def _near_wrap():
        h.inst._request_ids[0] = 65532
        h.trace('poke', '_request_ids[0]=65532')
    h.at(2.0, _near_wrap)
    h.run(10.0)
    h.snapshot()
    return h.finish()


def callback_exception(**kw):
    """A response callback that raises, on the ACE 2 and on the ACE Pro
    (Anhang B Nr. 3, fixed in 1.20b-pre): logged, connection unaffected."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()

    def _bad_cb(self, response):
        raise ValueError('callback bug under test')

    def _send(idx):
        h.inst.send_request_to(idx, {'method': 'get_info', 'params': {}},
                               _bad_cb)
    h.at(2.0, _send, 0)
    h.at(2.5, _send, 1)
    h.run(6.0)
    h.snapshot()
    return h.finish()


def ace_feed_retract(**kw):
    """Manual ACE_FEED / ACE_RETRACT on both units: feed_filament with the
    dwell length/speed + 0.1, status 'busy' meanwhile, unwind after."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()
    h.at(3.0, h.gcode_line, 'ACE_FEED INDEX=2 LENGTH=100 SPEED=50 ACE=0')
    h.at(8.0, h.gcode_line, 'ACE_RETRACT INDEX=2 LENGTH=100 SPEED=50 ACE=0')
    h.at(13.0, h.gcode_line, 'ACE_FEED INDEX=0 LENGTH=100 SPEED=50 ACE=1')
    h.at(18.0, h.gcode_line, 'ACE_RETRACT INDEX=0 LENGTH=100 SPEED=50 ACE=1')
    h.run(24.0)
    h.snapshot()
    return h.finish()


def v1_stuck_busy_timeout(**kw):
    """The ACE Pro reports 'busy' forever: ACE_FEED waits 30 s, reconnects
    twice, then the stuck-after-reconnects give-up (idle: plain command
    error, no pause)."""
    h = _harness(_mixed, 2, **kw)
    h.build()
    h.ready()

    def _stick():
        h._unit('v1a').device.stuck = True
    h.at(3.0, _stick)
    h.at(5.0, h.gcode_line, 'ACE_FEED INDEX=0 LENGTH=50 SPEED=50 ACE=1')
    h.run(120.0)
    h.snapshot()
    return h.finish()


def late_join_via_switch(**kw):
    """ace_device_count 1, only the ACE 2 present at start; the ACE Pro
    appears later and ACE_SWITCH's rescan late-joins it (appended, index 1,
    connected)."""
    h = _harness(_mixed, 1, **kw)
    h.build()
    h._unit('v1a').plugged = False
    h.ready()
    h.at(3.0, h.replug, 'v1a')
    h.at(4.0, h.gcode_line, 'ACE_SWITCH TARGET=0 AUTOLOAD=0')
    h.run(8.0)
    h.snapshot()
    return h.finish()


def insert_idle_v2(**kw):
    """A spool is inserted into an empty slot of the active ACE 2 while
    idle: the heartbeat sees empty -> present, auto_feed pre-loads
    feed_length mm and waits for the unit to go ready."""
    kw = dict(kw)
    ov = {'feed_length': '300'}
    ov.update(kw.pop('overrides', None) or {})
    h = _harness(_mixed, 2, overrides=ov, **kw)
    h.build()
    h.ready()
    h.at(3.0, h._unit('v2a').device.insert, 3, 'AHPLGN-103', 'PLA',
         (30, 200, 30))
    h.run(15.0)
    h.snapshot()
    return h.finish()


def insert_idle_v1(**kw):
    """Same on the ACE Pro, which is not the active unit: the gate flips,
    the identity is read from the status, no pre-load (active-only)."""
    kw = dict(kw)
    ov = {'feed_length': '300'}
    ov.update(kw.pop('overrides', None) or {})
    h = _harness(_mixed, 2, overrides=ov, **kw)
    h.build()
    h.ready()
    h.at(3.0, h._unit('v1a').device.insert, 1, 'AHPLWH-104', 'PLA',
         'Anycubic', (250, 250, 250))
    h.run(10.0)
    h.snapshot()
    return h.finish()


# ---------------------------------------------------------------------------
# M1: port protection and the shim rule


def excluded_port_mcu(**kw):
    """A V2-looking port that is Klipper's own [mcu ace2k] serial: the
    discovery must skip it (one log line), the other unit connects."""
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A, slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA', 'brand': 'Anycubic', 'color': (20, 20, 20)}}),
                unit_v2(clock, 'v2a', PATH_V2_A)]
    kw.setdefault('extra_sections', {'mcu ace2k': {'serial': PATH_V2_A}})
    h = _harness(make, 1, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def excluded_port_config(**kw):
    """Same port excluded via [ace] exclude_ports instead of [mcu]."""
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A, slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA', 'brand': 'Anycubic', 'color': (20, 20, 20)}}),
                unit_v2(clock, 'v2a', PATH_V2_A)]
    kw.setdefault('overrides', {'exclude_ports': PATH_V2_A + ', /dev/ttyNOPE'})
    h = _harness(make, 1, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def excluded_port_counted(**kw):
    """ace_device_count 2 with the ACE 2 port excluded: the excluded unit
    is attached hardware and counts in the option, but the expectation
    drops to 1 while its port is present - the ACE Pro connects, no
    soft-fail (HW 2026-10-21: 4 expected, 1 excluded, soft-fail 2/4 and
    a rescan that could never reach 4)."""
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A, slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA', 'brand': 'Anycubic', 'color': (20, 20, 20)}}),
                unit_v2(clock, 'v2a', PATH_V2_A)]
    kw.setdefault('overrides', {'exclude_ports': PATH_V2_A, 'startup_wait': '3.0'})
    h = _harness(make, 2, **kw)
    h.build()
    h.ready()
    h.run(8.0)
    h.snapshot()
    return h.finish()


def shim_answers(**kw):
    """A unit held in Simon-CR's bootloader shim answers GET_INFO with
    V2.0.0-shim: held, no further command, no reconnect churn, no
    watchdog; the healthy V1 beside it runs normally."""
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A, slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA', 'brand': 'Anycubic', 'color': (20, 20, 20)}}),
                unit_v2(clock, 'v2a', PATH_V2_A, firmware='V2.0.0-shim')]
    h = _harness(make, 2, **kw)
    h.build()
    h.ready()
    h.run(30.0)
    h.snapshot()
    return h.finish()


def oem_iap_answers(**kw):
    """OEM bootloader (IAP) reports 1.0.2: same hold as the shim."""
    def make(clock):
        return [unit_v2(clock, 'v2a', PATH_V2_A, firmware='1.0.2')]
    h = _harness(make, 1, **kw)
    h.build()
    h.ready()
    h.run(15.0)
    h.snapshot()
    return h.finish()



# -- M5a: D4 unit places -------------------------------------------------------

UID_A = (0x8f3b5218, 0x07b09108, 0x3131334a)
UID_B = (0x8f3b9387, 0x07b09108, 0x3131334a)
PATH_V1_C = '/dev/serial/by-path/platform-xhci-hcd.0.auto-usb-0:1.5:1.0'


def _four(clock, v2a_path=PATH_V2_A, v2b_path=PATH_V2_B, v1_paths=(PATH_V1_A, PATH_V1_B)):
    """Dirk's rig: two ACE 2 (keyed) and two ACE Pro (keyless)."""
    units = [unit_v2(clock, 'v2a', v2a_path, uid=UID_A),
             unit_v2(clock, 'v2b', v2b_path, uid=UID_B)]
    for i, path in enumerate(v1_paths):
        units.append(unit_v1(clock, 'v1%s' % 'abc'[i], path))
    return units


def _boot(make, count, save_vars=None, run=3.0, **kw):
    """One boot: build, ready, run, snapshot; returns (entries, save_vars
    after the boot) so a second boot can inherit the machine state."""
    h = _harness(make, count, save_vars=save_vars, **kw)
    h.build()
    h.ready()
    h.run(run)
    h.snapshot()
    sv = dict(h.save_variables.allVariables)
    return h.finish(), sv


def unit_places_v2_ports_swapped(**kw):
    """Boot 1 writes the place table from the current order (v2_order
    first: ACE 2 A=0, B=1, ACE Pro block 2,3). Boot 2 with the two ACE 2
    cables swapped: the keyed units keep their indices, the probe decides,
    not the port."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    e1, sv = _boot(lambda c: _four(c), 4, overrides=ov, **kw)
    e2, _ = _boot(lambda c: _four(c, v2a_path=PATH_V2_B, v2b_path=PATH_V2_A),
                  4, save_vars={'ace__unit_places': sv['ace__unit_places']},
                  overrides=ov, **kw)
    return e1 + e2


def unit_places_v1_removed(**kw):
    """Saved table from the four-unit rig; boot with one ACE Pro missing
    (count 3): D11 keeps every index = place. The missing ACE Pro's place 2
    holds 'absent:port:...' (waited for, then kept), the other ACE Pro
    stays at index 3; status shows 4 units, index 2 not present."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    e1, sv = _boot(lambda c: _four(c), 4, overrides=ov, **kw)
    e2, _ = _boot(lambda c: _four(c, v1_paths=(PATH_V1_B,)), 3,
                  save_vars={'ace__unit_places': sv['ace__unit_places']},
                  overrides=ov, **kw)
    return e1 + e2


def unit_places_fifth_unit_dropped(**kw):
    """Five units on the bus, ace_device_count 5: four are connected, the
    fifth (last ACE Pro by port) stays out with one log line, the expected
    count is capped so the startup does not soft-fail."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    e, _ = _boot(lambda c: _four(c, v1_paths=(PATH_V1_A, PATH_V1_B, PATH_V1_C)),
                 5, overrides=ov, **kw)
    return e


def unit_name_gcode(**kw):
    """ACE_UNIT_NAME names the ACE 2 (index 1 in the mixed fleet, v2_order
    usb) and an ACE Pro place; both show in get_status, the ACE 2 name
    follows the device to a swapped port on the next boot, the ACE Pro
    name stays with its place."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    h = _harness(lambda c: _four(c), 4, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.gcode_line('ACE_UNIT_NAME ACE=1 NAME=Rechts')
    h.gcode_line('ACE_UNIT_NAME ACE=2 NAME=ProLinks')
    h.gcode_line('ACE_UNIT_NAME ACE=9 NAME=Nope')
    h.run(0.5)
    h.snapshot()
    sv = dict(h.save_variables.allVariables)
    e1 = h.finish()
    e2, _ = _boot(lambda c: _four(c, v2a_path=PATH_V2_B, v2b_path=PATH_V2_A),
                  4, save_vars={'ace__unit_places': sv['ace__unit_places']},
                  overrides=ov, **kw)
    return e1 + e2


def unit_places_v2_removed_name_stays(**kw):
    """Name the ACE 2 at index 0 (place 0), then boot with that ACE 2
    unplugged (count 3): D11 keeps index 0 for it ('absent:'), the name
    stays on the place with its key (shown as not connected); renaming
    the absent place is allowed (same key). Third boot with the ACE 2
    back: the name is on it."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    h = _harness(lambda c: _four(c), 4, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.gcode_line('ACE_UNIT_NAME ACE=0 NAME=Oben')
    h.run(0.5)
    h.snapshot()
    sv = dict(h.save_variables.allVariables)
    e1 = h.finish()
    h = _harness(lambda c: [u for u in _four(c) if u.label != 'v2a'], 3,
                 save_vars={'ace__unit_places': sv['ace__unit_places']},
                 overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.gcode_line('ACE_UNIT_NAME ACE=0 NAME=Falsch')
    h.run(0.5)
    h.snapshot()
    sv = dict(h.save_variables.allVariables)
    e2 = h.finish()
    e3, _ = _boot(lambda c: _four(c), 4,
                  save_vars={'ace__unit_places': sv['ace__unit_places']},
                  overrides=ov, **kw)
    return e1 + e2 + e3


def _hs(ace, slot, color='FF0000'):
    return {'ace_index': ace, 'slot': slot, 'type': 'PLA', 'subtype': '',
            'color': color, 'brand': 'Generic'}


def unit_state_v2_removed_parks(**kw):
    """Head 0 is loaded from the ACE 2 at index 0 (place 0). Boot 2
    without that ACE 2 (count 3): D11 keeps index 0 as 'absent:<uid>', so
    nothing else can be addressed as the unit that fed head 0 (HW
    2026-10-25: an unload pulled the wrong unit's slot); head_source
    stays on index 0 and points at a place nothing opens. Boot 3 with
    the ACE 2 back: same index, same state. (The parking path of
    _reconcile_unit_state is for a key that leaves the table.)"""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    seed = {'ace__head_source': {'0': _hs(0, 0), '1': None, '2': None,
                                 '3': None}}
    e1, sv = _boot(lambda c: _four(c), 4, save_vars=seed, overrides=ov, **kw)
    sv2 = {k: v for k, v in sv.items() if k.startswith('ace__')}
    e2, sv = _boot(lambda c: [u for u in _four(c) if u.label != 'v2a'], 3,
                   save_vars=sv2, overrides=ov, **kw)
    sv3 = {k: v for k, v in sv.items() if k.startswith('ace__')}
    e3, _ = _boot(lambda c: _four(c), 4, save_vars=sv3, overrides=ov, **kw)
    return e1 + e2 + e3


def unit_state_follows_moved_unit(**kw):
    """Head 3 is loaded from the ACE Pro at index 3 (place 3, socket key),
    head 1 has filament STAGED from it. Boot 2 with the other ACE Pro
    unplugged (count 3): D11 keeps the unit at index 3 (its socket is its
    key), place 2 is 'absent:'; head_source and the staged entry stay put.
    Before D11 the ACE Pro block closed up and the state had to follow."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    seed = {'ace__head_source': {'0': _hs(0, 0, '0000FF'), '1': None,
                                 '2': None, '3': _hs(3, 1)},
            'ace__bg_staged': {'1': [3, 2]}}
    e1, sv = _boot(lambda c: _four(c), 4, save_vars=seed, overrides=ov, **kw)
    sv2 = {k: v for k, v in sv.items() if k.startswith('ace__')}
    e2, _ = _boot(lambda c: _four(c, v1_paths=(PATH_V1_B,)), 3,
                  save_vars=sv2, overrides=ov, **kw)
    return e1 + e2


def unit_assign_remove_and_reboot(**kw):
    """ACE_UNIT_ASSIGN KEY=<ACE 2 B> PLACE=NONE on the four-unit rig: the
    unit leaves place 1 (its place becomes 'free:' below the ACE Pros),
    goes on the removed list, restart hint. Boot 2: the unit is detected
    but not assigned (devices list: removed, present, no place), index 1
    is 'free:', nothing opens it. Then ACE_UNIT_ASSIGN ... PLACE=2 puts
    it back; boot 3 has it at index 1 again."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    key_b = 'uid:8f3b9387-07b09108-3131334a'
    h = _harness(lambda c: _four(c), 4, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.gcode_line('ACE_UNIT_ASSIGN KEY=%s PLACE=NONE' % key_b)
    h.run(0.5)
    h.snapshot()
    sv = {k: v for k, v in h.save_variables.allVariables.items()
          if k.startswith('ace__')}
    e1 = h.finish()
    h = _harness(lambda c: _four(c), 4, save_vars=sv, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.snapshot()
    h.gcode_line('ACE_UNIT_ASSIGN KEY=%s PLACE=2' % key_b)
    h.run(0.5)
    sv = {k: v for k, v in h.save_variables.allVariables.items()
          if k.startswith('ace__')}
    e2 = h.finish()
    e3, _ = _boot(lambda c: _four(c), 4, save_vars=sv, overrides=ov, **kw)
    return e1 + e2 + e3


def unit_assign_swap_and_forget(**kw):
    """ACE_UNIT_ASSIGN KEY=<ACE Pro A> PLACE=1 while the ACE 2 A holds
    place 0: the two swap places (restart hint). Boot 2 with the ACE Pro
    A unplugged: its place 0 is 'absent:', ACE_UNIT_FORGET of a connected
    unit is refused, of the absent one accepted (place freed, removed
    list and parked state cleared). Boot 3: place 0 'free:' below the
    others, the forgotten socket would be a new unit if it came back."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    key_pro_a = 'port:platform-xhci-hcd.0.auto-usb-0:1.1:1.0'
    key_a = 'uid:8f3b5218-07b09108-3131334a'
    h = _harness(lambda c: _four(c), 4, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.gcode_line('ACE_UNIT_ASSIGN KEY=%s PLACE=1' % key_pro_a)
    h.run(0.5)
    h.snapshot()
    sv = {k: v for k, v in h.save_variables.allVariables.items()
          if k.startswith('ace__')}
    e1 = h.finish()
    h = _harness(lambda c: [u for u in _four(c) if u.label != 'v1a'], 3,
                 save_vars=sv, overrides={'startup_wait': '2.0', **ov}, **kw)
    h.build()
    h.ready()
    h.run(3.0)
    h.snapshot()
    h.gcode_line('ACE_UNIT_FORGET KEY=%s' % key_a)
    h.gcode_line('ACE_UNIT_FORGET KEY=%s' % key_pro_a)
    h.run(0.5)
    sv = {k: v for k, v in h.save_variables.allVariables.items()
          if k.startswith('ace__')}
    e2 = h.finish()
    e3, _ = _boot(lambda c: [u for u in _four(c) if u.label != 'v1a'], 3,
                  save_vars=sv, overrides=ov, **kw)
    return e1 + e2 + e3


def status_without_dryer_fields_v1(**kw):
    """The active unit (an ACE Pro) reports neither `dryer_status` nor
    `temp` (a codec without dryer and probe). get_status() must still
    answer: it runs unguarded in the webhooks path, a KeyError there
    would shut the printer down (M5c)."""
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A,
                        omit=('dryer_status', 'temp'))]
    h = _harness(make, 1, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def _check_rolled_back(h, idx):
    """Trace the connection state of unit `idx` right after a failed
    _open_ace: connected False, no heartbeat timer, no reader fd, no
    writer queue (the fake-connection class of Anhang B Nr. 11)."""
    i = h.inst
    h.trace('check', 'after_failed_open', idx,
            {'connected': bool(i._connected_per_ace.get(idx, False)),
             'heartbeat': idx in i._heartbeat_timers,
             'reader_fd': idx in i._ace_dev_fds,
             'writer_queue': idx in i._writer_queues,
             'error': str(i._connect_error_per_ace.get(idx, ''))})


def connect_late_failure_v2(**kw):
    """_open_ace of the ACE 2 fails AFTER the unit was marked connected
    (the reader thread does not start). The half-open connection is
    rolled back (connected False, port closed), nothing keeps running on
    the dead handle, and a later reconnect brings the unit up (M5d,
    Anhang B Nr. 11)."""
    h = _harness(_mixed, 2, **kw)
    h.fail_thread_start(1, after=1)
    h.build()
    # The startup connect retry runs 1 s later inside ready() (reactor
    # pause), so the state check sits between the failure and the retry.
    h.at(0.5, _check_rolled_back, h, 0)
    h.ready()
    h.run(0.5)
    h.snapshot()
    h.run(30.0)
    h.snapshot()
    return h.finish()


def connect_late_failure_v1(**kw):
    """Same late connect failure for the ACE Pro: register_fd of its
    reader raises after the connected mark."""
    h = _harness(_mixed, 2, **kw)
    h.fail_register_fd(1)
    h.build()
    # The startup connect retry runs 1 s later inside ready() (reactor
    # pause), so the state check sits between the failure and the retry.
    h.at(0.5, _check_rolled_back, h, 1)
    h.ready()
    h.run(0.5)
    h.snapshot()
    h.run(30.0)
    h.snapshot()
    return h.finish()


def driver_v2_disabled(**kw):
    """enable_ace_v2: false with an ACE 2 on the bus: its driver runs no
    discovery, only the ACE Pro is found and connected."""
    ov = {'enable_ace_v2': 'false'}
    h = _harness(_mixed, 1, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def driver_v1_disabled(**kw):
    """enable_ace_v1: false with an ACE Pro on the bus: only the ACE 2 is
    found and connected."""
    ov = {'enable_ace_v1': 'false'}
    h = _harness(_mixed, 1, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def late_join_idle_rescan(**kw):
    """First boot, count 2, the ACE Pro absent, startup_wait 3 s: the
    table gets place 0 = ACE 2 and place 1 stays free. The ACE Pro appears
    at 14 s with nothing else going on (no swap, no switch): the idle
    rescan (every 10 s outside a print) places it at 1 and connects it."""
    h = _harness(_mixed, 2, overrides={'startup_wait': '3.0'}, **kw)
    h.build()
    h._unit('v1a').plugged = False
    h.ready()
    h.at(14.0, h.replug, 'v1a')
    h.run(26.0)
    h.snapshot()
    return h.finish()


def _legacy_table(sv):
    """The table as the first keyed builds wrote it: ACE 2 units keyed,
    ACE Pro entries present but without a key."""
    out = []
    for e in sv['ace__unit_places']:
        if e and str(e.get('key') or '').startswith('port:'):
            out.append({'key': None, 'name': e.get('name', '')})
        else:
            out.append(e)
    return out


def unit_legacy_keyless_waited(**kw):
    """Boot 1 writes the keyed table; boot 2 reads it with the ACE Pro
    entry stripped of its key (a table from a build that keyed only ACE 2
    units). The ACE Pro is late (2 s): it is waited for by count like a
    first boot, then takes the keyless place 1 and is keyed by its socket."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    e1, sv = _boot(_mixed, 2, overrides=ov, **kw)
    h = _harness(_mixed, 2, save_vars={'ace__unit_places': _legacy_table(sv)},
                 overrides={'startup_wait': '3.0', **ov}, **kw)
    h.build()
    h._unit('v1a').plugged = False
    h.at(2.0, h.replug, 'v1a')
    h.ready()
    h.run(5.0)
    h.snapshot()
    return e1 + h.finish()


def unit_legacy_keyless_gone(**kw):
    """As above, but the ACE Pro never comes: after the wait the keyless
    entry is cleared (a free place from now on, no second wait at the next
    boot) and a found-fewer warning is given."""
    ov = {'v2_order': 'first'}
    ov.update(kw.pop('overrides', None) or {})
    e1, sv = _boot(_mixed, 2, overrides=ov, **kw)
    h = _harness(lambda c: [u for u in _mixed(c) if u.label != 'v1a'], 2,
                 save_vars={'ace__unit_places': _legacy_table(sv)},
                 overrides={'startup_wait': '3.0', **ov}, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return e1 + h.finish()


# ---------------------------------------------------------------------------
# CFS Nano driver (phase 1: identity + presence, no motion)


def _v2_and_cfs(clock, **cfs_kw):
    kw = {'slots': (0, 2)}
    kw.update(cfs_kw)
    return [unit_v2(clock, 'v2a', PATH_V2_A,
                    slots={1: {'sku': 'AHPLBK-101', 'type': 'PLA',
                               'color': (200, 30, 30)}}),
            unit_cfs(clock, 'cfs', **kw)]


def connect_cfs(**kw):
    """enable_cfs on, an ACE 2 and a CFS Nano: the CFS port is probed
    before the claim (uid key), connects on the threaded engine,
    discover_device + get_info via the link, presence per slot from 0x08
    (slots 1 and 3 present)."""
    ov = {'enable_cfs': 'true'}
    h = _harness(_v2_and_cfs, 2, overrides=ov, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def cfs_disabled(**kw):
    """enable_cfs left at its default (off) with a CFS on the bus: no
    probe, no connect, only the ACE 2."""
    h = _harness(_v2_and_cfs, 1, **kw)
    h.build()
    h.ready()
    h.run(5.0)
    h.snapshot()
    return h.finish()


def cfs_insert_remove(**kw):
    """A spool goes into slot 2 of the CFS and slot 1 comes out while
    idle: the gates follow the presence mask; the 0x30 event reply is
    consumed and shows as box_event once."""
    ov = {'enable_cfs': 'true'}
    h = _harness(_v2_and_cfs, 2, overrides=ov, **kw)
    h.build()
    h.ready()
    h.at(3.0, h._unit('cfs').device.insert, 1)
    h.at(6.0, h._unit('cfs').device.remove, 0)
    h.run(10.0)
    h.snapshot()
    return h.finish()


def cfs_motion_unsupported(**kw):
    """ACE_FEED / ACE_RETRACT on the CFS: answered unsupported by the
    link, nothing but status polls reaches the wire."""
    ov = {'enable_cfs': 'true'}
    h = _harness(_v2_and_cfs, 2, overrides=ov, **kw)
    h.build()
    h.ready()
    h.at(3.0, h.gcode_line, 'ACE_FEED INDEX=0 LENGTH=100 SPEED=50 ACE=1')
    h.at(6.0, h.gcode_line, 'ACE_RETRACT INDEX=0 LENGTH=100 SPEED=50 ACE=1')
    h.run(9.0)
    h.snapshot()
    return h.finish()


SCENARIOS = {
    'connect_cfs': connect_cfs,
    'cfs_disabled': cfs_disabled,
    'cfs_insert_remove': cfs_insert_remove,
    'cfs_motion_unsupported': cfs_motion_unsupported,
    'late_join_idle_rescan': late_join_idle_rescan,
    'unit_legacy_keyless_waited': unit_legacy_keyless_waited,
    'unit_legacy_keyless_gone': unit_legacy_keyless_gone,
    'driver_v2_disabled': driver_v2_disabled,
    'driver_v1_disabled': driver_v1_disabled,
    'status_without_dryer_fields_v1': status_without_dryer_fields_v1,
    'connect_late_failure_v2': connect_late_failure_v2,
    'connect_late_failure_v1': connect_late_failure_v1,
    'unit_places_v2_ports_swapped': unit_places_v2_ports_swapped,
    'unit_places_v1_removed': unit_places_v1_removed,
    'unit_places_fifth_unit_dropped': unit_places_fifth_unit_dropped,
    'unit_name_gcode': unit_name_gcode,
    'unit_places_v2_removed_name_stays': unit_places_v2_removed_name_stays,
    'unit_state_v2_removed_parks': unit_state_v2_removed_parks,
    'unit_state_follows_moved_unit': unit_state_follows_moved_unit,
    'unit_assign_remove_and_reboot': unit_assign_remove_and_reboot,
    'unit_assign_swap_and_forget': unit_assign_swap_and_forget,
    'excluded_port_mcu': excluded_port_mcu,
    'excluded_port_config': excluded_port_config,
    'excluded_port_counted': excluded_port_counted,
    'shim_answers': shim_answers,
    'oem_iap_answers': oem_iap_answers,
    'connect_mixed_v2_order_usb': connect_mixed_v2_order_usb,
    'connect_mixed_v2_order_last': connect_mixed_v2_order_last,
    'print_fa_v2': print_fa_v2,
    'print_fa_v1_forbidden': print_fa_v1_forbidden,
    'print_fa_v2_dropped_arm': print_fa_v2_dropped_arm,
    'print_comms_loss_pauses': print_comms_loss_pauses,
    'print_comms_loss_other_unit_no_pause': print_comms_loss_other_unit_no_pause,
    'print_reconnect_rearms_fa': print_reconnect_rearms_fa,
    'print_comms_loss_restored_before_pause':
        print_comms_loss_restored_before_pause,
    'print_comms_loss_second_unit_stays_down':
        print_comms_loss_second_unit_stays_down,
    'print_unit_unpowered_pauses': print_unit_unpowered_pauses,
    'print_comms_loss_during_pause_pauses_on_resume':
        print_comms_loss_during_pause_pauses_on_resume,
    'v1_write_eio': v1_write_eio,
    'v2_writer_eio': v2_writer_eio,
    'id_wrap_v2': id_wrap_v2,
    'callback_exception': callback_exception,
    'ace_feed_retract': ace_feed_retract,
    'v1_stuck_busy_timeout': v1_stuck_busy_timeout,
    'late_join_via_switch': late_join_via_switch,
    'insert_idle_v2': insert_idle_v2,
    'insert_idle_v1': insert_idle_v1,
    'unplug_v2_replug': unplug_v2_replug,
    'unplug_v2_gone': unplug_v2_gone,
    'unplug_v1_replug': unplug_v1_replug,
    'v1_read_eio_once': v1_read_eio_once,
    'silence_v2': silence_v2,
    'head_mode_connect': head_mode_connect,
    'startup_wait_late_unit': startup_wait_late_unit,
    'startup_softfail': startup_softfail,
    'connect_v1': connect_v1,
    'connect_v2': connect_v2,
    'connect_mixed': connect_mixed,
    'heartbeat_60s_mixed': heartbeat_60s_mixed,
}


def summarize(entries, limit=80):
    kinds = {}
    for e in entries:
        kinds[e[1]] = kinds.get(e[1], 0) + 1
    print('entries=%d kinds=%s' % (len(entries), sorted(kinds.items())))
    for e in entries[:limit]:
        s = H.json.dumps(e, sort_keys=True, ensure_ascii=False)
        print(s[:220])


if __name__ == '__main__':
    name = sys.argv[1] if len(sys.argv) > 1 else 'connect_mixed'
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    summarize(SCENARIOS[name](), limit)
