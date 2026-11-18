# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit Flexi test profiles. Never imports legacy MultiACE distances.

Toolhead defaults < ACE:slot override < explicit per-test request. None masks a
head default; missing values remain missing. No temperature or length is guessed.
Existing head_feed_length/retract options are never read or changed here.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import json,math,re
class ProfileError(ValueError):pass
FIELDS=frozenset(('load_limit_mm','retract_limit_mm','temperature_min','temperature_max','target_temp','timeout_s'))
class TestProfiles:
    def __init__(self,routes,raw=None):
        self.routes=routes
        if raw is None:raw={}
        if isinstance(raw,str):
            if len(raw)>16384:raise ProfileError('Flexi test profiles are oversized')
            def pairs(items):
                d={}
                for k,v in items:
                    if k in d:raise ProfileError('Duplicate profile key: '+k)
                    d[k]=v
                return d
            try:raw=json.loads(raw,object_pairs_hook=pairs)
            except (ValueError,TypeError) as exc:raise ProfileError('Invalid profile JSON: '+str(exc))
        if not isinstance(raw,dict) or set(raw)-{'heads','routes'}:raise ProfileError('Profiles require heads/routes objects')
        self.data={'heads':{},'routes':{}}
        for group,pattern,max_count in [('heads',r'[0-3]',4),('routes',r'[0-3]:[0-3]',16)]:
            values=raw.get(group,{})
            if not isinstance(values,dict) or len(values)>max_count:raise ProfileError('Invalid '+group+' profile table')
            for key,profile in values.items():
                if not isinstance(key,str) or not re.fullmatch(pattern,key):raise ProfileError('Invalid '+group+' profile key')
                self.data[group][key]=self._validate(profile)
    def _validate(self,profile):
        if not isinstance(profile,dict) or set(profile)-FIELDS:raise ProfileError('Unknown test-profile field')
        out={}
        for k,v in profile.items():
            if v is None:out[k]=None;continue
            if type(v) not in (int,float) or not math.isfinite(v):raise ProfileError('Profile values must be finite numbers or unset')
            valid=(0<v<=3000 if k.endswith('_mm') else 0<v<=1200 if k=='timeout_s' else 190<=v<=260 if k=='target_temp' else 100<=v<=300)
            if not valid:raise ProfileError('Invalid '+k)
            out[k]=v
        lo,hi,target=out.get('temperature_min'),out.get('temperature_max'),out.get('target_temp')
        if lo is not None and hi is not None and lo>hi:raise ProfileError('Temperature range is reversed')
        if target is not None and ((lo is not None and target<lo) or (hi is not None and target>hi)):raise ProfileError('Target contradicts verified temperature range')
        return out
    def encode(self):return json.dumps(self.data,sort_keys=True,separators=(',',':'))
    def resolve(self,ace,slot,head,operation,target=None,distance=None,timeout=None):
        self.routes.require_destination(ace,slot,head)
        if operation not in ('load','unload'):raise ProfileError('Only load/unload test profiles are supported')
        field='load_limit_mm' if operation=='load' else 'retract_limit_mm'
        values=dict(self.data['heads'].get(str(head),{}));values.update(self.data['routes'].get('%d:%d'%(ace,slot),{}))
        for key,value in [('target_temp',target),(field,distance),('timeout_s',timeout)]:
            if value is None:continue
            if key in (field,'timeout_s') and values.get(key) is not None and value>values[key]:raise ProfileError('Test request exceeds configured '+key)
            values[key]=value
        values=self._validate(values)
        missing=[k for k in (field,'temperature_min','temperature_max','target_temp','timeout_s') if values.get(k) is None]
        return {'route':'%d:%d'%(ace,slot),'head':head,'operation':operation,'values':values,'missing':missing,'complete':not missing}
