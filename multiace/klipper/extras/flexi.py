# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent MultiACE Flexi plugin entry point.

The module owns the ``[flexi]`` section, discovers the optional MultiACE
backend only after ``klippy:ready``, and exposes read-only status. When
``enforce`` is enabled it installs a version-pinned H1 boundary adapter around
the backend's existing ``send_request_to``; that adapter delegates all
decisions to the backend's own motor gate and never dispatches anything.

When ``enforce_h2`` is also enabled, it installs the independent H2 provider
and a version-pinned interception at ``MultiAce._run_tipform``, the first
movement-capable MultiACE script boundary. H2 denies before Klipper dispatch;
it is deliberately not global G-code interception.

It never commands filament movement, never calls an ACE transport method, and
never edits ``ace.py`` or a printer configuration.

Removal is file-scoped: delete the ``[flexi]`` include file, this module,
``flexi_h1_adapter.py``, ``flexi_h2.py`` and ``flexi_h2_interceptor.py``;
``[ace]``, ``printer.cfg`` and the OEM firmware are untouched.
"""
# @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
# optional fourth operating mode; original MultiACE modes must not depend on it.
import json
import logging

COMMAND = 'FLEXI_STATUS'
REQUIRED_BACKEND = ('send_request_to',)


class FlexiPlugin:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.enabled = config.getboolean('enabled', True)
        self.required = config.getboolean('require_multiace', False)
        self.enforce = config.getboolean('enforce', True)
        self.enforce_h2 = config.getboolean('enforce_h2', True)
        self.state = 'waiting' if self.enabled else 'disabled'
        self.reason = None if self.enabled else 'disabled by configuration'
        self.backend = None
        self.adapter = None
        self.h2_provider = None
        self.h2 = None
        self.authorization = 'not-installed'
        self.printer.register_event_handler('klippy:ready', self._on_ready)
        self.printer.register_event_handler('klippy:disconnect', self._on_disconnect)
        self.gcode.register_command(
            COMMAND, self.cmd_FLEXI_STATUS,
            desc='Report Flexi plugin state and H1 adapter state (read-only)')

    def _on_disconnect(self):
        if self.enabled:
            self.state = 'disconnected'

    def _refuse(self, message):
        factory = getattr(self.printer, 'config_error', None)
        raise (factory(message) if factory else RuntimeError(message))

    def _on_ready(self):
        if not self.enabled:
            self.state = 'disabled'
            return
        if self.backend is not None:
            self.state = 'ready'
            return
        backend = self.printer.lookup_object('ace', None)
        if backend is None:
            self.state = 'multiace_missing'
            self.reason = 'no [ace] section loaded'
            if self.required:
                self._refuse('[flexi] require_multiace is set but no [ace] '
                             'section is loaded')
            logging.info('[flexi] MultiACE backend not present; plugin idle')
            return
        missing = [name for name in REQUIRED_BACKEND
                   if not hasattr(backend, name)]
        if missing:
            self.state = 'incompatible'
            self.reason = 'backend missing: ' + ', '.join(missing)
            if self.required or self.enforce:
                self._refuse('[flexi] H1 enforcement unavailable '
                             '(incompatible): ' + self.reason)
            logging.warning('[flexi] incompatible MultiACE backend: %s',
                            self.reason)
            return
        if not self.enforce:
            self.backend = backend
            self.state = 'ready'
            self.reason = 'enforce is disabled; H1 adapter not installed'
            self.authorization = 'observe-only'
            logging.info('[flexi] observe-only; MultiACE behavior unchanged')
            return

        try:
            from .flexi_h1_adapter import H1Adapter
            adapter = H1Adapter(backend)
            installed, state, reason = adapter.install()
        except Exception as exc:
            installed, state, reason = (
                False, 'adapter_error',
                '%s: %s' % (type(exc).__name__, str(exc)[:160]))
        if not installed:
            self.state = state
            self.reason = reason
            logging.error('[flexi] H1 enforcement unavailable (%s): %s',
                          state, reason)
            # Fail closed: never continue believing a boundary is installed
            # when it is not. Setting enforce: False is the explicit way to
            # run status-only with original MultiACE behavior.
            self._refuse('[flexi] H1 enforcement unavailable (%s): %s'
                         % (state, reason))
            return
        self.adapter = adapter
        self.backend = backend
        self.state = 'ready'
        self.reason = None
        self.authorization = 'h1-adapter'

        if self.enforce_h2:
            try:
                from .flexi_h2 import H2Provider
                from .flexi_h2_interceptor import H2Interceptor
                provider = H2Provider(enabled=True)
                h2 = H2Interceptor(backend, provider)
                h2_installed, h2_state, h2_reason = h2.install()
            except Exception as exc:
                h2_installed, h2_state, h2_reason = (
                    False, 'h2_adapter_error',
                    '%s: %s' % (type(exc).__name__, str(exc)[:160]))
            if not h2_installed:
                self.state = h2_state
                self.reason = h2_reason
                logging.error('[flexi] H2 enforcement unavailable (%s): %s',
                              h2_state, h2_reason)
                self._refuse('[flexi] H2 enforcement unavailable (%s): %s'
                             % (h2_state, h2_reason))
                return
            self.h2_provider = provider
            self.h2 = h2

    def _h2_status(self):
        if self.h2 is not None:
            return self.h2.status()
        if not self.enabled:
            enforcement = 'inactive'
        elif not self.enforce:
            enforcement = 'observe-only'
        elif not self.enforce_h2:
            enforcement = 'disabled'
        else:
            enforcement = 'unavailable'
        return {
            'installed': False,
            'enforcement': enforcement,
            'physical_confirmation': 'not-observed',
            'reason': ('H2 interception is installed only when enforce and '
                       'enforce_h2 are both true'),
        }

    def _adapter_status(self):
        if self.adapter is not None:
            return self.adapter.status()
        return {
            'installed': False,
            'enforcement': ('observe-only' if self.backend is not None
                            else 'inactive'),
            'physical_confirmation': 'not-observed',
        }

    def get_status(self, eventtime=None):
        return {
            'enabled': self.enabled,
            'state': self.state,
            'reason': self.reason,
            'multiace': self.backend is not None,
            'movement': 'never',
            'authorization': self.authorization,
            'adapter': self._adapter_status(),
            'h2': self._h2_status(),
        }

    def cmd_FLEXI_STATUS(self, gcmd):
        gcmd.respond_info(json.dumps(self.get_status(), sort_keys=True))


def load_config(config):
    return FlexiPlugin(config)
