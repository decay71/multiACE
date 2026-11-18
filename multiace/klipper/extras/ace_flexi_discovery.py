# SPDX-License-Identifier: GPL-3.0-or-later
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
"""Flexi route discovery: find which toolhead each ACE slot feeds.

For one slot at a time, with every toolhead empty and every path recorded
clear:

1. feed slowly, bounded by a maximum length, watching all toolhead sensors;
2. stop on the first sensor that detects filament (that head is the route),
   or give up at the bound (the slot is not connected);
3. pull the filament back by the distance it was fed minus a margin, so the
   tip ends a little forward of where it started: inside the ACE, behind
   every splitter, without leaving the ACE drive. The margin has a fixed part
   and a proportional part: on 210 (ACE Pro Gen1) an unwind moved ~3.5 %
   further than a slow feed of the same time-measured length, so a fixed
   margin alone would pull long strands PAST their start point;
4. require every toolhead sensor to read clear again before the next slot.

The result is a PROPOSAL (routes, splitter groups, load lengths). Nothing is
saved to the routing configuration here; the operator reviews and saves.

Hardware access goes through an injected ``io`` object so the sequence is
testable offline. The ACE reports no position, so distances are speed x time
measured from the moment the unit accepted the feed.
"""
import copy
import json
import math
import os
import time

DEFAULTS = {'speed': 25, 'max_mm': 3000, 'margin_mm': 20, 'retract_speed': 40, 'return_pct': 6}
LIMITS = {'speed': (5, 50), 'max_mm': (300, 4000), 'margin_mm': (10, 200),
          'retract_speed': (10, 80), 'return_pct': (0, 15)}
POLL_S = 0.1
CONFIRMATION = 'DISCOVER_ROUTES'


class DiscoveryError(Exception):
    pass


class Aborted(DiscoveryError):
    pass


def params(raw=None):
    out = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            raise DiscoveryError('Unknown discovery setting: %s' % key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise DiscoveryError('Invalid discovery setting: %s' % key)
        low, high = LIMITS[key]
        if not low <= value <= high:
            raise DiscoveryError('%s must be between %s and %s' % (key, low, high))
        out[key] = value
    return out


def suggested_load_length(length_mm):
    """Feed budget for the stock loader: the measured tube plus 150 mm,
    rounded up to 50 mm. The sensor stops the load on arrival, so a larger
    budget only prevents a short-feed retry."""
    return int(math.ceil((float(length_mm) + 150.0) / 50.0) * 50)


def proposal(results):
    """Routes, splitter groups and load lengths from per-slot results."""
    routes, unconnected, lengths, by_head = {}, [], {}, {}
    for key in sorted(results):
        r = results[key]
        if r.get('status') == 'arrived':
            routes[key] = r['head']
            lengths[key] = suggested_load_length(r['length_mm'])
            by_head.setdefault(r['head'], []).append(key)
        elif r.get('status') == 'no_arrival':
            unconnected.append(key)
    joiners = {}
    for head, keys in sorted(by_head.items()):
        if len(keys) < 2:
            continue
        joiners['discovered_t%d' % head] = {
            'name': 'Splitter to T%d' % head, 'head': head,
            'branches': {k: 'ACE%s slot %s' % tuple(k.split(':')) for k in keys},
            'confirmed': False, 'reference': ''}
    return {'routes': routes, 'unconnected': unconnected,
            'load_lengths': lengths, 'joiners': joiners}


class RouteDiscovery:
    def __init__(self, io, path=None, clock=time.time):
        self.io, self.path, self.clock = io, path, clock
        self.doc = self._load()

    # ---- persistence -----------------------------------------------------
    def _blank(self):
        return {'schema': 1, 'state': 'idle', 'current': None, 'results': {},
                'params': dict(DEFAULTS), 'error': None, 'started': None,
                'finished': None, 'sources': []}

    def _load(self):
        doc = self._blank()
        if self.path:
            try:
                with open(self.path, 'r', encoding='utf-8') as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict) and raw.get('schema') == 1:
                    doc.update(raw)
            except (OSError, ValueError):
                pass
        if doc['state'] == 'running':
            # The host restarted mid-run: the filament position is unknown.
            doc.update(state='failed', error='Interrupted by a restart while ACE%s slot %s was being probed. '
                       'Check that tube by eye before running discovery again.' % tuple(doc.get('current') or ('?', '?')))
        return doc

    def _save(self):
        if not self.path:
            return
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self.doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ---- queries ---------------------------------------------------------
    @property
    def running(self):
        return self.doc['state'] == 'running'

    def status(self):
        out = copy.deepcopy(self.doc)
        out['proposal'] = proposal(self.doc['results'])
        out['defaults'] = dict(DEFAULTS)
        out['limits'] = {k: list(v) for k, v in LIMITS.items()}
        return out

    def blockers(self, sources):
        """Reasons the run cannot start. Empty list = may start."""
        io, out = self.io, []
        if self.running:
            out.append('Discovery is already running.')
        if self.doc['state'] == 'failed':
            out.append('The last discovery stopped with filament in an unknown place: %s '
                       'Inspect it, then acknowledge before running again.' % self.doc['error'])
        if io.printing():
            out.append('A print is running or paused.')
        busy = io.busy()
        if busy:
            out.append(busy)
        present = [h for h, v in sorted(io.sensors().items()) if v is not False]
        if present:
            out.append('Unload every toolhead first: sensor reads filament (or is unreadable) on %s.'
                       % ', '.join('T%d' % h for h in present))
        not_clear = io.paths_not_clear()
        if not_clear:
            out.append('These paths are not recorded clear: %s. Unload them or record an inspection first.'
                       % ', '.join('T%d' % h for h in not_clear))
        if not sources:
            out.append('No ACE slot with filament to probe.')
        for a, s in sources:
            if not io.slot_ready(a, s):
                out.append('ACE%d slot %d is not connected or has no filament.' % (a, s))
        return out

    # ---- operator actions --------------------------------------------------
    def acknowledge(self):
        if self.running:
            raise DiscoveryError('Discovery is running; stop it first.')
        if self.doc['state'] == 'failed':
            self.doc.update(state='idle', error=None)
            self._save()

    def clear_results(self):
        if self.running:
            raise DiscoveryError('Discovery is running; stop it first.')
        self.doc = self._blank()
        self._save()

    def begin(self, sources, raw_params=None):
        p = params(raw_params)
        sources = [tuple(int(v) for v in s) for s in sources]
        problems = self.blockers(sources)
        if problems:
            raise DiscoveryError(' '.join(problems))
        self.doc.update(state='running', current=None, params=p, error=None,
                        started=self.clock(), finished=None,
                        sources=['%d:%d' % s for s in sources])
        for s in sources:
            self.doc['results'].pop('%d:%d' % s, None)
        self._save()
        return sources

    def run(self, sources):
        """Probe each source in order. Call after begin(). Never raises for
        an expected stop; the outcome is in the saved document."""
        p = self.doc['params']
        try:
            for source in sources:
                self.doc['current'] = list(source)
                self._save()
                key = '%d:%d' % source
                self.doc['results'][key] = self._probe(source, p)
                self._save()
            self.doc.update(state='done', current=None)
        except Aborted as exc:
            self.doc.update(state='aborted', error=str(exc), current=None)
        except DiscoveryError as exc:
            self.doc.update(state='failed', error=str(exc))
        except Exception as exc:  # unexpected: position unknown
            self.doc.update(state='failed', error='Unexpected error: %s' % exc)
        finally:
            self.io.set_scope(None)
            self.doc['finished'] = self.clock()
            self._save()
        return self.status()

    # ---- one slot ----------------------------------------------------------
    def _probe(self, source, p):
        io = self.io
        a, s = source
        present = [h for h, v in sorted(io.sensors().items()) if v is not False]
        if present:
            raise DiscoveryError('Before probing ACE%d slot %d, %s still reads filament. Nothing was moved for this slot.'
                                 % (a, s, ', '.join('T%d' % h for h in present)))
        io.set_scope(source)
        speed, bound = p['speed'], p['max_mm']
        accepted = io.feed(a, s, bound, speed)
        if accepted == 'empty':
            io.set_scope(None)
            return {'status': 'empty', 'head': None, 'length_mm': 0, 'returned_mm': 0}
        if accepted != 'ok':
            io.set_scope(None)
            raise DiscoveryError('ACE%d did not accept the feed for slot %d (%s). Nothing moved.' % (a, s, accepted))
        t0 = io.now()
        deadline = bound / float(speed) + 3.0
        head, aborted, fed = None, False, None
        while True:
            io.pause(POLL_S)
            elapsed = io.now() - t0
            hits = [h for h, v in sorted(io.sensors().items()) if v]
            if hits:
                io.stop_feed(a, s)
                head, fed = hits[0], min(bound, elapsed * speed)
                break
            if io.abort_requested():
                io.stop_feed(a, s)
                aborted, fed = True, min(bound, elapsed * speed)
                break
            if elapsed >= deadline:
                io.stop_feed(a, s)
                fed = bound
                break
        io.wait_idle(a)
        back = max(0, int(fed * (1.0 - p.get('return_pct', 0) / 100.0) - p['margin_mm']))
        braked = False
        if back:
            if io.start_retract(a, s, back, p['retract_speed']) != 'ok':
                if head is not None:
                    io.mark_uncertain(head, source, 'route discovery: return retract was refused')
                raise DiscoveryError('ACE%d refused to pull slot %d back after feeding ~%d mm. Filament may be in the tubes; inspect before anything else.'
                                     % (a, s, fed))
            # Watch the slot's own inlet sensor while pulling back. If the
            # strand did not really advance (slipping, not gripped, blocked)
            # the time-based return is too long: brake as soon as the ACE
            # reports the slot empty instead of pulling the strand out.
            r0 = io.now()
            while io.now() - r0 < back / float(p['retract_speed']) + 1.0:
                io.pause(POLL_S)
                if io.slot_empty(a, s):
                    io.stop_unwind(a, s)
                    braked = True
                    break
            io.wait_idle(a)
        io.set_scope(None)
        if braked or io.slot_empty(a, s):
            result = {'head': head, 'length_mm': int(round(fed)), 'returned_mm': back,
                      'status': 'pulled_out'}
            self.doc['results']['%d:%d' % source] = result
            raise DiscoveryError('ACE%d slot %d reported empty while being pulled back%s: the filament did not travel '
                                 'as far as commanded (slipping, not gripped or blocked) and its end is now at the ACE inlet. '
                                 'Re-seat that spool, check its tube, then acknowledge.'
                                 % (a, s, ' (braked)' if braked else ''))
        still = [h for h, v in sorted(io.sensors().items()) if v is not False]
        if still:
            for h in still:
                io.mark_uncertain(h, source, 'route discovery: sensor still reads filament after return')
            raise DiscoveryError('After pulling ACE%d slot %d back, %s still reads filament. Inspect that tube.'
                                 % (a, s, ', '.join('T%d' % h for h in still)))
        if aborted:
            raise Aborted('Stopped by the operator during ACE%d slot %d; it was pulled back %d mm.' % (a, s, back))
        result = {'head': head, 'length_mm': int(round(fed)), 'returned_mm': back,
                  'status': 'arrived' if head is not None else 'no_arrival'}
        if len(hits) > 1:
            result['also_detected'] = hits[1:]
        return result
