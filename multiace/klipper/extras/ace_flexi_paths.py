# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable physical-path ledger for Flexi routed loading.

Flexi mode runs the original MultiACE loader (ACE_LOAD_HEAD -> FEED_AUTO) and
unloader. The stock code tracks the feeding source in ``head_source`` and, in
several places, clears it as soon as the toolhead sensor reads empty. On a
shared PTFE path (two or more ACE slots joined into one toolhead) an empty
toolhead sensor does not prove the joiner is clear, so Flexi keeps its own
record of which source may occupy each toolhead's tube.

State per toolhead::

    {'state': 'clear' | 'loaded' | 'in_progress' | 'uncertain',
     'owner': [ace, slot] | None, 'basis': str, 'at': float}

Each value means exactly one thing (no state is inferred from a command
acknowledgement):

* ``clear``       -- no filament from any source is believed to be past the
                     ACE outlets. Only a verified full stock unload or an
                     explicit operator inspection produces it.
* ``loaded``      -- the stock loader finished and the toolhead sensor saw
                     filament of ``owner``.
* ``in_progress`` -- a load of ``owner`` was started and has not reported.
                     After a restart this is treated as ``uncertain``.
* ``uncertain``   -- filament of ``owner`` may be anywhere between its ACE and
                     the nozzle (failed/short/interrupted load or unverified
                     unload). The same source may retry; a different source
                     may not enter a shared path until the owner is unloaded
                     or an operator records the path as inspected clear.

The module performs no I/O other than its own JSON file and never commands
hardware.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import json
import os
import time

STATES = ('clear', 'loaded', 'in_progress', 'uncertain')


class PathLedgerError(ValueError):
    pass


class PathLedger:
    def __init__(self, path=None, clock=time.time):
        self.path = path
        self.clock = clock
        self.heads = {}
        self.load_error = None
        if path:
            self._load()

    # ---- persistence -----------------------------------------------------
    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # Unreadable ledger: every head becomes uncertain (fail safe)
            # and the file is left untouched for inspection.
            self.load_error = str(exc)
            return
        heads = raw.get('heads') if isinstance(raw, dict) else None
        if not isinstance(heads, dict):
            self.load_error = 'invalid ledger document'
            return
        for key, rec in heads.items():
            try:
                head = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get('state') not in STATES:
                continue
            rec = dict(rec)
            if rec['state'] == 'in_progress':
                # The process ended while a load was running.
                rec.update(state='uncertain',
                           basis='interrupted during load (restart)')
            self.heads[head] = rec

    def _save(self):
        if not self.path:
            return
        doc = {'schema': 1, 'heads': {str(h): r for h, r in
                                      sorted(self.heads.items())}}
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ---- queries ---------------------------------------------------------
    def record(self, head):
        rec = self.heads.get(int(head))
        if rec is None:
            if self.load_error:
                return {'state': 'uncertain', 'owner': None,
                        'basis': 'path ledger unreadable: ' + self.load_error,
                        'at': None}
            # Nothing recorded yet (first use): the tube contents are not
            # known. Single-source heads are unaffected; a shared head needs
            # one verified unload or recorded inspection before a different
            # source may enter it.
            return {'state': 'uncertain', 'owner': None,
                    'basis': 'no Flexi path record yet', 'at': None}
        return copy.deepcopy(rec)

    @staticmethod
    def _src(source):
        return [int(source[0]), int(source[1])]

    def feed_allowed(self, head, source, shared):
        """(allowed, reason) for moving ``source`` forward into ``head``.

        Single-source heads have no other filament that could collide; a
        shared head admits only the recorded owner unless the path is clear.
        """
        rec = self.record(head)
        src = self._src(source)
        if rec['state'] == 'clear' or rec.get('owner') in (None, src):
            if rec.get('owner') is None and rec['state'] == 'uncertain' and shared:
                return False, ('T%d shared path state is uncertain (%s). '
                               'Inspect the joiner and record it clear, or '
                               'unload, before loading.' % (int(head), rec['basis']))
            return True, ''
        if not shared:
            return True, ''
        return False, ('T%d shared PTFE path holds ACE%d slot %d filament '
                       '(%s, %s). Unload it before loading ACE%d slot %d.'
                       % (int(head), rec['owner'][0], rec['owner'][1],
                          rec['state'], rec['basis'], src[0], src[1]))

    # ---- transitions -----------------------------------------------------
    def _set(self, head, state, owner, basis):
        self.heads[int(head)] = {'state': state,
                                 'owner': None if owner is None else self._src(owner),
                                 'basis': basis, 'at': self.clock()}
        self._save()

    def begin_load(self, head, source):
        self._set(head, 'in_progress', source, 'stock load started')

    def finish_load(self, head, source, arrived):
        if arrived:
            self._set(head, 'loaded', source, 'toolhead sensor detected filament')
        else:
            self._set(head, 'uncertain', source,
                      'load did not reach the toolhead sensor; filament '
                      'position unknown')

    def finish_unload(self, head, source, verified):
        if verified:
            self._set(head, 'clear', None,
                      'stock unload completed: sensor cleared and full retract '
                      'commanded')
        else:
            self._set(head, 'uncertain', source,
                      'unload not verified; filament may remain in the tube')

    def adopt_uncertain(self, head, source, basis):
        """Record an existing, unresolved uncertainty (e.g. a legacy Flexi
        recovery record) without discarding a more specific state."""
        rec = self.heads.get(int(head))
        if rec and rec.get('state') in ('uncertain', 'loaded') and rec.get('owner'):
            return
        self._set(head, 'uncertain', source, basis)

    def operator_clear(self, head, basis):
        self._set(head, 'clear', None, 'operator: ' + basis)

    def operator_loaded(self, head, source, basis):
        self._set(head, 'loaded', source, 'operator: ' + basis)

    def operator_same_source(self, head, source, basis):
        self._set(head, 'uncertain', source, 'operator: ' + basis)

    def status(self):
        return {str(h): self.record(h) for h in range(4)}
