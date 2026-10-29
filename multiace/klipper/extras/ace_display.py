"""Touchscreen filament identity (mixin of MultiAce).

Everything that writes a head's filament identity into stock
print_task_config and reads the touchscreen's edits back: the
SET_PRINT_FILAMENT_CONFIG wrapper, the heartbeat push (`_push_rfid_info`),
the display-edit capture, the official/spool-id handling and the
display-side slot helpers. State lives on the MultiAce instance
(initialised in ace.py); this module holds the code and its constant.
Second module of the R1 split (UNIT_DRIVER_DESIGN.md §11): every method
here is byte-identical to its former place in ace.py, proven by
tests/units/tools/move_check.py.
"""
import logging

# How often we may FORCE a head that keeps coming back as 'official' before
# giving up on it. Normally one force is enough - stock clears the flag on a
# successful set, so the next push finds a free head and the counter resets.
# It only ever climbs if something re-stamps the head behind us; SpoolLink
# demonstrably does not, but it is closed source and an
# update could change that. Then we retreat with one log line instead of
# fighting it at heartbeat rate.
FORCE_OFFICIAL_MAX = 3


class DisplayMixin:

    def _unloaded_display_slot(self, head):
        """(ace, slot) whose identity an UNLOADED ACE head shows. Multi/normal:
        the active device, slot==head. Head mode: the head's own ACE
        (head_ace_for) and its first loaded slot - head_ace_for returns the
        head index in multi, so it must not be used there."""
        if getattr(self, '_ace_mode', 'multi') == 'head':
            ace = self.head_ace_for(head)
            slot = self._first_loaded_slot_for_ace(ace)
            return ace, (head if slot is None else slot)
        return self._active_device_index, head

    def _ptc_color_to_override_hex(self, c):
        """SET_PRINT_FILAMENT_CONFIG arg comes in as RRGGBB or RRGGBBAA
        (with or without #). Picker overrides store '#rrggbb'."""
        if c is None:
            return ''
        s = str(c).lstrip('#').upper()
        if len(s) >= 6:
            return '#' + s[:6]
        return ''

    def _expect_ptc_push(self, head, ftype, color_rgba, vendor, subtype):
        """Record a SET_PRINT_FILAMENT_CONFIG line we just queued so the
        wrapper can recognise it as an ace.py-internal push and skip
        the override-capture path. Cap the buffer at 32 entries so a
        gcode that errored before the wrapper ran can't grow it
        unbounded."""
        self._expected_ptc_pushes.append({
            'head':    int(head),
            'type':    str(ftype or ''),
            'color':   str(color_rgba or '').upper().lstrip('#'),
            'vendor':  str(vendor or ''),
            'subtype': str(subtype or ''),
        })
        if len(self._expected_ptc_pushes) > 32:
            self._expected_ptc_pushes = self._expected_ptc_pushes[-32:]

    def _ptc_push_guarded(self, head, ftype, color_rgba, vendor, subtype, ctx):
        """Push one head's filament identity, tolerating a rejection.

        Used by the two RFID pushes that are NOT part of the display heal:
        the fresh rfid==2 transition and the unloaded-head fallback. Both
        would otherwise call run_script_from_command bare, inside the
        heartbeat's response callback. The response wrapper catches a
        raising callback and only logs it, but that would silently skip the
        rest of the heartbeat callback, and the rejection (a configured
        filament_spool_id, an official/locked tag) needs its own handling.

        Rejections are also negative-cached per head on the pushed
        identity: the fallback runs on EVERY heartbeat, so without this a
        rejection there would block the touchscreen with a fresh level-3
        popup every second - catching the exception alone does not help,
        the firmware raises it before we see it.
        """
        key = (str(ftype or ''), str(color_rgba or ''),
               str(vendor or ''), str(subtype or ''))
        if self._ptc_push_block.get(head) == key:
            return False
        self._expect_ptc_push(head, ftype, color_rgba, vendor, subtype)
        try:
            self.gcode.run_script_from_command(
                'SET_PRINT_FILAMENT_CONFIG '
                'CONFIG_EXTRUDER=%d '
                'FILAMENT_TYPE="%s" '
                'FILAMENT_COLOR_RGBA=%s '
                'VENDOR="%s" '
                'FILAMENT_SUBTYPE="%s"' % (
                    head, ftype, color_rgba, vendor, subtype))
            self._ptc_push_block.pop(head, None)
            return True
        except Exception as e:
            self._ptc_push_block[head] = key
            logging.warning(
                '[multiACE] %s push for head %d rejected - not repeating it '
                'for this identity: %s' % (ctx, head, e))
            return False

    def _ptc_spool_id_for(self, head):
        """The spool id currently configured on a head, 0 if none.

        Stock 1.5.2 only. Returns 0 on anything older (no such field), on a
        head without one, or if print_task_config cannot be read - so a
        machine that never sees a spool id behaves exactly as before.
        """
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc is None:
                return 0
            ids = (getattr(ptc, 'print_task_config', None)
                   or {}).get('filament_spool_id') or []
            return int(ids[head]) if 0 <= head < len(ids) else 0
        except Exception:
            return 0

    def _ptc_official_for(self, head):
        """True when stock has flagged this head as an official spool.

        Set from an RFID read (filament_detect stamps OFFICIAL for any
        identity that arrives with parameters) and cleared only by a
        SUCCESSFUL set - an unload does NOT clear it, so a head can stay
        flagged from a spool that is long gone.
        """
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc is None:
                return False
            flags = (getattr(ptc, 'print_task_config', None)
                     or {}).get('filament_official') or []
            return bool(flags[head]) if 0 <= head < len(flags) else False
        except Exception:
            return False

    def _identity_mode(self):
        """Identity policy on official-flagged heads, derived from the
        world switch: the spoollink world leaves them to SpoolLink, every
        other world forces ours through (the old multiace behaviour).
        [ace] identity_priority is dead - read for halt-safety only, its
        value is discarded (see the config read)."""
        return ('spoollink'
                if getattr(self, 'spool_mode', 'local') == 'spoollink'
                else 'multiace')

    def _force_official_inject(self, head, params, saved):
        """Bounded FORCE=1 ladder for a push onto an official-flagged head
        (multiace identity world; shared by our-push and user-push paths of
        the SET_PRINT_FILAMENT_CONFIG wrapper). Returns the pristine-params
        copy the caller's finally restores - captured here on the first
        mutation."""
        _n = self._force_official_count.get(head, 0)
        if _n < FORCE_OFFICIAL_MAX:
            if saved is None:
                saved = dict(params)
            params['FORCE'] = '1'
            self._force_official_count[head] = _n + 1
            logging.info(
                '[multiACE] head %d is flagged official - forcing '
                'our identity through (identity_priority=multiace)'
                % self._raw(head))
        elif _n == FORCE_OFFICIAL_MAX:
            self._force_official_count[head] = _n + 1
            logging.warning(
                '[multiACE] head %d keeps coming back as official '
                'after %d forced pushes - something is re-stamping '
                'it. Leaving it alone; set identity_priority: '
                'spoollink to stop trying.'
                % (self._raw(head), FORCE_OFFICIAL_MAX))
        return saved

    def _force_official_user(self, head, params, saved):
        """FORCE=1 for a USER push (picker, display, spool assign) onto an
        official-flagged head: unbounded, because the user's set is the
        declared truth and nothing of ours repeats it. The ladder's budget
        is reset so a later heal push starts fresh. The bounded ladder
        exists for our own 1 Hz heal pushes only; a stock side-feeder reader
        re-stamps OFFICIAL on every read, and with the ladder on the user
        path the fourth save of a session ran into stock's refusal until the
        next restart."""
        if saved is None:
            saved = dict(params)
        params['FORCE'] = '1'
        self._force_official_count.pop(head, None)
        logging.info(
            '[multiACE] head %d is flagged official - user set forces '
            'our identity through' % self._raw(head))
        return saved

    def _ptc_identity_unchanged(self, head, params):
        """Does this push carry the identity the head already shows?

        Decides whether an existing spool id may ride along. Same identity =
        we are only refreshing what is there, so the binding still describes
        the filament and is preserved. DIFFERENT identity = another spool is
        in that head, the binding would now point at the wrong one - we leave
        the id out and let stock clear it, rather than keep a link that lies.
        """
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            cfg = getattr(ptc, 'print_task_config', None) or {}

            def _cur(key):
                v = cfg.get(key) or []
                return str(v[head]) if 0 <= head < len(v) else ''
            return (str(params.get('FILAMENT_TYPE', '') or '').strip('"')
                        == _cur('filament_type')
                    and str(params.get('FILAMENT_COLOR_RGBA', '') or ''
                            ).upper().lstrip('#')
                        == _cur('filament_color_rgba').upper().lstrip('#'))
        except Exception:
            return False

    def _match_expected_push(self, incoming):
        """Index of the recorded push matching this command, else None.

        Peeked BEFORE the stock handler runs (to decide whether to carry a
        spool id) and popped after, so the comparison lives in one place.
        """
        norm_sub = self._norm_subtype(incoming.get('subtype', ''))
        for i, exp in enumerate(self._expected_ptc_pushes):
            if (exp['head'] == incoming['head']
                    and exp['type'] == incoming['type']
                    and exp['color'] == incoming['color']
                    and exp['vendor'] == incoming['vendor']
                    and self._norm_subtype(exp.get('subtype', '')) == norm_sub):
                return i
        return None

    def _wrap_set_print_filament_config(self, gcmd):
        """Replacement handler for SET_PRINT_FILAMENT_CONFIG. Always
        chains to the original print_task_config handler first so the
        printer state still updates. Then either pops a matching
        expected entry (= our own push) or treats the gcode as a
        display-driven user edit and persists it as an override."""

        if self._orig_set_ptc is not None:
            # Single chokepoint: normalise generic-equivalent vendor/subtype to
            # the canonical 'Generic'/'Basic' on the way INTO print_task_config
            # (every internal push, the display's own write and stock RFID all
            # reach _orig_set_ptc through this wrapper), so heads with the same
            # generic material store byte-identical strings and the stock
            # auto-replenish match can find a same-material head.
            # Only a real material (non-empty FILAMENT_TYPE) is touched, so
            # empty-clear ("?") pushes stay empty. Mutate + restore the SAME live
            # params dict so the expected-push read below still sees the ORIGINAL
            # raw values - the expected-push records are raw, so the comparison
            # must stay raw or the wrapper would mis-read its own push as a
            # display edit (spurious capture/loop).
            params = gcmd.get_command_parameters()
            saved = None
            # Carry an already-configured spool id back into our OWN pushes.
            # Stock 1.5.2 refuses a push to a head that has one unless the
            # command carries a spool id (or FORCE=1) - and that command IS
            # the only way a spool id can ever be set: the RFID route reads
            # info['SPOOL_ID'], a key no tag parser produces and which
            # filament_detect/set rejects outright. So a claimed head was
            # claimed by something sending this very command with one extra
            # parameter (SpoolLink), and passing the SAME value back both
            # satisfies the guard and preserves the claim.
            # Deliberately OUR pushes only: injecting it for a display edit
            # would let a hand edit overwrite a claimed head while silently
            # keeping the spool link, so the id would end up attached to a
            # filament nobody verified. A user edit keeps hitting the guard,
            # which is stock's intent.
            _ph = int(gcmd.get_int('CONFIG_EXTRUDER', -1))
            _skip_push = False
            if self._match_expected_push({
                    'head':    _ph,
                    'type':    str(gcmd.get('FILAMENT_TYPE', '') or ''),
                    'color':   str(gcmd.get('FILAMENT_COLOR_RGBA', '')
                                   or '').upper().lstrip('#'),
                    'vendor':  str(gcmd.get('VENDOR', '') or ''),
                    'subtype': str(gcmd.get('FILAMENT_SUBTYPE', '') or ''),
                }) is not None:
                # OUR OWN push only - everything below would be wrong for a
                # display edit, where the user's hand is on the wheel.
                if 'FILAMENT_SPOOL_ID' not in params:
                    _sid = self._ptc_spool_id_for(_ph)
                    if _sid > 0 and self._ptc_identity_unchanged(_ph, params):
                        saved = dict(params)
                        params['FILAMENT_SPOOL_ID'] = str(_sid)
                        logging.info(
                            '[multiACE] head %d keeps spool id %d - same '
                            'identity, the binding still fits'
                            % (self._raw(_ph), _sid))
                    elif _sid > 0:
                        logging.info(
                            '[multiACE] head %d drops spool id %d - a '
                            'different filament is in it now, the binding '
                            'would point at the wrong spool'
                            % (self._raw(_ph), _sid))
                # --- SpoolLink mode: the resolver is the setter for a head
                # whose slot has a bound Spoolman spool (decision B). Our
                # own push is converted into ONE resolver call (deduped on
                # the spool id - the heal fires every second, the resolver
                # must not); the empty-clear of a managed head routes
                # through filament_detect so the official flag dies with
                # the spool. Everything this branch does not claim falls
                # through to the normal push / official handling below.
                if 0 <= _ph <= 3 and self._spoollink_active():
                    _slt = str(params.get('FILAMENT_TYPE', '') or '').strip()
                    if _slt:
                        _smid = self._spoollink_smid_for(_ph)
                        _ent = self._spoollink_sent.get(_ph)
                        if _smid > 0:
                            _skip_push = True
                            if not _ent or int(_ent.get('sid', 0)) != _smid:
                                if _ent:
                                    _ent['n'] = 0   # new spool, fresh budget
                                    _ent['sid'] = _smid
                                    self._spoollink_sent[_ph] = _ent
                                self._spoollink_send(_ph, _smid, 'push')
                    else:
                        # Empty-clear: release the SpoolLink claim based on
                        # the head's PTC STATE, not the in-RAM marker - the
                        # marker dies with every restart while official +
                        # filament_spool_id persist, so a marker-gated
                        # clear could never fire again after a reboot (the
                        # heal would loop against the official flag at 1 Hz).
                        _ent = self._spoollink_sent.pop(_ph, None)
                        if (_ent is not None
                                or self._ptc_official_for(_ph)
                                or self._ptc_spool_id_for(_ph) > 0):
                            if self._spoollink_clear(_ph):
                                _skip_push = True
                _official = self._ptc_official_for(_ph)
                if _skip_push:
                    # The SpoolLink branch above already owns this push -
                    # the official handling below must neither log per
                    # tick nor stack a FORCE onto a push nobody sends.
                    pass
                elif not _official:
                    # Our force stuck (or the head never was official) -
                    # forget the attempts, so a later flag starts fresh.
                    self._force_official_count.pop(_ph, None)
                elif self._identity_mode() == 'spoollink':
                    # Keeping out means NOT SENDING IT. Handing our own push
                    # to stock anyway raises "official filament, not
                    # configurable" - a level-3 popup on the printer for a
                    # push we already decided not to insist on. Gating only
                    # the FORCE injection is not enough.
                    _skip_push = True
                    logging.info(
                        '[multiACE] head %d is flagged official - leaving it '
                        'to SpoolLink (identity_priority=spoollink)'
                        % self._raw(_ph))
                elif ('FORCE' not in params
                        and self._identity_mode() == 'multiace'):
                    # NON-ACE heads: a factory-tag read can stamp OFFICIAL
                    # on a feeder head. The heal loop skips non-ACE heads
                    # entirely, so every push on one that reaches this
                    # wrapper is a deliberate user action = declared truth,
                    # forced every time. ACE heads keep the bounded ladder
                    # (their pushes are the 1 Hz heal, which must not fight
                    # a re-stamping reader forever).
                    if self.head_uses_ace(_ph):
                        saved = self._force_official_inject(_ph, params, saved)
                    else:
                        saved = self._force_official_user(_ph, params, saved)
            elif 0 <= _ph <= 3:
                # USER push (display/picker edit - not one of our expected
                # pushes). The whole official block above sits inside the
                # expected-push gate, so a hand set on an official head would
                # run STRAIGHT into stock's refusal = bare id-522 "System
                # anomaly". In the multiace world (spool_mode local/spoolman)
                # the user's set IS the declared truth, so it gets the same
                # bounded FORCE ladder. spoollink mode deliberately
                # unchanged: SpoolLink owns identities there, a hand set
                # keeps hitting stock's guard (its popup names the state
                # honestly).
                _official = self._ptc_official_for(_ph)
                if not _official:
                    self._force_official_count.pop(_ph, None)
                elif ('FORCE' not in params
                        and self._identity_mode() == 'multiace'):
                    saved = self._force_official_user(_ph, params, saved)
            if str(params.get('FILAMENT_TYPE', '') or '').strip():
                nv = (self._norm_vendor_push(params.get('VENDOR'))
                      if 'VENDOR' in params else None)
                ns = (self._norm_subtype_push(params.get('FILAMENT_SUBTYPE'))
                      if 'FILAMENT_SUBTYPE' in params else None)
                if ((nv is not None and nv != params.get('VENDOR'))
                        or (ns is not None
                            and ns != params.get('FILAMENT_SUBTYPE'))):
                    # Only capture once - the spool-id injection above may
                    # already hold the pristine copy the finally restores.
                    if saved is None:
                        saved = dict(params)
                    if nv is not None:
                        params['VENDOR'] = nv
                    if ns is not None:
                        params['FILAMENT_SUBTYPE'] = ns
            try:
                if _skip_push:
                    # identity_priority=spoollink on an official head: not
                    # sending it at all is the whole point (see above).
                    pass
                elif saved is not None and self._raw_set_ptc is not None:
                    # We mutated params -> call the RAW stock handler directly.
                    # _orig_set_ptc is the extended-command lambda that re-parses
                    # _params from the raw commandline and would throw our
                    # normalisation away (see the registration comment); the raw
                    # method reads the already-parsed (mutated) params, so
                    # 'Generic'/'Basic' actually lands in print_task_config.
                    self._raw_set_ptc(gcmd)
                else:
                    self._orig_set_ptc(gcmd)
            finally:
                if saved is not None:
                    params.clear()
                    params.update(saved)
        try:
            head = gcmd.get_int('CONFIG_EXTRUDER', None)
            if head is None:
                return
            incoming = {
                'head':    int(head),
                'type':    str(gcmd.get('FILAMENT_TYPE', '') or ''),
                'color':   str(gcmd.get('FILAMENT_COLOR_RGBA', '') or '').upper().lstrip('#'),
                'vendor':  str(gcmd.get('VENDOR', '') or ''),
                'subtype': str(gcmd.get('FILAMENT_SUBTYPE', '') or ''),
            }
            _i = self._match_expected_push(incoming)
            if _i is not None:
                self._expected_ptc_pushes.pop(_i)
                return

            self._capture_display_edit(incoming)
        except Exception as e:
            logging.info(
                '[multiACE] _wrap_set_print_filament_config error: %s' % e)

    def _capture_display_edit(self, ev):
        """Persist a display-driven SET_PRINT_FILAMENT_CONFIG into
        self._slot_overrides.

        Mapping rules:
        - head_source[head] set with real values (= loaded) -> (src.ace, src.slot)
        - head_source[head] is None (= unloaded but slot N of the active
          ACE is wired to extruder N by the parallel splitter)
          -> (active_device, head)
        - head_source[head] still in cmd_ACE_LOAD_HEAD's placeholder
          state (type='' / color='000000') -> skip; pushes during that
          window are ace.py's own internal work and the (ace, slot)
          mapping isn't user intent yet.
        """
        if self._swap_in_progress:

            return
        head = int(ev['head'])
        if not self.head_uses_ace(head):
            # A head not driven by the ACE (a manual/TPU head, or a feeder head
            # in head mode) isn't backed by an ACE slot - a display edit for it
            # must stay local (printer extruder config) and must NOT be captured
            # as an override on the active ACE's slot (that would push the edit
            # onto the ACE head's slot identity). In multi this is identical to
            # the old head_is_manual guard (head_uses_ace == not manual there).
            self._fa_trace(
                'display edit for head %d ignored (no MMU slot: manual or '
                'feeder)' % head)
            return
        # Stock firmware's own RFID auto-fill re-pushes the spool SKU into the
        # subtype field with VENDOR="NONE" (a sentinel multiACE never emits;
        # we always push the real brand or ""). That is NOT a user display
        # edit - capturing it baked the SKU in as a bogus subtype (e.g.
        # 'AHPLBK-101' showing as 'PLA AHPLBK-101'). Ignore it.
        if (ev.get('vendor') or '').strip().upper() == 'NONE':
            self._fa_trace(
                'display edit for head %d ignored (VENDOR=NONE = stock RFID '
                'auto-fill, not a user edit)' % head)
            return
        src = self._head_source.get(head)
        if src:

            # Skip ONLY the genuine mid-load placeholder window (head_source is
            # still being filled inside cmd_ACE_LOAD_HEAD). Gating on an empty
            # head_source 'type' was WRONG: a head loaded from a spool WITHOUT
            # RFID keeps type='' permanently, so display edits to it were
            # silently dropped forever (web worked because it writes the
            # override directly, bypassing this path). Internal identity pushes
            # are already filtered by the expected-push match above, and empty
            # pushes by the all-empty guard below, so anything reaching here
            # with src set (and not mid-load) is a genuine display edit.
            if getattr(self, '_in_internal_load_head', False):
                return
            ace_idx = int(src.get('ace_index', 0))
            slot_idx = int(src.get('slot', 0))
        else:
            # Unloaded head. Multi/normal: slot==head on the active ACE
            # (byte-identical). Head mode: the head's WIRED ACE + its first
            # loaded slot, matching what _push_rfid_info shows, so the edit lands
            # on the slot the user actually sees - not slot==head on the
            # globally-active device (head_ace_for returns the head index in
            # multi, so it must be head-gated).
            if getattr(self, '_ace_mode', 'multi') == 'head':
                ace_idx = self.head_ace_for(head)
                _s = self._first_loaded_slot_for_ace(ace_idx)
                slot_idx = _s if _s is not None else head
            else:
                ace_idx = self._active_device_index
                slot_idx = head

        key = '%d_%d' % (ace_idx, slot_idx)
        existing = self._slot_overrides.get(key) or {}

        ptc = self.printer.lookup_object('print_task_config', None)
        ptc_status = ptc.get_status() if ptc is not None else {}
        ptc_types = ptc_status.get('filament_type', []) or []
        ptc_vendors = ptc_status.get('filament_vendor', []) or []
        ptc_subs = ptc_status.get('filament_sub_type', []) or []
        ptc_rgbas = ptc_status.get('filament_color_rgba', []) or []
        ptc_type = (ptc_types[head] if head < len(ptc_types) else '') or ''
        ptc_vendor = (ptc_vendors[head] if head < len(ptc_vendors) else '') or ''
        ptc_sub = (ptc_subs[head] if head < len(ptc_subs) else '') or ''
        ptc_rgba = (ptc_rgbas[head] if head < len(ptc_rgbas) else '') or ''
        if ptc_type == 'NONE':
            ptc_type = ''
        if ptc_vendor == 'NONE':
            ptc_vendor = ''
        if ptc_rgba.upper() in ('00000000', '000000FF'):
            ptc_rgba = ''

        inc_type = (ev.get('type') or '').strip()
        inc_color_raw = (ev.get('color') or '').strip().lstrip('#').upper()
        inc_vendor = (ev.get('vendor') or '').strip()
        inc_subtype = (ev.get('subtype') or '').strip()

        # The TOUCHSCREEN sends placeholder pushes with an all-zero colour, so
        # a colour-only edit to black is indistinguishable from one. (Not a
        # stock convention: stock decides "configured" on type/vendor and
        # defaults an unset colour to FFFFFFFF - this is the display's own
        # habit, and this guard is the ingest rule for it.)
        # Treat black as a placeholder ONLY when the slot has
        # no existing identity (no override material/colour AND no live PTC
        # material) - there it really is the empty marker. With an identity
        # present, black is a genuine user colour pick and must persist
        # (dropping it would let the next resync re-push the old colour).
        # '' and 00000000 (transparent) are always the empty marker -> skip.
        # multiACE's own empty/black pushes never reach here (popped by the
        # expected-push match in _wrap_set_print_filament_config), so this only
        # loosens genuine display edits.
        has_identity = bool(existing.get('material') or existing.get('color')
                            or ptc_type)
        if (not inc_type and not inc_vendor
                and (inc_color_raw in ('', '00000000')
                     or (inc_color_raw in ('000000', '000000FF')
                         and not has_identity))):
            return

        merged_material = inc_type or existing.get('material') or ptc_type
        merged_brand = inc_vendor or existing.get('brand') or ptc_vendor
        # A display material pick carries the full identity, so its subtype is
        # authoritative even when EMPTY (e.g. picking plain Generic must clear a
        # prior 'Matte'/'Basic'). The display sends FILAMENT_SUBTYPE as its own
        # field, so an empty value is a real "no subtype", not "unspecified".
        # Only fall back to the stored/ptc subtype on a partial edit that
        # carries no type at all.
        if inc_type:
            merged_subtype = inc_subtype
        else:
            merged_subtype = inc_subtype or existing.get('subtype') or ptc_sub
        if inc_color_raw and inc_color_raw != '00000000':
            merged_color = self._ptc_color_to_override_hex(inc_color_raw)
        elif existing.get('color'):
            merged_color = existing['color']
        elif ptc_rgba:
            merged_color = self._ptc_color_to_override_hex(ptc_rgba)
        else:
            merged_color = ''

        new_override = {
            'ace':      ace_idx,
            'slot':     slot_idx,
            'material': merged_material,
            'brand':    merged_brand,
            'subtype':  merged_subtype,
            'color':    merged_color,
        }
        if existing == new_override:
            return
        self._slot_overrides[key] = new_override
        logging.info(
            '[multiACE] display edit -> override (MMU %d / slot %d): %s' % (
                ace_idx, slot_idx, new_override))
        self._save_slot_overrides()
        # V2 identity snapshot: an edit on a LOADED head is the user
        # re-declaring what the head holds - fold it into the capture too
        # (quad's want side and the same-lane inherit read the capture;
        # the push already follows the live override). Unloaded heads
        # have nothing to fold.
        if src:
            try:
                if merged_material:
                    src['type'] = merged_material
                    src['subtype'] = merged_subtype or ''
                elif merged_subtype:
                    src['subtype'] = merged_subtype
                _c = (merged_color or '').strip().lstrip('#').upper()[:6]
                if _c:
                    src['color'] = _c
                if merged_brand:
                    src['brand'] = merged_brand
                self._save_head_source()
            except Exception:
                pass

    def _refresh_filament_exist_flags(self):
        """Recompute print_task_config.filament_exist from the live toolhead
        sensors. The Snapmaker display shows "/" (no filament) for a head only
        when filament_exist[head] is False; with filament present but no type
        it shows "?", with a type the material. Stock recomputes that flag only
        on runout / feed-port / sensor-toggle events (print_task_config
        _runout_evt_handle etc.), so after an idle unload it goes stale (stays
        True) and an emptied head wrongly shows "?" instead of "/". We can't
        set the flag via SET_PRINT_FILAMENT_CONFIG (no such parameter), so
        trigger the stock refresh whenever the toolhead filament state may have
        changed (load/unload, display resync)."""
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc is not None and hasattr(ptc, 'update_filament_flags'):
                ptc.update_filament_flags()
        except Exception as e:
            logging.info('[multiACE] _refresh_filament_exist_flags: %s' % e)

    def _push_rfid_info(self):
        logging.info('[multiACE] _push_rfid_info: active_device=%d, head_source=%s' % (
            self._active_device_index, str({k: (v['ace_index'] if v else None) for k, v in self._head_source.items()})))
        active = self._active_device_index
        # Keep the display's "/" (no-filament) state current: filament_exist is
        # sensor-derived and goes stale after an idle unload (see helper).
        self._refresh_filament_exist_flags()

        lines = []
        # Heads that got a REAL declared identity pushed this pass (loaded +
        # override or RFID). After the pushes run we sync each into the stock
        # auto-replenish backup so a post-load override is what replenish
        # matches (see the block after the push loop).
        backup_heads = []
        for head in range(4):
            # Heads that don't use the ACE (manual = hand-loaded, feeder = stock
            # side feeder) have no ACE source. Don't touch their display filament
            # info at all (stock behaviour): without this they fall into the
            # empty-head branch below and get the ACTIVE ACE's slot data pushed
            # onto them - so a feeder slot wrongly shows the ACE's RFID on the
            # touchscreen. Only the ACE head(s) get ACE-slot identity.
            if not self.head_uses_ace(head):
                logging.info(
                    '[multiACE] _push_rfid_info: head %d - non-MMU '
                    '(manual/feeder), leaving display filament info untouched'
                    % head)
                continue
            source = self._head_source.get(head)
            if source:

                src_ace = int(source.get('ace_index', 0))
                src_slot = int(source.get('slot', 0))
                ace_info = self._info_per_ace.get(src_ace, {}) or {}
                slots = ace_info.get('slots', []) or []
                slot = slots[src_slot] if src_slot < len(slots) else {}
                override = self._override_for(src_ace, src_slot)
                fallback_type = source.get('type') or slot.get('type', 'PLA')
                fallback_color = source.get('color') or self.rgb2hex(*slot.get('color', (0, 0, 0)))
                fallback_brand = source.get('brand') or slot.get('brand', 'Generic')
                fallback_subtype = source.get('subtype', '') or slot.get('subtype', '')
                logging.info(
                    '[multiACE] _push_rfid_info: head %d - loaded from MMU %d / slot %d, '
                    'pushing %s' % (head, src_ace, src_slot,
                                    'override' if override is not None else 'source'))

                if override is not None:
                    push_type = override.get('material') or fallback_type
                    push_color = self._override_color_to_rgba(override.get('color', ''))
                    push_brand = override.get('brand') or fallback_brand
                    push_subtype = override.get('subtype', '') or ''
                    self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
                    lines.append(
                        'SET_PRINT_FILAMENT_CONFIG '
                        'CONFIG_EXTRUDER=%d '
                        'FILAMENT_TYPE="%s" '
                        'FILAMENT_COLOR_RGBA=%s '
                        'VENDOR="%s" '
                        'FILAMENT_SUBTYPE="%s"' % (
                            head, push_type, push_color, push_brand, push_subtype))
                    backup_heads.append(head)
                else:
                    # SOLL precedence: Override > RFID > "?" (empty). RFID/known
                    # identity = source.type (captured at load) or a live
                    # rfid==2 slot. If neither, the head has no known identity
                    # -> clear the per-head filament config so the display shows
                    # "?" (a loaded head with an empty type renders as "?", not
                    # the stale slicer/job colour that caused "wrong colours for
                    # loaded slots"). Loaded heads keep their captured identity
                    # because source.type is set at load for RFID/override loads.
                    rfid_type = source.get('type') or (
                        slot.get('type', '') if slot.get('rfid') == 2 else '')
                    if not rfid_type:
                        logging.info(
                            '[multiACE] _push_rfid_info: head %d - loaded, no '
                            'override/RFID; clearing display ("?")' % head)
                        self._expect_ptc_push(head, '', '000000FF', '', '')
                        lines.append(
                            'SET_PRINT_FILAMENT_CONFIG '
                            'CONFIG_EXTRUDER=%d '
                            'FILAMENT_TYPE="" '
                            'FILAMENT_COLOR_RGBA=000000FF '
                            'VENDOR="" '
                            'FILAMENT_SUBTYPE=""' % head)
                    else:
                        self._expect_ptc_push(head, rfid_type, fallback_color, fallback_brand, fallback_subtype)
                        lines.append(
                            'SET_PRINT_FILAMENT_CONFIG '
                            'CONFIG_EXTRUDER=%d '
                            'FILAMENT_TYPE="%s" '
                            'FILAMENT_COLOR_RGBA=%s '
                            'VENDOR="%s" '
                            'FILAMENT_SUBTYPE="%s"' % (
                                head, rfid_type, fallback_color, fallback_brand, fallback_subtype))
                        backup_heads.append(head)
            else:

                disp_ace, disp_slot = self._unloaded_display_slot(head)

                empty_override = self._override_for(disp_ace, disp_slot)
                if empty_override is not None:
                    ace_info = self._info_per_ace.get(disp_ace, {}) or {}
                    aslots = ace_info.get('slots', []) or []
                    aslot = aslots[disp_slot] if disp_slot < len(aslots) else {}
                    push_type = empty_override.get('material') or aslot.get('type', 'PLA')
                    push_color = self._override_color_to_rgba(empty_override.get('color', ''))
                    push_brand = empty_override.get('brand') or aslot.get('brand', 'Generic')
                    push_subtype = empty_override.get('subtype', '') or ''
                    logging.info(
                        '[multiACE] _push_rfid_info: head %d - unloaded, '
                        'pushing override (MMU %d / slot %d)' % (
                            head, disp_ace, disp_slot))
                    self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
                    lines.append(
                        'SET_PRINT_FILAMENT_CONFIG '
                        'CONFIG_EXTRUDER=%d '
                        'FILAMENT_TYPE="%s" '
                        'FILAMENT_COLOR_RGBA=%s '
                        'VENDOR="%s" '
                        'FILAMENT_SUBTYPE="%s"' % (
                            head, push_type, push_color, push_brand, push_subtype))
                    continue
                # No override: show the head's wired ACE slot RFID identity
                # instead of
                # blank-clearing and relying on the heartbeat heal loop to
                # restore it ~1s later (caused a blank->fill flicker /
                # incomplete display on every ACE switch). Multi topology: slot N
                # feeds head N (disp_ace/disp_slot == active/head). Head mode:
                # the wired ACE's first loaded slot (resolved above).
                ace_info = self._info_per_ace.get(disp_ace, {}) or {}
                aslots = ace_info.get('slots', []) or []
                aslot = aslots[disp_slot] if disp_slot < len(aslots) else {}
                if aslot.get('rfid') == 2:
                    push_type = aslot.get('type', 'PLA')
                    push_color = self.rgb2hex(*aslot.get('color', (0, 0, 0)))
                    push_brand = aslot.get('brand', 'Generic')
                    push_subtype = aslot.get('subtype', '')
                    logging.info(
                        '[multiACE] _push_rfid_info: head %d - unloaded, '
                        'pushing MMU %d slot %d RFID' % (head, disp_ace, disp_slot))
                    self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
                    lines.append(
                        'SET_PRINT_FILAMENT_CONFIG '
                        'CONFIG_EXTRUDER=%d '
                        'FILAMENT_TYPE="%s" '
                        'FILAMENT_COLOR_RGBA=%s '
                        'VENDOR="%s" '
                        'FILAMENT_SUBTYPE="%s"' % (
                            head, push_type, push_color, push_brand, push_subtype))
                    continue
                logging.info(
                    '[multiACE] _push_rfid_info: head %d - empty, clearing display' % head)
                self._expect_ptc_push(head, '', '000000FF', '', '')
                lines.append(
                    'SET_PRINT_FILAMENT_CONFIG '
                    'CONFIG_EXTRUDER=%d '
                    'FILAMENT_TYPE="" '
                    'FILAMENT_COLOR_RGBA=000000FF '
                    'VENDOR="" '
                    'FILAMENT_SUBTYPE=""' % head)
        # One line at a time, not one script. As a batch a single refused
        # head aborted the ones after it AND failed the whole command - a
        # single genuine spool on one head would turn every display refresh
        # red in the web queue and leave the other heads unpushed. Each head
        # now stands on its own; a refusal is logged and the
        # rest still go out. The identity_priority=multiace force above
        # removes the only refusal we know of, so this is the backstop for
        # the ones we do not.
        for _ln in lines:
            try:
                self.gcode.run_script_from_command(_ln)
            except Exception as pe:
                logging.info(
                    '[multiACE] _push_rfid_info: one head refused, '
                    'continuing with the rest: %s' % pe)
        # Sync the DECLARED identity (override > RFID) we just pushed for each
        # LOADED head into the stock auto-replenish backup. filament_info_backup
        # is otherwise written ONLY on sensor insert/remove
        # (filament_switch_sensor_ace.py:158), so a post-load OVERRIDE never
        # reaches it -> stock auto-replenish matches the STALE insert-time RFID
        # identity, not the user's declared one, and rejects a same-declared
        # twin on colour. An override IS the declared truth; replenish must
        # match it. The stock
        # override IS the declared truth; replenish must match it. The stock
        # guard (print_task_config.py:325 copies only a non-empty, non-'NONE'
        # vendor) still applies, and backup_heads excludes the empty-"?" branch,
        # so an unidentified head never freezes a bogus backup. Runs after the
        # pushes so print_task_config already holds them.
        if backup_heads:
            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc is not None:
                for _bh in backup_heads:
                    try:
                        ptc.backup_filament_info(_bh)
                    except Exception as e:
                        logging.info(
                            '[multiACE] replenish backup sync failed head '
                            '%d: %s' % (_bh, e))

    def _display_head_for_slot(self, ace_idx, slot_idx, is_active):
        # For the heartbeat's unloaded-head display fallbacks: which HEAD (if
        # any) should mirror ACE ace_idx / slot slot_idx. Multi/normal: slot==head
        # on the ACTIVE ACE (head == slot_idx when is_active) - byte-identical to
        # the old behaviour. Head mode: the head WIRED to ace_idx, but only when
        # slot_idx is that head's shown (first-loaded) slot, so an unloaded ACE
        # head mirrors ITS OWN ACE, not whichever unit is globally active (the
        # "shows right fila, overwritten by active slot==head" bug). None = no
        # head should mirror this slot.
        if getattr(self, '_ace_mode', 'multi') == 'head':
            h = self._head_for_ace(ace_idx)
            if h is None or not self.head_uses_ace(h):
                return None
            show = self._first_loaded_slot_for_ace(ace_idx)
            show = show if show is not None else h
            return h if slot_idx == show else None
        if is_active and slot_idx < 4 and self.head_uses_ace(slot_idx):
            return slot_idx
        return None

    def _clear_filament_display(self, head):
        try:
            self._expect_ptc_push(head, '', '00000000', '', '')
            self.gcode.run_script_from_command(
                'SET_PRINT_FILAMENT_CONFIG '
                'CONFIG_EXTRUDER=%d '
                'FILAMENT_TYPE="" '
                'FILAMENT_COLOR_RGBA=00000000 '
                'VENDOR="" '
                'FILAMENT_SUBTYPE=""' % head)
        except Exception:
            pass
