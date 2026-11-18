# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit T2 sensor-test workflow. No automatic homing, heating or toolchange.

Nozzle seating/extrusion and print dispatch are deliberately outside this
controller. Preparing it creates the operator-requested recovery journal, not a
permission to move. Hardware calls can occur only after arm AND run.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
from .ace_flexi_interlocks import AtomicJournal
from .ace_flexi_executor import LiveCoordinator,Executor,ExecutionError
from .ace_flexi_transport import ACETransport
from .ace_flexi_authorization import TestAuthorization
from .ace_flexi_profiles import TestProfiles

class ControlledTest:
    def __init__(self,ace,provider,thermal,journal_path,clock,profiles=None):
        self.ace=ace;self.provider=provider;self.thermal=thermal;self.clock=clock
        self.c=LiveCoordinator(provider.routes,provider,journal=AtomicJournal(journal_path),clock=clock,max_age=5.)
        self.authorization=TestAuthorization(self.c)
        self.transport=ACETransport(ace,self.c,authorize=self.authorization.allows,clock=clock,step_mm=5,speed=10)
        self.executor=Executor(self.c,self.transport,authorization=self.authorization)
        self.request=None;self.profiles=profiles or TestProfiles(provider.routes);self.profile_status=None
    def prepare(self,operation,ace,slot,head,target,distance,timeout):
        if self.executor.job is not None:raise ExecutionError('An operation is active')
        if head!=2:raise ExecutionError('First controlled test is restricted to T2')
        if operation not in ('load','unload'):raise ExecutionError('Only sensor-test load/unload is supported')
        self.c.routes.require_destination(ace,slot,head)
        if operation=='load':self.c.routes.require_available(ace,slot)
        resolved=self.profiles.resolve(ace,slot,head,operation,target,distance,timeout)
        self.authorization.revoke();self.profile_status=resolved
        if operation=='load':self.provider.invalidate(head)
        if not resolved['complete']:
            self.request=None;self.thermal.profiles.pop(head,None);self.thermal.stable.pop(head,None)
            return self.readiness()
        values=resolved['values'];target=values['target_temp'];timeout=values['timeout_s']
        distance=values['load_limit_mm' if operation=='load' else 'retract_limit_mm']
        self.executor._limits(distance,timeout);self.thermal.set_profile(head,target)
        self.request=(operation,ace,slot,head,'PLA',distance,timeout)
        return self.readiness()
    def readiness(self):
        if self.request is None:return {'ready':False,'reason':'Route-specific test parameters are incomplete','profile':self.profile_status,'execution_enabled':False}
        operation,ace,slot,head,_,distance,timeout=self.request
        try:
            s=self.provider.read();d=self.provider.device(s,ace);self.provider.source_binding(s,head)
            source_ready=d.get('materials',{}).get(slot)=='PLA' and (operation!='load' or d.get('slots',{}).get(slot) is True)
            recovery=bool(self.c.snapshot().get('recovery_required'))
        except Exception as exc:return {'ready':False,'reason':str(exc),'execution_enabled':False}
        thermal=self.thermal.assess(head)
        return {'ready':source_ready and not recovery and thermal['ready'] and self.provider.path(s,head)['state']==('clear' if operation=='load' else 'occupied'),
                'thermal':thermal,'path':self.provider.path(s,head),'source':[ace,slot],'head':head,
                'execution_enabled':False,'test_stage':'toolhead_sensor','ready_to_print':False}
    def confirm_clear(self,confirmation):
        if self.executor.job is not None:raise ExecutionError('Stop the active operation before confirming path clearance')
        if self.request is None:raise ExecutionError('Prepare a test first')
        head=self.request[3]
        self.provider.attest_clear(head,self.c.routes.sources(head),confirmation)
        return self.readiness()
    def arm(self,confirmation):
        if self.request is None:raise ExecutionError('Prepare a test first')
        operation,ace,slot,head,_,distance,timeout=self.request
        self.authorization.arm(operation,ace,slot,head,distance,timeout,confirmation)
    def run(self):
        if self.request is None:raise ExecutionError('Prepare and arm a test first')
        return self.executor.start(*self.request)
    def tick(self):
        result=self.executor.tick()
        if self.executor.job is None:self.authorization.revoke()
        return result
    def cancel(self):
        self.authorization.revoke()
        if self.executor.job is None:return {'outcome':'not_running'}
        return self.executor.cancel()
    def recover_clear(self,confirmation):
        if self.executor.job is not None:raise ExecutionError('Confirm stopped operation before recovery')
        if self.request is None:raise ExecutionError('Prepare explicit recovery source first')
        self.c.acknowledge_clear_recovery(self.request[3],confirmation)
    def recover_loaded(self,confirmation):
        if self.executor.job is not None:raise ExecutionError('Stop active operation before recovery')
        if self.request is None:raise ExecutionError('Prepare the explicitly traced unload source first')
        operation,ace,slot,head,_,_,_=self.request
        if operation!='unload':raise ExecutionError('Loaded recovery is only for an explicitly prepared unload')
        self.c.acknowledge_loaded_recovery(head,(ace,slot),confirmation)

    def status(self):return dict(self.executor.status(),readiness=self.readiness(),armed=self.authorization.armed is not None,ready_to_print=False)
