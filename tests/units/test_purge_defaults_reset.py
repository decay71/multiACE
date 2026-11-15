"""ACE_SET_PURGE DEFAULTS=1: own purge options go back to their defaults,
live and in the config, and only the ones that were changed are written."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'klipper'))

from tests.units import harness  # noqa: E402,F401
from extras import ace as ace_mod  # noqa: E402


def _stub(bg=None, fail=None, **attrs):
    written, said = [], []
    base = dict(swap_purge_length=0, swap_purge_floor=0, swap_purge_cap=0,
                swap_purge_scale=100)
    base.update(attrs)
    s = types.SimpleNamespace(
        printer=types.SimpleNamespace(
            lookup_object=lambda n, d=None: bg if n == 'ace_bg_swap' else d),
        log_always=said.append, **base)

    def write(option, text, section='ace'):
        written.append((section, option, text))
        return fail
    s._cfg_write_ace_option = write
    return s, written, said


def test_changed_options_are_reset_and_written():
    bg = types.SimpleNamespace(prime_scale=0.35, prime_extra=0.)
    s, written, said = _stub(bg=bg, swap_purge_floor=1, swap_purge_scale=150)
    ace_mod.MultiAce._purge_defaults(s)
    assert (s.swap_purge_floor, s.swap_purge_scale) == (0, 100)
    assert (bg.prime_scale, bg.prime_extra) == (1.0, 40.)
    assert sorted(written) == [
        ('ace', 'swap_purge_floor', '0'),
        ('ace', 'swap_purge_scale', '100'),
        ('ace_bg_swap', 'prime_extra', '40'),
        ('ace_bg_swap', 'prime_scale', '1.0')]
    assert 'WARNING' not in said[0]


def test_nothing_set_writes_nothing():
    s, written, said = _stub(bg=types.SimpleNamespace(prime_scale=1.0,
                                                      prime_extra=40.))
    ace_mod.MultiAce._purge_defaults(s)
    assert written == []
    assert 'nothing was changed' in said[0]


def test_failed_config_write_is_reported_but_applied():
    s, written, said = _stub(swap_purge_cap=90, fail='read-only')
    ace_mod.MultiAce._purge_defaults(s)
    assert s.swap_purge_cap == 0
    assert 'WARNING' in said[0] and 'swap_purge_cap' in said[0]
