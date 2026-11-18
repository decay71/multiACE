# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only thermal and physical tool-selection prerequisites. Never heats/moves."""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import math
import time

class ThermalGuard:
    def __init__(self,printer,clock=time.monotonic):
        self.printer=printer;self.clock=clock;self.profiles={};self.stable={};self.last_sample={}

    def set_profile(self,head,target,tolerance=3.,soak=5.,verified_range=None):
        # Explicit approved PLA test recipe, not a heater-setting operation.
        lo,hi=(190,260) if verified_range is None else verified_range
        if (any(type(v) not in (int,float) or not math.isfinite(v) for v in (lo,hi)) or not 0<lo<=hi<=400):raise ValueError('Invalid verified material/tip temperature range')
        if (type(head) is not int or head not in range(4)
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in (target,tolerance,soak))
                or not lo<=target<=hi or not 0<tolerance<=5 or not 3<=soak<=60):
            raise ValueError('Invalid explicit PLA thermal profile')
        self.profiles[head]={'target':target,'tolerance':tolerance,'soak':soak};self.stable.pop(head,None)

    def assess(self,head):
        expected='extruder' if head==0 else 'extruder%d'%head
        result={'head':head,'extruder':expected,'ready':False}
        try:
            p=self.profiles.get(head)
            if p is None:raise ValueError('No approved thermal target for this toolhead')
            result['required_target']=p['target']
            lookup=self.printer.lookup_object;toolhead=lookup('toolhead',None);ext=lookup(expected,None)
            if toolhead is None or ext is None:raise ValueError('Toolhead/extruder object unavailable')
            if toolhead.get_extruder().get_name()!=expected:raise ValueError('Selected toolhead contradicts requested route')
            if set(toolhead.get_status(0).get('homed_axes',''))!=set('xyz'):raise ValueError('XYZ homing is required')
            # Match the existing U1 park-detector semantics, not just the logical
            # active extruder: exactly the requested tool must be ACTIVATE.
            exts=lookup('extruder_list',None)
            if not isinstance(exts,(list,tuple)) or len(exts)!=4:raise ValueError('Physical toolhead latch evidence unavailable')
            for h,e in enumerate(exts):
                if e.get_name()!=('extruder' if h==0 else 'extruder%d'%h):raise ValueError('Extruder ordering is inconsistent')
                if getattr(e,'park_detector',None) is None:raise ValueError('Physical toolhead latch detector missing')
                if e.get_park_detector_status().get('state')!=('ACTIVATE' if h==head else 'PARKED'):
                    raise ValueError('Physical toolhead latch/park state is ambiguous')
            state=ext.get_status(0)
            if state.get('activating_move') is not False:raise ValueError('Tool change is active or unknown')
            temp,target=state.get('temperature'),state.get('target')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (temp,target)):raise ValueError('Temperature evidence is invalid')
            result.update(temperature=temp,target=target)
            if state.get('can_extrude') is not True:raise ValueError('Firmware cold-extrusion protection is not satisfied')
            if target!=p['target'] or abs(temp-p['target'])>p['tolerance']:raise ValueError('Requested heater target/temperature not reached')
            now=self.clock();last=self.last_sample.get(head)
            if last is not None and not 0<=now-last<=2.:self.stable.pop(head,None)
            self.last_sample[head]=now
            since=self.stable.setdefault(head,now)
            if now<since:raise ValueError('Thermal clock regressed')
            result['stable_seconds']=now-since
            if now-since<p['soak']:return dict(result,reason='Temperature settling time is not complete')
            return dict(result,ready=True,reason='Selected physical head is homed and thermally ready')
        except Exception as exc:
            self.stable.pop(head,None);self.last_sample.pop(head,None)
            return dict(result,reason=str(exc))
