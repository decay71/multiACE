# SPDX-License-Identifier: GPL-3.0-or-later
"""Flexi SourceSelect policy. Configuration is not a hardware capability grant."""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import copy
import re
try:
    from .ace_flexi_production import ProductionError, digest
except ImportError:
    from ace_flexi_production import ProductionError, digest


class SourceSelect:
    TYPES = ('ace', 'stock', 'manual')

    def __init__(self, routes, config=None):
        self.routes = routes
        self.config = self.validate(config or {})
        self.temporary = {}
        self.revision = 0

    @staticmethod
    def head(head):
        if type(head) is not int or head not in range(4):
            raise ProductionError('SourceSelect needs a physical toolhead T0–T3')

    @staticmethod
    def record(record):
        if not isinstance(record, dict) or set(record) - {'type', 'identity', 'material', 'color'}:
            raise ProductionError('Invalid SourceSelect fields')
        if record.get('type') not in SourceSelect.TYPES:
            raise ProductionError('Unknown filament source type')
        for key in ('identity', 'material', 'color'):
            value = record.get(key, '')
            if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
                raise ProductionError('Invalid manual spool metadata')
        if record.get('color') and not re.fullmatch(r'#[0-9a-fA-F]{6}', record['color']):
            raise ProductionError('Color must be #RRGGBB')
        if record['type'] == 'manual' and not record.get('identity', '').strip():
            raise ProductionError('Manual/external spool identity is required')
        return copy.deepcopy(record)

    @classmethod
    def validate(cls, config):
        if not isinstance(config, dict) or set(config) - set('0123'):
            raise ProductionError('SourceSelect configuration needs physical head keys')
        return {key: cls.record(value) for key, value in config.items()}

    def effective(self, head):
        self.head(head)
        return copy.deepcopy(self.temporary.get(str(head), self.config.get(str(head), {'type': 'ace'})))

    def require_ace(self, source, allow_unavailable=False):
        head = self.routes.destination(*source)
        if not allow_unavailable:
            self.routes.require_available(*source)
        if self.effective(head)['type'] != 'ace':
            raise ProductionError('SourceSelect blocks ACE operations into T%d: %s source selected' % (head, self.effective(head)['type']))
        return head

    def override_manual(self, head, metadata, idle=False):
        self.head(head)
        if idle is not True:
            raise ProductionError('Stop all filament/print operations before changing sources')
        self.temporary[str(head)] = self.record(dict(metadata, type='manual'))
        self.revision += 1

    def restore(self, head, idle=False):
        self.head(head)
        if idle is not True:
            raise ProductionError('Stop operations before restoring the configured source')
        self.temporary.pop(str(head), None)
        self.revision += 1

    def context(self):
        return digest({'routes': self.routes.context(), 'configured': self.config,
                       'temporary': self.temporary, 'revision': self.revision})

    def status(self):
        return {'name': 'Flexi SourceSelect', 'heads': {str(h): self.effective(h) for h in range(4)},
                'configured': copy.deepcopy(self.config), 'temporary_heads': sorted(self.temporary),
                'context': self.context(), 'ace_routes_preserved': self.routes.as_dict(),
                'configuration_only': True, 'execution_qualified': False,
                'pathchek_required_for_source': False,
                'stock_dispatch_enabled': False, 'native_manual_dispatch_enabled': False,
                'note': 'PathCheck is optional for source configuration. External source selection blocks ACE automation. Stock/manual hardware integration needs separate qualification. Temporary overrides expire on restart; execution permissions never survive restart.'}
