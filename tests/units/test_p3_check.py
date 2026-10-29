"""P3 checker (tools/p3_check.py): self-tests on synthetic trees, plus the
allowlist that every `_cap('<name>')` in the shipped modules names a
capability ace_unit.py defines.
"""
import textwrap

from . import harness as H
from .tools import move_check as MV
from .tools import p3_check as P3


def _trees(tmp_path, old_body, new_body):
    old = tmp_path / 'old'
    new = tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    (old / 'ace.py').write_text(textwrap.dedent(old_body))
    (new / 'ace.py').write_text(textwrap.dedent(new_body))
    return MV.sources_from_dir(str(old)), MV.sources_from_dir(str(new))


def test_swap_is_unchanged_for_every_polarity(tmp_path):
    old, new = _trees(tmp_path, '''
        class MultiAce:
            def a(self, idx):
                if self._is_v2(idx):
                    return 1
            def b(self, idx):
                if not self._is_v2_idx(idx):
                    return 2
            def c(self, i):
                return [i for i in range(3) if not self._is_v2(i) and i]
            def d(self, idx):
                return self._is_v1(idx)
    ''', '''
        class MultiAce:
            def a(self, idx):
                if not self._cap(idx, 'fw_tag_pair_dedupe'):
                    return 1
            def b(self, idx):
                if self._cap(idx, 'v1_fa_health_log'):
                    return 2
            def c(self, i):
                return [i for i in range(3) if not self._cap(i, 'has_humidity') and i]
            def d(self, idx):
                return self._is_v1(idx)
    ''')
    res = P3.compare_trees(old, new, groups={})
    assert res['changed'] == res['added'] == res['removed'] == []
    assert len(res['unchanged']) == 5      # four methods + the class bases
    assert not MV.verdict(res)


def test_wrong_polarity_and_unknown_cap_are_changed(tmp_path):
    old, new = _trees(tmp_path, '''
        class MultiAce:
            def a(self, idx):
                if self._is_v2(idx):
                    return 1
            def b(self, idx):
                if self._is_v2(idx):
                    return 2
            def c(self, idx):
                if self._is_v2(idx):
                    return 3
    ''', '''
        class MultiAce:
            def a(self, idx):
                if self._cap(idx, 'fw_tag_pair_dedupe'):
                    return 1
            def b(self, idx):
                if not self._cap(idx, 'gen1_tunnel'):
                    return 2
            def c(self, idx):
                if self._cap(idx, 'has_humidity'):
                    return 3
    ''')
    res = P3.compare_trees(old, new, groups={})
    changed = sorted(k for k, _o, _n in res['changed'])
    assert changed == ['meth:MultiAce.a', 'meth:MultiAce.b']   # c is a true swap
    assert MV.verdict(res) == ['CHANGED meth:MultiAce.a', 'CHANGED meth:MultiAce.b']
    assert not MV.verdict(res, allow_changed=('MultiAce.a', 'MultiAce.b'))


def test_bg_form_passes_the_unit_owner(tmp_path):
    old, new = _trees(tmp_path, '''
        class AceBgSwap:
            def a(self, ace, ace_idx):
                if not ace._is_v2_idx(ace_idx):
                    return 1
            def b(self, ace, ace_idx):
                return 50 if ace._is_v2_idx(ace_idx) else 30
    ''', '''
        class AceBgSwap:
            def a(self, ace, ace_idx):
                if not self._cap(ace, ace_idx, 'has_slot_status_truth'):
                    return 1
            def b(self, ace, ace_idx):
                return 50 if self._cap(ace, ace_idx, 'self_stops_on_resistance') else 30
    ''')
    res = P3.compare_trees(old, new, groups={})
    assert res['changed'] == res['added'] == res['removed'] == []
    assert len(res['unchanged']) == 3
    sites = P3.cap_sites(new)
    assert [s['old'] for s in sites] == [
        'ace._is_v2(ace_idx)', 'ace._is_v2(ace_idx)']
    assert [s['scope'] for s in sites] == ['AceBgSwap.a', 'AceBgSwap.b']


def test_positional_default_is_not_the_bg_form(tmp_path):
    old, new = _trees(tmp_path, '''
        class MultiAce:
            def a(self, idx):
                if self._is_v2(idx):
                    return 1
    ''', '''
        class MultiAce:
            def a(self, idx):
                if self._cap(idx, 'has_decoder', False):
                    return 1
    ''')
    res = P3.compare_trees(old, new, groups={})
    assert res['changed'] == res['added'] == res['removed'] == []
    assert P3.cap_sites(new)[0]['old'] == 'self._is_v2(idx)'


def test_sites_name_the_predicate_they_stand_for(tmp_path):
    _old, new = _trees(tmp_path, '', '''
        class MultiAce:
            def a(self, idx):
                return self._cap(idx, 'v1_fa_health_log') or self._cap(idx, 'gen1_tunnel')
    ''')
    sites = P3.cap_sites(new)
    assert [s['old'] for s in sites] == [
        'not self._is_v2(idx)', '<no plain predicate: declare the site>']
    assert sites[0]['scope'] == 'MultiAce.a' and sites[0]['line'] == 4


def test_shipped_tree_against_itself_is_unchanged():
    src = MV.sources_from_ref(None)
    res = P3.compare_trees(src, src)
    assert res['changed'] == res['added'] == res['removed'] == []


def test_every_cap_site_names_a_defined_cap():
    H.load_ace()
    import sys
    U = sys.modules[H.PKG + '.ace_unit']
    known = set(U.STATIC_FIELDS) | set(U.DYNAMIC_FIELDS)
    sites = P3.cap_sites(MV.sources_from_ref(None), equiv={})
    bad = [s for s in sites if s['cap'] not in known]
    assert not bad, bad
    assert set(P3.CAP_EQUIV) <= known
