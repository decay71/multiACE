"""Settings cluster of MultiAce: the message catalog (i18n load, `_t`,
MULTIACE_SET_LANGUAGE), display index helpers, and the settings
write-through (config line surgery, atomic write, legacy save-variable
migration, `_wt_persist`) with the ACE_SET_* setters that persist through it.

Mixin of MultiAce, imported by ace.py. ``self`` is the MultiAce instance;
state is created in ace.py.__init__. Every definition here is byte-identical
to its former place in ace.py (tests/units/tools/move_check.py).
"""
import json
import logging
import os

from .ace_spool import SPOOLLINK_RESOLVE_METHOD

def _load_i18n_catalog(i18n_dir, lang):
    """Read <i18n_dir>/<lang>.json overlaid on en.json. Returns a dict
    (possibly empty if the i18n dir is missing) - caller falls back to
    the literal key when a string is not found."""
    out = {}
    try:
        en_path = os.path.join(i18n_dir, 'en.json')
        if os.path.isfile(en_path):
            with open(en_path, 'r', encoding='utf-8') as f:
                out = json.load(f)
    except Exception:
        out = {}
    if lang and lang != 'en':
        try:
            lp = os.path.join(i18n_dir, lang + '.json')
            if os.path.isfile(lp):
                with open(lp, 'r', encoding='utf-8') as f:
                    overlay = json.load(f)

                def _merge(base, ov):
                    for k, v in ov.items():
                        if isinstance(v, dict) and isinstance(base.get(k), dict):
                            _merge(base[k], v)
                        else:
                            base[k] = v
                _merge(out, overlay)
        except Exception:
            pass
    return out


def _ace_cfg_edit_option(text, option, value_str, section='ace'):
    """Pure line surgery for the settings write-through: set
    `option: value_str` inside the given [section] of
    the config text. Prefers the ACTIVE line, then an out-commented
    template line (uncomments it in place), else inserts directly after
    the section header. Returns (new_text, None) on success or
    (None, reason) - string matching only, ace.py imports no re."""
    lines = text.split('\n')
    header = '[%s]' % section
    sec_start = None
    sec_end = len(lines)
    for i, l in enumerate(lines):
        s = l.strip()
        if sec_start is None:
            if s == header:
                sec_start = i
            continue
        if s.startswith('[') and s.endswith(']'):
            sec_end = i
            break
    if sec_start is None:
        return None, 'no %s section in the file' % header

    def _opt_match(s):
        # s = candidate already stripped of leading '#'/whitespace; the
        # option name must be followed by ':' or '=' (guards against a
        # longer option sharing the prefix).
        if not s.startswith(option):
            return False
        rest = s[len(option):].lstrip()
        return rest[:1] in (':', '=')

    new_line = '%s: %s' % (option, value_str)
    hit = None
    for i in range(sec_start + 1, sec_end):
        s = lines[i].strip()
        if s.startswith('#'):
            continue
        if _opt_match(s):
            hit = i
            break
    if hit is None:
        for i in range(sec_start + 1, sec_end):
            s = lines[i].strip()
            if not s.startswith('#'):
                continue
            if _opt_match(s.lstrip('#').lstrip()):
                hit = i
                break
    if hit is not None:
        lines[hit] = new_line
    else:
        lines.insert(sec_start + 1, new_line)
    return '\n'.join(lines), None


def _wt_fmt_bool(v):
    return 'true' if v else 'false'


def _wt_fmt_str(v):
    return str(v).strip()


def _wt_fmt_heads(v):
    # ace__bg_heads stores a python int list; the config line is the
    # comma form the [ace_bg_swap] heads parser reads ('' = none).
    try:
        return ','.join(str(int(h)) for h in sorted(v))
    except (TypeError, ValueError):
        return str(v)


class SettingsMixin:
    def _t(self, key, **params):
        """
        Translate a dotted key against the loaded catalog. Returns the
        formatted string, or the key itself when not found (so log lines
        always carry SOMETHING readable). Index-style params are NOT
        auto-shifted here - caller passes display-ready values via
        self._disp(idx) when appropriate.
        """
        v = getattr(self, '_i18n', None) or {}
        for p in key.split('.'):
            if not isinstance(v, dict):
                return key
            v = v.get(p)
            if v is None:
                return key
        if not isinstance(v, str):
            return key
        if not params:
            return v
        try:
            return v.format(**params)
        except Exception:
            return v

    def _reload_i18n_catalog(self):
        """(Re)load self._i18n for the current self._language. Used at startup
        and live by MULTIACE_SET_LANGUAGE."""
        candidates = [self._i18n_primary] + self._i18n_fallbacks
        i18n_dir = next((path for path in candidates if os.path.isdir(path)),
                        self._i18n_primary)
        try:
            self._i18n = _load_i18n_catalog(i18n_dir, self._language)
        except Exception as e:
            logging.info('[multiACE] i18n catalog load failed: %s' % e)
            self._i18n = {}

    cmd_MULTIACE_SET_LANGUAGE_help = (
        '[multiACE] Set message language (LANG=<code>), live reload + persist')
    def cmd_MULTIACE_SET_LANGUAGE(self, gcmd):
        lang = gcmd.get('LANG').strip()
        if not lang:
            raise self._ace_error(gcmd, 'LANG is required', code=200)
        self._language = lang
        self._reload_i18n_catalog()
        sfx = self._wt_persist(gcmd, 'language', _wt_fmt_str(lang),
                               'ace__language',
                               shadow_attr='_language_cfg',
                               shadow_val=lang)
        self.log_always('[multiACE] language set to %s%s' % (lang, sfx))

    def _raw(self, idx):
        """Internal (0-based) index for internal log lines; same None/int
        handling as _disp, only without the display offset."""
        if idx is None:
            return '-'
        try:
            return int(idx)
        except (TypeError, ValueError):
            return idx

    def _disp(self, idx):
        """Apply display_index_base offset for user-facing messages."""
        if idx is None:
            return '-'
        try:
            return int(idx) + getattr(self, '_display_index_base', 0)
        except (TypeError, ValueError):
            return idx

    def _cfg_write_ace_option(self, option, value_str, section='ace'):
        """Settings write-through: persist a toggle
        by editing its one visible line in the canonical ace.cfg -
        atomic (tmp + fsync + os.replace), .bak of the prior content
        alongside. Returns None on success, else a short reason string;
        the caller reports it (the RAM value stays applied either way -
        the toggle worked, it just will not survive a restart).
        Concurrent writers (Fluidd editor open in a second tab) are
        last-writer-wins by design."""
        path = self.ACE_CFG_PATH
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError as e:
            return 'cannot read %s (%s)' % (path, e)
        new_text, err = _ace_cfg_edit_option(text, option, value_str,
                                             section=section)
        if err is not None:
            return '%s (%s) - set "%s: %s" there by hand' % (
                err, path, option, value_str)
        if new_text == text:
            return None
        if ('%s: %s' % (option, value_str)) not in new_text:
            return 'internal: edited text lost the option line (not written)'
        try:
            with open(path + '.bak', 'w', encoding='utf-8') as f:
                f.write(text)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(new_text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            return 'cannot write %s (%s)' % (path, e)
        return None

    # Settings write-through migration table:
    # (legacy save var, section, option line, file formatter,
    #  shadow attr or None, python converter for the shadow). The mode/
    # wiring family (ace__mode, ace__ace_head, ace__head_*) and the
    # auto_dry per-unit dict stay save_variables BY DESIGN (documented
    # exceptions - the S58 boot script / per-unit shapes), and pure state
    # (head_source, auto_dry_running, ...) is never config.
    _WT_MIGRATIONS = (
        ('ace__purge_matrix', 'ace', 'purge_matrix',
         _wt_fmt_bool, '_purge_matrix_cfg', bool),
        ('ace__pickup_cleaning', 'ace', 'pickup_cleaning',
         _wt_fmt_bool, '_pickup_cleaning_cfg', bool),
        ('ace__airprint_detection', 'ace', 'resistance_pause',
         _wt_fmt_bool, '_airprint_cfg', bool),
        ('ace__quad_replenish', 'ace', 'quad_replenish',
         _wt_fmt_bool, '_quad_replenish_cfg', bool),
        ('ace__quad_first', 'ace', 'quad_first',
         _wt_fmt_bool, '_quad_first_cfg', bool),
        ('ace__confirm_commands', 'ace', 'confirm_commands',
         _wt_fmt_bool, '_confirm_commands_cfg', bool),
        ('ace__language', 'ace', 'language',
         _wt_fmt_str, '_language_cfg', str),
        ('ace__spoolman_url', 'ace', 'spoolman_url',
         _wt_fmt_str, '_spoolman_url_cfg', str),
        ('ace__spoolman_auto', 'ace', 'spoolman_auto_sync',
         _wt_fmt_bool, '_spoolman_auto_cfg', bool),
        ('ace__bg_heads', 'ace_bg_swap', 'heads',
         _wt_fmt_heads, None, None),
    )

    def _migrate_settings_savevars(self, eventtime=None):
        """One-shot at klippy:ready: fold every legacy settings save
        variable into its config line and delete it (write-through).
        Fail-open PER ENTRY - a variable
        that cannot be written stays, the old precedence then yields the
        same effective value next boot and the migration retries."""
        if not self.save_variables:
            return
        for (var, section, option, fmt, shadow, conv) in self._WT_MIGRATIONS:
            try:
                if var not in self.save_variables.allVariables:
                    continue
                if (section == 'ace_bg_swap'
                        and self.printer.lookup_object('ace_bg_swap',
                                                       None) is None):
                    # Without the bg module the variable is inert and the
                    # target section may not exist in the cfg - leave it
                    # for a build that carries the module.
                    continue
                val = self.save_variables.allVariables[var]
                err = self._cfg_write_ace_option(option, fmt(val),
                                                 section=section)
                if err is not None:
                    logging.warning(
                        '[multiACE] settings migration: %s not written '
                        '(%s) - keeping the save variable' % (option, err))
                    continue
                if shadow is not None:
                    setattr(self, shadow, conv(val))
                self.delete_variable(var, write=True)
                logging.info(
                    '[multiACE] settings migration: %s folded into the '
                    'config line ([%s] %s: %s) and deleted'
                    % (var, section, option, fmt(val)))
            except Exception as e:
                logging.warning(
                    '[multiACE] settings migration %s failed (kept save '
                    'var): %s' % (var, e))

    def _wt_persist(self, gcmd, option, value_str, legacy_var,
                    section='ace', shadow_attr=None, shadow_val=None):
        """Shared write-through tail for the settings setters: honors
        PERSIST=0 (RAM only, config value returns at restart), writes
        the config line, updates the shadow and deletes the legacy save
        variable on success. Returns the suffix for the setter's user
        message ('' = persisted clean)."""
        persist = gcmd.get_int('PERSIST', 1, minval=0, maxval=1)
        if not persist:
            return ' (volatile - until restart)'
        err = self._cfg_write_ace_option(option, value_str,
                                         section=section)
        if err is not None:
            return (' - WARNING: config write failed (%s), applied for '
                    'this session only' % err)
        if shadow_attr is not None:
            setattr(self, shadow_attr, shadow_val)
        try:
            if (self.save_variables and legacy_var
                    and legacy_var in self.save_variables.allVariables):
                self.delete_variable(legacy_var, write=True)
        except Exception as e:
            logging.info('[multiACE] legacy %s delete failed: %s'
                         % (legacy_var, e))
        return ''

    cmd_ACE_SET_PICKUP_CLEANING_help = (
        '[multiACE] Enable/disable Pickup-Cleaning (ENABLE=0|1). Live + '
        'write-through (writes the pickup_cleaning config line; '
        'PERSIST=0 = until restart).')

    def cmd_ACE_SET_PICKUP_CLEANING(self, gcmd):
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        self._pickup_cleaning = enable
        sfx = self._wt_persist(gcmd, 'pickup_cleaning',
                               _wt_fmt_bool(enable), 'ace__pickup_cleaning',
                               shadow_attr='_pickup_cleaning_cfg',
                               shadow_val=enable)
        self.log_always('[multiACE] Pickup-Cleaning %s%s'
                        % ('ON' if enable else 'OFF', sfx))

    cmd_ACE_SET_PREFLIGHT_COPIES_help = (
        '[multiACE] Multifilament-Optimierung: max targets per slicer colour '
        'in the preflight (MAX=1..4; 1 = one slot per colour) and/or '
        'STRICT=0|1 (1 = a copy needs the exact hex colour, 0 = the colour '
        'name tiers count too). Live + write-through (writes the '
        'preflight_max_copies / preflight_copies_strict config lines; '
        'PERSIST=0 = until restart).')

    def cmd_ACE_SET_PREFLIGHT_COPIES(self, gcmd):
        n = gcmd.get_int('MAX', None)
        strict = gcmd.get_int('STRICT', None)
        if n is None and strict is None:
            raise self._ace_error(gcmd, '[multiACE] MAX or STRICT required',
                                  200)
        if n is not None:
            if n < 1 or n > 4:
                raise self._ace_error(gcmd, '[multiACE] MAX must be 1..4',
                                      200)
            self.preflight_max_copies = n
            sfx = self._wt_persist(gcmd, 'preflight_max_copies', str(n),
                                   None,
                                   shadow_attr='_preflight_max_copies_cfg',
                                   shadow_val=n)
            self.log_always('[multiACE] Preflight max copies per colour: '
                            '%d%s' % (n, sfx))
        if strict is not None:
            if strict not in (0, 1):
                raise self._ace_error(gcmd, '[multiACE] STRICT must be 0|1',
                                      200)
            st = bool(strict)
            self.preflight_copies_strict = st
            sfx = self._wt_persist(gcmd, 'preflight_copies_strict',
                                   _wt_fmt_bool(st), None,
                                   shadow_attr='_preflight_copies_strict_cfg',
                                   shadow_val=st)
            self.log_always('[multiACE] Preflight copies strict colour '
                            'match: %s%s' % ('ON' if st else 'OFF', sfx))

    cmd_ACE_SET_SPOOLMAN_help = (
        '[multiACE] Spoolman connection: ACE_SET_SPOOLMAN [URL=http://host:7912] '
        '[MODE=local|spoolman|spoollink] [AUTO=0|1]. URL= with no value clears '
        'it (mode falls back to local). MODE picks the spool world; spoolman/'
        'spoollink need a URL. AUTO syncs after a finished print. Live + '
        'write-through (spoolman_url/spool_mode/spoolman_auto_sync config '
        'lines; PERSIST=0 = until restart).')

    def cmd_ACE_SET_SPOOLMAN(self, gcmd):
        url = gcmd.get('URL', None)
        mode = gcmd.get('MODE', None)
        if mode is not None:
            mode = mode.strip().lower()
            if mode not in ('local', 'spoolman', 'spoollink'):
                raise self._ace_error(
                    gcmd, "MODE must be local, spoolman or spoollink "
                          "(got %r)" % mode, code=200)
        _was_spoolman_world = (self.spool_mode != 'local')
        _was_mode = self.spool_mode
        if url is not None:
            # '#' terminates gcode arguments, so a URL fragment can never
            # arrive here anyway - strip a trailing slash so the backend can
            # concatenate paths without doubling it.
            self.spoolman_url = url.strip().rstrip('/')
            if not self.spoolman_url and self.spool_mode != 'local' \
                    and mode is None:
                # URL cleared: a Spoolman world without an address cannot
                # work - fall back instead of stranding the mode.
                mode = 'local'
                self.log_always('[multiACE] Spoolman URL cleared - '
                                'spool mode falls back to local')
        if mode is not None:
            if mode != 'local' and not (self.spoolman_url or '').strip():
                raise self._ace_error(
                    gcmd, "MODE=%s needs a Spoolman URL (set URL= first "
                          "or in the same command)" % mode, code=200)
            # The spoollink world hands counting to the paxx resolver
            # (book_spool_use returns early, used_mm freezes, the /use push
            # no-ops). Without that agent - stock firmware, or paxx with the
            # Spoolman integration off - NOBODY would count. The web already
            # disables the option for this reason; refuse it here too, so the
            # gcode path cannot walk into the same hole. The check sits at the
            # moment of CHOICE only: _spoollink_active deliberately has no
            # agent gate, a briefly missing agent must not move counting
            # back to us behind the user's back.
            if mode == 'spoollink' and not self._spoollink_agent_present():
                raise self._ace_error(
                    gcmd, "MODE=spoollink needs the paxx SpoolLink resolver, "
                          "and '%s' is not registered on this printer. In that "
                          "world SpoolLink counts and multiACE does not, so "
                          "nothing would book consumption. Use MODE=spoolman "
                          "(we count) or enable paxx's Spoolman integration."
                          % SPOOLLINK_RESOLVE_METHOD, code=200)
            self.spool_mode = mode
        # Crossing WORLDS (local <-> spoolman/spoollink) drops ALL spool
        # bindings. LOCAL entries stay (their
        # weight is the way back); Spoolman-backed entries leave WITH
        # their last binding - they are cache, the spool lives in Spoolman
        # (_spool_drop_if_unbound_sm; the boot/print-end/manual pushes
        # have reported their consumption, a residue is logged there).
        # spoolman <-> spoollink is NOT a world crossing (same table, same
        # bindings - only who sets and counts changes), and a URL edit
        # within the same world (host rename) clears nothing either.
        _flip = ((self.spool_mode != 'local') != _was_spoolman_world)
        if _flip and self._spool_binding:
            _n = len(self._spool_binding)
            _sids = set(self._spool_binding.values())
            self._spool_binding.clear()
            _dropped = sum(1 for _sid in _sids
                           if self._spool_drop_if_unbound_sm(
                               _sid, 'world switch', force=True))
            self._save_spool_db(backup=True)
            self.log_always('[multiACE] Spoolman mode switch: cleared '
                            '%d spool binding(s), dropped %d cached '
                            'Spoolman entr%s' % (_n, _dropped,
                            'y' if _dropped == 1 else 'ies'))
        if _flip:
            # AFTER the mode assignment - the tag matcher gates on the
            # ACTIVE world. Into LOCAL this restores the local rows'
            # bindings immediately; into SPOOLMAN it binds whatever SM
            # rows already exist (usually none - the web follows up
            # with the tag adopt sweep, which creates the rest).
            self._spool_rebind_from_tag_cache('world switch')
        # Leaving SpoolLink: the resolver-set spool ids PERSIST in
        # print_task_config, SpoolLink keeps syncing Moonraker's active
        # spool from them and keeps BOOKING - in spoolman mode that runs
        # parallel to our own /use push = every gram counted twice. Clear
        # every head SpoolLink still claims; its
        # active-spool sync then drops to none (the HW-proven unload-clear
        # chain), and our own identity push repopulates type/colour on the
        # next heal tick. Fail-open per head - a clear that dies must not
        # kill the mode switch.
        if _was_mode == 'spoollink' and self.spool_mode != 'spoollink':
            _cleared = []
            for _h in range(4):
                try:
                    if (self._ptc_official_for(_h)
                            or self._ptc_spool_id_for(_h) > 0):
                        if self._spoollink_clear(_h):
                            _cleared.append(_h)
                except Exception as _e:
                    logging.info('[multiACE] [spoollink] exit-clear head '
                                 '%d failed: %s' % (_h, _e))
            try:
                self._spoollink_sent.clear()
            except Exception:
                pass
            if _cleared:
                _msg = ('[multiACE] [spoollink] mode left - cleared spool '
                        'id(s) on head(s) %s so SpoolLink stops booking'
                        % ', '.join(str(self._disp(h)) for h in _cleared))
                self.log_always(_msg)
                logging.info(_msg)
        # ENTERING SpoolLink: the mirror seed. Nothing
        # else re-sets the ids on re-entry - _spoollink_verify only tends
        # entries that EXIST in _spoollink_sent (the exit cleared them),
        # and the identity-push path stays silent because the SM-mode heal
        # already pushed the identity (without an id), so nothing needs
        # healing. Send the resolver once per SM-bound loaded head; the
        # send stamps _spoollink_sent, so the bounded verify takes over
        # from there. Fail-open per head.
        if _was_mode != 'spoollink' and self.spool_mode == 'spoollink':
            _seeded = []
            for _h in range(4):
                try:
                    # _spoollink_smid_for covers BOTH worlds: loaded ACE
                    # heads via head_source->slot binding, feeder/manual
                    # heads via their h<n> binding (no head_source by
                    # design). The send's feeder precedence keeps a
                    # reader-set id untouched.
                    _smid = self._spoollink_smid_for(_h)
                    if _smid and int(_smid) > 0:
                        if self._spoollink_send(_h, int(_smid),
                                                'mode enter'):
                            _seeded.append(_h)
                except Exception as _e:
                    logging.info('[multiACE] [spoollink] enter-seed head '
                                 '%d failed: %s' % (_h, _e))
            if _seeded:
                _msg = ('[multiACE] [spoollink] mode entered - resolver '
                        'seeded for head(s) %s'
                        % ', '.join(str(self._disp(h)) for h in _seeded))
                self.log_always(_msg)
                logging.info(_msg)
        auto = gcmd.get_int('AUTO', None, minval=0, maxval=1)
        if auto is not None:
            self.spoolman_auto = bool(auto)
        sfx = ''
        if mode is not None or _flip:
            sfx = self._wt_persist(gcmd, 'spool_mode',
                                   _wt_fmt_str(self.spool_mode),
                                   None,
                                   shadow_attr='_spool_mode_cfg',
                                   shadow_val=self.spool_mode)
        if url is not None:
            sfx3 = self._wt_persist(gcmd, 'spoolman_url',
                                    _wt_fmt_str(self.spoolman_url),
                                    'ace__spoolman_url',
                                    shadow_attr='_spoolman_url_cfg',
                                    shadow_val=self.spoolman_url)
            if sfx3 != sfx:
                sfx += sfx3
        if auto is not None:
            sfx2 = self._wt_persist(gcmd, 'spoolman_auto_sync',
                                    _wt_fmt_bool(self.spoolman_auto),
                                    'ace__spoolman_auto',
                                    shadow_attr='_spoolman_auto_cfg',
                                    shadow_val=self.spoolman_auto)
            if sfx2 != sfx:
                sfx += sfx2
        self.log_always('[multiACE] Spoolman: %s, mode %s, auto-sync %s%s'
                        % (self.spoolman_url or 'off', self.spool_mode,
                           'ON' if self.spoolman_auto else 'OFF', sfx))

    cmd_ACE_SET_CONFIRM_COMMANDS_help = (
        '[multiACE] Web UI asks back before a load/unload (ENABLE=0|1). '
        'Live + write-through (writes the confirm_commands config line; '
        'PERSIST=0 = until restart). UI-only - the engine is '
        'unaffected, and gcode/display commands are never gated by it.')

    def cmd_ACE_SET_CONFIRM_COMMANDS(self, gcmd):
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        self._confirm_commands = enable
        sfx = self._wt_persist(gcmd, 'confirm_commands',
                               _wt_fmt_bool(enable), 'ace__confirm_commands',
                               shadow_attr='_confirm_commands_cfg',
                               shadow_val=enable)
        self.log_always('[multiACE] Confirm commands %s%s'
                        % ('ON' if enable else 'OFF', sfx))

    cmd_ACE_SET_PA_SYNC_help = (
        '[multiACE] Enable/disable PA sync (ENABLE=0|1): ON auto-captures '
        'the stock flow routine onto the bound spool AND auto-applies the '
        'stored per-spool value on toolchange; OFF leaves pressure advance '
        'to the slicer/manual value. ACE_PA_CALIBRATE stays usable either '
        'way. Live + write-through (writes the pa_sync config line; '
        'PERSIST=0 = until restart).')

    def cmd_ACE_SET_PA_SYNC(self, gcmd):
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        self.pa_sync = enable
        sfx = self._wt_persist(gcmd, 'pa_sync', _wt_fmt_bool(enable), None,
                               shadow_attr='_pa_sync_cfg', shadow_val=enable)
        self.log_always('[multiACE] PA sync %s%s'
                        % ('ON' if enable else 'OFF', sfx))

    def cmd_ACE_SET_TAG_WRITE(self, gcmd):
        """Defaults for ACE_TAG_WRITE / the picker's write button. Each
        given field is applied live and written through to its config
        line (PERSIST=0 = RAM only)."""
        fmt = gcmd.get('FORMAT', None)
        uid = gcmd.get_int('UID_SKU', None, minval=0, maxval=1)
        if fmt is None and uid is None:
            self.log_always('[multiACE] tag write defaults: format=%s '
                            'uid_sku=%s' % (self.tag_write_format,
                                            self.tag_write_uid_sku))
            return
        out = []
        if fmt is not None:
            fmt = (fmt or '').strip().lower()
            if fmt not in ('openspool', 'anycubic'):
                raise self._ace_error(gcmd, 'FORMAT must be openspool or '
                                      'anycubic', code=200)
            self.tag_write_format = fmt
            sfx = self._wt_persist(gcmd, 'tag_write_format',
                                   _wt_fmt_str(fmt), None,
                                   shadow_attr='_tag_write_format_cfg',
                                   shadow_val=fmt)
            out.append('format=%s%s' % (fmt, sfx))
        if uid is not None:
            self.tag_write_uid_sku = bool(uid)
            sfx = self._wt_persist(gcmd, 'tag_write_uid_sku',
                                   _wt_fmt_bool(bool(uid)), None,
                                   shadow_attr='_tag_write_uid_sku_cfg',
                                   shadow_val=bool(uid))
            out.append('uid_sku=%s%s' % ('ON' if uid else 'OFF', sfx))
        self.log_always('[multiACE] tag write defaults: %s' % ', '.join(out))

    cmd_ACE_SET_AIRPRINT_DETECTION_help = (
        '[multiACE] Enable/disable Air-Print Detection (ENABLE=0|1): the '
        'resistance-watch pauses (per lane + per head) and the airlog chew '
        'detector. Live + write-through (writes the resistance_pause config '
        'line; PERSIST=0 = until restart). Enabling also starts '
        'the airlog sampler if it is not running.')

    def cmd_ACE_SET_AIRPRINT_DETECTION(self, gcmd):
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        self.resistance_pause = enable
        if enable and getattr(self, '_airlog_timer', None) is None:
            # The chew detector rides the airlog sampler - start it live
            # (same registration as the klippy:ready hook).
            self.airlog_enable = True
            self._airlog_state = None
            self._airlog_timer = self.reactor.register_timer(
                self._airlog_tick, self.reactor.NOW)
        # Disabling keeps the sampler running on purpose: it is log-only
        # baseline collection and losing it would cost data; only
        # the pauses are disarmed. Warnings stay on either way.
        # NAMING ASYMMETRY kept on purpose: the config line is
        # resistance_pause (renaming would halt existing configs), the
        # feature name is Air-Print Detection.
        sfx = self._wt_persist(gcmd, 'resistance_pause',
                               _wt_fmt_bool(enable),
                               'ace__airprint_detection',
                               shadow_attr='_airprint_cfg',
                               shadow_val=enable)
        self.log_always('[multiACE] Air-Print Detection %s%s'
                        % ('ON (pauses armed)'
                           if enable else 'OFF (warnings only)', sfx))

    cmd_ACE_SET_QUAD_REPLENISH_help = (
        '[multiACE] Toggle Quad Replenish (ENABLE=0|1): on a runout stock '
        'cannot serve, auto-reload the head from a slot with the same '
        'declared identity and RESUME. Live + write-through (writes the '
        'quad_replenish config line; PERSIST=0 = until restart). '
        'FIRST=0|1 sets the order vs stock: 1 (default) = drain the head '
        'OWN lane first (reaches more spools, keeps the sliced head layout, '
        'costs a reload per spool), 0 = stock head-switch first (instant, '
        'but rewrites the tool mapping for the rest of the print); '
        'write-through like ENABLE (quad_first config line).')

    def cmd_ACE_SET_QUAD_REPLENISH(self, gcmd):
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        first = gcmd.get_int('FIRST', None, minval=0, maxval=1)
        self.quad_replenish = enable
        sfx = self._wt_persist(gcmd, 'quad_replenish',
                               _wt_fmt_bool(enable), 'ace__quad_replenish',
                               shadow_attr='_quad_replenish_cfg',
                               shadow_val=enable)
        if first is not None:
            self.quad_first = bool(first)
            sfx2 = self._wt_persist(gcmd, 'quad_first',
                                    _wt_fmt_bool(bool(first)),
                                    'ace__quad_first',
                                    shadow_attr='_quad_first_cfg',
                                    shadow_val=bool(first))
            if sfx2 != sfx:
                sfx += sfx2
        self.log_always('[multiACE] Quad Replenish %s (order: %s)%s'
                        % (('ON (identity-auto slot failover)'
                            if enable else 'OFF'),
                           'quad first - drain the lane, then switch head'
                           if self.quad_first
                           else 'stock first - switch head, then reload',
                           sfx))
