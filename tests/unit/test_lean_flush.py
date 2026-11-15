"""Shipped lean flush: macros in ace.cfg and the option default."""
from pathlib import Path

CFG = (Path(__file__).resolve().parents[2] / "multiace" / "config"
       / "extended" / "ace.cfg")
SRC = Path(__file__).resolve().parents[2] / "multiace" / "klipper" / "extras"


def _block(name):
    text = CFG.read_text(encoding="utf-8")
    return text.split("[gcode_macro %s]" % name)[1].split("\n[")[0]


def test_stock_flush_macro_is_not_redefined():
    text = CFG.read_text(encoding="utf-8")
    assert "[gcode_macro ACE_LEAN_FLUSH]" in text
    assert "[gcode_macro ACE_LEAN_WIPE]" in text
    assert "[gcode_macro INNER_FLUSH_FILAMENT]" not in text


def test_lean_flush_keeps_the_heater_and_takes_stamp_length():
    block = _block("ACE_LEAN_FLUSH")
    assert "params.LENGTH|default(80.0)" in block
    assert "M104 S0" not in block
    assert "M109 S{target_temp}" in block
    assert "ACE_LEAN_WIPE" in block
    # Sizes are macro variables, not literals in the moves.
    for var in ("feed_rate", "retract", "brush_stroke", "brush_speed",
                "exit_y"):
        assert "variable_%s:" % var in block
    assert "G1 Y{exit_y}" in block


def test_lean_wipe_reads_its_sizes_from_the_flush_macro():
    block = _block("ACE_LEAN_WIPE")
    assert "printer['gcode_macro ACE_LEAN_FLUSH']" in block
    assert "INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=2" in block


def test_option_ships_off_and_commented():
    text = CFG.read_text(encoding="utf-8")
    assert "\n#lean_flush: false\n" in text
    assert "\nlean_flush:" not in text


def test_arrival_flow_check_is_not_skipped_by_lean_flush():
    swap = (SRC / "ace_swap.py").read_text(encoding="utf-8")
    assert "_skip_push" not in swap
    assert "ace__lean_wipe" not in swap


def test_lean_flush_is_limited_to_swaps_of_rigid_filament():
    feed = (SRC / "filament_feed_ace.py").read_text(encoding="utf-8")
    assert "def _lean_flush_swap(self)" in feed
    assert "_swap_in_progress" in feed.split("def _lean_flush_swap")[1][:500]
    assert "not filament_soft and _nozzle >= 0.3" in feed
