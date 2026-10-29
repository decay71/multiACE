"""M3: unit capabilities (ace_unit.py) against the live old predicates.

P2 of UNIT_DRIVER_DESIGN.md section 10: for every codec in {None, v1, v2},
every firmware string of the universe and every Gen-1 probe state, each
capability must equal the kind predicate it will replace in M4 - computed
here by calling the REAL MultiAce methods on a stub that carries only the
dicts they read (no copies of the old expressions). Quirks are part of the
oracle: a firmware-string cap is protocol-blind in the old code, so the
one documented divergence (old predicate True while _protocols has no
codec) is asserted explicitly instead of hidden.
"""
import json

from . import harness as H
from .scenarios import PATH_V1_A, PATH_V2_A, _harness, unit_v1, unit_v2

FW_UNIVERSE = (
    '', 'Unknown', 'V1.3.856', 'V1.3.863', 'CV1.3.863', 'CV1.3.871',
    'V1.1.31', 'V1.1.3O', 'V1.1.46O', 'V1.1.47O', 'V1.1.60O', 'V1.1.61O',
    'V1.1.62O', 'ace2k 0.1', 'V2.0.0-shim', 'v2.0.0-SHIM', '1.0.2', 'V1.0.2',
    'O', ' v1.1.60o ', 'V1.1.O', 'CV1.3.871O',
)


def _mods():
    A = H.load_ace()
    p1, p2 = H.protocol_modules()
    import sys
    U = sys.modules[H.PKG + '.ace_unit']
    return A, p1.AceProtocolV1, p2.AceProtocolV2, U


class _Cli:
    def __init__(self, state):
        self._state = state

    def support_state(self):
        return self._state


def _stub(A, codec, fw, probe):
    """A MultiAce-shaped object with the live predicate methods and only
    the attributes they read."""
    class Stub(object):
        _is_v2_idx = A.MultiAce._is_v2_idx
        _is_v2 = A.MultiAce._is_v2
        _is_v1 = A.MultiAce._is_v1
        _is_open_fw_idx = A.MultiAce._is_open_fw_idx
        _open_fw_version = A.MultiAce._open_fw_version
        _tag_read_capable = A.MultiAce._tag_read_capable
        _community_fw_kind = A.MultiAce._community_fw_kind
        _fw_not_working = staticmethod(A.MultiAce._fw_not_working)
        _caps = A.MultiAce._caps
        _cap = A.MultiAce._cap
    s = Stub()
    s._protocols = {} if codec is None else {0: codec()}
    s._ace_models = {0: ('ACE', fw)}
    s._ace_devices = ['x']
    s._gen1_tunnel_clients = {} if probe is None else {0: _Cli(probe)}
    s._unit_caps_cache = {}
    return s


def _universe(V1, V2):
    for codec in (None, V1, V2):
        for fw in FW_UNIVERSE:
            for probe in (None, (fw, True), (fw, False)):
                yield codec, fw, probe


def test_caps_equal_live_predicates():
    A, V1, V2, U = _mods()
    n = 0
    for codec, fw, probe in _universe(V1, V2):
        s = _stub(A, codec, fw, probe)
        caps = s._caps(0)
        v1 = s._is_v1(0)
        v2 = s._is_v2(0)
        assert v2 == s._is_v2_idx(0)
        # static table: every V2 flag is _is_v2, every V1 flag is _is_v1
        for name in ('has_slot_status_truth', 'busy_wait_floor', 'fa_monitor',
                     'needs_fa_prestop', 'has_rollback_assist', 'no_freewheel',
                     'self_stops_on_resistance', 'has_decoder', 'has_humidity',
                     'has_exhaust_valve', 'has_feed_check', 'v2_command_set'):
            assert caps[name] == v2, (name, codec, fw)
        for name in ('v1_fa_health_log', 'tag_identity_in_status',
                     'fw_tag_pair_dedupe', 'blocking_write_on_reactor'):
            assert caps[name] == v1, (name, codec, fw)
        for name in ('reconnectable', 'presence_sensor', 'has_dryer'):
            assert caps[name] == (v1 or v2), (name, codec, fw)
        assert caps['transport_engine'] == (
            'threaded' if v2 else 'reactor_fd' if v1 else None)
        assert caps['empty_token'] == ('empty' if v2 else 'empty1' if v1
                                       else None)
        assert caps['flash_engine'] == ('ota_v2' if v2 else 'iap_v1' if v1
                                        else None)
        assert caps['busy_reply'] == ('error_code' if v2 else
                                      'code0_forbidden' if v1 else None)
        assert caps['speed_unit'] == ('mm_s' if (v1 or v2) else None)
        assert caps['lanes_concurrent'] == (None if v2 else False)
        old_open = s._is_open_fw_idx(0)
        old_ver = s._open_fw_version(0)
        old_bites = old_ver is not None and old_ver >= U.INSERT_MIDPRINT_MIN_FW
        old_gen1 = bool(probe and probe[1])
        # the old sites paired the O test with the kind test (M4.5)
        old_read = (old_open and v2) or old_gen1
        if codec is None:
            assert caps is U.NO_CAPS
            assert all(v in (False, None, '') for v in caps.as_dict().values())
            # the documented divergence: the old string tests read
            # _ace_models without looking at the codec, the cap record
            # does (no codec -> NO_CAPS). It only shows while a unit's
            # model entry outlives its protocol.
            if old_open or s._community_fw_kind(0) or s._fw_not_working(fw):
                assert fw.strip()
            continue
        # firmware string: the live helpers, bootloader rule included
        assert caps['bootloader_held'] == s._fw_not_working(fw), (fw,)
        assert caps['community_fw'] == s._community_fw_kind(0), (fw,)
        assert caps['rc522_tunnel'] == (old_open and v2), (codec, fw)
        assert caps['can_write_tags'] == (old_open and v2)
        assert caps['open_fw_version'] == old_ver, (fw,)
        assert caps['insert_bites_while_busy'] == (old_bites and v2), (fw,)
        assert caps['dryer_flag_bits'] == (
            v2 and old_ver is not None and old_ver >= (1, 1, 61))
        assert caps['gen1_tunnel'] == old_gen1
        assert caps['tag_read'] == old_read, (codec, fw, probe)
        n += 1
    assert n == 2 * len(FW_UNIVERSE) * 3


def test_caps_record_shape():
    A, V1, V2, U = _mods()
    for codec in (V1, V2):
        static = codec.CAPS_STATIC
        assert set(static) == set(U.STATIC_FIELDS), codec.NAME
        caps = U.caps_for(codec, 'V1.1.60O', None)
        assert set(caps) == set(U.STATIC_FIELDS) | set(U.DYNAMIC_FIELDS)
        d = caps.as_dict()
        json.dumps(d)
        assert all(isinstance(k, str) for k in d)
        assert d['open_fw_version'] == [1, 1, 60]
    # reasons: every field where V1 and V2 differ names why
    for name in U.STATIC_FIELDS:
        if V1.CAPS_STATIC[name] != V2.CAPS_STATIC[name]:
            assert name in U.REASONS, name
    assert set(U.NO_CAPS) == set(U.STATIC_FIELDS) | set(U.DYNAMIC_FIELDS)
    # immutable
    caps = U.caps_for(V2, 'V1.1.31')
    try:
        caps.x = 1
    except AttributeError:
        pass
    else:
        raise AssertionError('UnitCaps must be immutable')
    assert U.caps_for(None, 'V1.1.60O') is U.NO_CAPS
    # an instance works as codec too
    assert U.caps_for(V2(), 'V1.1.31')['has_decoder'] is True


def test_telemetry_methods_single_source():
    """The codec constant and the contract checker's frozen copy agree."""
    from . import contract as C
    A, V1, V2, U = _mods()
    import sys
    P = sys.modules[H.PKG + '.ace_protocol']
    assert P.TELEMETRY_METHODS == C.TELEMETRY_METHODS
    assert V1.TELEMETRY_METHODS == P.TELEMETRY_METHODS
    assert V2.TELEMETRY_METHODS == P.TELEMETRY_METHODS
    assert A.TELEMETRY_METHODS is P.TELEMETRY_METHODS


def test_cap_is_live_on_a_running_fleet():
    """_cap follows the codec of the connected unit and recomputes when the
    firmware string changes (the port fallback / late get_info case)."""
    def make(clock):
        return [unit_v1(clock, 'v1a', PATH_V1_A),
                unit_v2(clock, 'v2a', PATH_V2_A)]
    h = _harness(make, 2)
    h.build()
    h.ready()
    h.run(5.0)
    try:
        ace = h.inst
        v1_idx = [i for i, p in ace._protocols.items() if p.NAME == 'v1'][0]
        v2_idx = [i for i, p in ace._protocols.items() if p.NAME == 'v2'][0]
        assert ace._cap(v1_idx, 'transport_engine') == 'reactor_fd'
        assert ace._cap(v2_idx, 'transport_engine') == 'threaded'
        assert ace._cap(v2_idx, 'has_decoder') is True
        assert ace._cap(v1_idx, 'has_decoder') is False
        assert ace._cap(v2_idx, 'rc522_tunnel') is False
        assert ace._cap(7, 'has_decoder') is False
        assert ace._cap(7, 'transport_engine') is None
        first = ace._caps(v2_idx)
        assert ace._caps(v2_idx) is first
        model = ace._ace_models[v2_idx][0]
        ace._ace_models[v2_idx] = (model, 'V1.1.60O')
        assert ace._cap(v2_idx, 'rc522_tunnel') is True
        assert ace._cap(v2_idx, 'insert_bites_while_busy') is True
        assert ace._cap(v2_idx, 'dryer_flag_bits') is False
        assert ace._caps(v2_idx) is not first
    finally:
        h.finish()
