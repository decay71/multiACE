# @MULTIACE_FLEXI_HOOK Flexi code below uses Flexi, ace_flexi_engine.py
"""Pause the 1 Hz ACE status poll while a long travel is queued.

One ACE poll beat costs 89-198 ms of reactor time on this host (the status
round trip plus the merge/split/push its callback performs), measured with the
module's own reactor-stall watchdog. During a long homing travel that starved
the MCU step queue and shut it down with 0003-0522 "Timer too close" before the
move finished, which is why homing only succeeded intermittently.

H1 pins the source of the ACE backend and of the Flexi motor gate, and those
pins cover whole files, so the gate cannot live in ace.py or
ace_flexi_engine.py without refusing to install. This module is a separate,
unpinned Klipper config section; it arms from klippy:ready and reaches the ACE
instance through the printer object table.

Only long non-print travels are gated. A print keeps the normal 1 Hz poll: its
status must stay live and its queued-move surplus is continuous by design.

The poll timers are unregistered for the travel and rebuilt through the ACE
module's own heartbeat factory afterwards, so no poll logic is duplicated. A
paused beat is not transport silence, so the resume stamps _last_rx_ts:
otherwise the first beat after a 10 s travel would read as an 8 s mute and
start a reconnect.
"""
# @MULTIACE_FLEXI_HOOK Flexi code below uses MULTIACE_FLEXI_COMPAT, Flexi
# @MULTIACE_FLEXI_COMPAT Standalone poll gate keeps the Flexi motor gate and the
# original ACE backend independently installable; it does not change poll policy.
import logging
import time

INTERVAL_S = 0.5
PAUSE_SURPLUS_S = 0.4
RESUME_SURPLUS_S = 0.1
READY_RETRY_S = 2.0


class AcePollGate:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.ace = None
        self.enabled = config.getboolean('enabled', True)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _handle_ready(self):
        if not self.enabled:
            logging.info('[multiACE][poll] motion gate disabled by config')
            return
        self._attempts = 0
        self.reactor.register_timer(self._arm, self.reactor.monotonic() + READY_RETRY_S)

    def _arm(self, eventtime):
        ace = self.ace or self.printer.lookup_object('ace', None)
        if ace is None or getattr(ace, 'reactor', None) is None:
            # The ACE object may not be built yet; keep waiting briefly.
            self._attempts = getattr(self, '_attempts', 0) + 1
            if self._attempts < 30:
                return eventtime + READY_RETRY_S
            logging.info('[multiACE][poll] motion gate gave up: no ACE object')
            return self.reactor.NEVER
        self.ace = ace
        if getattr(ace, '_ace_poll_gate_timer', None) is not None:
            return self.reactor.NEVER
        ace._ace_poll_gate_paused = False
        ace._ace_poll_gate_timer = self.reactor.register_timer(
            self._tick, self.reactor.monotonic() + 1.0)
        logging.info('[multiACE][poll] motion gate armed: the 1 Hz ACE poll '
                     'pauses during long travels')
        return self.reactor.NEVER

    def _tick(self, eventtime):
        try:
            self._gate()
        except Exception:
            logging.exception('[multiACE][poll] motion gate failed')
        return eventtime + INTERVAL_S

    def _gate(self):
        ace = self.ace
        timers = getattr(ace, '_heartbeat_timers', None)
        if not isinstance(timers, dict) or not timers:
            return
        toolhead = ace.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return
        status = toolhead.get_status(0)
        surplus = (float(status.get('print_time', 0.))
                   - float(status.get('estimated_print_time', 0.)))
        stats = ace.printer.lookup_object('print_stats', None)
        printing = stats is not None and (
            getattr(stats, 'state', '') or '').lower() in ('printing', 'paused')
        paused = getattr(ace, '_ace_poll_gate_paused', False)
        if surplus > PAUSE_SURPLUS_S and not printing and not paused:
            ace._ace_poll_gate_paused = True
            count = 0
            for idx, timer in list(timers.items()):
                try:
                    self.reactor.unregister_timer(timer)
                    count += 1
                except Exception:
                    pass
            if count:
                logging.info('[multiACE][poll] paused the 1 Hz ACE poll for a '
                             'long travel (%d unit(s))', count)
        elif surplus < RESUME_SURPLUS_S and paused:
            ace._ace_poll_gate_paused = False
            now = time.monotonic()
            count = 0
            for idx in list(timers):
                try:
                    ace._last_rx_ts[idx] = now
                    timers[idx] = self.reactor.register_timer(
                        ace._make_heartbeat_tick_for(idx), now + 0.5)
                    count += 1
                except Exception as exc:
                    logging.info('[multiACE][poll] resume failed MMU %d: %s', idx, exc)
            if count:
                logging.info('[multiACE][poll] resumed the 1 Hz ACE poll after '
                             'the travel (%d unit(s))', count)


def load_config(config):
    return AcePollGate(config)
