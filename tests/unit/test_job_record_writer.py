"""Print history writer (ace.py -> job_history.append_record), driven with
the real methods on stubs.

One record per print, written once at its end; the slots that fed a head are
collected from head_source assignments; print_stats:start on RESUME must not
open a second record; nothing may raise into Klipper.
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


def test_one_record_with_every_slot_that_fed_a_head(ace, tmp_path):
    ace._head_source[0] = _src(0, 0, 'FF0000')
    ace._job_note_start()
    ace._job_snapshot_sources()
    ace._head_source[0] = _src(0, 2, '#00FF00')      # swapped mid-print
    ace._head_source[1] = _src(1, 3, '0000FF')
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
    ace._job_note_end()
    ace.reactor.run()
    (rec,) = _records(tmp_path)
    assert rec['assignment']['0'][0]['slot'] == 3


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
