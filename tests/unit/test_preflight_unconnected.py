"""preflight_core: a head-mode head wired to an MMU that is not connected
gets no targets (neither MMU head nor feeder), the preview names it, and
multi plans only over the connected units. Without a unit list in head_ctx
(older backend) nothing changes."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _core():
    spec = importlib.util.spec_from_file_location(
        "preflight_core_uc", ROOT / "multiace/web/backend/preflight_core.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pp():
    spec = importlib.util.spec_from_file_location(
        "pp_uc", ROOT / "multiace/tools/post_process_virtual_toolheads.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HEAD_CTX = {"mode": "head", "ace_heads": [0, 1, 2, 3],
            "head_ace": {"0": 0, "1": 1, "2": 2, "3": 3}}


def test_head_on_missing_mmu_is_dropped():
    core = _core()
    ctx = dict(HEAD_CTX, connected_aces=[0, 1, 2])
    ace_heads, head_of_ace, num_of_head, feeders = core.head_maps(ctx)
    assert ace_heads == [0, 1, 2]
    assert 3 not in head_of_ace
    assert feeders == []
    assert core.unconnected_heads(ctx) == [{"head": 3, "ace": 3}]


def test_no_unit_list_plans_as_before():
    core = _core()
    ace_heads, _, _, feeders = core.head_maps(dict(HEAD_CTX))
    assert ace_heads == [0, 1, 2, 3]
    assert core.unconnected_heads(dict(HEAD_CTX)) == []


def test_feeder_head_untouched():
    core = _core()
    ctx = {"mode": "head", "ace_heads": [0, 1],
           "head_ace": {"0": 0, "1": 1, "2": 2, "3": 3},
           "connected_aces": [0, 1]}
    ace_heads, _, _, feeders = core.head_maps(ctx)
    assert ace_heads == [0, 1]
    assert feeders == [2, 3]
    assert core.unconnected_heads(ctx) == []


def test_multi_caps_units():
    core = _core()
    pp = _pp()
    ctx = {"mode": "multi", "num_aces": 4, "connected_aces": [0, 1, 2]}
    _, topo, _, _ = core.head_maps(ctx, pp)
    assert topo.num_aces == 3
