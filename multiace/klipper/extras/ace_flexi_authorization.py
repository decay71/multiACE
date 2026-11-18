# SPDX-License-Identifier: GPL-3.0-or-later
"""Single-use, expiring authorization for an explicitly approved sensor test.

No global unlock, mode change or persistent approval. Nothing is armed at import,
construction, prepare or restart. A future approved operator action must arm it.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import math
import threading
from .ace_flexi_executor import ExecutionError

class TestAuthorization:
    __test__=False
    def __init__(self,coordinator):self.c=coordinator;self.armed=None;self.active=None;self.lock=threading.RLock()
    def arm(self,operation,ace,slot,head,distance,timeout,confirmation):
        with self.lock:
            if self.c.journal is None:raise ExecutionError('Durable recovery journal is required')
            if confirmation!='CONTROLLED_PLA_SENSOR_TEST':raise ExecutionError('Explicit controlled-test confirmation required')
            if self.armed is not None or self.active is not None:raise ExecutionError('Revoke previous authorization before arming another operation')
            if type(head) is not int or head!=2:raise ExecutionError('First controlled test is restricted to T2')
            if operation not in ('load','unload'):raise ExecutionError('No switching, printing or failover authorization')
            self.c.routes.require_destination(ace,slot,head)
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (distance,timeout)) or not 0<distance<=3000 or not 0<timeout<=1200:
                raise ExecutionError('Invalid controlled-test bounds')
            self.c.reconcile();s=self.c.provider.read();self.c.provider.device(s,ace)
            if s.get('idle') is not True or s.get('motion_ready_heads',{}).get(head,s.get('motion_ready')) is not True:raise ExecutionError('Selected head is not idle and thermally ready')
            if self.c.snapshot().get('recovery_required'):raise ExecutionError('Recovery must be completed before arming')
            p=self.c.provider.path(s,head)
            if operation=='load' and p['state']!='clear':raise ExecutionError('Explicit full shared-path clearance is required')
            if operation=='unload' and (p['state']!='occupied' or p['owner']!=[ace,slot]):raise ExecutionError('Unload source must match established owner')
            self.armed={'operation':operation,'ace':ace,'slot':slot,'head':head,'material':'PLA','distance':distance,'timeout':timeout,'issued':self.c.clock(),'expires':self.c.clock()+30.}
    def check_request(self,operation,ace,slot,head,material,distance,timeout):
        with self.lock:
            a=self.armed
            return bool(a and a['issued']<=self.c.clock()<a['expires'] and all(a[k]==v for k,v in [('operation',operation),('ace',ace),('slot',slot),('head',head),('material',material),('distance',distance)]) and math.isclose(a['timeout'],timeout,rel_tol=0,abs_tol=1e-8))
    def bind(self,job):
        with self.lock:
            if not self.check_request(job['operation'],job['ace'],job['slot'],job['head'],'PLA',job['max_distance_mm'],job['deadline']-job['started']):
                raise ExecutionError('Controlled-test authorization expired or does not match operation')
            self.active=copy.deepcopy(job);self.armed=None
    def allows(self,job):
        with self.lock:
            return bool(self.active and self.active['started']<=self.c.clock()<self.active['deadline'] and all(self.active.get(k)==job.get(k) for k in ['token','ace','slot','head','operation','identity','source_identity','source_binding','max_distance_mm','deadline','epoch']))
    def revoke(self):
        with self.lock:self.armed=None;self.active=None
