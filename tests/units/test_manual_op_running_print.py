"""A hand-typed load/unload is ignored while the print runs unpaused."""
import types

from .harness import load_ace

M = load_ace().MultiAce


class Gcmd:
    def get_command(self):
        return 'ACE_LOAD_HEAD'


def stub(printing, swap=False, from_sd=False):
    s = types.SimpleNamespace()
    s.warned = []
    s._swap_in_progress = swap
    s._is_actively_printing = lambda: printing
    sd = types.SimpleNamespace(is_cmd_from_sd=lambda: from_sd)
    s.printer = types.SimpleNamespace(lookup_object=lambda n, d=None: sd)
    s._t = lambda key, **kw: key
    s._disp = lambda n: n + 1
    s.log_warn = s.warned.append
    return s


def blocked(s):
    return M._manual_op_in_running_print(s, Gcmd(), 2)


def test_ignored_in_a_running_print():
    s = stub(printing=True)
    assert blocked(s) is True
    assert s.warned == ['msg.manual_op_print_running']


def test_allowed_when_paused_or_idle():
    s = stub(printing=False)
    assert blocked(s) is False and s.warned == []


def test_file_lines_and_swaps_pass():
    assert blocked(stub(printing=True, from_sd=True)) is False
    assert blocked(stub(printing=True, swap=True)) is False
