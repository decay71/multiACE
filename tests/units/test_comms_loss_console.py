"""Console visibility of a comms loss, and the staged-mismatch refusals
during a print (support report 2026-10-26).

Before: the silence watchdog and the V2 threads pre-set the failed flag,
so the only console line (in _handle_per_ace_failure, gated on
`not was_failed`) never fired for them; three reconnect attempts and the
pause waiting behind a running swap were logging.info only. The user saw
nothing for minutes until the pause landed.

Before: a staged-slot mismatch at a swap (code 204) or load (code 203)
raised through _ace_error with the CodedException defaults, i.e.
action='cancel' - a repairable state ended the print.
"""
import ast
import importlib

from . import harness as H


def _src(mod):
    H.load_ace()
    m = importlib.import_module(H.PKG + '.' + mod)
    return open(m.__file__).read()


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == name:
            return n
    raise AssertionError(name + ' not found')


def _calls(node, attr):
    return [c for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == attr]


def _const_args(call):
    return [a.value for a in call.args if isinstance(a, ast.Constant)]


# --- point 2: one console warn per loss, at the reconnect entry ---------

def test_reconnect_entry_warns_once_on_the_console():
    tree = ast.parse(_src('ace'))
    fn = _func(tree, '_reconnect_or_pause')
    warns = [c for c in _calls(fn, 'log_warn')]
    assert len(warns) == 1
    t = _calls(warns[0], '_t')
    assert t and 'msg.ace_comms_lost_reconnecting' in _const_args(t[0])
    # latched per unit so a repeated entry for the same loss stays quiet
    src = ast.get_source_segment(_src('ace'), fn)
    assert 'if idx not in self._comms_loss_warned' in src
    assert 'self._comms_loss_warned.add(idx)' in src


def test_failure_handler_skips_the_warn_the_ladder_already_gave():
    src = _src('ace')
    fn = _func(ast.parse(src), '_handle_per_ace_failure')
    seg = ast.get_source_segment(src, fn)
    i = seg.index("msg.ace_serial_failed")
    assert 'if idx not in self._comms_loss_warned' in seg[:i]


def test_open_ace_clears_the_warn_latch():
    src = _src('ace')
    fn = _func(ast.parse(src), '_open_ace')
    seg = ast.get_source_segment(src, fn)
    a = seg.index('self._serial_failed_per_ace[idx] = False')
    b = seg.index('self._comms_loss_warned.discard(idx)')
    assert b > a and b - a < 200


def test_pause_says_it_waits_behind_a_running_swap_or_load():
    src = _src('ace')
    fn = _func(ast.parse(src), '_schedule_comms_pause')
    seg = ast.get_source_segment(src, fn)
    warn = seg.index('msg.ace_comms_pause_deferred')
    wait = seg.index("self.gcode.run_script('')")
    first_check = seg.index("'before waiting'")
    assert first_check < warn < wait
    gate = seg[first_check:warn]
    assert '_swap_in_progress or self._loading_heads' in gate


def test_i18n_has_both_console_keys():
    import json
    for lang in ('en', 'de', 'zh'):
        d = json.load(open('multiace/i18n/%s.json' % lang,
                           encoding='utf-8'))
        for k in ('ace_comms_lost_reconnecting', 'ace_comms_pause_deferred',
                  'pause_swap_staged_mismatch'):
            assert k in d['msg'], (lang, k)
        assert '{ace}' in d['msg']['ace_comms_lost_reconnecting']
        assert '{error}' in d['msg']['ace_comms_lost_reconnecting']
        for ph in ('{head}', '{sa}', '{ss}', '{la}', '{ls}'):
            assert ph in d['msg']['pause_swap_staged_mismatch'], (lang, ph)


# --- point 3: staged mismatch keeps the print ---------------------------

class _Gcmd:
    def error(self, message=None, **kw):
        kw['message'] = message
        return kw


def _ace_error_rig():
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')

    class Ace(object):
        _ace_error = A.MultiAce._ace_error
    return Ace()


def test_ace_error_default_keeps_the_phase1_contract():
    e = _ace_error_rig()._ace_error(_Gcmd(), 'x', code=203, head=2)
    assert e['id'] == 525 and e['code'] == 203 and e['index'] == 2
    assert 'level' not in e and 'action' not in e


def test_ace_error_keep_print_raises_without_cancelling():
    e = _ace_error_rig()._ace_error(_Gcmd(), 'x', code=203, head=2,
                                    keep_print=True)
    assert e['id'] == 525 and e['code'] == 203
    assert e['action'] == 'none' and e['level'] == 2


def test_swap_staged_mismatch_pauses_while_printing():
    src = _src('ace_swap')
    fn = _func(ast.parse(src), '_cmd_ACE_SWAP_HEAD_body')
    seg = ast.get_source_segment(src, fn)
    site = seg.index('code=204')
    before = seg[:site]
    # the pause branch sits right before the idle refusal and is gated on
    # a running print; it restores head + position first (the §12 pattern)
    i = before.rindex('if self._is_actively_printing():')
    branch = before[i:]
    assert "swap_status = 'staged_mismatch'" in branch
    assert '_swap_back_to_orig_for_pause' in branch
    assert '_restore_pos_for_pause(saved_pos)' in branch
    assert "msg.pause_swap_staged_mismatch" in branch
    assert branch.index('_restore_pos_for_pause') \
        < branch.index('_pause_for_recovery')


def test_load_staged_mismatch_keeps_a_paused_print():
    src = _src('ace')
    fn = _func(ast.parse(src), '_cmd_ACE_LOAD_HEAD_body')
    seg = ast.get_source_segment(src, fn)
    site = seg.index('code=203')
    tail = seg[site:site + 200]
    assert 'keep_print=self._is_print_active_or_paused()' in tail


def test_print_active_or_paused_helper():
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')

    class PS:
        def __init__(self, s):
            self.state = s

    class Printer:
        def __init__(self, s):
            self._ps = PS(s) if s is not None else None

        def lookup_object(self, name, default=None):
            return self._ps if self._ps is not None else default

    class Ace(object):
        _is_print_active_or_paused = A.MultiAce._is_print_active_or_paused
    for s, want in (('printing', True), ('paused', True),
                    ('standby', False), ('complete', False), (None, False)):
        a = Ace()
        a.printer = Printer(s)
        assert a._is_print_active_or_paused() is want, s
