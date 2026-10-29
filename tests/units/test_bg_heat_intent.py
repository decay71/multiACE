"""bg-swap heater ownership: the heater intent observer in ace.py and the
claim/release helpers of the bg engine, driven with the real methods on
stubs (no printer, no trapq).

The field case (docx-tech, bg transport mode): the slicer's `M109 S220
T<head>` arrived while the bg cold-pull held that head's heater; step 4
then set the target to 0 and the M109 waited forever (PID check_busy never
settles on 0). A value-equal request (M109 at the engine's own pull temp)
is invisible to any comparison of targets, so the observer records the
CALL, and the release restores the newest foreign request since the claim,
else the target found at the claim, else 0; after print end always 0.
"""
import importlib
import sys
import types

from . import harness as H


class FakeHeater:
    def __init__(self, name, target=0.):
        self.name = name
        self.target_temp = target

    def get_name(self):
        return self.name


class FakePheaters:
    def __init__(self):
        self.calls = []

    def set_temperature(self, heater, temp, wait=False):
        heater.target_temp = float(temp)
        self.calls.append((heater.get_name(), float(temp), bool(wait)))


class FakeReactor:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class FakePrintStats:
    def __init__(self, state='printing'):
        self.state = state

    def get_status(self, eventtime=None):
        return {'state': self.state}


class FakePrinter:
    def __init__(self, objects):
        self.objects = objects

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


def _bg_module():
    name = H.PKG + '.ace_bg_swap'
    if name in sys.modules:
        return sys.modules[name]
    H.load_ace()
    if 'chelper' not in sys.modules:
        sys.modules['chelper'] = types.ModuleType('chelper')
    fm = H.PKG + '.force_move'
    if fm not in sys.modules:
        # Klipper extra the engine borrows calc_move_time from; not needed
        # for the heater helpers under test.
        stub = types.ModuleType(fm)
        stub.calc_move_time = None
        sys.modules[fm] = stub
        setattr(sys.modules[H.PKG], 'force_move', stub)
    return importlib.import_module(name)


def _rig(print_state='printing', with_observer=True):
    A = H.load_ace()
    BG = _bg_module()
    reactor = FakeReactor()
    pheaters = FakePheaters()
    ps = FakePrintStats(print_state)

    class Ace(object):
        _install_heat_observer = A.MultiAce._install_heat_observer
        _observed_set_temperature = A.MultiAce._observed_set_temperature
        heat_set_own = A.MultiAce.heat_set_own
        heat_foreign_since = A.MultiAce.heat_foreign_since
    ace = Ace() if with_observer else object()
    printer = FakePrinter({'heaters': pheaters, 'ace': ace,
                           'print_stats': ps})
    if with_observer:
        ace.printer = printer
        ace.reactor = reactor
        ace._ext_heat_req = {}
        ace._own_heat_depth = 0
        ace._orig_set_temperature = None
        ace._install_heat_observer()

    class Bg(object):
        _heat_claim = BG.AceBgSwap._heat_claim
        _set_heat = BG.AceBgSwap._set_heat
        _release_heat = BG.AceBgSwap._release_heat
        _print_active = BG.AceBgSwap._print_active

        def __init__(self):
            self.said = []
            self._heat_claims = {}

        def _say(self, msg):
            self.said.append(msg)

        def _dh(self, i):
            return i + 1
    bg = Bg()
    bg.printer = printer
    bg.reactor = reactor
    return ace, bg, pheaters, reactor, ps


def test_observer_records_foreign_calls_only():
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder')
    bg._set_heat(ace, ph, h, 215.)            # own
    assert ace.heat_foreign_since(h, 0.) is None
    reactor.now = 101.0
    ph.set_temperature(h, 220.)                # slicer M104/M109
    assert ace.heat_foreign_since(h, 0.) == (220., 101.0)
    assert ace.heat_foreign_since(h, 101.5) is None
    # the wrapper still drives the real heater
    assert h.target_temp == 220.
    assert ph.calls == [('extruder', 215., False), ('extruder', 220., False)]


def test_value_equal_m109_is_restored():
    """docx-tech class: standby 70 found, own pull temp 215, slicer M109
    at the SAME value during the op -> hand-back keeps 215, not 70, not 0."""
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder', 70.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    reactor.now += 30
    ph.set_temperature(h, 215.)                # M109 S215 T0, value-equal
    reactor.now += 1
    assert bg._release_heat(ace, 0, h, ph, 'cold-pull') == 215.
    assert h.target_temp == 215.
    assert any('slicer request' in s for s in bg.said)


def test_field_case_m109_220_after_claim():
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder', 70.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    reactor.now += 60
    ph.set_temperature(h, 220.)                # Orca M104 S220 T0
    reactor.now += 24
    ph.set_temperature(h, 220.)                # M109 S220 T0 before the T
    assert bg._release_heat(ace, 0, h, ph, 'cold-pull') == 220.
    assert h.target_temp == 220.


def test_no_foreign_request_restores_found_target():
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder', 70.)
    reactor.now += 1
    ph.set_temperature(h, 70.)                 # a request BEFORE the claim
    reactor.now += 1
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 250.)
    reactor.now += 40
    assert bg._release_heat(ace, 0, h, ph, 'bg load done') == 70.
    assert any('target found at takeover' in s for s in bg.said)


def test_found_zero_and_nothing_asked_cools_to_zero():
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder', 0.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    assert bg._release_heat(ace, 0, h, ph, 'cold-pull') == 0.
    assert h.target_temp == 0.
    assert not bg.said


def test_print_ended_during_op_cools_to_zero():
    """TURN_OFF_HEATERS at print end/cancel bypasses the observer; once
    print_stats is neither printing nor paused the intent is 0."""
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder', 70.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    reactor.now += 5
    ph.set_temperature(h, 220.)
    ps.state = 'cancelled'
    assert bg._release_heat(ace, 0, h, ph, 'cold-pull') == 0.
    assert h.target_temp == 0.
    assert any('print ended' in s for s in bg.said)


def test_paused_print_keeps_intent():
    ace, bg, ph, reactor, ps = _rig(print_state='paused')
    h = FakeHeater('extruder', 70.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    assert bg._release_heat(ace, 0, h, ph, 'cold-pull') == 70.


def test_display_zero_during_op_wins():
    """control/extruder_temp from the touchscreen runs through
    pheaters.set_temperature too: a foreign 0 is the newest intent."""
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder', 70.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    reactor.now += 5
    ph.set_temperature(h, 0.)
    assert bg._release_heat(ace, 0, h, ph, 'cold-pull') == 0.
    assert h.target_temp == 0.


def test_second_release_is_a_noop():
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder', 70.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    reactor.now += 5
    ph.set_temperature(h, 220.)
    assert bg._release_heat(ace, 0, h, ph, 'feed failure') == 220.
    n = len(ph.calls)
    assert bg._release_heat(ace, 0, h, ph, 'load abort') is None
    assert len(ph.calls) == n
    assert h.target_temp == 220.


def test_old_ace_without_observer_restores_found_target():
    ace, bg, ph, reactor, ps = _rig(with_observer=False)
    h = FakeHeater('extruder', 70.)
    bg._heat_claim(0, h)
    bg._set_heat(ace, ph, h, 215.)
    assert h.target_temp == 215.
    assert bg._release_heat(ace, 0, h, ph, 'cold-pull') == 70.
    assert h.target_temp == 70.


def test_heat_set_own_never_waits():
    """_own_heat_depth is process-wide, not per greenlet: a waiting own set
    would yield the reactor and book a foreign M109 as ours. The helper
    takes no wait argument and always passes wait=False."""
    import inspect
    ace, bg, ph, reactor, ps = _rig()
    h = FakeHeater('extruder')
    sig = inspect.signature(ace.heat_set_own)
    assert 'wait' not in sig.parameters
    ace.heat_set_own(ph, h, 215.)
    assert ph.calls == [('extruder', 215., False)]
    try:
        ace.heat_set_own(ph, h, 215., True)
    except TypeError:
        pass
    else:
        raise AssertionError('heat_set_own must not accept a wait flag')


def test_transport_only_load_leaves_no_claim():
    """A transport-only bg load never sets the heater, so it must not claim
    it either: the claim would survive the transport return and the next
    release without a claim of its own (e.g. _fail_unload before the next
    HEAT claim) would pick it up. The claim call in _load_core sits under
    the `not transport_only` branch, next to the set."""
    import ast
    import os
    path = os.path.join(H.EXTRAS, 'ace_bg_swap.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_load_core':
            fn = node
            break
    assert fn is not None
    claims = []

    def walk(node, guards):
        for child in ast.iter_child_nodes(node):
            g = guards
            if isinstance(child, ast.If):
                g = guards + [ast.unparse(child.test)]
                for sub in child.body:
                    walk(sub, g)
                for sub in child.orelse:
                    walk(sub, guards + ['else: ' + ast.unparse(child.test)])
                continue
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == '_heat_claim'):
                claims.append(list(guards))
            walk(child, g)
    walk(fn, [])
    assert claims, 'no _heat_claim in _load_core'
    for guards in claims:
        assert 'not transport_only' in guards, guards


def test_waittemp_wait_reasserts_a_moved_target():
    """The tip-form `waittemp:` wait in _unload_core sets its target once
    and then only watches the temperature. A foreign set in that window
    (a slicer preheat for the parked head, a departed-tool M104) moved the
    target and the wait ran into its 180 s timeout, pulling at the foreign
    temperature. The wait loop must compare the live TARGET with its own
    value and re-assert it, in both directions, like the HEAT wait does."""
    import ast
    import os
    path = os.path.join(H.EXTRAS, 'ace_bg_swap.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_unload_core')
    branch = next(n for n in ast.walk(fn)
                  if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "kind == 'waittemp'")
    loop = next(n for n in branch.body if isinstance(n, ast.While))
    reasserts = []
    for n in ast.walk(loop):
        if isinstance(n, ast.If):
            sets = [c for c in ast.walk(n)
                    if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == '_set_heat']
            if sets:
                reasserts.append(ast.unparse(n.test))
    assert reasserts, 'no _set_heat under a condition inside the waittemp loop'
    # the guard reads the live target (_t) against the wanted value (c),
    # as a magnitude: a target moved UP must be pulled back too
    assert any('abs(_t - c)' in g for g in reasserts), reasserts
