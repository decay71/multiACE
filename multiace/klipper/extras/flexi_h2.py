# SPDX-License-Identifier: GPL-3.0-or-later
"""H2 authorization contract and provider.

H2 covers movement that MultiACE itself initiates through Klipper. It is
deliberately separate from the H1 ACE-request adapter: H1 controls motor
requests on the ACE wire, H2 controls MultiACE-owned Klipper dispatch.

The provider is pure and does not dispatch, move, mutate recovery state or
consume a token. Unknown or unclassified movement is denied. A caller must
supply an active job token; the interceptor obtains that context read-only
from the existing MultiACE workflow.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import time
import uuid


POLICY = 'flexi-h2-v2'
ORIGINS = frozenset(('multiace',))
CLASSIFICATIONS = frozenset((
    'initiate', 'continuation', 'controlled_stop', 'recovery',
    'tip_conditioning', 'unclassified',
))
TIP_ACTIONS = frozenset(('unload', 'switch', 'failover'))


class H2Denied(RuntimeError):
    """Raised by the interception layer after a denied H2 decision."""

    def __init__(self, message, decision=None):
        super().__init__(message)
        self.decision = decision


class H2Request:
    """Structured movement-authorization request. No policy lives here."""

    def __init__(self, origin, operation, classification='unclassified',
                 site=None, head=None, stepper=None, parameters=None,
                 job_token=None, job_active=False, movement_locked=True,
                 recovery_required=False, phase=None, source=None, mode=None,
                 is_movement=True, context=None):
        self.origin = str(origin or '')
        self.operation = str(operation or 'unclassified')
        self.classification = str(classification or 'unclassified')
        self.site = dict(site or {})
        self.head = head if type(head) is int else None
        self.stepper = stepper if isinstance(stepper, str) else None
        self.parameters = dict(parameters or {})
        self.job_token = job_token if isinstance(job_token, str) else None
        self.job_active = bool(job_active)
        self.movement_locked = bool(movement_locked)
        self.recovery_required = bool(recovery_required)
        self.phase = phase if isinstance(phase, str) else None
        self.source = tuple(source) if isinstance(source, (list, tuple)) else None
        self.mode = mode if isinstance(mode, str) else None
        self.is_movement = bool(is_movement)
        self.context = dict(context or {})

    def summary(self):
        return {
            'origin': self.origin,
            'operation': self.operation,
            'classification': self.classification,
            'site': dict(self.site),
            'head': self.head,
            'stepper': self.stepper,
            'has_token': self.job_token is not None,
            'job_active': self.job_active,
            'movement_locked': self.movement_locked,
            'recovery_required': self.recovery_required,
            'phase': self.phase,
            'source': list(self.source) if self.source is not None else None,
            'mode': self.mode,
            'is_movement': self.is_movement,
            'lock_reason': self.context.get('lock_reason'),
        }


class H2Decision:
    def __init__(self, allowed, reason, classification, request_id=None,
                 policy=POLICY):
        self.allowed = bool(allowed)
        self.reason = str(reason)
        self.classification = str(classification)
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self.policy = policy
        self.physical_confirmed = False
        self.filament_unloaded = False
        self.toolhead_sensor_clear = 'not-observed'

    def as_dict(self):
        return {
            'allowed': self.allowed,
            'reason': self.reason,
            'classification': self.classification,
            'request_id': self.request_id,
            'policy': self.policy,
            'physical_confirmed': False,
            'filament_unloaded': False,
            'toolhead_sensor_clear': 'not-observed',
        }


class H2Provider:
    """Independent H2 policy. Returns a decision; never raises for input."""

    def __init__(self, enabled=True, allow_recovery=False,
                 clock=time.monotonic):
        self.enabled = bool(enabled)
        self.allow_recovery = bool(allow_recovery)
        self.clock = clock
        self._last = None
        self._counters = {'allowed': 0, 'denied': 0}

    def authorize(self, request):
        if not isinstance(request, H2Request):
            return self._record(H2Request('unclassified', 'unclassified'),
                                False, 'invalid-request')
        if not self.enabled:
            return self._record(request, True, 'provider-disabled')
        if not request.is_movement:
            return self._record(request, True, 'non-motion')
        if request.origin not in ORIGINS:
            return self._record(request, False, 'unclassified-origin')
        if request.classification not in CLASSIFICATIONS:
            return self._record(request, False, 'unclassified-movement')
        if request.classification == 'unclassified':
            return self._record(request, False, 'unclassified-movement')

        token = request.job_token
        valid_token = (isinstance(token, str) and 0 < len(token) <= 256
                       and not any(ch.isspace() for ch in token))
        if (request.recovery_required
                and request.classification not in ('controlled_stop', 'recovery')):
            return self._record(request, False, 'recovery-required')

        if request.classification in ('initiate', 'continuation',
                                       'tip_conditioning'):
            if not token:
                return self._record(request, False, 'missing-job-token')
            if not valid_token:
                return self._record(request, False, 'invalid-job-token')
            if not request.job_active:
                return self._record(request, False, 'no-active-job')
            if request.movement_locked:
                return self._record(request, False, 'movement-locked')
            if request.classification == 'tip_conditioning':
                if request.operation != 'filament_tip_conditioning':
                    return self._record(
                        request, False, 'tip-conditioning-operation-mismatch')
                action = request.context.get('job_action')
                if action not in TIP_ACTIONS:
                    return self._record(
                        request, False, 'tip-conditioning-wrong-action')
                if request.phase != 'tipform':
                    return self._record(
                        request, False, 'tip-conditioning-wrong-phase')
                return self._record(
                    request, True, 'tip-conditioning-unload-phase')
            return self._record(request, True,
                                'active-job-' + request.classification)

        if request.classification == 'controlled_stop':
            if not valid_token:
                return self._record(request, False, 'missing-job-token')
            # A stop may proceed while movement is locked; it is not new motion.
            return self._record(request, True, 'controlled-stop')

        if request.classification == 'recovery':
            if not self.allow_recovery:
                return self._record(request, False, 'recovery-not-enabled')
            if not valid_token or not request.job_active:
                return self._record(request, False, 'missing-job-token')
            if request.context.get('recovery_authorized') is not True:
                return self._record(request, False, 'recovery-not-authorized')
            return self._record(request, True, 'authorized-recovery')

        return self._record(request, False, 'unclassified-movement')

    def _record(self, request, allowed, reason):
        decision = H2Decision(allowed, reason, request.classification)
        self._counters['allowed' if allowed else 'denied'] += 1
        self._last = {
            'request': request.summary(),
            'decision': decision.as_dict(),
            'at': self.clock(),
        }
        return decision

    def status(self):
        return {
            'enabled': self.enabled,
            'policy': POLICY,
            'allow_recovery': self.allow_recovery,
            'counters': dict(self._counters),
            'last': dict(self._last) if self._last is not None else None,
            'physical_confirmation': 'not-observed',
        }
