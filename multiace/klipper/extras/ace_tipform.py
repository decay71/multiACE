# multiACE: per-material tip-forming tables ([ace_tipform] config section).
#
# The stock tip form is the INNER_FILAMENT_UNLOAD macro; its actual pull
# moves live in CONTROL_RETRACT_ACTION (u1_firmware fluidd.cfg:687) - one
# fixed choreography for everything. This module makes ONLY that move step
# configurable per material; the frame around it (discard position, heat,
# cool+fan, ooze scrape-off, nozzle clean, discard) stays stock either way
# (the U1 has NO cutter - "CUTOFF" is a wipe against the discard edge). The
# background swap engine consumes the same tables (its built-in
# COLD_PULL_NORMAL/SOFT constants are the fallback).
#
# Section keys:
#   mode: stock | custom     'stock' (default) = the unchanged INNER macro.
#   default: <table>         fallback table for every material
#   soft: <table>            fallback for soft/flexible filament (SOFT=1)
#   <material>: <table>      per-material, key = filament type lowercased
#                            (pla, petg, abs, ...; free-form, e.g. petg_basf
#                            works if you declare that type on the slot)
#   <vendor>_<material>: <table>   per-vendor+material (optional axis). The
#                            vendor is the slot's declared BRAND lowercased,
#                            internal spaces -> '_' (e.g. 'prusament_pla',
#                            'prusa_research_petg'). Matches only when the slot
#                            carries a real brand (RFID/override); a generic/
#                            empty/NONE vendor falls through to <material>.
#
# Table = comma-separated tokens (Prusa-style ramming/cooling expressible):
#   <mm>@<feedrate>   extruder move, negative = retract, feedrate in mm/min
#   pause:<ms>        dwell
#   temp:<C>          set nozzle target (no wait)
#   waittemp:<C>      set target and WAIT until reached, both directions
#                     (SkinnyDip pattern; 100-350. In the bg path this waits
#                     passively - no fan on a parked head - so it is SLOW
#                     there; bounded, proceeds with a warning on timeout)
#   fan:<0-255>       part fan (inline path only; the bg engine skips it -
#                     a parked head's fan is not addressable there)
#   unloadtemp:<C>    NOT a step: extracted at parse time as the table's
#                     UNLOAD-TEMP parameter - the whole unload (heat,
#                     INNER TEMP= / custom-frame heat, probe pre-warm)
#                     runs at this temp for the table's material/vendor.
#                     Without it the unload runs at the load temp.
#                     Range 175-350 (pull moves need >
#                     min_extrude_temp). A table may consist of ONLY
#                     this token = stock pull choreography at a custom
#                     temp (e.g. 'pla: unloadtemp:220').
#   loadtemp:<C>      NOT a step either: the LOAD-TEMP parameter - load
#                     heat, seat press, phase3 verify and the bg load
#                     half run at this temp instead of the DB load temp
#                     (chain: tipform -> DB get_load_temp -> 250). Set
#                     to the print temp, the whole swap runs at one
#                     temperature and needs no reheat. The stuck-tip
#                     RETRY still escalates to the DB load temp, the
#                     hotter pull is what frees a stuck tip. Range
#                     175-350; 'pla: unloadtemp:220, loadtemp:220' is a
#                     valid param-only table (comma-separated!).
#
# Example (PETG with ramming + cooling moves):
#   [ace_tipform]
#   mode: custom
#   default: 57@400, 3@1500, -27@2700, -5.5@40, -37.5@1500
#   petg: 30@400, 0.7@1600, 0.7@2400, -27@2700, pause:200, 2@600, -2@600, -35@1500
#
# Lookup order: <vendor>_<material> -> <material> -> soft (when SOFT) ->
# default -> None (= stock macro inline / built-in table in the bg engine).
# A table with a NET push (sum of moves > 0) is refused at startup - the tip
# must end OUT of the melt zone or the next load jams.

import logging
import os
import re

TOKEN_MOVE = 'move'
TOKEN_PAUSE = 'pause'
TOKEN_TEMP = 'temp'
TOKEN_WAITTEMP = 'waittemp'
TOKEN_FAN = 'fan'

MAX_MOVE_MM = 200.
MAX_FEEDRATE = 6000.
MAX_PAUSE_MS = 60000.
MAX_TEMP = 350.
MIN_WAITTEMP = 100.
# Klipper hard-refuses extruder moves below min_extrude_temp (170) - a
# move AFTER a waittemp below this margin fails as 'custom gcode error'
# mid-unload. Reject it at parse time instead.
MIN_MOVE_TEMP = 175.


def parse_table(raw):
    """Parse a table string into a token list:
    ('move', mm, feedrate) / ('pause', seconds) / ('temp', C) /
    ('waittemp', C) / ('fan', n).
    Raises ValueError with a user-readable message on any bad token."""
    tokens = []
    net = 0.
    unload_temp = None
    load_temp = None
    last_waittemp = None
    for part in str(raw).replace('\n', ',').split(','):
        part = part.strip()
        if not part:
            continue
        low = part.lower()
        if low.startswith('pause:'):
            ms = float(low.split(':', 1)[1])
            if not 0 <= ms <= MAX_PAUSE_MS:
                raise ValueError('pause out of range (0-%d ms): %r'
                                 % (int(MAX_PAUSE_MS), part))
            tokens.append((TOKEN_PAUSE, ms / 1000.))
        elif low.startswith('temp:'):
            c = float(low.split(':', 1)[1])
            if not 0 <= c <= MAX_TEMP:
                raise ValueError('temp out of range (0-%d C): %r'
                                 % (int(MAX_TEMP), part))
            tokens.append((TOKEN_TEMP, c))
        elif low.startswith('waittemp:'):
            # Set the target AND wait until the nozzle reaches it - BOTH
            # directions (the SkinnyDip pattern: drop for the toolchange,
            # dip cool, restore). Bounded below: waiting for a temperature
            # the hotend cannot passively reach would stall the unload.
            c = float(low.split(':', 1)[1])
            if not MIN_WAITTEMP <= c <= MAX_TEMP:
                raise ValueError('waittemp out of range (%d-%d C): %r'
                                 % (int(MIN_WAITTEMP), int(MAX_TEMP), part))
            tokens.append((TOKEN_WAITTEMP, c))
            last_waittemp = c
        elif low.startswith('fan:'):
            v = int(float(low.split(':', 1)[1]))
            if not 0 <= v <= 255:
                raise ValueError('fan out of range (0-255): %r' % part)
            tokens.append((TOKEN_FAN, v))
        elif low.startswith('unloadtemp:'):
            # Parameter, not a step (see the header note): extracted and
            # returned separately, never enters the token stream.
            c = float(low.split(':', 1)[1])
            if not MIN_MOVE_TEMP <= c <= MAX_TEMP:
                raise ValueError('unloadtemp out of range (%d-%d C): %r'
                                 % (int(MIN_MOVE_TEMP), int(MAX_TEMP), part))
            unload_temp = c
        elif low.startswith('loadtemp:'):
            # Parameter like unloadtemp: the load-side temp (load heat,
            # seat press, phase3, bg load half). Floor MIN_MOVE_TEMP -
            # phase3 extrudes.
            c = float(low.split(':', 1)[1])
            if not MIN_MOVE_TEMP <= c <= MAX_TEMP:
                raise ValueError('loadtemp out of range (%d-%d C): %r'
                                 % (int(MIN_MOVE_TEMP), int(MAX_TEMP), part))
            load_temp = c
        elif '@' in part:
            mm_s, f_s = part.split('@', 1)
            try:
                mm = float(mm_s)
                feedrate = float(f_s)
            except ValueError:
                raise ValueError('bad move token %r (expected '
                                 '<mm>@<feedrate>, comma-separated)' % part)
            if abs(mm) > MAX_MOVE_MM:
                raise ValueError('move too long (max %d mm): %r'
                                 % (int(MAX_MOVE_MM), part))
            if not 0 < feedrate <= MAX_FEEDRATE:
                raise ValueError('feedrate out of range (1-%d mm/min): %r'
                                 % (int(MAX_FEEDRATE), part))
            if last_waittemp is not None and last_waittemp < MIN_MOVE_TEMP:
                raise ValueError(
                    'move %r after waittemp:%d - Klipper refuses extruder '
                    'moves below min_extrude_temp (170); use waittemp >= '
                    '%d before further moves (true cold pulls below that '
                    'are not possible on the inline path)'
                    % (part, int(last_waittemp), int(MIN_MOVE_TEMP)))
            net += mm
            tokens.append((TOKEN_MOVE, mm, feedrate))
        else:
            raise ValueError("unrecognised token %r (expected mm@feedrate, "
                             "pause:ms, temp:C, waittemp:C, fan:0-255, "
                             "unloadtemp:C or loadtemp:C)"
                             % part)
    if tokens and not any(t[0] == TOKEN_MOVE for t in tokens):
        raise ValueError('table has no moves')
    if not tokens and unload_temp is None and load_temp is None:
        raise ValueError('table has no moves')
    if net > 0.:
        raise ValueError('table pushes a NET %+.1f mm - the tip must end '
                         'retracted out of the melt zone' % net)
    return tokens, unload_temp, load_temp


class AceTipform:
    def __init__(self, config):
        self.printer = config.get_printer()
        mode = config.get('mode', 'stock').strip().lower()
        if mode not in ('stock', 'custom'):
            raise config.error(
                "[ace_tipform] mode must be 'stock' or 'custom' (got %r)"
                % mode)
        self.tables = {}
        self.unload_temps = {}
        self.load_temps = {}
        # Every option in the section is a table (keys are free-form
        # material names); read them ALL so Klipper's every-option-must-be-
        # accessed check never halts on a user-added material.
        items = [(opt, config.get(opt))
                 for opt in config.get_prefix_options('') if opt != 'mode']
        self._apply(mode, items)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command(
            'ACE_TIPFORM_RELOAD', self.cmd_ACE_TIPFORM_RELOAD,
            desc='[multiACE] Re-read the [ace_tipform] section from ace.cfg '
                 'and apply it live (no Klipper restart). Usage: '
                 'ACE_TIPFORM_RELOAD [FILE=<path>]')

    def _apply(self, mode, items):
        """Parse + swap the table state. Shared by startup and the live
        reload so both paths validate and degrade identically (a bad table
        is dropped LOUDLY, never a halt: validation rules evolve and a
        previously-saved table can turn invalid on an update; the web
        editor is the strict gate). Built into locals first, so a
        reload that dies mid-parse cannot leave half-swapped state."""
        tables, unload_temps, load_temps = {}, {}, {}
        dropped = []
        for opt, raw in items:
            try:
                _toks, _utemp, _ltemp = parse_table(raw)
                if _toks:
                    tables[opt.strip().lower()] = _toks
                if _utemp is not None:
                    unload_temps[opt.strip().lower()] = _utemp
                if _ltemp is not None:
                    load_temps[opt.strip().lower()] = _ltemp
            except ValueError as e:
                dropped.append(opt)
                logging.error('[multiACE] ace_tipform: table %r DISABLED '
                              '(falls back to stock): %s' % (opt, e))
        self.mode = mode
        self.tables = tables
        self.unload_temps = unload_temps
        self.load_temps = load_temps
        logging.info('[multiACE] ace_tipform: mode=%s tables=%s '
                     'unload_temps=%s load_temps=%s'
                     % (self.mode, sorted(self.tables.keys()) or 'none',
                        sorted(self.unload_temps.keys()) or 'none',
                        sorted(self.load_temps.keys()) or 'none'))
        if (self.unload_temps or self.load_temps) and self.mode != 'custom':
            # A set-but-inert knob must be LOUD, never silently skipped.
            logging.error(
                "[multiACE] ace_tipform: unloadtemp/loadtemp set for %s but "
                "mode is 'stock' - IGNORED. Set 'mode: custom' to activate "
                "(choreography stays stock for param-only tables)."
                % sorted(set(self.unload_temps.keys())
                         | set(self.load_temps.keys())))
        return dropped

    def _default_cfg_path(self):
        # Same derivation as ace.py's mode-switch script lookup: ace.cfg
        # lives under the save_variables directory (printer_data/config),
        # in extended/. Hardcoded fallback = the fixed U1 path.
        try:
            sv = self.printer.lookup_object('save_variables', None)
            if sv is not None:
                cand = os.path.join(
                    os.path.dirname(os.path.abspath(sv.filename)),
                    'extended', 'ace.cfg')
                if os.path.exists(cand):
                    return cand
        except Exception:
            pass
        return '/home/lava/printer_data/config/extended/ace.cfg'

    @staticmethod
    def _parse_section_text(text):
        """Extract 'key: value' options of the [ace_tipform] section from
        raw cfg text. Line-based on purpose (no configparser: Klipper's
        dialect differs and a parse error here must never raise) - the web
        editor writes single-line 'key: value' options, which is all this
        needs to read back. Returns (mode, [(key, value)...], found)."""
        in_sec, found, mode, items = False, False, 'stock', []
        for line in text.splitlines():
            s = line.strip()
            if not s or s[0] in '#;':
                continue
            if s.startswith('['):
                in_sec = re.match(r'^\[\s*ace_tipform\s*\]', s) is not None
                found = found or in_sec
                continue
            if not in_sec:
                continue
            m = re.match(r'^([A-Za-z0-9_\-]+)\s*[:=]\s*(.*)$', s)
            if not m:
                continue
            k, v = m.group(1).strip().lower(), m.group(2).strip()
            if k == 'mode':
                mode = v.lower()
            elif v:
                items.append((k, v))
        return mode, items, found

    def cmd_ACE_TIPFORM_RELOAD(self, gcmd):
        """Live re-read of the section, so a web tipform save applies
        without a Klipper restart, like the [ace] parameters. The web
        backend fires this after writing the cfg; on an older
        build without the command it degrades to the old restart hint."""
        path = gcmd.get('FILE', None) or self._default_cfg_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read()
        except Exception as e:
            raise gcmd.error('[multiACE] tipform reload: cannot read %s: %s'
                             % (path, e))
        mode, items, found = self._parse_section_text(text)
        if not found:
            raise gcmd.error('[multiACE] tipform reload: no [ace_tipform] '
                             'section in %s' % path)
        if mode not in ('stock', 'custom'):
            raise gcmd.error("[multiACE] tipform reload: mode must be "
                             "'stock' or 'custom' (got %r) - nothing "
                             "changed" % mode)
        dropped = self._apply(mode, items)
        msg = ('[multiACE] tipform reloaded live: mode=%s, %d table(s)%s'
               % (self.mode,
                  len(set(self.tables) | set(self.unload_temps)
                      | set(self.load_temps)),
                  (', DROPPED invalid: %s' % ', '.join(dropped))
                  if dropped else ''))
        gcmd.respond_info(msg)
        logging.info(msg)

    def table_for(self, material, vendor=None, soft=False):
        """The custom table for a (vendor, material), or None = use the stock
        path (INNER macro inline / built-in table in the bg engine).

        Precedence: '<vendor>_<material>' -> '<material>' -> 'soft' ->
        'default'. The vendor key is CONSTRUCTED and checked for membership
        (never parsed back) - a missing/empty/generic vendor simply doesn't
        match and falls through to the plain material, so no vendor
        canonicalisation is needed (the fallback is the safety net). The
        join is '_' (not a space): a space is invalid in a Klipper config
        option name and the web editor's key validator forbids it. The
        vendor part itself is lowercased and its internal spaces collapsed
        to '_' so 'Prusament PLA' and a vendor field 'Prusa Research' both
        land on a stable key."""
        if self.mode != 'custom':
            return None
        mat = (material or '').strip().lower()
        ven = '_'.join((vendor or '').strip().lower().split())
        if mat and ven and ven not in ('none', 'generic'):
            vkey = '%s_%s' % (ven, mat)
            if vkey in self.tables:
                return self.tables[vkey]
        if mat and mat in self.tables:
            return self.tables[mat]
        if soft and 'soft' in self.tables:
            return self.tables['soft']
        return self.tables.get('default')

    def unload_temp_for(self, material, vendor=None, soft=False):
        """The custom unload temp for a (vendor, material), or None = the
        unload runs at the load temp.
        Same precedence + key normalisation as table_for, same mode gate:
        the whole section is inert in mode: stock (a param-only table
        just means stock CHOREOGRAPHY at the custom temp)."""
        if self.mode != 'custom':
            return None
        mat = (material or '').strip().lower()
        ven = '_'.join((vendor or '').strip().lower().split())
        if mat and ven and ven not in ('none', 'generic'):
            vkey = '%s_%s' % (ven, mat)
            if vkey in self.unload_temps:
                return self.unload_temps[vkey]
        if mat and mat in self.unload_temps:
            return self.unload_temps[mat]
        if soft and 'soft' in self.unload_temps:
            return self.unload_temps['soft']
        return self.unload_temps.get('default')

    def load_temp_for(self, material, vendor=None, soft=False):
        """The custom LOAD temp for a (vendor, material), or None = fall
        through to the DB chain (get_load_temp -> 250). Same precedence,
        key normalisation and mode gate as unload_temp_for."""
        if self.mode != 'custom':
            return None
        mat = (material or '').strip().lower()
        ven = '_'.join((vendor or '').strip().lower().split())
        if mat and ven and ven not in ('none', 'generic'):
            vkey = '%s_%s' % (ven, mat)
            if vkey in self.load_temps:
                return self.load_temps[vkey]
        if mat and mat in self.load_temps:
            return self.load_temps[mat]
        if soft and 'soft' in self.load_temps:
            return self.load_temps['soft']
        return self.load_temps.get('default')

    def get_status(self, eventtime):
        # tables = UNION of move-tables and param-only (unloadtemp:/
        # loadtemp:) keys - the web's restart-pending banner compares cfg
        # keys against this list; a param-only entry missing here would
        # flag "restart pending" forever.
        return {
            'mode': self.mode,
            'tables': sorted(set(self.tables.keys())
                             | set(self.unload_temps.keys())
                             | set(self.load_temps.keys())),
            'unload_temps': dict(self.unload_temps),
            'load_temps': dict(self.load_temps),
        }


def load_config(config):
    return AceTipform(config)
