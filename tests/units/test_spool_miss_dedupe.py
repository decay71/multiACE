"""The 'tag ... matches no table entry' line logs ONCE per (slot, code):
the V2 heartbeat re-ingests the same read every second, and an unknown
card (field log 2026-10-26: a Bambu UID on MMU 1 slot 1) logged at 1 Hz,
309 lines in five minutes. Real methods on a stub."""
import importlib
import logging
import os
import sys
import types


class H:
    EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                          'multiace', 'klipper', 'extras')
    PKG = 'multiace_spool_miss_extras'

    @staticmethod
    def load_ace():
        name = H.PKG + '.ace'
        if name in sys.modules:
            return sys.modules[name]
        pkg = types.ModuleType(H.PKG)
        pkg.__path__ = [H.EXTRAS]
        sys.modules[H.PKG] = pkg
        return importlib.import_module(name)


def _rig():
    A = H.load_ace()
    SP = sys.modules[H.PKG + '.ace_spool']

    _NAMES = ('_spool_bind_tag_codes', '_spool_bind_by_tag',
              '_spool_miss_forget', '_spool_bind', '_sku_canon', '_sku_codes',
              '_sku_bases', '_sku_base', '_spool_by_sku', '_spools_with_base',
              '_spool_key', '_spool_label')

    class Ace(object):
        # copied as class-dict entries so staticmethods stay static
        locals().update({n: SP.SpoolMixin.__dict__[n] for n in _NAMES})

        def __init__(self):
            self._spools = {}
            self._spool_binding = {}
            self._spool_miss_logged = {}
            self.spoolman_url = ''
            self.said = []
            self._v1_tag_seen = {}

        def _disp(self, i):
            return i + 1

        def _raw(self, i):
            return i

        def log_always(self, msg):
            self.said.append(msg)

        def _spoollink_active(self):
            return False

        def _slot_read_sku(self, a, s):
            return ''

        def _slot_read_sku_raw(self, a, s):
            return ''

        def _save_spool_db(self, backup=False):
            pass
    return Ace()


def _miss_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if 'matches no table entry' in r.getMessage()]


def test_same_code_logs_once(caplog):
    ace = _rig()
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            ace._spool_bind_tag_codes(0, 0, '', '88532BB3')
    assert len(_miss_lines(caplog)) == 1


def test_changed_code_logs_again(caplog):
    ace = _rig()
    with caplog.at_level(logging.INFO):
        ace._spool_bind_tag_codes(0, 0, '', 'AAAA1111')
        ace._spool_bind_tag_codes(0, 0, '', 'AAAA1111')
        ace._spool_bind_tag_codes(0, 0, '', 'BBBB2222')
        ace._spool_bind_tag_codes(0, 1, '', 'AAAA1111')   # other slot
    assert len(_miss_lines(caplog)) == 3


def test_gate_empty_or_binding_resets(caplog):
    ace = _rig()
    with caplog.at_level(logging.INFO):
        ace._spool_bind_tag_codes(0, 0, '', 'AAAA1111')
        ace._spool_miss_forget(0, 0)                      # slot went empty
        ace._spool_bind_tag_codes(0, 0, '', 'AAAA1111')
        ace._spool_bind(ace._spool_key(0, 0), 7)         # a binding lands
        ace._spool_bind_tag_codes(0, 0, '', 'AAAA1111')
    assert len(_miss_lines(caplog)) == 3
