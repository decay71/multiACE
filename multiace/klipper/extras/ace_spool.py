"""Spool table, tag binding and consumption booking (mixin of MultiAce).

The local spool table (load/save, keys, SKU and card-UID matching), the
binding of a read tag to a table row (V1 status path, V2 ingest, the
connect rescan and the stale-release rules), the identity fill from a
bound spool, consumption booking (`book_spool_use`, the 1 Hz sampler), the
per-spool pressure advance, the SpoolLink resolver calls and the
ACE_SPOOL_* commands. State lives on the MultiAce instance (initialised in
ace.py); this module holds the code and its constants. Third module of the
R1 split (UNIT_DRIVER_DESIGN.md §11): every method here is byte-identical
to its former place in ace.py, proven by tests/units/tools/move_check.py.
"""
import logging
import json
import os
import re

from .ace_protocol import GATE_AVAILABLE

SPOOL_SAMPLE_INTERVAL = 1.0
SPOOL_FLUSH_INTERVAL = 60.0          # disk writes: see _spool_sample_tick
# Euclidean RGB distance above which a tag colour and its table entry are
# called different (see _spool_enrich_tag_info). Generous on purpose: shade
# and white-balance differences between a written tag and a typed hex are
# normal, a swapped SPOOL is not - #FFFFFF vs #FEFEFE is 43 and stays quiet,
# any two distinguishable filament colours are far above this.
SPOOL_COLOR_WARN_DIST = 60.0
# Sanity clamp per sample: >30mm/s flow is already extreme, so a bigger jump
# is a position reset (SET_POSITION / homing), not extrusion - ignore it.
SPOOL_SAMPLE_MAX_MM = 200.
SPOOL_FILAMENT_AREA_MM2 = 2.405      # 1.75mm filament
SPOOL_DENSITY_DEFAULT = 1.24         # g/cm3 (PLA)
SPOOL_DENSITY_BY_MATERIAL = {
    'pla': 1.24, 'petg': 1.27, 'abs': 1.04, 'asa': 1.07, 'tpu': 1.21,
    'pc': 1.20, 'pa': 1.14, 'pva': 1.23, 'hips': 1.04, 'pet': 1.27,
}

# SpoolLink mode (identity_priority: spoollink + the paxx resolver agent
# present): an SM-bound head is SET via the resolver's documented spool_id
# path instead of SET_PRINT_FILAMENT_CONFIG - the call is fire-and-forget
# (Moonraker answers nothing back), so the heal loop verifies against
# PTC's filament_spool_id and re-sends at most SPOOLLINK_SEND_MAX times,
# SPOOLLINK_RESEND_S apart, before retreating with one warning.
SPOOLLINK_RESOLVE_METHOD = 'spoollink_resolve_spool'
# Grace before the startup serviceability check: the resolver may
# register its remote method in its own klippy:ready handler.
SPOOLLINK_STARTUP_CHECK_DELAY = 5.0
SPOOLLINK_RESEND_S = 5.0
SPOOLLINK_SEND_MAX = 3


class SpoolMixin:

    def _resolve_tag_binds(self, idx, items):
        """Decide the tag bindings of ONE unit, with every slot's fresh code
        already on record. `items` is [(slot, sku, info_or_None)] in slot
        order; info != None additionally gets the tag-enrichment (V2 carries
        an info dict, a V1 status does not).

        Deliberately ONE decision path for both protocols: they differ only
        in WHEN their reads are complete - a V1 status carries all four
        codes at once, a V2 answers per slot - so only the collecting
        differs, never the deciding."""
        for slot, sku, info in items:
            try:
                # The card UID of the SAME read (V2 host read or firmware
                # sentinel) first; a V1 status carries none.
                spool = self._spool_bind_tag_codes(
                    idx, slot, (info or {}).get('uid'), sku)
                if info is not None:
                    self._spool_enrich_tag_info(info, spool,
                                                ace_idx=idx, slot=slot)
            except Exception as e:
                logging.info('[multiACE] [spool] bind MMU %d slot %d failed '
                             '(ignored): %s'
                             % (self._raw(idx), self._raw(slot), e))

    def _spoollink_agent_present(self):
        """Is the paxx resolver agent registered right now?
        (has_remote_method - the same gate pattern stock 1.5.2 uses for
        spoolman_set_active_spool.)"""
        try:
            wh = self.printer.lookup_object('webhooks')
            return bool(wh.has_remote_method(SPOOLLINK_RESOLVE_METHOD))
        except Exception:
            return False

    def _spoollink_startup_check(self, eventtime=None):
        """One-shot a few seconds after klippy:ready: is the configured
        spoollink world actually serviceable? ACE_SET_SPOOLMAN refuses to
        ENTER it without the resolver, but a config line survives a move to
        stock firmware (where SpoolLink does not exist) or paxx's Spoolman
        integration being switched off.

        Warn, never fall back: the world switch is explicit and a silent
        fallback would move counting back to us behind the user's back
        (the same reason _spoollink_active has no agent gate). Delayed
        because the agent may register its remote method in its own ready
        handler, i.e. after ours."""
        try:
            if getattr(self, 'spool_mode', 'local') != 'spoollink':
                return
            if self._spoollink_agent_present():
                return
            self.log_warn(self._t('msg.spoollink_no_agent',
                                  method=SPOOLLINK_RESOLVE_METHOD))
        except Exception:
            logging.warning('[multiACE] spoollink startup check failed',
                            exc_info=True)

    def _spoollink_active(self):
        """SpoolLink world = the explicit switch position, nothing else.
        No agent-presence gate and no per-print latch: the choice is
        explicit and stable, so a briefly missing agent must not silently
        move counting back to us - _spoollink_send has its own failure
        path when the resolver is gone."""
        return getattr(self, 'spool_mode', 'local') == 'spoollink'

    def _spoollink_smid_for(self, head):
        """Spoolman id of the spool bound to the slot feeding `head`,
        else 0. Only a bound, Spoolman-backed spool qualifies - that is
        decision B: unbound slots / job identities keep the normal push
        and stay display-editable. A head WITHOUT head_source resolves
        via its h<n> HEAD binding (feeder/manual) - without that fallback
        the SL world would count feeder consumption NOWHERE (our booking is
        off in SL, and SpoolLink never learns the spool)."""
        try:
            src = self._head_source.get(head)
            if src:
                sid = self._spool_binding.get(
                    self._spool_key(src['ace_index'], src['slot']))
            else:
                sid = self._spool_binding.get(self._spool_head_key(head))
            sp = self._spools.get(str(sid)) if sid else None
            smid = str((sp or {}).get('spoolman_id') or '').strip()
            return int(smid) if smid.isdigit() else 0
        except Exception:
            return 0

    def _spoollink_send(self, head, smid, why):
        """Hand the head's spool to the SpoolLink resolver
        (spoollink_resolve_spool(channel, spool_id=...), the spool_id path
        its own header documents). SpoolLink fetches the spool from
        Spoolman, pushes the identity through the RFID-official path
        (spoollink/set) and stamps filament_spool_id - which its
        active-spool sync turns into Moonraker-side counting on every
        toolchange. One setter, no FORCE, no OFFICIAL fight.

        FEEDER/MANUAL precedence: the stock side feeder has its OWN reader, and
        SpoolLink resolves its card UID itself - that source SAW the
        physical spool, our h<n> binding is hand-declared. So for a
        non-ACE head this send is a GAP-FILLER, never an override: a
        different id already on the head wins and the send is skipped."""
        try:
            if not self.head_uses_ace(head):
                _cur = self._ptc_spool_id_for(head)
                if _cur > 0 and _cur != int(smid):
                    logging.info(
                        '[multiACE] [spoollink] head %d: feeder reader has '
                        'spool_id=%d, our binding says %d - reader wins, '
                        'not sending (%s)'
                        % (self._raw(head), _cur, int(smid), why))
                    self._spoollink_sent.pop(head, None)
                    return False
            wh = self.printer.lookup_object('webhooks')
            wh.call_remote_method(SPOOLLINK_RESOLVE_METHOD,
                                  channel=int(head), spool_id=int(smid))
            ent = self._spoollink_sent.get(head) or {'n': 0}
            self._spoollink_sent[head] = {
                'sid': int(smid),
                't': self.reactor.monotonic(),
                'n': int(ent.get('n', 0)) + 1}
            logging.info('[multiACE] [spoollink] head %d -> resolver '
                         'spool_id=%d (%s, attempt %d)'
                         % (self._raw(head), smid, why,
                            self._spoollink_sent[head]['n']))
            return True
        except Exception as e:
            logging.warning('[multiACE] [spoollink] head %d resolver call '
                            'failed (%s): %s' % (self._raw(head), why, e))
            return False

    def _spoollink_clear(self, head):
        """Release a SpoolLink-set head on the empty-clear: route the
        clear through the stock RFID-clear path (resets identity, the
        official flag AND filament_spool_id - the default info struct
        has no SPOOL_ID key, so PTC stores info.get('SPOOL_ID', 0) = 0),
        because SET_PRINT_FILAMENT_CONFIG cannot un-official a head and
        the flag otherwise survives the spool it belonged to.
        Two rungs: filament_detect's request_clear_filament_info first
        (drives the reader-side cache too), else the direct PTC callback
        with a default struct - the exact call paxx's own spoollink/set
        endpoint uses. EVERY outcome logs its reason, otherwise an
        un-cleared spool id only shows up in SpoolLink's Moonraker log.
        Returns True when a clear was dispatched; False -> caller keeps
        the normal push as fallback."""
        try:
            fd = self.printer.lookup_object('filament_detect', None)
            if fd is not None and hasattr(fd, 'request_clear_filament_info'):
                fd.request_clear_filament_info(int(head))
                logging.info('[multiACE] [spoollink] head %d cleared via '
                             'filament_detect' % self._raw(head))
                return True
            logging.info('[multiACE] [spoollink] head %d: filament_detect '
                         '%s - trying the direct PTC clear'
                         % (self._raw(head),
                            'not found' if fd is None
                            else 'has no request_clear_filament_info'))
        except Exception as e:
            logging.info('[multiACE] [spoollink] head %d filament_detect '
                         'clear failed (%s) - trying the direct PTC clear'
                         % (self._raw(head), e))
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            cb = getattr(ptc, '_rfid_filament_info_update_cb', None)
            if ptc is None or cb is None:
                logging.info('[multiACE] [spoollink] head %d clear: no PTC '
                             'callback either - falling back to the normal '
                             'push' % self._raw(head))
                return False
            from extras import filament_protocol as _fp
            cb(int(head), dict(_fp.FILAMENT_INFO_STRUCT), is_clear=True)
            logging.info('[multiACE] [spoollink] head %d cleared via direct '
                         'PTC callback' % self._raw(head))
            return True
        except Exception as e:
            logging.info('[multiACE] [spoollink] head %d clear failed on '
                         'both rungs (falling back to normal push): %s'
                         % (self._raw(head), e))
            return False

    def _spoollink_verify(self, head):
        """Heal-loop hook for a managed head: the resolver call is
        fire-and-forget, so verify against PTC's filament_spool_id and
        re-send (bounded) while it still shows a different spool."""
        ent = self._spoollink_sent.get(head)
        if not ent:
            return
        try:
            # The entry is a MEMORY of what was sent, not the truth: the
            # binding can change underneath it (unload, slot released,
            # re-assignment), and a head-mode unload push carries the
            # wired slot's identity, so the empty-clear never ran for it.
            # Re-derive from the binding every time; a stale id must not
            # be re-sent whenever stock clears the field during a load.
            _want = self._spoollink_smid_for(head)
            _old = int(ent.get('sid', 0))
            if _want != _old:
                self._spoollink_sent.pop(head, None)
                if _want > 0:
                    ent = {'sid': int(_want), 't': 0., 'n': 0}
                    self._spoollink_sent[head] = ent
                    logging.info(
                        '[multiACE] [spoollink] head %d: binding now says '
                        'spool_id=%d (sent %d) - following the slot'
                        % (self._raw(head), _want, _old))
                else:
                    _cur0 = self._ptc_spool_id_for(head)
                    if (_cur0 == _old and _cur0 > 0
                            and self.head_uses_ace(head)):
                        logging.info(
                            '[multiACE] [spoollink] head %d: no bound '
                            'spool any more (sent %d) - releasing the '
                            'claim' % (self._raw(head), _old))
                        self._spoollink_clear(head)
                    else:
                        logging.info(
                            '[multiACE] [spoollink] head %d: no bound '
                            'spool any more (sent %d) - resends stopped'
                            % (self._raw(head), _old))
                    return
            _cur = self._ptc_spool_id_for(head)
            if _cur == int(ent.get('sid', 0)):
                if ent.get('n', 0):
                    ent['n'] = 0    # settled - a later change starts fresh
                return
            if _cur > 0 and not self.head_uses_ace(head):
                # Feeder/manual: a DIFFERENT id is the feeder reader's own
                # resolution = external truth, not a lost send (see
                # _spoollink_send precedence). Stop resending, log once.
                self._spoollink_sent.pop(head, None)
                logging.info(
                    '[multiACE] [spoollink] head %d: feeder reader set '
                    'spool_id=%d (ours was %s) - reader wins, resends '
                    'stopped' % (self._raw(head), _cur, ent.get('sid')))
                return
            now = self.reactor.monotonic()
            if now - float(ent.get('t', 0.)) < SPOOLLINK_RESEND_S:
                return
            n = int(ent.get('n', 0))
            if n < SPOOLLINK_SEND_MAX:
                self._spoollink_send(head, ent['sid'], 'verify')
            elif n == SPOOLLINK_SEND_MAX:
                ent['n'] = n + 1
                logging.warning(
                    '[multiACE] [spoollink] head %d never took spool_id=%s '
                    'after %d sends - is the SpoolLink service healthy?'
                    % (self._raw(head), ent.get('sid'), n))
        except Exception:
            pass

    def _inherit_prev_capture(self, head, ace_index, slot, ident):
        """Stock's backup-guard semantics, ported to the head_source
        capture: an EMPTY identity never overwrites a good
        one when the head reloads from the SAME (ace, slot). A fresh
        undeclared spool in the lane inherits what the lane last
        verifiably held - exactly how stock's filament_info_backup
        refuses empty overwrites (the vendor guard,
        print_task_config.py:325), which is why stock replenish keeps
        working across like-for-like spool swaps. Different slot
        -> no inherit: the ghost risk stays scoped to the lane, and a
        user who swaps in a different colour undeclared inherits
        knowingly (declared-truth stance - like a wrong RFID).
        Mutates and returns ident; the identity chain survives the
        provisional pre-load stamp because that stamp inherits too."""
        try:
            own_t = (ident.get('type') or '').strip()
            if own_t and own_t.upper() != 'NONE':
                return ident
            prev = self._head_source.get(head)
            if not prev:
                return ident
            if (prev.get('ace_index') != ace_index
                    or prev.get('slot') != slot):
                return ident
            prev_t = (prev.get('type') or '').strip()
            if not prev_t or prev_t.upper() == 'NONE':
                return ident
            ident['type'] = prev.get('type')
            own_c = (ident.get('color') or '').lstrip('#').upper()[:6]
            if not own_c:
                # '' is the only "unknown" - a literal 000000 that reaches
                # a capture is a declared black (_device_color_hex).
                ident['color'] = prev.get('color', '')
            if not (ident.get('brand') or '').strip():
                ident['brand'] = prev.get('brand', '')
            if not (ident.get('subtype') or '') and prev.get('subtype'):
                ident['subtype'] = prev.get('subtype', '')
            logging.info(
                '[multiACE] capture: head %d inherits identity %s/%s '
                'from the previous load (same ace%d/s%d, new spool '
                'undeclared - stock backup-guard semantics)',
                head, ident.get('type'), ident.get('color') or '-',
                ace_index, slot)
        except Exception:
            pass
        return ident

    def _device_color_hex(self, slot_info):
        """Colour a slot's DEVICE data declares, '' when it declares none.

        THE ONE PLACE that resolves the all-zero ambiguity.
        An empty ACE slot reports RGB (0,0,0), but a tag may legitimately
        encode black - the raw value cannot tell those apart, so writing
        '000000' into a capture made "unknown" and "black" the same
        string and every reader downstream had to guess (and could, e.g.,
        let a quad cascade load orange into a black head).

        Stock's own discriminator settles it and is used here: an
        identity exists when a TYPE is declared, never from the colour
        (stock tests filament_type/filament_vendor for "configured" and
        defaults an unset colour to FFFFFFFF). So (0,0,0) WITH a type is
        the tag's black and is kept; (0,0,0) without one is "nothing
        known" and becomes ''.

        Normalising here is what makes '000000' unambiguous everywhere
        else: downstream it can only come from a deliberate declaration -
        an override, an RFID tag, a display edit - so no reader needs a
        sentinel test of its own. The two boundaries that still carry one
        are the ones facing OUTWARD (the touchscreen's placeholder push,
        our own empty-marker push); they are ingest rules for foreign
        data, not guesses about ours."""
        try:
            rgb = slot_info.get('color')
            if not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
                return ''
            hexv = self.rgb2hex(*[int(v) for v in rgb[:3]])
        except (TypeError, ValueError):
            return ''
        if hexv != '000000':
            return hexv
        mat = (slot_info.get('material') or slot_info.get('type')
               or '').strip()
        return hexv if mat and mat.upper() != 'NONE' else ''

    def _slot_declared_identity(self, ace_idx, slot_idx):
        """(type_lower, color_rgb_hex_upper_or_'') of a slot's DECLARED
        identity - override first, then RFID slot info (overrides are user
        truth). None when no type known."""
        try:
            ov = self._slot_overrides.get(
                '%d_%d' % (ace_idx, slot_idx)) or {}
            info = self._info_per_ace.get(ace_idx) or {}
            slots_by_index = {s.get('index', n): s
                              for n, s in enumerate(info.get('slots') or [])
                              if isinstance(s, dict)}
            s = slots_by_index.get(slot_idx) or {}
            mat = (ov.get('material') or s.get('material')
                   or s.get('type') or '').strip()
            if not mat or mat.upper() == 'NONE':
                return None
            color = (ov.get('color') or '').strip().lstrip('#').upper()[:6]
            if not color:
                # _device_color_hex resolves the all-zero ambiguity; an
                # override colour needs no such test, the user typed it.
                color = self._device_color_hex(s)
            return (mat.lower(), color)
        except Exception:
            return None

    def _load_spool_db(self):
        """Read the spool table. Missing/corrupt file -> empty table, never
        a startup failure: filament book-keeping must not keep a printer
        from printing."""
        try:
            with open(self.spool_db_path, 'r') as f:
                data = json.load(f)
            sp = data.get('spools') or {}
            self._spools = {str(k): dict(v) for k, v in sp.items()
                            if isinstance(v, dict)}
            self._spool_binding = {str(k): str(v) for k, v in
                                   (data.get('binding') or {}).items()}
            self._spool_next_id = int(data.get('next_id', 1))
        except (IOError, OSError, ValueError, TypeError, AttributeError) as e:
            logging.info('[multiACE] [spool] no usable table at %s (%s) - '
                         'starting empty' % (self.spool_db_path, e))
            self._spools, self._spool_binding, self._spool_next_id = {}, {}, 1
        # A binding pointing at a deleted spool is dead weight - drop it.
        for key in [k for k, v in self._spool_binding.items()
                    if v not in self._spools]:
            self._spool_binding.pop(key, None)
        # Bindings from the WRONG world are leftovers (either/or split) - in
        # Spoolman mode that is a local entry bound by an old unfiltered tag
        # auto-bind. Same
        # rule the world SWITCH applies, applied at load so an affected
        # printer heals on its own. The entries themselves stay: local
        # spools are never dropped, they are just not part of this world.
        stale = [k for k, v in self._spool_binding.items()
                 if not self._spool_in_world(self._spools.get(v))]
        for key in stale:
            sid = self._spool_binding.pop(key, None)
            logging.info('[multiACE] [spool] binding %s -> #%s dropped at '
                         'load (entry not in the active spool world)'
                         % (key, sid))
        if stale:
            self._save_spool_db()

    def _save_spool_db(self, backup=False):
        """Persist the table. Atomic (tmp + replace) so a power cut cannot
        leave a half-written file. `backup` keeps ONE rolling <file>.bak -
        and only USER edits ask for it: a backup taken on every consumption
        flush would be seconds old and worthless, and it must never grow
        into a pile of files (one fixed name, overwritten)."""
        try:
            d = os.path.dirname(self.spool_db_path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            tmp = self.spool_db_path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({'spools': self._spools,
                           'binding': self._spool_binding,
                           'next_id': self._spool_next_id}, f, indent=1)
            if backup:
                try:
                    if os.path.exists(self.spool_db_path):
                        os.replace(self.spool_db_path,
                                   self.spool_db_path + '.bak')
                except OSError:
                    pass
            os.replace(tmp, self.spool_db_path)
            self._spool_last_write = self.reactor.monotonic()
        except (IOError, OSError, TypeError, ValueError) as e:
            logging.info('[multiACE] [spool] save failed (%s): %s'
                         % (self.spool_db_path, e))

    def _spool_key(self, ace_idx, slot):
        return '%d_%d' % (int(ace_idx), int(slot))

    def _spool_head_key(self, head):
        """Binding key for a spool bound to a HEAD instead of an ACE slot -
        the feeder/manual case: those heads have no slot,
        their spool sits physically at the head's side feeder. 'h<n>' is
        deliberately unparseable as '<ace>_<slot>': every existing consumer
        of the binding dict int()-parses the key inside a try/except
        (_spool_slot_of, the ASSIGN occupied-check) or string-matches the
        exact slot key, so head keys flow through them as 'not a slot'
        without any of them needing to learn the new domain."""
        return 'h%d' % int(head)

    def _spool_binding_where(self, key):
        """Display text for a binding key: 'ACE x / Slot y' for a slot key,
        'Head n' for a head key ('h<n>', feeder/manual heads). The two spool
        console commands used to int-parse every key as '<ace>_<slot>', and
        a head key raised ValueError there, which Klipper turns into a
        shutdown."""
        try:
            if key.startswith('h'):
                return 'Head %d' % self._disp(int(key[1:]))
            a, sl = key.split('_')
            return 'MMU %d / Slot %d' % (self._disp(int(a)),
                                         self._disp(int(sl)))
        except Exception:
            return str(key)

    @staticmethod
    def _sku_canon(sku):
        """Canonical form of an RFID SKU for COMPARISON only - the stored
        value stays verbatim (that is what the user typed and sees).
        A leading '#' is dropped because it is a user marker to tell an
        own-written tag from a factory one, not part of the identity - and
        because '#' terminates gcode arguments, so it can never travel
        through ACE_SPOOL_SET anyway. Case-insensitive: writer apps are
        not consistent about it."""
        return (sku or '').strip().lstrip('#').strip().lower()

    def _sku_codes(self, sku):
        """A stored SKU may carry SEVERAL codes, comma-separated (a Bambu
        roll has a chip per flange = two card UIDs).
        Canonical list, empties dropped; a single code is a 1-list."""
        return [c for c in (self._sku_canon(x)
                            for x in str(sku or '').split(','))
                if c]

    def _sku_bases(self, sku):
        return {self._sku_base(c) for c in self._sku_codes(sku)}

    def _spool_in_world(self, sp):
        """True when this entry belongs to the ACTIVE spool world (the
        either/or split): with a Spoolman URL set only Spoolman-backed
        entries take part in tag matching, without one only local entries.
        Searching the WHOLE table would, in Spoolman mode, re-bind exactly
        the local entries the list's world filter hides and resurrect spools
        the user believed gone. A written 'SM<id>'/bare-id tag still
        auto-binds its adopted spool - that path stays."""
        return (bool(str((sp or {}).get('spoolman_id') or '').strip())
                == (getattr(self, 'spool_mode', 'local') != 'local'))

    def _spool_by_sku(self, sku):
        """(id, spool) of the table entry carrying this tag, or (None, None).
        First match wins; a duplicated SKU is a table error, not a state we
        try to resolve here. Only entries of the ACTIVE world take part
        (see _spool_in_world) - the sole caller is the tag auto-bind."""
        want = self._sku_canon(sku)
        if not want:
            return (None, None)
        for sid, sp in self._spools.items():
            if not self._spool_in_world(sp):
                continue
            if want in self._sku_codes(sp.get('sku')):
                return (sid, sp)
        # Fallback: a tag carrying the BARE Spoolman spool id. That is the
        # convention of tag-writer apps ("I use the Spoolman spool ID as the
        # SKU"), while our own Spoolman import generates 'SM<id>' - so their
        # tag '123' would never find our entry 'SM123' on the exact compare
        # above. Matched against the spoolman_id FIELD, not by stripping an
        # 'SM' prefix off the string: a real vendor SKU like 'SM100-BLK' would
        # be mangled by that, whereas this can only ADD matches. Our SM scheme
        # stays as it is (the generated string is fixed in our table so a
        # Spoolman renumbering cannot invalidate written tags).
        for sid, sp in self._spools.items():
            if not self._spool_in_world(sp):
                continue
            if self._sku_canon(sp.get('spoolman_id')) == want:
                return (sid, sp)
        return (None, None)

    def _spool_slot_of(self, sid, exclude=None):
        """(ace, slot) this spool is bound to, or None. `exclude` skips one
        binding key (the slot we are about to bind it to)."""
        sid = str(sid)
        for k, v in self._spool_binding.items():
            if v != sid or k == exclude:
                continue
            try:
                a, s = (int(x) for x in k.split('_'))
                return (a, s)
            except Exception:
                continue
        return None

    def _sku_base(self, sku):
        """Canonical SKU without our own '_2', '_3', ... duplicate suffix -
        i.e. what the physical TAG reads. Entries sharing a base are the
        same article, and no tag can tell them apart.

        Hand-rolled instead of a regex: ace.py imports no `re` at all, and
        adding one for a suffix strip is not worth it - a NameError here
        would fire on every tag read."""
        s = self._sku_canon(sku)
        i = s.rfind('_')
        if i > 0 and s[i + 1:].isdigit():
            return s[:i]
        return s

    def _spools_with_base(self, sku):
        """All table ids whose SKU shares this base code. World-filtered
        like _spool_by_sku (same caller, the tag auto-bind): an invisible
        local twin must not trip the ambiguity refusal in Spoolman mode."""
        base = self._sku_base(sku)
        if not base:
            return []
        return [sid for sid, sp in self._spools.items()
                if self._spool_in_world(sp)
                and base in self._sku_bases(sp.get('sku'))]

    def _spool_unique_sku(self, sku):
        """A SKU must identify ONE table entry, so a collision gets a '_2',
        '_3', ... suffix. Underscore, not '#': '#' terminates gcode
        arguments, so 'X#2' would arrive as 'X' and silently produce the
        DUPLICATE the suffix exists to prevent. Factory codes use
        dashes, so an underscore also reads as ours at a glance.

        The suffixed entry can never be matched by a tag read - the physical
        tag still says 'X' - so it is a hand-managed entry by construction.
        That is the accepted limit of using factory spools: auto-binding
        works for one spool per article, a second
        one in SIMULTANEOUS use needs its own written tag."""
        base = (sku or '').strip()
        if not base:
            return base, False
        taken = set()
        for sp in self._spools.values():
            taken.update(self._sku_codes(sp.get('sku')))
        if not (set(self._sku_codes(base)) & taken):
            return base, False
        n = 2
        while self._sku_canon('%s_%d' % (base, n)) in taken:
            n += 1
        return '%s_%d' % (base, n), True

    def _spool_bind(self, key, sid):
        """Bind a spool to a slot. ONE spool sits in ONE slot, so binding it
        somewhere else MOVES it - a spool bound twice books its consumption
        twice and its remaining weight drops at double speed. ACE_SPOOL_ASSIGN
        had this rule inline; every other writer must follow it too, so it
        lives in one helper."""
        sid = str(sid)
        moved_from = [k for k, v in self._spool_binding.items()
                      if v == sid and k != key]
        for k in moved_from:
            self._spool_binding.pop(k, None)
        self._spool_binding[key] = sid
        # A binding landed here: the next unmatched read logs again.
        try:
            _a, _s = str(key).split('_', 1)
            self._spool_miss_forget(int(_a), int(_s))
        except Exception:
            pass
        # A move empties the old slot, and an unexplained empty slot reads
        # like a bug - so say where it went. Transition only, never state.
        for k in moved_from:
            try:
                _a, _sl = k.split('_')
                logging.info('[multiACE] [spool] #%s moved from MMU %d / '
                             'Slot %d to MMU %d / Slot %d'
                             % (sid, self._raw(int(_a)), self._raw(int(_sl)),
                                self._raw(int(key.split('_')[0])),
                                self._raw(int(key.split('_')[1]))))
            except Exception:
                pass
        return moved_from

    def _spool_drop_if_unbound_sm(self, sid, why, force=False):
        """A Spoolman-backed spool whose LAST binding just went away leaves
        the table (the local row is a cache of what sits
        in the printer, the spool itself lives in Spoolman and comes back
        via search+adopt). LOCAL spools always stay - the table is their
        only home, the remaining weight must survive re-insertion. A move
        (same spool re-bound elsewhere) never lands here unbound, so it
        never deletes.

        Unsynced consumption DEFERS the drop instead of dying with it:
        the loudest case is a spool running empty MID-print - gate-empty
        releases the binding long before the print-end sync runs, so an
        immediate drop would lose the whole print's consumption with
        Spoolman perfectly reachable. The row stays, unbound; the next
        successful push writes SYNCED_MM back and THAT handler removes it
        (cmd_ACE_SPOOL_SET). `force` is the world SWITCH: with the URL
        gone the debt is unpushable and a kept row would strand invisibly
        in the local world - there the loss is logged and accepted."""
        sid = str(sid or '')
        sp = self._spools.get(sid)
        if not sp or not str(sp.get('spoolman_id') or '').strip():
            return False
        if any(str(v) == sid for v in self._spool_binding.values()):
            return False
        try:
            debt = (float(sp.get('used_mm') or 0.)
                    - float(sp.get('spoolman_synced_mm') or 0.))
        except (TypeError, ValueError):
            debt = 0.
        if debt > 0.5 and not force:
            logging.info('[multiACE] [spool] SM%s #%s kept unbound (%s, '
                         '%.1fmm unsynced) - drops after the next '
                         'successful push'
                         % (sp.get('spoolman_id'), sid, why, debt))
            return False
        self._spools.pop(sid, None)
        logging.info('[multiACE] [spool] SM%s dropped from the table (%s'
                     '%s)' % (sp.get('spoolman_id'), why,
                              (', UNSYNCED %.1fmm lost' % debt)
                              if debt > 0.5 else ''))
        return True

    def _spool_miss_forget(self, ace_idx, slot):
        """Let the next unmatched read of this slot log again (slot emptied
        or a binding landed)."""
        try:
            (getattr(self, '_spool_miss_logged', None) or {}).pop(
                (int(ace_idx), int(slot)), None)
        except Exception:
            pass

    def _spool_release_slot(self, ace_idx, slot, why):
        """Drop a slot's binding (see the gate-transition call site). A
        LOCAL spool's entry is never touched - its remaining weight is
        exactly what must survive so the spool can come back later. A
        Spoolman-backed entry leaves with its last binding (see
        _spool_drop_if_unbound_sm)."""
        key = self._spool_key(ace_idx, slot)
        sid = self._spool_binding.pop(key, None)
        if sid is None:
            return
        try:
            sp = dict(self._spools.get(str(sid)) or {})
            self._spool_drop_if_unbound_sm(sid, 'slot released: %s' % why)
            self._save_spool_db()
            self.log_always(
                '[multiACE] MMU %d / Slot %d: spool #%s (%s) unbound - %s'
                % (self._disp(ace_idx), self._disp(slot), sid,
                   self._spool_label(sp) if sp else '?', why))
        except Exception as e:
            logging.info('[multiACE] [spool] release failed (ignored): %s' % e)

    def _slot_read_sku(self, ace_idx, slot):
        """Canon SKU of the last tag READ physically at (ace, slot) this
        boot - V2 from the cmd13/identify ingest cache, V1 from the
        status-transition tracker. None when no read exists; absence
        proves nothing (the stale-release rule below treats it as
        'cannot tell' and keeps refusing)."""
        try:
            info = ((getattr(self, '_v2_filament_info_per_ace', None) or {})
                    .get(int(ace_idx)) or {}).get(int(slot))
            canon = self._sku_canon((info or {}).get('sku'))
            if canon:
                return canon
            return ((getattr(self, '_v1_tag_seen', None) or {})
                    .get(int(ace_idx)) or {}).get(int(slot)) or None
        except Exception:
            return None

    def _slot_read_sku_raw(self, ace_idx, slot):
        """The last tag code READ at (ace, slot), VERBATIM (what the user
        should see in the table), '' when none. V2 from the cmd13/identify
        cache; V1 keeps only the canonical form."""
        try:
            info = ((getattr(self, '_v2_filament_info_per_ace', None) or {})
                    .get(int(ace_idx)) or {}).get(int(slot))
            raw = str((info or {}).get('sku') or '').strip()
            if raw:
                return raw
            return str(((getattr(self, '_v1_tag_seen', None) or {})
                        .get(int(ace_idx)) or {}).get(int(slot)) or '')
        except Exception:
            return ''

    def _spool_retry_bind_at(self, ace_idx, slot):
        """Re-offer a slot its own last READ tag after a stale binding AT
        that slot was released: the slot's own bind attempt may have been
        REFUSED moments earlier (its spool was still bound to the slot
        whose bind ran first), and nothing re-reads until the next boot -
        without this a pairwise swap heals only half. Bounded recursion:
        each release consumes one stale link, and a slot whose read has
        no cache entry simply stays refused."""
        uid = self._slot_read_uid(int(ace_idx), int(slot))
        sku = self._slot_read_sku(ace_idx, slot)
        if uid or sku:
            self._spool_bind_tag_codes(int(ace_idx), int(slot), uid, sku)

    def _spool_release_head(self, head, why):
        """Drop a HEAD binding (h<n>, feeder/manual) - the twin of
        _spool_release_slot. Same rules: a local spool's row survives with
        its remaining weight, a Spoolman-backed row leaves with its last
        binding (open debt held by _spool_drop_if_unbound_sm until
        pushed)."""
        key = self._spool_head_key(head)
        sid = self._spool_binding.pop(key, None)
        if sid is None:
            return
        try:
            sp = dict(self._spools.get(str(sid)) or {})
            self._spool_drop_if_unbound_sm(sid, 'head released: %s' % why)
            self._save_spool_db()
            _msg = ('[multiACE] Head %d: spool #%s (%s) unbound - %s'
                    % (self._disp(head), sid,
                       self._spool_label(sp) if sp else '?', why))
            self.log_always(_msg)
            # log_always is respond-pipe only - mirror to klippy.log
            # so a later analysis can tell "did not run" from "ran unseen".
            logging.info(_msg)
        except Exception as e:
            logging.info('[multiACE] [spool] head release failed '
                         '(ignored): %s' % e)

    def _spool_head_reader_capture(self, head, info, is_clear):
        """Feeder-reader auto-bind: the
        stock fm175xx read arrives in our filament_detect hook carrying the
        card's UID and (Snapmaker M1 layout only) a numeric SKU - the head
        twin of _spool_bind_by_tag. The card UID hex is the universal
        anchor: every card has one, factory or self-written, and stock
        fills CARD_UID even when the DATA layout does not parse (Anycubic-
        layout tags read err but still deliver their UID). The layout SKU
        int is offered second. Match order per candidate: local table via
        _spool_by_sku (a local row whose sku IS the UID/code matches in
        every world); no hit -> the load-bearing 'matches no table entry'
        line in HEAD form, which the web kick-sweep regex also matches, so
        Spoolman adoption via card_uids runs the moment the tag is read.
        Reader-wins stays above everything: a SpoolLink spool_id stamp on
        the head means SL owns it - no bind (and the SM-mode heartbeat
        would release it again anyway). Binding rules, lean vs the slot
        twin: hand-assigned same-base variant sticks; ambiguous base
        (owned twice) warns once and refuses; a spool held by an OCCUPIED
        slot refuses (physically still in the ACE - two places, one
        spool); held by an EMPTY slot or another head it MOVES (the fresh
        read here is the strongest evidence of where it physically is;
        heads have no connect-rescan, so a stale head binding would
        otherwise stick forever)."""
        try:
            if is_clear:
                return
            head = int(head)
            if head < 0 or head > 3 or self.head_uses_ace(head):
                return
            uid_hex = ''
            _uid = info.get('CARD_UID')
            if isinstance(_uid, (list, tuple)) and len(_uid) >= 4:
                try:
                    uid_hex = ''.join('%02X' % (int(b) & 0xFF) for b in _uid)
                except Exception:
                    uid_hex = ''
            _sku_i = 0
            try:
                _sku_i = int(info.get('SKU') or 0)
            except Exception:
                _sku_i = 0
            cands = [c for c in (uid_hex,
                                 str(_sku_i) if _sku_i > 0 else '') if c]
            if not cands:
                return
            if self._head_tag_seen.get(head) == cands[0]:
                return    # same card re-read - transition-only
            self._head_tag_seen[head] = cands[0]
            logging.info(
                '[multiACE] [spool] feeder read on head %d: card_uid=%s '
                'sku_int=%s' % (self._raw(head), uid_hex or '-',
                                _sku_i or '-'))
            # Only the SPOOLLINK world defers to a SpoolLink resolution -
            # in multiace (local/spoolman) the heartbeat takes the stamp
            # back anyway, so skipping the bind here would just lose the
            # binding until the next insert.
            if self._spoollink_active() and self._ptc_spool_id_for(head) > 0:
                logging.info(
                    '[multiACE] [spool] head %d: SpoolLink already resolved '
                    'this card (spool_id=%d) - SL world, no table bind'
                    % (self._raw(head), self._ptc_spool_id_for(head)))
                return
            sid = spool = None
            code = cands[0]
            for c in cands:
                sid, spool = self._spool_by_sku(c)
                if spool is not None:
                    code = c
                    break
            if spool is None:
                # Wording is load-bearing (web kick regex 'tag .+ matches
                # no table entry') - same contract as the slot line.
                _line = ('[multiACE] [spool] tag %r on head %d '
                         'matches no table entry' % (cands[0],
                                                     self._disp(head)))
                logging.info(_line)
                if (getattr(self, 'spoolman_url', '') or '').strip():
                    self.log_always(_line)
                return
            key = self._spool_head_key(head)
            if self._spool_binding.get(key) == sid:
                return
            _cur = self._spools.get(self._spool_binding.get(key) or '')
            if _cur is not None and self._sku_base(
                    code) in self._sku_bases(_cur.get('sku')):
                return    # hand-assigned variant of the same code sticks
            if len(self._spools_with_base(code)) > 1:
                if self._spool_conflict_said.get(key) != sid:
                    self._spool_conflict_said[key] = sid
                    self.log_warn(
                        '[multiACE] Head %d: tag %r matches several spools '
                        '- assign by hand' % (self._disp(head), code))
                return
            for k, v in self._spool_binding.items():
                if v != sid or k == key:
                    continue
                try:
                    a, s = (int(x) for x in k.split('_'))
                except Exception:
                    continue    # another head holds it - the move below wins
                if self._slot_is_occupied(a, s):
                    if self._spool_conflict_said.get(key) != sid:
                        self._spool_conflict_said[key] = sid
                        self.log_warn(
                            '[multiACE] Head %d: spool #%s is bound to the '
                            'occupied MMU %d / Slot %d - not moving it'
                            % (self._disp(head), sid, self._disp(a),
                               self._disp(s)))
                    return
            if self._spool_binding.get(key) is not None:
                self._spool_release_head(
                    head, 'tag %r read at the feeder - re-binding' % code)
            self._spool_bind(key, sid)
            self._spool_conflict_said.pop(key, None)
            self._save_spool_db()
            _msg = ('[multiACE] Head %d: spool #%s (%s) bound by feeder '
                    'tag %r' % (self._disp(head), sid,
                                self._spool_label(spool), code))
            self.log_always(_msg)
            logging.info(_msg)
        except Exception as e:
            logging.info('[multiACE] [spool] head tag bind failed '
                         '(ignored): %s' % e)

    def _spool_bind_tag_codes(self, ace_idx, slot, uid, sku, unbind=True):
        """Bind a slot by everything ONE tag read delivered: the card UID
        first (the per-chip key), the sku as the fallback for reads
        without a UID or a UID the table does not know yet. Returns the
        bound spool or None.

        UID known to the table -> it decides, the sku is not consulted.
        Otherwise the sku decides (with `unbind`), and the UID only runs
        with unbind=False: its no-match line still triggers the web's
        card_uids adopt, but it must not release the binding the sku just
        made. Only a UID -> it decides alone (with `unbind`)."""
        uid = str(uid or '').strip()
        sku = str(sku or '').strip()
        u = self._sku_canon(uid)
        if u and self._spool_by_sku(uid)[1] is not None:
            return self._spool_bind_by_tag(ace_idx, slot, uid, unbind=unbind)
        if self._sku_canon(sku):
            spool = self._spool_bind_by_tag(ace_idx, slot, sku, unbind=unbind)
            if u and u != self._sku_canon(sku):
                self._spool_bind_by_tag(ace_idx, slot, uid, unbind=False)
            return spool
        if u:
            return self._spool_bind_by_tag(ace_idx, slot, uid, unbind=unbind)
        return None

    def _spool_bind_by_tag(self, ace_idx, slot, sku, unbind=True):
        """A freshly READ tag identifies the physical spool -> bind it to the
        slot it was read in, so consumption books against the right entry
        without a click. Returns the bound spool (or None).

        `unbind=False`: a no-match must NOT release an existing binding.
        For a tag carrying TWO codes (Anycubic sku + card UID) the sku
        bind already decided the slot; the UID's no-match line is still
        logged (the web adopts from it) but proves nothing about the
        predecessor (it would otherwise unbind the sku binding a
        millisecond later).

        Deliberately does NOT create an entry for an unknown tag: a SKU is
        not proven unique per spool - it may be a PRODUCT code shared by
        every spool of that article, and we cannot tell one from a serial.
        Auto-creating would then let several physical spools bind to one
        entry and mis-book consumption. Naming a spool stays a user act."""
        try:
            sid, spool = self._spool_by_sku(sku)
            if spool is None:
                if self._sku_canon(sku):
                    # On the RESPONSE pipe too, not just klippy.log: the web
                    # backend's gcode_response listener uses this exact line
                    # as its adopt trigger, so an unmatched tag is fetched
                    # from Spoolman the moment it is READ (insert or boot
                    # rescan) instead of on the next 60 s sweep tick. Fires
                    # per read EVENT, never per status tick, so no pipe
                    # pressure. Spoolman mode only -
                    # in the local world nobody reacts to it, the console
                    # line would be pure noise there; klippy.log always
                    # gets it. Wording is load-bearing - keep in sync with
                    # _SPOOL_UNMATCHED_RE in web/backend/main.py.
                    # ONCE per (slot, code): the V2 heartbeat re-ingests the
                    # same read every second, and a card the table does
                    # not know (a Bambu UID, say) would otherwise log at
                    # 1 Hz for as long as it sits there. The entry is
                    # dropped when the slot empties or a bind lands, so
                    # the next spool or a changed code logs again.
                    _mk = (int(ace_idx), int(slot))
                    _ml = getattr(self, '_spool_miss_logged', None)
                    if _ml is None:
                        _ml = self._spool_miss_logged = {}
                    _quiet = _ml.get(_mk) == self._sku_canon(sku)
                    _ml[_mk] = self._sku_canon(sku)
                    _line = ('[multiACE] [spool] tag %r on MMU %d slot %d '
                             'matches no table entry'
                             % (sku, self._disp(ace_idx), self._disp(slot)))
                    if not unbind:
                        # The tag's OTHER code (its sku) already decided
                        # the binding a moment ago; say so, or the line
                        # reads as "nothing bound" and looks like a failure.
                        # Suffix only - the web
                        # backend keys on the wording before it.
                        _kb = self._spool_binding.get(
                            self._spool_key(ace_idx, slot))
                        _ks = self._spools.get(_kb) if _kb is not None else None
                        if _ks is not None:
                            _line += (' - slot stays bound to #%s %s via its '
                                      'sku %s' % (_kb, self._spool_label(_ks),
                                                  (_ks.get('sku') or '-')))
                    # The spool bound here carries no code at all (added or
                    # assigned before any read): the code read at its slot is
                    # its own - learn it instead of reporting a no-match.
                    key = self._spool_key(ace_idx, slot)
                    _lsid = self._spool_binding.get(key)
                    _lsp = (self._spools.get(_lsid) if _lsid is not None
                            else None)
                    if _lsp is not None and not self._sku_codes(
                            _lsp.get('sku')):
                        _lsp['sku'] = (sku or '').strip()
                        self._save_spool_db(backup=True)
                        logging.info('[multiACE] [spool] learned code %r for '
                                     'spool #%s (bound to MMU %d slot %d, '
                                     'had no code)', _lsp['sku'], _lsid,
                                     self._raw(ace_idx), self._raw(slot))
                        self.log_always('[multiACE] spool #%s: learned tag '
                                        'code %s' % (_lsid, _lsp['sku']))
                        return _lsp
                    if not _quiet:
                        logging.info(_line)
                        if (getattr(self, 'spoolman_url', '') or '').strip():
                            self.log_always(_line)
                    # A read that matches NOTHING still proves WHICH spool
                    # physically sits here - and that it is not the bound
                    # one, when the bases differ: release it. Without this
                    # the predecessor's binding survives a spool swap to an
                    # un-adopted roll and keeps BOOKING against the wrong
                    # entry. Guard:
                    # same BASE code keeps the binding - that is the
                    # hand-assigned '_2' variant whose physical tag reads
                    # the base (its entry carries the suffix, so the base
                    # lookup finds nothing), i.e. the SAME spool. An empty
                    # canon never reaches this branch (absence proves
                    # nothing). A dangling binding (spool deleted) is
                    # released too.
                    key = self._spool_key(ace_idx, slot)
                    _bound_sid = self._spool_binding.get(key) if unbind else None
                    if _bound_sid is not None:
                        _cur = self._spools.get(_bound_sid)
                        if (_cur is None or self._sku_base(sku)
                                not in self._sku_bases(_cur.get('sku'))):
                            self._spool_binding.pop(key, None)
                            self._spool_conflict_said.pop(key, None)
                            self._save_spool_db()
                            self.log_always(self._t(
                                'msg.spool_tag_unbound',
                                sku=(sku or '').strip(), id=_bound_sid,
                                spool=self._spool_label(_cur or {}),
                                ace=self._disp(ace_idx),
                                slot=self._disp(slot)))
                            # Mirrored: log_always only reaches the console,
                            # so a later log analysis could not tell a
                            # release that HAPPENED from one that never ran.
                            logging.info(
                                '[multiACE] [spool] unbound #%s from MMU %d '
                                'slot %d (tag %r matches nothing)',
                                _bound_sid, self._raw(ace_idx),
                                self._raw(slot), (sku or '').strip())
                return None
            key = self._spool_key(ace_idx, slot)
            if self._spool_binding.get(key) != sid:
                # A HAND-ASSIGNED variant of the same code wins. The second
                # spool's entry carries the suffixed SKU while its physical
                # tag still reads the base code, so without this the next
                # tag read would bind the BASE entry straight over the
                # assignment the user just made - every re-insert undoing
                # their choice.
                _cur = self._spools.get(self._spool_binding.get(key) or '')
                if _cur is not None and self._sku_base(
                        sku) in self._sku_bases(_cur.get('sku')):
                    return _cur
                # Once a code exists MORE THAN ONCE in the table, the user
                # has declared "I own two of these" - and a tag cannot tell
                # them apart. Auto-binding for that code stops: booking
                # nothing is honest, booking it onto the twin (whose slot is
                # empty, so the guard below never fires) silently drains the
                # wrong spool's weight. Assign by hand; that assignment then
                # sticks via the branch above.
                if len(self._spools_with_base(sku)) > 1:
                    if self._spool_conflict_said.get(key) != sid:
                        self._spool_conflict_said[key] = sid
                        self.log_warn(self._t('msg.spool_tag_ambiguous',
                            sku=(sku or '').strip(),
                            ace=self._disp(ace_idx), slot=self._disp(slot)))
                    return None
                # Same rule as ACE_SPOOL_ASSIGN, for the automatic path: a
                # spool sitting in an OCCUPIED slot is not moved here. Two
                # physical spools reading the same tag (factory SKUs are a
                # product code as far as we can tell) would otherwise pass
                # the binding back and forth, and the loser books nothing.
                # Refuse and say so - the user gives the second one its own
                # entry via '+' (which suffixes the SKU, see
                # _spool_unique_sku).
                # EXCEPTION - stale release: when the tag
                # freshly READ at the held slot is a DIFFERENT spool, the
                # old binding is provably stale (rolls swapped while
                # nothing watched) - move instead of refusing. The refusal
                # stays for a held slot whose read MATCHES this base code
                # (two physical spools, one code - no tag can tell them
                # apart) and for a held slot with NO read (cannot tell).
                # _spool_bind's moved_from pop does the actual release;
                # afterwards the held slot is re-offered its own read,
                # because ITS bind may have been refused moments earlier,
                # before this release existed - a pairwise swap otherwise
                # heals only half.
                _held = self._spool_slot_of(sid, exclude=key)
                _stale_held = None
                if _held is not None and self._slot_is_occupied(*_held):
                    # Evidence = every code the held slot's last read
                    # delivered: its sku and its card UID. Stale when the
                    # read shows a different code AND its UID is not one of
                    # this spool's own codes (that chip sitting there means
                    # the spool IS there).
                    _held_read = self._slot_read_sku(*_held)
                    _held_uid = self._slot_read_uid(*_held)
                    _held_codes = [c for c in (_held_read, _held_uid) if c]
                    if (_held_codes
                            and all(self._sku_base(c) != self._sku_base(sku)
                                    for c in _held_codes)
                            and not (_held_uid and self._sku_canon(_held_uid)
                                     in self._sku_codes(spool.get('sku')))):
                        _stale_held = _held
                        logging.info(
                            '[multiACE] [spool] stale binding: %s (id %s) '
                            'bound at MMU %d slot %d, but that slot reads '
                            'sku %r - releasing for re-bind',
                            self._spool_label(spool), sid,
                            self._raw(_held[0]), self._raw(_held[1]),
                            _held_read)
                    else:
                        if self._spool_conflict_said.get(key) != sid:
                            self._spool_conflict_said[key] = sid
                            self.log_warn(self._t('msg.spool_tag_duplicate',
                                id=sid, spool=self._spool_label(spool),
                                ace=self._disp(ace_idx), slot=self._disp(slot),
                                oace=self._disp(_held[0]), oslot=self._disp(_held[1])))
                        return None
                self._spool_bind(key, sid)
                self._spool_conflict_said.pop(key, None)
                self._save_spool_db()
                self.log_always(self._t('msg.spool_tag_bound',
                    spool=self._spool_label(spool), id=sid,
                    ace=self._disp(ace_idx), slot=self._disp(slot)))
                logging.info('[multiACE] [spool] bound #%s to MMU %d slot %d '
                             'by tag %r', sid, self._raw(ace_idx),
                             self._raw(slot), (sku or '').strip())
                if _stale_held is not None:
                    self._spool_retry_bind_at(*_stale_held)
            return spool
        except Exception as e:
            logging.info('[multiACE] [spool] tag bind failed (ignored): %s' % e)
            return None

    def _v1_tag_bind_from_status(self, idx, result):
        """Tag auto-bind for a V1 (ACE Pro) - the counterpart of the V2
        path, which hangs off get_filament_info (cmd 13) and therefore
        NEVER ran on a Pro: _merge_v2_filament_info returns immediately
        for a non-v2 protocol, and that function holds the only other
        _spool_bind_by_tag call, so a Pro could never auto-bind through it.

        The Pro has no per-slot tag command; its SKU rides in the normal
        status, so the trigger is a TRANSITION of that field: bind once
        when a slot's code appears or changes, not on every heartbeat
        (act on changes, not states). A fresh Klipper start has
        no previous value, so the first status after boot binds every
        occupied slot. V1 only, deliberately: the V2 path
        is HW-proven and stays untouched.

        Everything downstream is the SHARED _spool_bind_by_tag - world
        filter, the hand-assignment-wins rule, the duplicate-code stop and
        the occupied-slot refusal apply exactly as on a V2."""
        try:
            if not self._cap(idx, 'tag_identity_in_status'):
                return
            seen = self._v1_tag_seen.setdefault(idx, {})
            # TWO passes on purpose. _spool_bind_by_tag proves a binding
            # STALE by reading the holding slot's tag (_slot_read_sku), and
            # that evidence comes from `seen` - so deciding slot by slot
            # inside ONE loop lets each decision see the neighbours with
            # their PREVIOUS codes (after a boot: none at all). A pairwise
            # swap then reads as "a second spool with the same code" and the
            # binding refuses to move. Pass 1 records every fresh code,
            # pass 2
            # decides - then every decision sees the unit as it really is.
            to_bind = []
            for i, slot in enumerate(result.get('slots') or []):
                if not isinstance(slot, dict):
                    continue
                # Genuine tag only (rfid 2), same precondition the V2 path
                # uses before it asks for the tag at all.
                sku = slot.get('sku') if slot.get('rfid') == 2 else ''
                canon = self._sku_canon(sku)
                fresh = seen.get(i) != canon
                seen[i] = canon
                if not fresh or not canon:
                    continue                     # no transition / no code
                if self._is_empty_status(slot.get('status', '')):
                    continue                     # nothing physically there
                to_bind.append((i, sku))
            self._resolve_tag_binds(
                idx, [(i, sku, None) for i, sku in to_bind])
        except Exception as e:
            logging.info('[multiACE] [spool] V1 tag bind failed (ignored): '
                         '%s' % e)

    def _spool_enrich_tag_info(self, info, spool, ace_idx=None, slot=None):
        """Fill what the TAG cannot carry from the bound spool: sub-type and
        vendor. Enrichment, not override - a field the tag delivered always
        wins, so the precedence chain (override > RFID > '?') is untouched.

        This is the whole point of the tag-as-pointer approach: the writer
        apps can set material and colour but no sub-type, and the sub-type
        drives the tipform table (incl. unloadtemp), the DB temperatures and
        the material-strict preflight matching."""
        if not spool:
            return info
        try:
            if not (info.get('subtype') or ''):
                st = (spool.get('subtype') or '').strip()
                if st:
                    info['subtype'] = st
            if not (info.get('brand') or ''):
                vn = (spool.get('vendor') or '').strip()
                if vn:
                    info['brand'] = vn
            tag_mat = (info.get('type') or '').strip()
            sp_mat = (spool.get('material') or '').strip()
            # Compare BASE materials, not raw strings: info['type'] is the
            # raw tag string here - the heartbeat split into base + subtype
            # runs LATER (single fix point). A 'PLA Matte' tag against a
            # correctly maintained table entry 'PLA' would warn on every
            # restart and tell the user to correct a table that is right; a
            # 'Snapmaker PLA Tr' factory tag likewise. Split both sides - the
            # table too, in case
            # have done the same. Split both sides - the table too, in case
            # someone typed the merged form as the material.
            tag_base = self._split_type_subtype(tag_mat)[0] or tag_mat
            sp_base = self._split_type_subtype(sp_mat)[0] or sp_mat
            # COLOUR, same treatment as material - and load-bearing since
            # factory spools share a SKU: it is what catches "bound to the
            # wrong entry" when the two spools differ in colour (the case
            # the duplicate guard cannot see, because the other spool is in
            # storage, not in a slot). Same colour = nothing catches it,
            # which is the accepted limit of the tag-as-pointer approach.
            # Both sides must DECLARE a colour - an all-zero tag colour
            # means unknown, never black - and the compare is a
            # distance, not equality, so #FFFFFF vs #FEFEFE stays quiet.
            try:
                tag_rgb = [int(c) for c in (info.get('color') or [])][:3]
            except (TypeError, ValueError):
                tag_rgb = []
            sp_hex = (spool.get('color') or '').strip().lstrip('#')
            if (len(tag_rgb) == 3 and any(tag_rgb) and len(sp_hex) == 6):
                try:
                    sp_rgb = [int(sp_hex[i:i + 2], 16) for i in (0, 2, 4)]
                    dist = sum((a - b) ** 2
                               for a, b in zip(tag_rgb, sp_rgb)) ** 0.5
                    if any(sp_rgb) and dist > SPOOL_COLOR_WARN_DIST:
                        self.log_warn(self._t('msg.spool_tag_color_mismatch',
                            id=spool.get('id', '?'),
                            table='#%s' % sp_hex.upper(),
                            tag='#%02X%02X%02X' % tuple(tag_rgb),
                            ace=self._disp(ace_idx) if ace_idx is not None else '?',
                            slot=self._disp(slot) if slot is not None else '?'))
                except (TypeError, ValueError):
                    pass
            if tag_base and sp_base and tag_base.lower() != sp_base.lower():
                # The bind came from the tag ID, so this is one physical
                # spool with two contradicting materials = a typo in the
                # table. Follow the TAG (device truth) but say so - silently
                # diverging identities are how wrong-material prints happen.
                self.log_warn(self._t('msg.spool_tag_material_mismatch',
                    id=spool.get('id', '?'), table=sp_mat, tag=tag_mat,
                    ace=self._disp(ace_idx) if ace_idx is not None else '?',
                    slot=self._disp(slot) if slot is not None else '?'))
        except Exception as e:
            logging.info('[multiACE] [spool] enrich failed (ignored): %s' % e)
        return info

    def _fill_identity_from_bound_spool(self, idx, result):
        """A card that carries no identity of its own (a MIFARE chip, a
        blank NTAG, an unknown Gen-1 tag) only points at its spool. When
        the spool table lists the card UID and that spool is bound to the
        slot, the slot shows the spool's material, colour, vendor and
        sub-type and counts as identified (rfid 2) for the display, the
        load capture, Quad and the preflight.

        Derived from the live binding on every status, for ACE 2 and ACE
        Pro alike, and never written into a read cache: an unbind or a
        spool edit shows on the next tick, and nothing comes back as tag
        evidence after a restart. Conditions: slot occupied and not mid
        insert, no material from the tag, the slot's card UID is one of
        the bound spool's codes (a binding held over from a predecessor
        roll does not qualify), and the spool declares material and
        colour (a zero colour next to a material would read as black).
        A UID kept across a restart counts like every other kept tag
        read, so a roll swapped while nothing watched shows as its
        predecessor until it is read again.
        sku stays empty: the table code is not a tag code. ident_src
        tells the web the identity is table data, not tag data."""
        seen = self._spool_ident_fill
        for n, s in enumerate(result.get('slots', []) or []):
            if not isinstance(s, dict):
                continue
            i = s.get('index', n)
            sid = None
            try:
                status = s.get('status', '')
                # No skip for the ACE Pro 'shifting'/'preload': it reports
                # our own lane moves that way too, and an insert passes an
                # empty status first, which clears the UID anyway.
                if (not self._is_empty_status(status)
                        and s.get('rfid') != 3
                        and status != 'identifying'
                        and not (s.get('type') or '').strip()):
                    uid = s.get('uid') or self._slot_read_uid(idx, i)
                    sp = self.spool_for_slot(idx, i) if uid else None
                    hx = ((sp or {}).get('color') or '').strip().lstrip('#')
                    mat = ((sp or {}).get('material') or '').strip()
                    rgb = None
                    if len(hx) == 6 and all(c in '0123456789abcdefABCDEF'
                                            for c in hx):
                        rgb = [int(hx[k:k + 2], 16) for k in (0, 2, 4)]
                    if (sp and self._sku_canon(uid)
                            in self._sku_codes(sp.get('sku'))
                            and mat and mat.upper() != 'NONE'
                            and rgb is not None):
                        tt = ((self._gen1_tunnel_reads.get(idx) or {})
                              .get(i) or {})
                        s['tag_format'] = (
                            s.get('tag_format')
                            or (getattr(self, '_rc_last_fmt', None)
                                or {}).get((idx, i))
                            or tt.get('format') or 'unknown')
                        s['type'] = mat
                        s['color'] = rgb
                        s['brand'] = (sp.get('vendor') or '').strip()
                        s['subtype'] = (sp.get('subtype') or '').strip()
                        s['uid'] = uid
                        s['rfid'] = 2
                        s['ident_src'] = 'spool'
                        sid = str(sp.get('id', ''))
            except Exception as e:
                logging.info('[multiACE] [spool] identity fill MMU %d slot '
                             '%d failed (ignored): %s'
                             % (self._raw(idx), self._raw(i), e))
            if seen.get((idx, i)) != sid:
                if sid:
                    logging.info('[multiACE] [spool] MMU %d slot %d: card '
                                 'carries no identity - showing spool #%s'
                                 % (self._raw(idx), self._raw(i), sid))
                elif seen.get((idx, i)):
                    logging.info('[multiACE] [spool] MMU %d slot %d: spool '
                                 'identity no longer shown'
                                 % (self._raw(idx), self._raw(i)))
                if sid:
                    seen[(idx, i)] = sid
                else:
                    seen.pop((idx, i), None)

    def spool_for_slot(self, ace_idx, slot):
        """The spool dict bound to a slot, or None."""
        sid = self._spool_binding.get(self._spool_key(ace_idx, slot))
        return self._spools.get(sid) if sid else None

    def _spool_density(self, spool):
        try:
            d = float(spool.get('density') or 0)
            if d > 0:
                return d
        except (TypeError, ValueError):
            pass
        mat = (spool.get('material') or '').strip().lower()
        return SPOOL_DENSITY_BY_MATERIAL.get(mat, SPOOL_DENSITY_DEFAULT)

    def _spool_mm_to_g(self, spool, mm):
        # mm * mm2 = mm3; g/cm3 / 1000 = g/mm3
        return mm * SPOOL_FILAMENT_AREA_MM2 * self._spool_density(spool) / 1000.

    def _spool_for_head_pa(self, head):
        """(sid, spool) feeding `head`, resolved exactly like
        book_spool_use: ACE head via head_source -> slot binding, feeder/
        manual via the h<n> head binding. (None, None) when unbound."""
        try:
            src = self._head_source.get(head)
            if src:
                sid = self._spool_binding.get(
                    self._spool_key(src['ace_index'], src['slot']))
            elif not self.head_uses_ace(head):
                sid = self._spool_binding.get(self._spool_head_key(head))
            else:
                return None, None
            sp = self._spools.get(sid) if sid else None
            return (sid, sp) if sp is not None else (None, None)
        except Exception:
            return None, None

    def _spool_pa_store(self, sid, spool, nozzle_key, value):
        """Merge one measured value into the spool's pa_matrix + persist.
        Merge, never replace: other nozzles' entries survive (mod
        contract)."""
        try:
            m = spool.get('pa_matrix')
            if not isinstance(m, dict):
                m = {}
            m[str(nozzle_key)] = round(float(value), 6)
            spool['pa_matrix'] = m
            self._save_spool_db()
            logging.info('[multiACE] [pa] spool #%s: stored %s = %.6f'
                         % (sid, nozzle_key, float(value)))
            self.log_always('[multiACE] PA %.6f saved for spool %s (%s)'
                            % (float(value),
                               spool.get('label') or sid, nozzle_key))
        except Exception as e:
            logging.info('[multiACE] [pa] store failed: %s' % e)

    def _apply_spool_pa(self, head, why=''):
        """Apply the bound spool's stored PA for the head's CURRENT nozzle.
        Silent no-op when unbound / no entry / value already applied (the
        toolchange hook fires constantly mid-print - the cache keeps this
        to one SET_PRESSURE_ADVANCE per actual change). SpoolLink mode is
        skipped: PA there belongs to the pechex mod's own apply path, and
        our table is not the leading store in that world."""
        try:
            if not self.pa_sync:
                return
            if self._spoollink_active():
                return
            sid, sp = self._spool_for_head_pa(head)
            if sp is None:
                return
            key = self._nozzle_key_for_head(head)
            m = sp.get('pa_matrix')
            if not key or not isinstance(m, dict) or key not in m:
                return
            val = float(m[key])
            cache = getattr(self, '_pa_applied', None)
            if cache is None:
                cache = self._pa_applied = {}
            if cache.get(head) == (sid, key, val):
                return
            self._set_head_pa(head, val)
            cache[head] = (sid, key, val)
            logging.info('[multiACE] [pa] head %d: applied %.6f (%s, '
                         'spool #%s%s)'
                         % (head, val, key, sid,
                            (', ' + why) if why else ''))
        except Exception as e:
            logging.info('[multiACE] [pa] apply failed (head %s): %s'
                         % (head, e))

    def book_spool_use(self, head, mm, why='move'):
        """Book `mm` of filament against the spool feeding `head`. Public:
        the bg engine calls it for its stealth prime, which bypasses the
        extruder axis the sampler watches. No spool bound -> no-op.

        `mm` may be NEGATIVE (the sampler passes net deltas): filament
        pulled back toward the ACE is rewound onto the spool, so a retract
        un-books. That includes the INNER cold-pull - head_source still
        points at the OLD spool during an unload, so the melt-zone
        content it returns is credited to the spool it physically lands
        on. used_mm never goes below 0; the weight moves by the delta
        actually applied, so the two cannot drift apart at the clamp."""
        try:
            if not mm:
                return
            if self._spoollink_active():
                # SpoolLink mode: counting belongs COMPLETELY to SpoolLink:
                # its active-spool sync lets Moonraker
                # book extrusion against the active head's spool. Booking
                # here too would count everything twice in Spoolman. With
                # used_mm frozen, the backend /use push no-ops by itself
                # (delta stays 0), so this one gate switches the whole
                # counting side.
                return
            src = self._head_source.get(head)
            if src:
                sid = self._spool_binding.get(
                    self._spool_key(src['ace_index'], src['slot']))
            elif not self.head_uses_ace(head):
                # Feeder/manual head: no head_source by design, its
                # spool is bound to the HEAD instead (key 'h<n>', set via
                # ACE_SPOOL_ASSIGN HEAD=). The 1Hz sampler books every head
                # unconditionally, so this one branch is all the consumption
                # tracking a feeder spool needs.
                # The multiace world (local/spoolman) counts non-ACE heads
                # itself, always - a SpoolLink spool_id stamp is taken BACK
                # by the heartbeat (see the stamp-clear there), never obeyed
                # The spoollink world is fully gated above.
                sid = self._spool_binding.get(self._spool_head_key(head))
            else:
                return
            spool = self._spools.get(sid) if sid else None
            if spool is None:
                return
            prev_used = float(spool.get('used_mm') or 0.)
            # NEVER round a running accumulator. The 1-second sample books a
            # few mm = a few THOUSANDTHS of a gram; round(...,1) on the
            # stored value threw every single decrement away, so the weight
            # could not move below 16.8mm/s sustained flow. used_mm had
            # the same trap
            # one notch smaller (increments < 0.05mm vanish). Rounding
            # happens at the DISPLAY edge (main.py payload, UI).
            applied = max(0., prev_used + float(mm)) - prev_used
            spool['used_mm'] = prev_used + applied
            w = spool.get('weight_g')
            if w is not None:
                spool['weight_g'] = max(
                    0., float(w) - self._spool_mm_to_g(spool, applied))
            self._spool_dirty = True
            # Audit trail: the per-sample booking is debug-invisible, so a
            # wrong/missing attribution could only be guessed at. One
            # TRANSITION line per (head, spool) pair says where consumption
            # goes; the per-print summary in _on_print_end says how much went
            # there. Log transitions and summaries, never the 1Hz state.
            if (head, sid) not in self._spool_audit_pairs:
                self._spool_audit_pairs.add((head, sid))
                self._spool_print_base.setdefault(sid, prev_used)
                # src is None on the feeder/manual h<n> branch - naming its
                # ACE/slot here threw and silently killed this line for
                # every feeder booking (caught below as 'booking failed').
                logging.info(
                    '[multiACE] [spool] booking: head %d -> spool #%s (%s), '
                    '%s, used so far %.0fmm (%s)'
                    % (head, sid, self._spool_label(spool),
                       ('MMU %d / Slot %d'
                        % (self._raw(src['ace_index']),
                           self._raw(src['slot'])))
                       if src else 'head binding',
                       prev_used, why))
            logging.debug('[multiACE] [spool] %s: %.1fmm on head %d (%s)'
                          % (sid, mm, head, why))
        except Exception as e:
            logging.info('[multiACE] [spool] booking failed (ignored): %s' % e)

    def _spool_sample_tick(self, eventtime):
        """Sample every extruder axis and book the NET delta against the
        spool feeding that head. Net, not positive-only: a retract followed
        by its un-retract is zero net filament off the spool, but the
        positive-only variant booked the un-retract as fresh consumption -
        every travel/toolchange retract cycle double-counted (a third too
        much on a real multicolour file). An implausible jump in EITHER
        direction is a position reset (G92-class), not extrusion
        (SPOOL_SAMPLE_MAX_MM)."""
        try:
            for head in range(4):
                ext = self.printer.lookup_object(
                    'extruder' if head == 0 else 'extruder%d' % head, None)
                if ext is None:
                    continue
                pos = getattr(ext, 'last_position', None)
                if pos is None:
                    continue
                prev = self._spool_epos.get(head)
                self._spool_epos[head] = pos
                if prev is None:
                    continue
                delta = pos - prev
                if delta == 0. or abs(delta) > SPOOL_SAMPLE_MAX_MM:
                    continue
                self.book_spool_use(head, delta, 'extrude')
        except Exception as e:
            logging.info('[multiACE] [spool] sampler error (ignored): %s' % e)
        try:
            # Rides this 1 Hz host tick (no device traffic of its own).
            sweep = getattr(self, '_bg_staged_sweep', None)
            if sweep is not None:
                sweep(eventtime)
        except Exception as e:
            logging.info('[multiACE] staged sweep failed: %s' % e)
        try:
            # Flush sparsely: booking happens in memory every second, but
            # the printer's eMMC must not see a write per second for hours
            # (the 0003-0528 page-eviction class). A crash loses at most
            # SPOOL_FLUSH_INTERVAL of consumption - grams, not spools.
            if (getattr(self, '_spool_dirty', False)
                    and (eventtime - getattr(self, '_spool_last_write', 0.)
                         >= SPOOL_FLUSH_INTERVAL)):
                self._spool_dirty = False
                self._save_spool_db()
        except Exception as e:
            logging.info('[multiACE] [spool] sampler error (ignored): %s' % e)
        return eventtime + SPOOL_SAMPLE_INTERVAL

    def _slot_read_uid(self, idx, slot):
        """The card UID last read at this slot: the ACE 2 host read
        (rc522 registry), the ACE Pro tunnel read, or the UID a firmware
        read carried (sentinel, kept in the tag cache). '' when none."""
        try:
            uid = (getattr(self, '_rc_last_uid', None) or {}).get(
                (idx, slot), '')
            if not uid:
                ent = ((getattr(self, '_gen1_tunnel_reads', None) or {})
                       .get(idx) or {}).get(slot) or {}
                uid = ent.get('uid', '')
            if not uid:
                ent = ((getattr(self, '_v2_filament_info_per_ace', None)
                        or {}).get(idx) or {}).get(slot) or {}
                uid = ent.get('uid', '')
            return str(uid or '').replace(':', '').strip().upper()
        except Exception:
            return ''

    def cmd_ACE_SPOOL_ADD(self, gcmd):
        sid = str(self._spool_next_id)
        self._spool_next_id += 1
        spool = {
            'id': sid,
            'material': (gcmd.get('MATERIAL', '') or '').strip(),
            'color': (gcmd.get('COLOR', '') or '').strip().lstrip('#').upper()[:6],
            'vendor': (gcmd.get('VENDOR', '') or '').strip(),
            'subtype': (gcmd.get('SUBTYPE', '') or '').strip(),
            # Spoolman's own spool id, when the entry came from (or matches)
            # a Spoolman database. Kept so a later import/sync can find THIS
            # entry again instead of adding a duplicate - the format must
            # carry it from the start, retrofitting an identity later means
            # migrating every existing table.
            'spoolman_id': (gcmd.get('SPOOLMAN_ID', '') or '').strip(),
            'label': (gcmd.get('LABEL', '') or '').strip(),
            'sku': (gcmd.get('SKU', '') or '').strip(),
            'used_mm': 0.,
        }
        # A SKU identifies ONE entry. Adding a spool whose tag SKU is already
        # taken (the second factory spool of an article) gets a suffix, and
        # the user is told what that costs: the tag still reads the ORIGINAL
        # code, so this entry can never be recognised automatically.
        spool['sku'], _sku_suffixed = self._spool_unique_sku(spool['sku'])
        w = gcmd.get_float('WEIGHT', None, minval=0., maxval=10000.)
        if w is not None:
            spool['weight_g'] = round(w, 1)
            spool['weight_initial_g'] = round(w, 1)
        d = gcmd.get_float('DENSITY', None, minval=0.5, maxval=3.0)
        if d is not None:
            spool['density'] = d
        # Bind target read BEFORE the entry is stored: a refused HEAD= must
        # not leave a created-but-unbound spool behind the error.
        a = gcmd.get_int('ACE', None, minval=0, maxval=3)
        sl = gcmd.get_int('SLOT', None, minval=0, maxval=3)
        h = gcmd.get_int('HEAD', None, minval=0, maxval=3)
        if h is not None and self.head_uses_ace(h):
            raise self._ace_error(gcmd, self._t(
                'msg.spool_head_not_feeder', head=self._disp(h)), code=200)
        # The card UID read at that slot is the per-chip key: carry it next
        # to the tag SKU, so the spool binds by either from then on (V1 and
        # V2 alike). Not when another spool already lists it.
        if a is not None and sl is not None:
            _uid = self._slot_read_uid(a, sl)
            if (_uid and self._sku_canon(_uid)
                    not in self._sku_codes(spool['sku'])
                    and self._spool_by_sku(_uid)[1] is None):
                spool['sku'] = (('%s,%s' % (spool['sku'], _uid))
                                if spool['sku'] else _uid)
        self._spools[sid] = spool
        logging.info('[multiACE] [spool] added #%s (%s) sku=%r%s', sid,
                     self._spool_label(spool), spool['sku'],
                     (' bound to MMU %d slot %d' % (a, sl))
                     if a is not None and sl is not None else
                     ((' bound to head %d' % h) if h is not None else ''))
        if a is not None and sl is not None:
            self._spool_bind(self._spool_key(a, sl), sid)
        elif h is not None:
            # Feeder/manual head (the head picker's "+" button): bind via
            # the h<n> key, same domain as ACE_SPOOL_ASSIGN HEAD=.
            self._spool_bind(self._spool_head_key(h), sid)
        self._save_spool_db(backup=True)
        self.log_always('[multiACE] Spool #%s added: %s'
                        % (sid, self._spool_label(spool)))
        if _sku_suffixed:
            self.log_warn(self._t('msg.spool_sku_suffixed',
                id=sid, sku=spool['sku']))
        if a is not None and sl is not None:
            self._warn_no_tag_code(a, sl, sid)

    def cmd_ACE_SPOOL_SET(self, gcmd):
        sid = str(gcmd.get_int('ID', minval=1))
        spool = self._spools.get(sid)
        if spool is None:
            raise self._ace_error(gcmd, 'No spool #%s in the table' % sid,
                                  code=200)
        # Distinguishes a USER edit from the backend's consumption flush
        # (ID= + SYNCED_MM= alone) - the two want different tails: an edit
        # gets the .bak backup + the response-pipe line, a flush must stay
        # quiet (a periodic push during a print would otherwise land its
        # lines in the densest pipe moments, and overwrite the backup
        # with a seconds-old state the backup exists to protect against).
        _user_edit = False
        for key, param in (('material', 'MATERIAL'), ('vendor', 'VENDOR'),
                           ('subtype', 'SUBTYPE'), ('label', 'LABEL'),
                           ('spoolman_id', 'SPOOLMAN_ID')):
            v = gcmd.get(param, None)
            if v is not None:
                spool[key] = v.strip()
                _user_edit = True
        # SKU is NOT in the loop above: it is an identity, so it needs the
        # collision check the other fields do not. Taking a code away from
        # another entry is never allowed - two entries with one code make
        # a tag read ambiguous, which is exactly what the table exists to
        # prevent. ADD may still auto-suffix (a command cannot ask, and a
        # suffix never overwrites); an EDIT has a user in front of it, so it
        # refuses and lets them pick. Strict sku-vs-sku on purpose, NOT
        # _spool_by_sku - that one also resolves a bare Spoolman id, which
        # would refuse codes that are not actually taken.
        v = gcmd.get('SKU', None)
        if v is not None:
            _new = v.strip()
            _want = self._sku_canon(_new)
            if _want:
                _holder = next((s for s, sp2 in self._spools.items()
                                if str(s) != str(sid)
                                and (set(self._sku_codes(_new))
                                     & set(self._sku_codes(sp2.get('sku'))))),
                               None)
                if _holder is not None:
                    raise self._ace_error(gcmd, self._t('msg.spool_sku_taken',
                        sku=_new, id=_holder), 200)
            spool['sku'] = _new
            _user_edit = True
        v = gcmd.get('COLOR', None)
        if v is not None:
            spool['color'] = v.strip().lstrip('#').upper()[:6]
            _user_edit = True
        w = gcmd.get_float('WEIGHT', None, minval=0., maxval=10000.)
        if w is not None:
            spool['weight_g'] = round(w, 1)
            if spool.get('weight_initial_g') is None:
                spool['weight_initial_g'] = round(w, 1)
            _user_edit = True
        d = gcmd.get_float('DENSITY', None, minval=0.5, maxval=3.0)
        if d is not None:
            spool['density'] = d
            _user_edit = True
        if gcmd.get_int('RESET_USED', 0):
            spool['used_mm'] = 0.
            _user_edit = True
        # How much of used_mm Spoolman has already been told about. The push
        # reports the DELTA and advances this only after a successful reply,
        # so a failed or half-done sync repeats instead of losing or double-
        # counting filament. Set by the web backend, not by hand.
        s = gcmd.get_float('SYNCED_MM', None, minval=0.)
        if s is not None:
            spool['spoolman_synced_mm'] = round(s, 1)
        if s is not None and not _user_edit:
            # Consumption flush (backend write-back after a 2xx from
            # Spoolman): quiet tail, see _user_edit above. And the moment
            # a DEFERRED drop resolves: an unbound Spoolman row only
            # survived its last unbind because consumption was still owed
            # (_spool_drop_if_unbound_sm) - paid now, so it leaves here.
            try:
                _debt = (float(spool.get('used_mm') or 0.)
                         - float(spool.get('spoolman_synced_mm') or 0.))
            except (TypeError, ValueError):
                _debt = 0.
            if (str(spool.get('spoolman_id') or '').strip()
                    and _debt <= 0.5
                    and not any(str(v) == sid
                                for v in self._spool_binding.values())):
                self._spools.pop(sid, None)
                self._save_spool_db()
                logging.info('[multiACE] [spool] SM%s #%s left the table '
                             'after its final sync'
                             % (spool.get('spoolman_id'), sid))
                return
            self._save_spool_db()
            logging.info('[multiACE] [spool] #%s synced_mm -> %.1f'
                         % (sid, spool['spoolman_synced_mm']))
            return
        self._save_spool_db(backup=True)
        self.log_always('[multiACE] Spool #%s updated: %s'
                        % (sid, self._spool_label(spool)))

    def cmd_ACE_SPOOL_PA(self, gcmd):
        sid = str(gcmd.get_int('ID', minval=1))
        spool = self._spools.get(sid)
        if spool is None:
            raise self._ace_error(gcmd, 'No spool #%s in the table' % sid,
                                  code=200)
        if self._spoollink_active():
            # PA in the SpoolLink world belongs to the pechex mod (the
            # apply/capture paths skip it too) - refuse the edit rather
            # than create a second truth.
            raise self._ace_error(gcmd, 'PA is managed by SpoolLink in '
                                        'this spool mode', code=200)
        dele = gcmd.get('DELETE', None)
        key = gcmd.get('KEY', None)
        if dele is not None:
            m = spool.get('pa_matrix')
            if isinstance(m, dict) and dele in m:
                m.pop(dele, None)
                self._save_spool_db()
                self.log_always('[multiACE] PA %s removed from spool %s'
                                % (dele, spool.get('label') or sid))
            else:
                raise self._ace_error(gcmd, 'Spool #%s has no PA entry %s'
                                      % (sid, dele), code=200)
        elif key is not None:
            key = key.strip()
            if not self._pa_key_valid(key):
                raise self._ace_error(gcmd, 'Invalid PA key %r (expected '
                                            'e.g. 0.4_standard)' % key,
                                      code=200)
            val = gcmd.get_float('VALUE', minval=0., maxval=5.)
            self._spool_pa_store(sid, spool, key, val)
            # If this spool currently feeds a head, apply the new value
            # right away (the apply cache keys on the value, so a changed
            # number is not "already applied").
            for h in range(4):
                _hs, _hsp = self._spool_for_head_pa(h)
                if _hs == sid:
                    self._apply_spool_pa(h, why='edit')
        else:
            m = spool.get('pa_matrix')
            if isinstance(m, dict) and m:
                self.log_always('[multiACE] PA of spool %s: %s'
                                % (spool.get('label') or sid,
                                   ', '.join('%s=%s' % (k, m[k])
                                             for k in sorted(m))))
            else:
                self.log_always('[multiACE] Spool %s has no stored PA'
                                % (spool.get('label') or sid))

    def cmd_ACE_SPOOL_DELETE(self, gcmd):
        sid = str(gcmd.get_int('ID', minval=1))
        spool = self._spools.get(sid)
        if spool is None:
            raise self._ace_error(gcmd, 'No spool #%s in the table' % sid,
                                  code=200)
        bound = [k for k, v in self._spool_binding.items() if v == sid]
        if bound and not gcmd.get_int('FORCE', 0):
            where = ', '.join(self._spool_binding_where(k) for k in bound)
            raise self._ace_error(
                gcmd, 'Spool #%s is still assigned (%s) - unassign first '
                      'or use FORCE=1' % (sid, where), code=200)
        for k in bound:
            self._spool_binding.pop(k, None)
        self._spools.pop(sid, None)
        self._save_spool_db(backup=True)
        self.log_always('[multiACE] Spool #%s deleted' % sid)

    def cmd_ACE_SPOOL_IMPORT(self, gcmd):
        path = gcmd.get('PATH')
        mode = (gcmd.get('MODE', 'merge') or 'merge').strip().lower()
        if mode not in ('merge', 'replace'):
            raise self._ace_error(gcmd, 'MODE must be merge or replace',
                                  code=200)
        try:
            with open(path, 'r') as f:
                data = json.load(f)
        except (IOError, OSError, ValueError) as e:
            raise self._ace_error(
                gcmd, 'Cannot read spool table %s: %s' % (path, e), code=200)
        src = data.get('spools')
        if not isinstance(src, dict):
            raise self._ace_error(
                gcmd, 'No spool table in %s (expected a "spools" object)'
                      % path, code=200)
        if mode == 'replace':
            self._spools = {str(k): dict(v) for k, v in src.items()
                            if isinstance(v, dict)}
            self._spool_binding = {
                str(k): str(v) for k, v in (data.get('binding') or {}).items()
                if str(v) in self._spools}
            self._spool_next_id = max(
                [int(data.get('next_id', 1))]
                + [int(k) + 1 for k in self._spools if str(k).isdigit()])
            n = len(self._spools)
            upd = 0
        else:
            n = 0
            upd = 0
            # Spoolman id = the cross-table identity: an entry that carries
            # one UPDATES the matching local entry instead of adding a
            # duplicate, so importing the same export twice is idempotent.
            by_sm = {}
            for k, v in self._spools.items():
                smid = str(v.get('spoolman_id') or '').strip()
                if smid:
                    by_sm[smid] = k
            for _k, v in sorted(src.items(),
                                key=lambda kv: int(kv[0])
                                if str(kv[0]).isdigit() else 0):
                if not isinstance(v, dict):
                    continue
                smid = str(v.get('spoolman_id') or '').strip()
                if smid and smid in by_sm:
                    tgt = self._spools[by_sm[smid]]
                    keep_id = tgt['id']
                    tgt.update(v)
                    tgt['id'] = keep_id      # local id stays - slots point at it
                    upd += 1
                    continue
                sid = str(self._spool_next_id)
                self._spool_next_id += 1
                sp = dict(v)
                sp['id'] = sid
                self._spools[sid] = sp
                if smid:
                    by_sm[smid] = sid
                n += 1
        self._save_spool_db(backup=True)
        self.log_always(
            '[multiACE] Spool table %s: %d entr%s imported%s from %s'
            % (mode, n, 'y' if n == 1 else 'ies',
               (', %d updated by spoolman id' % upd) if mode == 'merge' and upd
               else '', path))

    def cmd_ACE_SPOOL_LIST(self, gcmd):
        if not self._spools:
            self.log_always('[multiACE] Spool table is empty '
                            '(ACE_SPOOL_ADD to start one)')
            return
        where = {}
        for k, v in self._spool_binding.items():
            where[v] = self._spool_binding_where(k)
        self.log_always('[multiACE] Spool table:')
        for sid in sorted(self._spools, key=lambda x: int(x)):
            sp = self._spools[sid]
            w = sp.get('weight_g')
            # Full precision on request: one decimal like Spoolman, no ~, plus the
            # booking check): one decimal like Spoolman, no ~, plus the
            # raw counters so booked-vs-synced is readable in one line.
            u = float(sp.get('used_mm') or 0.)
            s = float(sp.get('spoolman_synced_mm') or 0.)
            extra = ''
            if u or s:
                extra = '  used %.1fmm / synced %.1fmm' % (u, s)
            self.log_always(
                '  #%-3s %-28s %s%s%s' % (
                    sid, self._spool_label(sp),
                    ('%.1f g left' % w) if w is not None else 'weight unknown',
                    extra,
                    ('  [%s]' % where[sid]) if sid in where else ''))

    def _spool_label(self, spool):
        parts = [p for p in (spool.get('label') or '').split() if p]
        if parts:
            return ' '.join(parts)
        bits = []
        if spool.get('vendor'):
            bits.append(spool['vendor'])
        if spool.get('material'):
            bits.append(spool['material'])
        if spool.get('subtype') and spool['subtype'].lower() not in (
                'basic', 'generic'):
            bits.append(spool['subtype'])
        if spool.get('color'):
            bits.append('#' + spool['color'])
        return ' '.join(bits) or 'spool'

    def _spool_rebind_from_tag_cache(self, why):
        """Re-run the tag auto-bind against the LAST READ tag of every
        occupied slot. The world switch clears all bindings, but the
        spools still sit in their slots with their tags already read -
        without this every roll would need a physical re-insert or a
        Klipper restart before it is recognised again. Uses the last READ
        of each slot (card UID first, then the sku: V2 tag cache, V1
        status tracker, host UID reads incl. the Gen-1 tunnel), so it can
        only ever see what a fresh read saw; _spool_bind_by_tag applies
        the ACTIVE world and all its guards (ambiguity, occupied move,
        hand-assign precedence). Gate-guarded: an emptied slot may keep a
        stale read, and a stale code must not resurrect a binding for a
        roll that is gone."""
        n = 0
        try:
            for idx in range(len(self._ace_devices)):
                gates = self._gate_status_per_ace.get(idx) or []
                for slot in range(len(gates)):
                    if gates[slot] != GATE_AVAILABLE:
                        continue
                    uid = self._slot_read_uid(idx, slot)
                    sku = self._slot_read_sku_raw(idx, slot)
                    if not (self._sku_canon(uid) or self._sku_canon(sku)):
                        continue
                    if self._spool_bind_tag_codes(idx, slot, uid,
                                                  sku) is not None:
                        n += 1
            if n:
                self.log_always('[multiACE] [spool] %d tag binding(s) '
                                'restored (%s)' % (n, why))
        except Exception as e:
            logging.info('[multiACE] [spool] tag rebind failed (ignored): '
                         '%s' % e)

    cmd_ACE_SPOOL_ASSIGN_help = (
        '[multiACE] Bind a spool to a slot: ACE_SPOOL_ASSIGN ACE=n SLOT=n '
        '[ID=n], or to a feeder/manual head: ACE_SPOOL_ASSIGN HEAD=n [ID=n]. '
        'Without ID the binding is CLEARED (spool taken out - the entry '
        'keeps its remaining weight for when it comes back).')

    def cmd_ACE_SPOOL_ASSIGN(self, gcmd):
        h = gcmd.get_int('HEAD', None, minval=0, maxval=3)
        sid = gcmd.get_int('ID', None, minval=0)
        if h is not None:
            # Head binding is the feeder/manual case ONLY: an ACE head's
            # spool follows the SLOT it loads from (that binding moves with
            # every swap), so a fixed head binding there would double-book.
            # The refusal gates BINDING only - clearing must always work,
            # or a head switched back to ACE could never shed the binding
            # from its feeder days (a state only reachable while the head
            # was a feeder must stay removable after it no longer is).
            if sid and self.head_uses_ace(h):
                raise self._ace_error(gcmd, self._t(
                    'msg.spool_head_not_feeder', head=self._disp(h)),
                    code=200)
            key, where = self._spool_head_key(h), 'Head %d' % self._disp(h)
        else:
            a = gcmd.get_int('ACE', minval=0, maxval=3)
            sl = gcmd.get_int('SLOT', minval=0, maxval=3)
            key = self._spool_key(a, sl)
            where = 'MMU %d / Slot %d' % (self._disp(a), self._disp(sl))
        if not sid:
            old = self._spool_binding.pop(key, None)
            if old is not None:
                self._spool_drop_if_unbound_sm(old, 'unassigned')
            self._save_spool_db(backup=True)
            self.log_always('[multiACE] %s: spool binding %s'
                            % (where,
                               ('cleared (was #%s)' % old) if old else 'was empty'))
            # SL mode, head unbind: take back OUR resolver-set id - but
            # only when the head still shows exactly the id we sent (a
            # feeder-reader-set id is not ours to clear, send precedence).
            if h is not None and old is not None and self._spoollink_active():
                try:
                    _ent = self._spoollink_sent.pop(h, None)
                    if (_ent is not None
                            and self._ptc_spool_id_for(h)
                            == int(_ent.get('sid', 0))):
                        self._spoollink_clear(h)
                except Exception as _e:
                    logging.info('[multiACE] [spoollink] unassign-clear '
                                 'head %d failed: %s' % (h, _e))
            return
        sid = str(sid)
        if sid not in self._spools:
            raise self._ace_error(gcmd, 'No spool #%s in the table' % sid,
                                  code=200)
        # A spool sitting in an OCCUPIED slot cannot be assigned elsewhere:
        # physically it is right there, and taking it out is what frees it
        # (the gate-empty release above). So this can only ever be a
        # mis-pick, and running it silently would move the spool and
        # leave its old slot unbound. Refuse instead:
        # the UI hides these spools, this is the backstop that a stale or
        # bypassed UI cannot get around. _spool_bind keeps its move
        # semantics - it is the mechanism, this is the policy.
        for _k, _v in list(self._spool_binding.items()):
            if _v != sid or _k == key:
                continue
            try:
                _oa, _os = (int(x) for x in _k.split('_'))
                _gates = self._gate_status_per_ace.get(_oa) or []
                _occupied = (_os < len(_gates)
                             and _gates[_os] == GATE_AVAILABLE)
            except Exception:
                _occupied = False
            if _occupied:
                raise self._ace_error(gcmd, self._t(
                    'msg.spool_bound_elsewhere', id=sid,
                    spool=self._spool_label(self._spools[sid]),
                    ace=self._disp(_oa), slot=self._disp(_os)), code=200)
        # The TARGET side has no red guard: binding over an already-bound
        # key simply displaces the previous holder (the web greys taken
        # targets out, gcode can still do it). The
        # displaced spool must then follow the decided lifecycle - a
        # Spoolman row whose LAST binding this was leaves the table (or
        # waits unbound for its final push), instead of lingering forever.
        _prev = self._spool_binding.get(key)
        self._spool_bind(key, sid)
        if _prev is not None and str(_prev) != sid:
            self._spool_drop_if_unbound_sm(_prev, 'displaced by #%s' % sid)
        # A hand assignment LEARNS the card UID read in that slot: a Bambu
        # roll carries a chip per flange, so its
        # second side reads as an unknown UID - pick the roll in the
        # picker and it gets 'A,B', and every side binds by itself from
        # then on. Only UIDs (the registry holds host-read UIDs, never an
        # article sku), only when not yet listed.
        # LEARN_UID=0 (the web asks first): bind without
        # learning. Absent = learn, as before (console use).
        _learn = gcmd.get_int('LEARN_UID', 1, minval=0, maxval=1)
        if h is None and _learn:
            try:
                _uid = self._slot_read_uid(a, sl)
                _sp = self._spools[sid]
                if _uid and (self._sku_canon(_uid)
                             not in self._sku_codes(_sp.get('sku'))):
                    _old = (_sp.get('sku') or '').strip()
                    _sp['sku'] = ('%s,%s' % (_old, _uid)) if _old else _uid
                    logging.info('[multiACE] [spool] learned UID %s for '
                                 'spool #%s (sku now %r)'
                                 % (_uid, sid, _sp['sku']))
                    self.log_always('[multiACE] spool #%s: learned card '
                                    'UID %s' % (sid, _uid))
            except Exception as _e:
                logging.info('[multiACE] [spool] UID learn skipped: %s'
                             % _e)
            # Same for the tag's article CODE (Anycubic factory sku): a
            # stock ACE 2 reports no card UID, so the code is the only
            # thing the next read can match. An adopted Spoolman row
            # carries only its generated SM<id>, and a read that matches
            # nothing RELEASES a binding whose base differs - without the
            # learn every restart undid the hand assignment. Two spools of
            # one article then share the code: auto-bind stops for it and
            # the hand choice sticks (the duplicate rule). Never a written
            # SM<id>/bare-id tag - that id names its own spool.
            try:
                _code = self._slot_read_sku_raw(a, sl)
                _sp = self._spools[sid]
                _cc = self._sku_canon(_code)
                if (_cc and _cc not in self._sku_codes(_sp.get('sku'))
                        and not re.fullmatch(r'(?:sm)?\d+', _cc)
                        and _cc != self._sku_canon(
                            self._slot_read_uid(a, sl))):
                    _old = (_sp.get('sku') or '').strip()
                    _sp['sku'] = ('%s,%s' % (_old, _code)) if _old else _code
                    logging.info('[multiACE] [spool] learned tag code %r '
                                 'for spool #%s (sku now %r)'
                                 % (_code, sid, _sp['sku']))
                    self.log_always('[multiACE] spool #%s: learned tag '
                                    'code %s' % (sid, _code))
            except Exception as _e:
                logging.info('[multiACE] [spool] tag code learn skipped: '
                             '%s' % _e)
        self._save_spool_db(backup=True)
        if h is None:
            self._warn_no_tag_code(a, sl, sid)
        # Feeder/manual heads never pass the toolchange PA hook (no
        # head_source), so a head binding applies the stored value here.
        if h is not None:
            self._apply_spool_pa(h, why='assign')
        self.log_always('[multiACE] %s: spool #%s (%s)'
                        % (where, sid,
                           self._spool_label(self._spools[sid])))
        # SL mode, head bind: hand the spool to SpoolLink right away so a
        # feeder head counts from the moment it is bound (gap-filler only -
        # the send's feeder precedence skips it when the feeder reader
        # already set a different spool).
        if h is not None and self._spoollink_active():
            try:
                _smid = self._spoollink_smid_for(h)
                if _smid > 0:
                    self._spoollink_send(h, _smid, 'assign')
            except Exception as _e:
                logging.info('[multiACE] [spoollink] assign-send head %d '
                             'failed: %s' % (h, _e))
