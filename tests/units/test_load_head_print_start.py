"""A RESUME that replays a load on an empty head: the real ace.py on the H0
harness.

HW 2026-10-24: RESUME continued the file with ACE_LOAD_HEAD for an empty
head. The load stamped head_source and started the feed; 110 ms later the
print_stats:start of the RESUME ran the print-start cleanup, saw a
head_source on a head whose sensor was still clear, and dropped it. The
feed then resolved the slot without it and loaded a different slot.
"""
from . import scenarios as S


class FakeSensor:
    def __init__(self, present):
        self.runout_buttun_state = present

    def get_status(self, eventtime):
        return {'filament_detected': self.runout_buttun_state}


def _rig():
    def make(clock):
        return [S.unit_v2(clock, 'v2a', S.PATH_V2_A,
                          slots={0: {'sku': 'AHPLBK-101', 'type': 'PLA',
                                     'color': (200, 30, 30)}})]
    h = S._harness(make, 1)
    h.build()
    h.ready()
    h.run(2.0)
    for head in range(4):
        h.printer.objects['filament_motion_sensor e%d_filament' % head] = (
            FakeSensor(False))
    ace = h.inst
    ace._head_source[0] = {'ace_index': 0, 'slot': 1, 'type': 'PLA',
                           'color': 'FF8D28', 'brand': 'Generic'}
    return h, ace


def test_print_start_keeps_head_source_of_a_loading_head():
    h, ace = _rig()
    ace._loading_heads.add(0)
    h.print_start()
    assert ace._head_source[0] is not None
    assert ace._head_source[0]['slot'] == 1
    ace._loading_heads.discard(0)
    h.print_end()
    h.finish()


def test_print_start_still_clears_a_stale_head_source():
    h, ace = _rig()
    h.print_start()
    assert ace._head_source[0] is None
    h.print_end()
    h.finish()


def test_load_head_marks_the_head_for_the_whole_command():
    h, ace = _rig()
    seen = []

    def body(gcmd):
        seen.append(set(ace._loading_heads))
        raise RuntimeError('feed failed')
    ace._cmd_ACE_LOAD_HEAD_body = body

    class Gcmd:
        def get(self, name, default=None):
            return {'HEAD': '2'}.get(name, default)
    try:
        ace.cmd_ACE_LOAD_HEAD(Gcmd())
    except RuntimeError:
        pass
    assert seen == [{2}]
    assert ace._loading_heads == set()
    h.finish()
