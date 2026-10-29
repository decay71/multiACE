# Unit capabilities (UNIT_DRIVER_DESIGN.md section 4, step M3).
#
# A unit is one ACE-like device with four lanes, addressed by idx. What the
# host may expect of it is a UnitCaps: the codec's static table
# (AceProtocolV1/V2.CAPS_STATIC) merged with what the runtime firmware
# string and the Gen-1 tunnel probe add. ace.py computes it live per idx
# (MultiAce._caps / _cap) from (codec class, firmware string, probe state)
# and recomputes when that key changes: the firmware string can arrive late
# or fall back to the port's last known value, the probe runs lazily.
#
# Every value equals the kind predicate it will replace in M4, on the same
# inputs, quirks included (section 4 rule "eine Stelle, eine Cap"). The
# firmware-string caps are derived only for the codecs v1 and v2; other
# codecs get False for them. tests/units/test_caps.py is the oracle that
# holds each value against the live old predicate over the whole input
# universe. This module imports nothing from ace.py.

# ACE2-Open (Simon-CR) firmware strings end in the letter O (V1.1.60O).
# From 1.1.47O a lane bites on an insert even while a sibling lane feeds the
# printing head; from 1.1.61O the dryer status frame carries the
# rotisserie / auto-roll flag bits.
INSERT_MIDPRINT_MIN_FW = (1, 1, 47)
DRYER_FLAG_BITS_MIN_FW = (1, 1, 61)

# Static caps every codec table must define (the V1/V2 columns of the
# design's table). Missing in a codec's CAPS_STATIC -> None.
STATIC_FIELDS = (
    'transport_engine', 'blocking_write_on_reactor', 'reconnectable',
    'presence_sensor', 'empty_token', 'has_slot_status_truth',
    'busy_wait_floor', 'fa_monitor', 'needs_fa_prestop',
    'has_rollback_assist', 'no_freewheel', 'self_stops_on_resistance',
    'has_decoder', 'v1_fa_health_log', 'tag_identity_in_status',
    'fw_tag_pair_dedupe', 'has_humidity', 'has_dryer', 'has_exhaust_valve',
    'has_feed_check', 'flash_engine', 'v2_command_set', 'lanes_concurrent',
    'speed_unit', 'busy_reply', 'has_stable_id',
)

# Caps derived from the firmware string and the Gen-1 probe.
DYNAMIC_FIELDS = (
    'open_fw_version', 'community_fw', 'bootloader_held', 'rc522_tunnel',
    'can_write_tags', 'insert_bites_while_busy', 'dryer_flag_bits',
    'gen1_tunnel', 'tag_read',
)

# Why V1 and V2 differ, per field where they do (design: mandatory).
REASONS = {
    'transport_engine': 'V1 reader is a reactor fd callback, V2 runs reader '
                        'and writer in background threads',
    'blocking_write_on_reactor': 'the V1 sync write runs on the reactor '
                                 'thread and waits for homing to clear',
    'empty_token': "V1 firmware reports an empty slot as 'empty1', V2 as "
                   "'empty' (compared by prefix, never by equality)",
    'has_slot_status_truth': 'only the ACE 2 reports a per-slot motion '
                             'state (ready/feeding/assisting/...)',
    'busy_wait_floor': 'wait_ace_ready_on raises its timeout to 60 s for a '
                       'V2 (long motions without an intermediate status)',
    'fa_monitor': 'the 0.1 s velocity tick with dropped-arm verify needs '
                  'slot_status truth',
    'needs_fa_prestop': 'a V2 rejects a motor command while assisting, so '
                        'the assist is stopped first',
    'has_rollback_assist': 'feed mode 3 (rollback assist) exists on the '
                           'ACE 2 only',
    'no_freewheel': 'the ACE 2 gearbox does not freewheel; a V1 lets the '
                    'extruder pull',
    'self_stops_on_resistance': 'the ACE 2 ends a feed at resistance; the '
                                'V1 is open-loop (self-stop unconfirmed)',
    'has_decoder': 'get_feed_info with a signed lane encoder exists on the '
                   'ACE 2 only',
    'v1_fa_health_log': 'log-only monitor for the unit that has no '
                        'slot_status truth',
    'tag_identity_in_status': 'the V1 status carries sku/type/color per '
                              'slot; the V2 answers a separate query',
    'fw_tag_pair_dedupe': 'the Gen-1 reader shares one antenna per slot '
                          'pair and reports the neighbour; V2 reads are '
                          'checked by the tunnel instead',
    'has_humidity': 'only the ACE 2 reports a humidity reading',
    'has_exhaust_valve': 'SET_VALVE (cmd 66) is an ACE 2 command',
    'has_feed_check': 'set_feed_check exists on the ACE 2 only',
    'flash_engine': 'ACE Pro flashes over the V1 IAP JSON-RPC, ACE 2 over '
                    'the protobuf OTA',
    'v2_command_set': 'the A_* diagnostics speak protobuf commands',
    'lanes_concurrent': 'V1 serialises lanes; for the ACE 2 it is open',
    'busy_reply': "V1 rejects busy as code=0 msg=FORBIDDEN, V2 as "
                  "code=n error_n",
    'rc522_tunnel': 'firmware string ends in O (ACE2-Open), an ACE 2 '
                    'patch; the ACE Pro community firmware is CV-prefixed '
                    'and has the Gen-1 tunnel instead',
    'gen1_tunnel': 'a Gen-1 CFW ACE Pro that answered the tunnel probe',
    'has_stable_id': 'the ACE 2 answers discover_device with its 96-bit MCU '
                     'id (uid1..3), unique per device; the ACE Pro has no '
                     'such query and an empty USB serial, so it is only '
                     'known by its port',
}


def _fw_text(firmware):
    return (firmware or '').strip()


def fw_not_working(firmware):
    """True when a GET_INFO version names a bootloader, not a running ACE
    application: Simon-CR's shim (any *-shim) or the OEM IAP ('1.0.2').
    Same rule as MultiAce._fw_not_working (M1)."""
    fw = _fw_text(firmware)
    if not fw or fw == 'Unknown':
        return False
    low = fw.lower()
    return low.endswith('-shim') or low.lstrip('v') == '1.0.2'


def open_fw_version(firmware):
    """(major, minor, patch) of an ACE2-Open firmware string, else None.
    'V1.1.46O' -> (1, 1, 46). Same rule as MultiAce._open_fw_version."""
    txt = _fw_text(firmware).upper()
    if not txt.endswith('O'):
        return None
    try:
        return tuple(int(part) for part in txt[:-1].lstrip('V').split('.'))
    except ValueError:
        return None


def is_open_fw(firmware):
    """The runtime firmware string ends in the LETTER O: the unit has the
    RC522 tag tunnel. Same rule as MultiAce._is_open_fw_idx."""
    fw = _fw_text(firmware)
    return bool(fw) and fw.upper().endswith('O')


def community_fw_kind(firmware):
    """'ace2_open' (trailing O), 'gen1_cfw' (leading CV), '' for stock or
    unknown. Display only. Same rule as MultiAce._community_fw_kind."""
    fw = _fw_text(firmware).upper()
    if not fw:
        return ''
    if fw.endswith('O'):
        return 'ace2_open'
    if fw.startswith('CV'):
        return 'gen1_cfw'
    return ''


class UnitCaps(object):
    """Immutable capability record of one unit: str keys only, values are
    bool, str, tuple or None, exportable via as_dict()."""
    __slots__ = ('_fields', 'reasons', 'key')

    def __init__(self, fields, reasons=None, key=None):
        for k in fields:
            if not isinstance(k, str):
                raise TypeError('UnitCaps key must be str: %r' % (k,))
        object.__setattr__(self, '_fields', dict(fields))
        object.__setattr__(self, 'reasons', dict(reasons or {}))
        object.__setattr__(self, 'key', key)

    def __setattr__(self, name, value):
        raise AttributeError('UnitCaps is immutable')

    def __getitem__(self, name):
        return self._fields[name]

    def get(self, name, default=None):
        return self._fields.get(name, default)

    def __contains__(self, name):
        return name in self._fields

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)

    def keys(self):
        return self._fields.keys()

    def items(self):
        return self._fields.items()

    def as_dict(self):
        """JSON-safe copy (tuples become lists)."""
        out = {}
        for k, v in self._fields.items():
            out[k] = list(v) if isinstance(v, tuple) else v
        return out

    def __repr__(self):
        return 'UnitCaps(%r)' % (self._fields,)


def _none_fields():
    d = dict((k, None) for k in STATIC_FIELDS)
    for k in DYNAMIC_FIELDS:
        d[k] = None
    for k in ('reconnectable', 'presence_sensor', 'has_slot_status_truth',
              'busy_wait_floor', 'fa_monitor', 'needs_fa_prestop',
              'has_rollback_assist', 'no_freewheel',
              'self_stops_on_resistance', 'has_decoder', 'v1_fa_health_log',
              'tag_identity_in_status', 'fw_tag_pair_dedupe', 'has_humidity',
              'has_dryer', 'has_exhaust_valve', 'has_feed_check',
              'v2_command_set', 'blocking_write_on_reactor',
              'lanes_concurrent', 'bootloader_held', 'rc522_tunnel',
              'can_write_tags', 'insert_bites_while_busy', 'dryer_flag_bits',
              'gen1_tunnel', 'tag_read'):
        d[k] = False
    d['community_fw'] = ''
    return d


# No codec (today's "_protocols.get(idx) is None"): every flag False, every
# value None.
NO_CAPS = UnitCaps(_none_fields(), REASONS, key=(None, '', None))

# The codecs whose firmware string the dynamic caps are derived from.
FW_DERIVED_KINDS = ('v1', 'v2')


def caps_for(codec, firmware='', gen1_probe=None):
    """UnitCaps of a unit with this codec (class or instance, NAME +
    CAPS_STATIC), runtime firmware string and Gen-1 tunnel probe state
    ((firmware, supported) or None, as Gen1TagTunnel.support_state()
    returns it). codec None -> NO_CAPS."""
    if codec is None:
        return NO_CAPS
    name = getattr(codec, 'NAME', None)
    fields = _none_fields()
    static = getattr(codec, 'CAPS_STATIC', None) or {}
    for k in STATIC_FIELDS:
        if k in static:
            fields[k] = static[k]
    fw = _fw_text(firmware)
    fields['bootloader_held'] = fw_not_working(fw)
    fields['community_fw'] = community_fw_kind(fw)
    if name in FW_DERIVED_KINDS:
        ver = open_fw_version(fw)
        # ACE2-Open is a patch on the ACE 2 firmware: the O caps belong to
        # the v2 kind only (design table, column O). An ACE Pro string never
        # ends in O; its community firmware is CV-prefixed (gen1_tunnel).
        tunnel = is_open_fw(fw) and name == 'v2'
        fields['open_fw_version'] = ver
        fields['rc522_tunnel'] = tunnel
        fields['can_write_tags'] = tunnel
        fields['insert_bites_while_busy'] = (
            tunnel and ver is not None and ver >= INSERT_MIDPRINT_MIN_FW)
        fields['dryer_flag_bits'] = (
            tunnel and ver is not None and ver >= DRYER_FLAG_BITS_MIN_FW)
        fields['gen1_tunnel'] = bool(gen1_probe and gen1_probe[1])
        fields['tag_read'] = bool(tunnel or fields['gen1_tunnel'])
    key = (codec if isinstance(codec, type) else type(codec), firmware,
           gen1_probe)
    return UnitCaps(fields, REASONS, key=key)
