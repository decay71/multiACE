# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure Flexi routing policy. No device I/O and no execution authorization.

This experimental integration deliberately keeps filament execution locked.
Stored routes and journals are not proof of the installed tubing or clearance.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import json
import re
import hashlib
import configparser

EXECUTION_ENABLED = False
MAX_UNITS = 4
MAX_SLOTS = 4
MAX_ROUTES = MAX_UNITS * MAX_SLOTS
HEADS = 4


class RouteError(ValueError):
    pass


class RouteConflict(RouteError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RouteError('Duplicate route: ' + key)
        result[key] = value
    return result


class SlotAvailability:
    STATES = ('assigned', 'empty', 'unused')

    def __init__(self, raw=None):
        if raw is None:
            raw = {}
        if isinstance(raw, str):
            if len(raw) > 4096:
                raise RouteError('Slot availability configuration is too large')
            try:
                raw = json.loads(raw, object_pairs_hook=_unique_object)
            except (ValueError, TypeError) as exc:
                raise RouteError('Invalid slot availability JSON: ' + str(exc))
        if not isinstance(raw, dict) or len(raw) > MAX_ROUTES:
            raise RouteError('Slot availability must be an ACE:slot object')
        for key, value in raw.items():
            if not isinstance(key, str) or not re.fullmatch(r'[0-3]:[0-3]', key):
                raise RouteError('Invalid slot availability source')
            if not isinstance(value, str) or value not in self.STATES:
                raise RouteError('Slot availability must be assigned, empty, or unused')
        self._entries = tuple(sorted(raw.items()))

    def as_dict(self):
        return dict(self._entries)

    def encode(self):
        return json.dumps(self.as_dict(), sort_keys=True, separators=(',', ':'))

    def get(self, ace, slot):
        return self.as_dict().get('%d:%d' % (ace, slot), 'assigned')


class Topology:
    """Declared direct PTFE joiners, never sensor-derived physical proof."""
    def __init__(self, raw=None):
        if raw is None:raw={}
        if isinstance(raw,str):
            if len(raw)>16384:raise RouteError('Topology is oversized')
            try:raw=json.loads(raw,object_pairs_hook=_unique_object)
            except (ValueError,TypeError) as exc:raise RouteError('Invalid topology JSON: '+str(exc))
        if not isinstance(raw,dict) or set(raw)-{'joiners','manual_overrides'}:raise RouteError('Invalid topology fields')
        joins=raw.get('joiners',{});manual=raw.get('manual_overrides',{})
        if not isinstance(joins,dict) or len(joins)>16 or not isinstance(manual,dict) or len(manual)>32:raise RouteError('Topology collections are invalid')
        seen=set()
        for key,j in joins.items():
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',key):raise RouteError('Invalid joiner identifier')
            if not isinstance(j,dict) or set(j)!={'name','head','branches','confirmed','reference'}:raise RouteError('Joiner needs name, head, branches and physical confirmation fields')
            for field,limit in (('name',80),('reference',512)):
                if not isinstance(j[field],str) or len(j[field])>limit or any(ord(c)<32 for c in j[field]):raise RouteError('Invalid joiner text')
            if not j['name'].strip() or type(j['head'])is not int or j['head']not in range(4) or type(j['confirmed'])is not bool:raise RouteError('Invalid joiner destination/confirmation')
            if j['confirmed'] and not j['reference'].strip():raise RouteError('Physical confirmation requires an inspection reference')
            branches=j['branches']
            if not isinstance(branches,dict) or not 2<=len(branches)<=32:raise RouteError('A joiner must declare 2–32 branches')
            SlotAvailability({source:'assigned' for source in branches})
            labels=[]
            for source,label in branches.items():
                if source in seen:raise RouteError('A direct route cannot belong to multiple joiners')
                if not isinstance(label,str) or not label.strip() or len(label)>32 or any(ord(c)<32 for c in label):raise RouteError('Invalid branch label')
                labels.append(label);seen.add(source)
            if len(labels)!=len(set(labels)):raise RouteError('Joiner branch labels must be unique')
        SlotAvailability({source:'assigned' for source in manual})
        if any(type(v)is not bool for v in manual.values()):raise RouteError('Manual override flags must be Boolean')
        self._data=json.loads(json.dumps({'joiners':joins,'manual_overrides':manual}))

    def as_dict(self):return json.loads(json.dumps(self._data))
    def encode(self):return json.dumps(self._data,sort_keys=True,separators=(',',':'))
    def validate_routes(self,routes):
        for j in self._data['joiners'].values():
            for source in j['branches']:
                if source not in routes or routes[source]!=j['head']:raise RouteError('Joiner branches must match saved toolhead destinations')
    def patched(self,patch):
        if not isinstance(patch,dict) or set(patch)-{'joiners','manual_overrides'}:raise RouteError('Expected explicit topology changes')
        result=self.as_dict()
        for group,changes in patch.items():
            if not isinstance(changes,dict):raise RouteError('Topology patch must be an object')
            for key,value in changes.items():
                if value is None:result[group].pop(key,None);continue
                if group=='joiners' and isinstance(value,dict):
                    value=json.loads(json.dumps(value));old=result[group].get(key)
                    if old is None or any(old.get(k)!=value.get(k) for k in ('name','head','branches')):
                        value['confirmed']=False;value['reference']=''
                result[group][key]=value
        return Topology(result)


class Routes:
    """Immutable source-to-head map; an omitted source is disabled."""
    def __init__(self, raw, slot_status=None, topology=None):
        if isinstance(raw, str):
            if len(raw) > 4096:
                raise RouteError('Route configuration is too large')
            try:
                raw = json.loads(raw, object_pairs_hook=_unique_object)
            except (ValueError, TypeError) as exc:
                raise RouteError('Invalid route JSON: ' + str(exc))
        if not isinstance(raw, dict) or len(raw) > MAX_ROUTES:
            raise RouteError('Routes must be a source-to-toolhead object')
        normalized = {}
        for source, head in raw.items():
            if not isinstance(source, str) or not re.fullmatch(r'[0-3]:[0-3]', source):
                raise RouteError('Source must be ACE:slot using zero-based indices')
            if type(head) is not int or head not in range(HEADS):
                raise RouteError('Toolhead must be an integer from 0 to 3')
            normalized[source] = head
        self._entries = tuple(sorted(normalized.items()))
        self.availability = SlotAvailability(slot_status)
        self.topology = Topology(topology)

    def as_dict(self):
        return dict(self._entries)

    def encode(self):
        return json.dumps(self.as_dict(), sort_keys=True, separators=(',', ':'))

    def patched(self, changes, unit_indices, allow_unconfigured=False):
        """Omission preserves a route; only an explicit null removes it.

        ``allow_unconfigured`` admits routes for a planned but currently
        absent ACE (for example ACE2/ACE3 before the hardware is installed).
        Execution remains evidence-gated: an absent unit cannot produce a
        device binding, so the route is retained but cannot be operated.
        """
        validate_route_update(changes, '0' * 64)
        result = self.as_dict()
        detected = set(unit_indices)
        for key, head in changes.items():
            if head is None:
                result.pop(key, None)
            else:
                if (not allow_unconfigured and int(key[0]) not in detected
                        and result.get(key) != head):
                    raise RouteError('Cannot assign an unavailable ACE; its existing routes are preserved')
                result[key] = head
        return Routes(result, self.availability.as_dict(), self.topology.as_dict())

    def with_status_patch(self, changes, unit_indices, allow_unconfigured=False):
        patch = SlotAvailability(changes).as_dict()
        result = self.availability.as_dict()
        detected = set(unit_indices)
        for key, value in patch.items():
            if (not allow_unconfigured and int(key[0]) not in detected
                    and result.get(key, 'assigned') != value):
                raise RouteError('Cannot edit availability of an unavailable ACE; existing settings are preserved')
            result[key] = value
        return Routes(self.as_dict(), result, self.topology.as_dict())

    def with_topology_patch(self, patch):
        result=Routes(self.as_dict(),self.availability.as_dict(),self.topology.patched(patch).as_dict())
        result.topology.validate_routes(result.as_dict())
        return result

    def validate_topology(self):self.topology.validate_routes(self.as_dict())

    def context(self):
        # Preserve legacy fingerprints when the new key is absent/empty.
        status = self.availability.as_dict()
        topology=self.topology.as_dict()
        if topology['joiners'] or topology['manual_overrides']:
            return {'routes':self.as_dict(),'slot_status':status,'topology':topology}
        return {'routes': self.as_dict(), 'slot_status': status} if status else self.as_dict()

    def context_encode(self):
        return json.dumps(self.context(), sort_keys=True, separators=(',', ':'))

    def require_available(self, ace, slot):
        head = self.destination(ace, slot)
        status = self.availability.get(ace, slot)
        if status != 'assigned':
            raise RouteError('ACE %d slot %d is %s; new feeding is unavailable' % (ace, slot, status))
        return head

    def destination(self, ace, slot):
        if type(ace) is not int or not 0 <= ace < MAX_UNITS or type(slot) is not int or not 0 <= slot < MAX_SLOTS:
            raise RouteError('Invalid ACE or slot index')
        key = '%d:%d' % (ace, slot)
        entries = self.as_dict()
        if key not in entries:
            raise RouteError('ACE %d slot %d has no configured Flexi route' % (ace, slot))
        return entries[key]

    def require_destination(self, ace, slot, head):
        if type(head) is not int or self.destination(ace, slot) != head:
            raise RouteError('Requested toolhead contradicts the configured tubing route')
        return head

    def sources(self, head):
        if type(head) is not int or head not in range(HEADS):
            raise RouteError('Invalid toolhead')
        return tuple(tuple(map(int, key.split(':'))) for key, value in self._entries if value == head)

    def resolve_load_source(self, head, ace, slot, ace_given, slot_given, owner=None):
        """Pick the (ACE, slot) that physically feeds `head`.

        The saved routes are the tube map: a load must never feed a slot
        routed elsewhere and then wait on this head's sensor. Callers that
        pass only ACE (the web/display auto-load) used to fall back to the
        stock slot==head and active-unit assumptions, which cross-feed on a
        two-MMU printer. With no routes configured the legacy value is
        returned unchanged.
        """
        sources = self.sources(head)
        if not sources:
            return (ace, slot)
        owner_pair = None
        if isinstance(owner, dict):
            oa, os_ = owner.get('ace_index'), owner.get('slot')
            if type(oa) is int and type(os_) is int:
                owner_pair = (oa, os_)
        if owner_pair is not None and owner_pair not in sources:
            owner_pair = None

        def feeds(pair):
            return pair in sources

        def actual(pair):
            try:
                return 'T%d' % self.destination(pair[0], pair[1])
            except RouteError:
                return 'no configured route'

        if ace_given and slot_given:
            pair = (ace, slot)
            if feeds(pair):
                return pair
            raise RouteError(
                'ACE %d slot %d feeds %s, not T%d - refused before feeding. '
                'The saved route is the physical tube map; load the slot that '
                'feeds T%d or correct the route.'
                % (ace, slot, actual(pair), head, head))
        if ace_given:
            candidates = [pair for pair in sources if pair[0] == ace]
            if not candidates:
                raise RouteError(
                    'ACE %d has no configured route to T%d (%s) - refused '
                    'before feeding.' % (
                        ace, head,
                        ', '.join('ACE %d slot %d' % pair for pair in sources)))
            if owner_pair in candidates:
                return owner_pair
            return sorted(candidates)[0]
        if slot_given:
            pair = (ace, slot)
            if feeds(pair):
                return pair
            candidates = [pair for pair in sources if pair[1] == slot]
            if len(candidates) == 1:
                return candidates[0]
            raise RouteError(
                'ACE %d slot %d feeds %s, not T%d - refused before feeding.'
                % (ace, slot, actual(pair), head))
        if owner_pair is not None:
            return owner_pair
        if len(sources) == 1:
            return sources[0]
        return sorted(sources)[0]

    def display_source(self, head, present, owner=None):
        """Preview metadata only; never establishes path ownership or clearance.

        A valid loaded owner wins. An unloaded shared head previews a source
        only when exactly one routed slot is present; never guess first-slot.
        """
        sources = self.sources(head)
        if owner:
            ace, slot = owner.get('ace_index'), owner.get('slot')
            if (owner.get('load_failed') or (ace, slot) not in sources
                    or (ace, slot) not in present):
                return None
            if type(ace) is not int or type(slot) is not int:
                return None
            return (ace, slot)
        candidates = [source for source in sources if source in present]
        return candidates[0] if len(candidates) == 1 else None

    def validate_units(self, unit_indices, allow_absent=False):
        """Reject malformed indices; optionally allow configured-but-absent units.

        A planned ACE2/ACE3 route is valid configuration even when the unit is
        not connected. It remains non-operable until live device evidence
        exists. Callers that require a complete connected fleet leave
        ``allow_absent`` false.
        """
        unit_indices = tuple(unit_indices)
        if any(type(idx) is not int or not 0 <= idx < MAX_UNITS for idx in unit_indices):
            raise RouteError('Invalid detected unit indices')
        if allow_absent:
            return
        detected = set(unit_indices)
        missing = sorted({int(key.split(':')[0]) for key, _ in self._entries} - detected)
        if missing:
            raise RouteError('Routes reference undetected ACE units: ' + ', '.join(map(str, missing)))

    def shared_heads(self):
        return tuple(h for h in range(HEADS) if len(self.sources(h)) > 1)

    def connections(self):
        """Explicit physical connection type per configured route.

        This describes the saved wiring only.  It is not sensor evidence and
        never authorizes movement or shared-path clearance.  A single route to
        a toolhead is a direct PTFE connection and needs no fitting; two or
        more routes to the same toolhead are shared and must identify their
        joiner/branches.  A manual override is reported as another configured
        topology rather than being guessed at.
        """
        routes = self.as_dict()
        topology = self.topology.as_dict()
        joiners = {}
        for joiner_id, joiner in topology['joiners'].items():
            for source, label in joiner['branches'].items():
                joiners[source] = {
                    'id': joiner_id,
                    'name': joiner['name'],
                    'head': joiner['head'],
                    'branch': label,
                    'branches': dict(joiner['branches']),
                    'confirmed': joiner['confirmed'],
                    'reference': joiner['reference'],
                }
        manual = topology['manual_overrides']
        counts = {}
        for head in routes.values():
            counts[head] = counts.get(head, 0) + 1
        result = {}
        for source, head in sorted(routes.items()):
            joiner = joiners.get(source)
            if joiner is not None:
                result[source] = {
                    'type': 'shared', 'configured': True,
                    'head': head, 'fitting_required': True,
                    'joiner': joiner,
                    'reason': 'Shared connection through a recorded combiner/joiner; branch inspection is still required.',
                }
            elif source in manual:
                result[source] = {
                    'type': 'other', 'configured': True,
                    'head': head, 'fitting_required': True,
                    'joiner': None,
                    'reason': 'Other configured topology; verify it physically before relying on it.',
                }
            elif counts.get(head, 0) == 1:
                result[source] = {
                    'type': 'direct', 'configured': True,
                    'head': head, 'fitting_required': False,
                    'joiner': None,
                    'reason': 'Direct PTFE connection; no fitting required.',
                }
            else:
                result[source] = {
                    'type': 'shared', 'configured': False,
                    'head': head, 'fitting_required': True,
                    'joiner': None,
                    'reason': 'Shared toolhead path; record the joiner and branch before relying on shared-path clearance.',
                }
        return result

    def device_heads(self, ace):
        return tuple(sorted({head for key, head in self._entries if int(key.split(':')[0]) == ace}))

    def status(self):
        return {'available': True, 'schema': 1, 'routes': self.as_dict(),
                'slot_status': self.availability.as_dict(), 'topology':self.topology.as_dict(),
                'connections': self.connections(),
                'shared_heads': list(self.shared_heads()),
                'execution_enabled': EXECUTION_ENABLED, 'physical_path_verified': False,
                'stage': 'offline-routing',
                'reason': 'Filament operations remain locked until live interlocks and routing are validated.'}


def encode_command(routes):
    """Hex transports validated JSON without G-code quoting/injection."""
    return 'ACE_SET_FLEXI_ROUTES DATA=' + Routes(routes).encode().encode('utf-8').hex()


def decode_command(data):
    if not isinstance(data, str) or len(data) > 8192 or not re.fullmatch(r'[0-9a-fA-F]+', data):
        raise RouteError('Invalid route command payload')
    try:
        return Routes(bytes.fromhex(data).decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise RouteError(str(exc))


def validate_route_update(changes, base_revision):
    if not isinstance(base_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', base_revision):
        raise RouteError('A saved routing baseline fingerprint is required; refresh the editor')
    if not isinstance(changes, dict) or len(changes) > MAX_ROUTES:
        raise RouteError('Expected explicit route changes')
    Routes({key: head for key, head in changes.items() if head is not None})
    for key in changes:
        if not isinstance(key, str) or not re.fullmatch(r'[0-3]:[0-3]', key):
            raise RouteError('Invalid explicit route key')


def encode_route_update(changes, base_revision, slot_status_changes=None, topology_changes=None, discovery_approval=None):
    validate_route_update(changes, base_revision)
    payload = {'changes': changes, 'base_revision': base_revision}
    if slot_status_changes is not None:
        payload['slot_status_changes'] = SlotAvailability(slot_status_changes).as_dict()
    if topology_changes is not None:
        Topology().patched(topology_changes);payload['topology_changes']=topology_changes
    if discovery_approval is not None:
        if not isinstance(discovery_approval,dict) or len(discovery_approval)>32 or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{32}',v) for v in discovery_approval.values()):raise RouteError('Invalid discovery approval')
        SlotAvailability({k:'assigned' for k in discovery_approval});payload['discovery_approval']=discovery_approval
    return 'ACE_SET_FLEXI_ROUTES DATA=' + json.dumps(
        payload, sort_keys=True,
        separators=(',', ':')).encode().hex()


def decode_route_update(data):
    if not isinstance(data, str) or len(data) > 65536 or not re.fullmatch(r'[0-9a-fA-F]+', data):
        raise RouteError('Invalid routing update payload')
    try:
        payload = json.loads(bytes.fromhex(data).decode(), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise RouteError(str(exc)) from exc
    if not isinstance(payload, dict) or not {'changes', 'base_revision'} <= set(payload) or set(payload) - {'changes', 'base_revision', 'slot_status_changes','topology_changes','discovery_approval'}:
        raise RouteError('Routing saves require explicit changes and an editing baseline')
    validate_route_update(payload['changes'], payload['base_revision'])
    if 'slot_status_changes' in payload:
        if not isinstance(payload['slot_status_changes'], dict):
            raise RouteError('Expected explicit slot availability changes')
        SlotAvailability(payload['slot_status_changes'])
    if 'topology_changes' in payload:Topology().patched(payload['topology_changes'])
    if 'discovery_approval' in payload:encode_route_update(payload['changes'],payload['base_revision'],discovery_approval=payload['discovery_approval'])
    return payload


def routing_document(raw):
    """Read-only parsing of the canonical [ace] block; hash the entire file.

    Full-file fingerprint also catches edits to dependent settings or overrides.
    No configuration code is evaluated.
    """
    if not isinstance(raw, bytes) or len(raw) > 4 * 1024 * 1024:
        raise RouteError('Invalid or oversized ACE configuration')
    try:
        text = raw.decode('utf-8')
        blocks = re.findall(r'^\[ace\][ \t]*\r?\n(.*?)(?=^\[|\Z)', text, re.M | re.S)
        if len(blocks) != 1:
            raise RouteError('Exactly one [ace] configuration section is required')
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string('[ace]\n' + blocks[0])
        values = parser['ace']
        return {'revision': hashlib.sha256(raw).hexdigest(),
                'routes': Routes(values.get('flexi_routes', '{}'), values.get('flexi_slot_status', '{}'), values.get('flexi_topology','{}')),
                'production': values.get('flexi_production', '{}'),
                'test_profiles': values.get('flexi_test_profiles', '{}')}
    except (UnicodeError, configparser.Error) as exc:
        raise RouteError('Cannot read routing baseline: ' + str(exc)) from exc
