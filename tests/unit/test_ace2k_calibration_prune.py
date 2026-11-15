"""One calibration file per unit: the oldest file of a UID is the factory
backup, a later byte-identical file is a copy and goes; a later file that
differs stays."""
import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'multiace', 'web', 'backend'))
F = importlib.import_module('ace2k_flasher')

UID_A = '8F3B521807B091083131334A'
UID_B = '8F3B938707B091083131334A'


def _put(d, uid, stamp, data):
    name = 'ace-calibration-%s-%s.txt' % (uid, stamp)
    with open(os.path.join(str(d), name), 'wb') as fh:
        fh.write(data)
    return name


def test_copies_of_the_oldest_file_are_removed(tmp_path):
    first = _put(tmp_path, UID_A, '20261108-120000', b'cal A')
    copy1 = _put(tmp_path, UID_A, '20261109-055400', b'cal A')
    copy2 = _put(tmp_path, UID_A, '20261110-071300', b'cal A')
    other = _put(tmp_path, UID_B, '20261109-060000', b'cal B')
    removed = F.prune_calibrations(str(tmp_path))
    assert removed == {copy1: first, copy2: first}
    assert sorted(os.listdir(str(tmp_path))) == sorted([first, other])


def test_a_later_file_that_differs_stays(tmp_path):
    first = _put(tmp_path, UID_A, '20261108-120000', b'cal A')
    changed = _put(tmp_path, UID_A, '20261110-071300', b'cal A2')
    assert F.prune_calibrations(str(tmp_path)) == {}
    assert sorted(os.listdir(str(tmp_path))) == sorted([first, changed])


def test_other_files_and_a_missing_directory_are_left_alone(tmp_path):
    with open(os.path.join(str(tmp_path), 'notes.txt'), 'wb') as fh:
        fh.write(b'x')
    assert F.prune_calibrations(str(tmp_path)) == {}
    assert os.listdir(str(tmp_path)) == ['notes.txt']
    assert F.prune_calibrations(os.path.join(str(tmp_path), 'nope')) == {}
