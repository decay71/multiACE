"""[ace_tipform] bgprimespeed:<mm/min> - the feedrate of the background
prime, in the unit of every other speed in the tip-forming string."""
import importlib.util
import pathlib

import pytest

TF = (pathlib.Path(__file__).resolve().parents[2]
      / "multiace/klipper/extras/ace_tipform.py")


@pytest.fixture(scope="module")
def tf():
    spec = importlib.util.spec_from_file_location("tf_bgprime", TF)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mod(tf, mode, **tables):
    m = object.__new__(tf.AceTipform)
    m._apply(mode, list(tables.items()))
    return m


def test_the_token_is_a_parameter_not_a_step(tf):
    (toks, ut, lt, bt, prime, bgpf, bgchunk, bgretract, bgretractf,
     bgscale, bgseat, bgseatf) = tf.parse_table(
        "57@400, -57@1500, bgprimespeed:360, bgchunk:12, bgretract:2.0, "
        "bgretractspeed:1800, bgprimescale:0.35, bgseat:38, bgseatspeed:450")
    assert toks == [("move", 57.0, 400.0), ("move", -57.0, 1500.0)]
    assert bgpf == 360.0 and bgchunk == 12.0 and bgretract == 2.0
    assert bgretractf == 1800.0 and bgscale == 0.35
    assert bgseat == 38.0 and bgseatf == 450.0
    assert ut is lt is bt is prime is None


def test_it_may_be_the_only_content_of_a_table(tf):
    toks, ut, lt, bt, prime, bgpf, *_rest = tf.parse_table("bgprimespeed:300")
    assert toks == [] and bgpf == 300.0


@pytest.mark.parametrize("bad", ["59", "1201", "0", "-5", "fast"])
def test_out_of_range_is_refused(tf, bad):
    with pytest.raises(ValueError):
        tf.parse_table("bgprimespeed:%s" % bad)


def test_the_range_edges_are_allowed(tf):
    assert tf.parse_table("bgprimespeed:60")[5] == 60.0
    assert tf.parse_table("bgprimespeed:1200")[5] == 1200.0


def test_it_follows_the_material_vendor_soft_default_lookup(tf):
    m = _mod(tf, "custom", default="bgprimespeed:240",
             soft="bgprimespeed:120", petg="bgprimespeed:480",
             sunlu_petg="bgprimespeed:600")
    assert m.bg_prime_speed_for("petg", vendor="Sunlu") == 600.0
    assert m.bg_prime_speed_for("petg") == 480.0
    assert m.bg_prime_speed_for("pla", soft=True) == 120.0
    assert m.bg_prime_speed_for("pla") == 240.0


def test_without_it_the_config_key_applies(tf):
    m = _mod(tf, "custom", pla="unloadtemp:215")
    assert m.bg_prime_speed_for("pla") is None


def test_it_is_inert_in_stock_mode(tf):
    m = _mod(tf, "stock", pla="bgprimespeed:360")
    assert m.bg_prime_speed_for("pla") is None


def test_the_status_lists_the_table_and_the_speed(tf):
    m = _mod(tf, "custom", pla="bgprimespeed:360")
    st = m.get_status(0)
    assert "pla" in st["tables"]
    assert st["bg_prime_speeds"] == {"pla": 360.0}


def test_a_bad_value_drops_only_that_table(tf):
    m = object.__new__(tf.AceTipform)
    dropped = m._apply("custom", [("pla", "bgprimespeed:5"),
                                  ("petg", "bgprimespeed:360")])
    assert dropped == ["pla"]
    assert m.bg_prime_speed_for("petg") == 360.0


def test_new_bg_tokens_lookup_and_status(tf):
    m = _mod(tf, "custom",
             default="bgchunk:8, bgretract:1.5, bgretractspeed:1200, "
                     "bgprimescale:0.5, bgseat:40, bgseatspeed:300",
             pla="bgchunk:10, bgretract:2.0, bgretractspeed:1800, "
                 "bgprimescale:0.35, bgseat:38, bgseatspeed:450")
    assert m.bg_chunk_for("pla") == 10.0
    assert m.bg_retract_for("pla") == 2.0
    assert m.bg_retract_speed_for("pla") == 1800.0
    assert m.bg_prime_scale_for("pla") == 0.35
    assert m.bg_seat_for("pla") == 38.0
    assert m.bg_seat_speed_for("pla") == 450.0
    assert m.bg_retract_speed_for("petg") == 1200.0
    assert m.bg_seat_speed_for("petg") == 300.0
    st = m.get_status(0)
    assert st["bg_retract_speeds"] == {"default": 1200.0, "pla": 1800.0}
    assert st["bg_seat_speeds"] == {"default": 300.0, "pla": 450.0}
    assert st["bg_seats"] == {"default": 40.0, "pla": 38.0}


@pytest.mark.parametrize("bad", [
    "bgchunk:1", "bgchunk:51",
    "bgretract:-1", "bgretract:11",
    "bgretractspeed:59", "bgretractspeed:3601",
    "bgprimescale:0.04", "bgprimescale:2.1",
    "bgseat:9", "bgseat:61",
    "bgseatspeed:59", "bgseatspeed:901",
])
def test_new_bg_tokens_out_of_range_refused(tf, bad):
    with pytest.raises(ValueError):
        tf.parse_table(bad)


def test_pulse_retract_and_unretract_check_docked():
    bg_path = TF.with_name("ace_bg_swap.py")
    src = bg_path.read_text(encoding="utf-8")
    for anchor in ("_since_grip_pulse >= _chunk_size", "_since_pulse >= _chunk_size"):
        idx = src.index(anchor)
        block = src[idx:idx + 750]
        check_before_retract = block.index("self._check_docked(toolhead, ext, head)")
        retract_move = block.index("-_retract_size")
        pause_call = block.index("self._pause(0.15)")
        check_before_push = block.index(
            "self._check_docked(toolhead, ext, head)", pause_call)
        push_move = block.index("_retract_size, _retract_speed", check_before_push)
        assert check_before_retract < retract_move < pause_call < check_before_push < push_move
