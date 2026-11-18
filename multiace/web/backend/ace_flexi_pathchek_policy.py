# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared native PathChek request schema. No hardware or filesystem access."""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import json, math, re
try:
    from .ace_flexi import Routes, RouteError
except ImportError:
    from ace_flexi import Routes, RouteError

FIELDS = {
 'qualify': {'source','record','confirmation'},
 'start': {'source','client','mode','initial_min_mm','initial_max_mm','cleared_sources','confirmation'},
 'heartbeat': {'token','client'},
 'step': {'token','client','sequence','direction','length','confirmation'},
 'confirm_stop': {'token','client','sequence','position_min_mm','position_max_mm','reference','confirmation'},
 'complete': {'token','client','sequence','cleared_sources','reference','confirmation'},
 'cancel': {'token','client'},
 'recover': {'disposition','token','cleared_sources','position_min_mm','position_max_mm','reference','confirmation'},
}

def validate(payload, routes):
    if not isinstance(payload,dict) or payload.get('action') not in FIELDS:
        raise RouteError('Unknown PathChek operation')
    action=payload['action']
    if set(payload) != FIELDS[action] | {'action'}:
        raise RouteError('Unexpected or missing PathChek fields')
    if 'source' in payload:
        source=payload['source']
        if not isinstance(source,list) or len(source)!=2 or any(type(n) is not int for n in source):
            raise RouteError('Select an explicit zero-based ACE and slot')
        routes.require_available(*source)
    for key in ('token','client'):
        if key in payload and (not isinstance(payload[key],str) or not re.fullmatch('[0-9a-f]{32}',payload[key])):
            raise RouteError('Invalid operation/client identity')
    if 'sequence' in payload and (type(payload['sequence']) is not int or payload['sequence']<0):
        raise RouteError('Expected current PathChek state sequence')
    for key in ('initial_min_mm','initial_max_mm','position_min_mm','position_max_mm','length'):
        if key in payload and (type(payload[key]) not in (int,float) or not math.isfinite(payload[key]) or payload[key]<0 or (key=='length' and payload[key]<=0)):
            raise RouteError('Invalid measured position or bounded movement')
    if 'cleared_sources' in payload:
        items=payload['cleared_sources']
        if not isinstance(items,list) or len(items)>32 or any(not isinstance(s,list) or len(s)!=2 or any(type(n)is not int for n in s) for s in items):
            raise RouteError('Explicit shared-branch inspection list required')
    if action=='recover' and payload['disposition'] not in ('parked','removed'):
        raise RouteError('Select explicitly retained parking or fully removed/isolated recovery')
    if action=='start' and payload['mode'] not in ('commissioning','qualified'):
        raise RouteError('Unknown PathChek session mode')
    if action=='step' and payload['direction'] not in ('feed','retract'):
        raise RouteError('Invalid movement direction')
    for key in ('confirmation','reference'):
        if key in payload and (not isinstance(payload[key],str) or not payload[key].strip() or len(payload[key])>512):
            raise RouteError('Explicit operator confirmation/reference required')
    return payload

def command(payload,routes):
    validate(payload,routes)
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>16000:raise RouteError('PathChek request is oversized')
    return 'ACE_FLEXI_PATHCHEK DATA='+raw.hex()
