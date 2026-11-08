"""
Unit tests for the lean flush and wiper routine.
Validates ACE_LEAN_FLUSH definition, parameter contracts, 2-stage purge (fan timing),
temperature hold, single fast brush sweep, cutoff snap, and safe corridor departure.
"""
from pathlib import Path


def test_ace_cfg_defines_lean_flush_macro():
    cfg_path = Path(__file__).resolve().parents[2] / "multiace" / "config" / "extended" / "ace.cfg"
    assert cfg_path.exists(), f"Configuration file not found at {cfg_path}"
    content = cfg_path.read_text(encoding="utf-8")

    # Verify dedicated lean flush macro exists in ace.cfg and stock INNER_FLUSH_FILAMENT is untouched
    assert "[gcode_macro ACE_LEAN_FLUSH]" in content
    assert "[gcode_macro INNER_FLUSH_FILAMENT]" not in content


def test_ace_cfg_lean_flush_contract():
    cfg_path = Path(__file__).resolve().parents[2] / "multiace" / "config" / "extended" / "ace.cfg"
    content = cfg_path.read_text(encoding="utf-8")

    block = content.split("[gcode_macro ACE_LEAN_FLUSH]")[1].split("[gcode_macro")[0]

    # Defaults: LENGTH=80 (stock contract), feed_rate=450 (7.5 mm/s), stroke=30.0mm
    assert "params.LENGTH|default(80.0)" in block
    assert "feed_rate = 450" in block
    assert "y_stroke = 30.0" in block

    # Two-stage fan control: Stage 1 fan OFF (M107), Stage 2 fan ON (M106 S255)
    assert "M107" in block
    assert "M106 S255" in block

    # Pre-wipe anti-ooze retract
    assert "G1 E-0.8 F1800" in block

    # Single fast silicone brush sweep (F6000)
    assert "G1 Y{y_stroke} F6000" in block
    assert "G1 Y-{y_stroke} F6000" in block

    # Cutoff blade snap (ACTION=2)
    assert "INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=2" in block

    # Safe corridor exit sequence
    assert "INNER_DISCARD_FILAMENT_BASE_DISCARD" in block
    assert "MOVE_TO_XY_IDLE_POSITION_EXTRUDER" in block
    assert "G1 Y250 F12000" in block

    # No temperature shutdown
    assert "M104 S0" not in block
    assert "M109 S{target_temp}" in block


def test_ace_cfg_lean_flush_ships_off_by_default():
    cfg_path = Path(__file__).resolve().parents[2] / "multiace" / "config" / "extended" / "ace.cfg"
    content = cfg_path.read_text(encoding="utf-8")

    assert "#lean_flush: False" in content
    # Make sure it is not enabled by default in shipped config
    assert "\nlean_flush: True" not in content


