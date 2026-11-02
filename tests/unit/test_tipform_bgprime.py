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
    toks, ut, lt, bt, prime, bgpf = tf.parse_table(
        "57@400, -57@1500, bgprimespeed:360")
    assert toks == [("move", 57.0, 400.0), ("move", -57.0, 1500.0)]
    assert bgpf == 360.0 and ut is lt is bt is prime is None


def test_it_may_be_the_only_content_of_a_table(tf):
    toks, *_rest, bgpf = tf.parse_table("bgprimespeed:300")
    assert toks == [] and bgpf == 300.0


@pytest.mark.parametrize("bad", ["59", "1201", "0", "-5", "fast"])
def test_out_of_range_is_refused(tf, bad):
    with pytest.raises(ValueError):
        tf.parse_table("bgprimespeed:%s" % bad)


def test_the_range_edges_are_allowed(tf):
    assert tf.parse_table("bgprimespeed:60")[-1] == 60.0
    assert tf.parse_table("bgprimespeed:1200")[-1] == 1200.0


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
