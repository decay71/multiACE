"""Print history writer (ace.py -> job_history.append_record), driven with
the real methods on stubs.

One record per print, written once at its end; the slots that fed a head are
collected from head_source assignments, but only for heads that printed (the
job's extruder activations, plus the head already active at the start when it
extruded); print_stats:start on RESUME must not open a second record; nothing
may raise into Klipper.
"""
import importlib
import json
import os
import shutil
import sys
import types

import pytest

EXTRAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..',
                      'multiace', 'klipper', 'extras')
PKG = 'multiace_job_record_extras'


def _ace():
    name = PKG + '.ace'
    if name in sys.modules:
        return sys.modules[name]
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [EXTRAS]
    sys.modules[PKG] = pkg
    return importlib.import_module(name)


class Reactor:
    def __init__(self):
        self.pending = []

    def monotonic(self):
        return 0.0

    def register_callback(self, cb, waketime=0.0):
        self.pending.append(cb)

    def run(self):
        for cb in self.pending:
            cb(0.0)
        self.pending = []


class PrintStats:
    def __init__(self):
        self.status = {'filename': 'cube.gcode', 'state': 'printing'}

    def get_status(self, _t):
        return dict(self.status)


class Extruder:
    def __init__(self, index):
        self.extruder_index = index


class Toolhead:
    def __init__(self):
        self.index = 0

    def get_extruder(self):
        return Extruder(self.index)


class Printer:
    def __init__(self, ps):
        self.ps = ps

    def lookup_object(self, name, default=None):
        return self.ps if name == 'print_stats' else default


@pytest.fixture
def ace(tmp_path, monkeypatch):
    mod = _ace()
    monkeypatch.setenv('MULTIACE_DATA_DIR', str(tmp_path))
    a = mod.MultiAce.__new__(mod.MultiAce)
    a.reactor = Reactor()
    a.ps = PrintStats()
    a.printer = Printer(a.ps)
    a.toolhead = Toolhead()
    a._web_dir = str(tmp_path / 'multiace_web')
    a.paths = {'config_dir': str(tmp_path), 'printer_data': str(tmp_path)}
    a._job = None
    a._job_history_mod = None
    a._head_source = a._new_head_source()
    return a


def _records(tmp_path):
    p = tmp_path / 'jobs.jsonl'
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _src(ace_index, slot, color):
    return {'ace_index': ace_index, 'slot': slot, 'color': color}


def _activate(ace, head):
    """What Klipper's extruder:activate_extruder event does to the job."""
    ace.toolhead.index = head
    ace._job_note_active()


def test_one_record_with_every_slot_that_fed_a_head(ace, tmp_path):
    ace._head_source[0] = _src(0, 0, 'FF0000')
    ace._job_note_start()
    ace._job_snapshot_sources()
    ace.ps.status['filament_used'] = 500.0           # head 0 printed first
    ace._head_source[0] = _src(0, 2, '#00FF00')      # swapped mid-print
    ace._head_source[1] = _src(1, 3, '0000FF')
    _activate(ace, 1)
    ace.ps.status['state'] = 'complete'
    ace._job_note_end()
    assert _records(tmp_path) == []                  # nothing while/at stop
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert rec['filename'] == 'cube.gcode'
    assert rec['result'] == 'completed'
    assert rec['assignment']['0'] == [
        {'ace_index': 0, 'slot': 0, 'color': 'FF0000'},
        {'ace_index': 0, 'slot': 2, 'color': '00FF00'}]
    assert rec['assignment']['1'] == [
        {'ace_index': 1, 'slot': 3, 'color': '0000FF'}]


def test_resume_does_not_open_a_second_record(ace, tmp_path):
    ace._job_note_start()
    first = ace._job
    ace._head_source[0] = _src(0, 1, 'FFFFFF')
    ace.ps.status['filament_used'] = 10.0
    ace._job_note_start()                            # RESUME, same file
    assert ace._job is first
    ace.ps.status['state'] = 'cancelled'
    ace._job_note_end()
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert rec['result'] == 'cancelled'
    assert rec['assignment']['0'][0]['slot'] == 1


def test_loads_outside_a_print_are_not_recorded(ace, tmp_path):
    ace._head_source[0] = _src(0, 0, 'FF0000')
    ace._job_note_end()
    ace.reactor.run()
    assert _records(tmp_path) == []


def test_failed_load_is_not_recorded_until_confirmed(ace, tmp_path):
    ace._job_note_start()
    failed = dict(_src(0, 3, 'FF0000'), load_failed=True)
    ace._head_source[0] = failed
    ace._job_snapshot_sources()
    assert ace._job['loadout'] == {}
    failed['load_failed'] = False                    # confirmed later
    ace.ps.status['filament_used'] = 10.0
    ace._job_note_end()
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert rec['assignment']['0'][0]['slot'] == 3


def test_a_head_that_only_stayed_loaded_is_not_recorded(ace, tmp_path):
    # Head 2 is left loaded from an earlier print and never extrudes; head 0
    # is active at the start but the print switches to head 3 straight away.
    ace._head_source[0] = _src(0, 0, 'AAAAAA')
    ace._head_source[2] = _src(1, 1, 'BBBBBB')
    ace._head_source[3] = _src(3, 0, 'CCCCCC')
    ace._job_note_start()                            # head 0 active, no filament
    ace._job_snapshot_sources()
    _activate(ace, 3)
    ace.ps.status['filament_used'] = 800.0
    ace._head_source[3] = _src(3, 3, 'DDDDDD')       # swapped mid-print
    _activate(ace, 3)
    ace.ps.status['state'] = 'complete'
    ace._job_note_end()
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert sorted(rec['assignment']) == ['3']
    assert [x['slot'] for x in rec['assignment']['3']] == [0, 3]


def test_the_head_active_at_the_start_counts_when_it_extruded(ace, tmp_path):
    ace._head_source[0] = _src(0, 0, 'AAAAAA')
    ace._head_source[1] = _src(0, 1, 'BBBBBB')
    ace._job_note_start()                            # head 0 active
    ace._job_snapshot_sources()
    ace.ps.status['filament_used'] = 300.0           # head 0 printed...
    _activate(ace, 1)                                # ...then switched
    ace._job_note_end()
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert sorted(rec['assignment']) == ['0', '1']


def test_a_print_that_never_switched_credits_the_start_head(ace, tmp_path):
    ace._head_source[0] = _src(0, 0, 'AAAAAA')
    ace._head_source[1] = _src(0, 1, 'BBBBBB')       # loaded, idle
    ace._job_note_start()
    ace._job_snapshot_sources()
    ace.ps.status['filament_used'] = 5.0             # tiny, but it printed
    ace._job_note_end()
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert sorted(rec['assignment']) == ['0']


def test_a_print_that_extruded_nothing_records_no_heads(ace, tmp_path):
    ace._head_source[0] = _src(0, 0, 'AAAAAA')
    ace._job_note_start()
    ace._job_snapshot_sources()
    ace.ps.status['state'] = 'cancelled'
    ace._job_note_end()
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert rec['assignment'] == {}


def test_finds_job_history_in_the_web_backend_first(ace, tmp_path):
    backend = tmp_path / 'multiace_web' / 'backend'
    backend.mkdir(parents=True)
    src = os.path.join(EXTRAS, '..', '..', 'tools', 'job_history.py')
    shutil.copy(src, backend / 'job_history.py')
    assert ace._job_history().__file__ == str(backend / 'job_history.py')


def test_errors_never_reach_klipper(ace, tmp_path):
    ace.printer = None                               # lookup_object -> AttributeError
    ace._job_note_start()
    ace._job = {'filename': 'x', 'start_time': 0.0, 'loadout': {}}
    ace._job_write(ace._job)
    ace._job_note_end()
    ace._head_source.observer = lambda h, s: 1 / 0
    ace._head_source[2] = _src(0, 0, None)


# ---------------------------------------------------------------------------
# Per-swap times (the calibration input swap_cost / job_history read)
# ---------------------------------------------------------------------------

class _Gcmd:
    def __init__(self, **args):
        self.args = args

    def get_int(self, name, default=None, **_kw):
        if name in self.args:
            return self.args[name]
        if default is None:
            raise KeyError(name)
        return default


def _finish(ace):
    ace.ps.status['state'] = 'complete'
    ace._job_note_end()
    ace.reactor.run()


def test_swap_times_are_written_with_the_record(ace, tmp_path):
    ace._job_note_start()
    ace._job_note_swap('cross_ace_inline', 154.04)
    ace._job_note_swap('cross_ace_bg', 12.0)
    _finish(ace)
    (rec,) = _records(tmp_path)
    assert rec['swaps'] == [{'kind': 'cross_ace_inline', 'seconds': 154.0},
                            {'kind': 'cross_ace_bg', 'seconds': 12.0}]


def test_a_print_without_swaps_writes_an_empty_list(ace, tmp_path):
    ace._job_note_start()
    _finish(ace)
    (rec,) = _records(tmp_path)
    assert rec['swaps'] == []


def test_swaps_outside_a_print_are_not_recorded(ace, tmp_path):
    ace._job_note_swap('same_ace', 80.0)             # no job open: no raise
    assert ace._job is None


def test_a_swap_with_no_kind_is_not_recorded(ace, tmp_path):
    ace._job_note_start()
    ace._job_note_swap(None, 4.0)
    _finish(ace)
    assert _records(tmp_path)[0]['swaps'] == []


def test_swaps_per_job_are_capped(ace, tmp_path):
    ace._job_note_start()
    for _ in range(ace.JOB_SWAPS_MAX + 50):
        ace._job_note_swap('same_ace', 70.0)
    _finish(ace)
    assert len(_records(tmp_path)[0]['swaps']) == ace.JOB_SWAPS_MAX


def test_recorded_swaps_calibrate_the_estimate(ace, tmp_path):
    """The point of recording them: job_history turns them into the medians
    SwapCostModel.with_calibration prefers once there are enough."""
    for i in range(5):
        ace._job_note_start()
        ace._job_note_swap('cross_ace_inline', 150.0 + i)
        _finish(ace)
        ace._job = None
    hist = ace._job_history()
    stats = hist.aggregate_swap_stats(hist.load_records(str(tmp_path)))
    assert stats['cross_ace_inline']['n'] == 5
    assert stats['cross_ace_inline']['median_s'] == 152.0
    assert stats['cross_ace_inline']['calibrated'] is True


@pytest.mark.parametrize('source,bg,args,kind', [
    # nothing loaded yet, a different ACE, or the same ACE on another slot
    (None, False, dict(HEAD=0, ACE=0, SLOT=1), 'cross_ace_inline'),
    (_src(1, 0, 'FF0000'), False, dict(HEAD=0, ACE=0, SLOT=1),
     'cross_ace_inline'),
    (_src(0, 2, 'FF0000'), False, dict(HEAD=0, ACE=0, SLOT=1), 'same_ace'),
    # the auto-load block before the print body
    (None, False, dict(HEAD=0, ACE=0, SLOT=1, INITIAL=1), 'first_load'),
    # already on that slot: a finished background load is the handover,
    # anything else a pick that needs no filament change (not timed)
    (_src(0, 1, 'FF0000'), True, dict(HEAD=0, ACE=0, SLOT=1), 'cross_ace_bg'),
    (_src(0, 1, 'FF0000'), False, dict(HEAD=0, ACE=0, SLOT=1), None),
    # a failed earlier load does not count as "already there"
    (dict(_src(0, 1, 'FF0000'), load_failed=True), False,
     dict(HEAD=0, ACE=0, SLOT=1), 'same_ace'),
])
def test_swap_kind_is_read_before_the_swap_runs(ace, source, bg, args, kind):
    ace.head_uses_ace = lambda head: True
    ace._bg_load_unverified = {0} if bg else set()
    if source is not None:
        ace._head_source[0] = source
    assert ace._swap_kind_before(_Gcmd(**args)) == kind


def test_a_head_without_an_ace_is_not_timed(ace):
    ace.head_uses_ace = lambda head: False
    ace._bg_load_unverified = set()
    assert ace._swap_kind_before(_Gcmd(HEAD=2, ACE=0, SLOT=2)) is None


def test_a_malformed_command_never_raises(ace):
    ace.head_uses_ace = lambda head: True
    assert ace._swap_kind_before(_Gcmd(HEAD=0)) is None     # no ACE=
