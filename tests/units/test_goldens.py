"""M0: golden traces of the current V1/V2 behaviour (H0 harness).

Each scenario in scenarios.py drives the real ace.py against fake units on
a virtual clock and yields a trace; the trace is compared entry by entry
with tests/units/goldens/<name>.jsonl.

A golden changes ONLY when the behaviour is meant to change. To accept a
new golden after such a change:

    UPDATE_GOLDENS=1 python3 -m pytest tests/units -q

and commit the .jsonl together with the code change that caused it.
"""
import os

import pytest

from . import harness as H
from .scenarios import SCENARIOS

UPDATE = os.environ.get('UPDATE_GOLDENS', '').strip().lower() in (
    '1', 'true', 'yes')


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_golden(name):
    actual = H.canonical(SCENARIOS[name]())
    assert not [e for e in actual if e[1] == 'contract'], \
        'contract violations (tests/units/contract.py): %s' % [
            e for e in actual if e[1] == 'contract']
    assert not [e for e in actual if e[1] == 'status_bad_keys'], \
        'get_status() returned non-str keys: %s' % [
            e for e in actual if e[1] == 'status_bad_keys']
    assert not [e for e in actual if e[1] == 'timer' and e[2] == 'returned_none'], \
        'a reactor timer returned None (Klipper would shut down)'
    expected = H.read_golden(name)
    if UPDATE or expected is None:
        path = H.write_golden(name, actual)
        if expected is None and not UPDATE:
            pytest.skip('golden written for the first time: %s' % path)
        return
    diff = H.diff_entries(expected, actual)
    assert not diff, diff


@pytest.mark.parametrize('name', ['connect_mixed', 'unplug_v2_gone',
                                  'print_fa_v2_dropped_arm'])
def test_reproducible_three_times(name):
    """The design's M0 proof: the same scenario yields the same trace
    three times in a row (threads, async callbacks and device replies are
    ordered by the harness, not by scheduling)."""
    runs = [H.canonical(SCENARIOS[name]()) for _ in range(3)]
    for i in (1, 2):
        diff = H.diff_entries(runs[0], runs[i])
        assert not diff, 'run 0 vs run %d: %s' % (i, diff)


def test_every_shipped_option_is_read():
    """Paragraph 30: an [ace] option present in the shipped ace.cfg that
    ace.py never reads halts Klipper. The harness records every read."""
    entries = H.canonical(SCENARIOS['connect_mixed']())
    read = set(next(e for e in entries if e[1] == 'cfg_read')[2])
    shipped = H.shipped_sections()['ace']
    missing = sorted('ace.%s' % o for o in shipped if 'ace.%s' % o not in read)
    assert not missing, 'shipped [ace] options never read: %s' % missing


def test_predicate_inventory_unchanged():
    """Section 4 of the design: no NEW place may branch on the unit kind
    (NAME == 'v2', _is_v2_idx, a firmware literal) without the inventory
    being updated on purpose:

        python3 -m tests.units.tools.predicate_inventory --update
    """
    from .tools import predicate_inventory as PI
    import json
    with open(PI.GOLDEN, encoding='utf-8') as f:
        expected = json.load(f)
    actual = PI.golden_form(PI.inventory())
    added = sorted({tuple(x) for x in actual} - {tuple(x) for x in expected})
    removed = sorted({tuple(x) for x in expected} - {tuple(x) for x in actual})
    assert not added and not removed, (
        'predicate sites changed:\n+ %s\n- %s' % (
            '\n+ '.join(json.dumps(list(x)) for x in added),
            '\n- '.join(json.dumps(list(x)) for x in removed)))
