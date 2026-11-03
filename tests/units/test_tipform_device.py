"""[ace_tipform] per-device rows: '<key>@<driver>' applies only to heads
fed by that driver and is tried before the plain chain. A section without
'@' rows answers exactly as before."""
import importlib
import os
import sys
import types

from . import harness as H


def _tf(items, mode='custom'):
    H.load_ace()
    T = importlib.import_module(H.PKG + '.ace_tipform')
    tf = object.__new__(T.AceTipform)
    tf._apply(mode, items)
    return tf


ROWS = [('pla', '-20@1500'),
        ('pla@cfs', '-10@600'),
        ('sunlu_pla@cfs', '-5@300'),
        ('petg', 'unloadtemp:240'),
        ('default@cfs', 'unloadtemp:200')]


def test_plain_chain_unchanged_without_device():
    tf = _tf(ROWS)
    assert tf.table_for('PLA') == tf.tables['pla']
    assert tf.table_for('PLA', vendor='Sunlu') == tf.tables['pla']
    assert tf.unload_temp_for('PETG') == 240
    assert tf.unload_temp_for('ABS') is None


def test_device_rows_win_for_their_driver():
    tf = _tf(ROWS)
    assert tf.table_for('PLA', device='cfs') == tf.tables['pla@cfs']
    assert tf.table_for('PLA', vendor='Sunlu', device='cfs') == \
        tf.tables['sunlu_pla@cfs']
    assert tf.table_for('PLA', device='ace_v2') == tf.tables['pla']


def test_device_chain_comes_before_the_plain_chain():
    tf = _tf(ROWS)
    # default@cfs is part of the cfs chain and beats the plain petg row
    assert tf.unload_temp_for('PETG', device='cfs') == 200
    assert tf.unload_temp_for('PETG', device='ace_v1') == 240


def test_stock_mode_stays_inert():
    tf = _tf(ROWS, mode='stock')
    assert tf.table_for('PLA', device='cfs') is None


def test_reload_parser_and_merge_accept_device_keys():
    H.load_ace()
    T = importlib.import_module(H.PKG + '.ace_tipform')
    mode, items, found = T.AceTipform._parse_section_text(
        '[ace_tipform]\nmode: custom\npla@cfs: unloadtemp:215\n')
    assert found and ('pla@cfs', 'unloadtemp:215') in items
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(root, 'multiace', 'tools'))
    try:
        import merge_ace_cfg as M
    finally:
        sys.path.pop(0)
    assert M.KEY_RE.match('pla@cfs: unloadtemp:215').group(1) == 'pla@cfs'
    assert M.COMMENTED_KEY_RE.match('#pla@cfs: x').group(1) == 'pla@cfs'


def test_device_of_a_head():
    H.load_ace()
    A = importlib.import_module(H.PKG + '.ace')
    C = importlib.import_module(H.PKG + '.ace_protocol_cfs')
    V1 = importlib.import_module(H.PKG + '.ace_protocol_v1')
    ace = types.SimpleNamespace(
        _head_source={0: {'ace_index': 1}, 1: None},
        _protocols={0: object.__new__(V1.AceProtocolV1),
                    1: object.__new__(C.AceProtocolCfs)},
        _ace_mode='multi', _active_device_index=0,
        head_uses_ace=lambda h: h != 3,
        head_ace_for=lambda h: h)
    dev = types.MethodType(A.MultiAce._tipform_device_for, ace)
    assert dev(0) == 'cfs'
    assert dev(1) == 'ace_v1'       # no source: the active unit
    assert dev(3) == ''             # feeder/manual head
    ace._ace_mode = 'head'
    ace.head_ace_for = lambda h: 1
    assert dev(1) == 'cfs'          # head mode: the wired unit
