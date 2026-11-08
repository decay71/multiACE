"""
Unit tests for the lean flush and wiper test macros.
Validates the 2-stage purge (fan timing), temperature hold, anti-ooze retract,
and dual-brush sweep motion.
"""
from pathlib import Path


def test_ace_cfg_defines_lean_wipe_and_purge_macros():
    cfg_path = Path(__file__).resolve().parents[2] / "multiace" / "config" / "extended" / "ace.cfg"
    assert cfg_path.exists(), f"Configuration file not found at {cfg_path}"
    content = cfg_path.read_text(encoding="utf-8")

    # Verify macros exist in ace.cfg
    assert "[gcode_macro TEST_WIPE_MOVE]" in content
    assert "[gcode_macro TEST_LEAN_PURGE]" in content
    assert "[gcode_macro INNER_FLUSH_FILAMENT]" in content


def test_lean_purge_two_stage_fan_and_heat():
    cfg_path = Path(__file__).resolve().parents[2] / "multiace" / "config" / "extended" / "ace.cfg"
    content = cfg_path.read_text(encoding="utf-8")

    # Grab the TEST_LEAN_PURGE section
    purge_block = content.split("[gcode_macro TEST_LEAN_PURGE]")[1].split("[gcode_macro")[0]

    # Stage 1: Fan OFF (M107) before initial extrusion
    assert "M107" in purge_block
    # Stage 2: Fan ON (M106 S255) for remaining extrusion
    assert "M106 S255" in purge_block
    # Must park at discard chute
    assert "MOVE_TO_DISCARD_FILAMENT_POSITION" in purge_block
    # Must heat to target temp
    assert "M109 S{target_temp}" in purge_block
    # Never turn heater off (no M104 S0)
    assert "M104 S0" not in purge_block


def test_inner_flush_filament_override():
    cfg_path = Path(__file__).resolve().parents[2] / "multiace" / "config" / "extended" / "ace.cfg"
    content = cfg_path.read_text(encoding="utf-8")

    assert "[gcode_macro INNER_FLUSH_FILAMENT]" in content
    block = content.split("[gcode_macro INNER_FLUSH_FILAMENT]")[1].split("[gcode_macro")[0]

    # Supports optional fallback to stock macro via save_variables
    assert "_STOCK_INNER_FLUSH_FILAMENT" in block
    assert "ace__lean_wipe" in block
    # Calibrated speed 7.5 mm/s (F450) and stroke 30mm
    assert "feed_rate = 450" in block
    assert "y_stroke = 30.0" in block
    # Two-stage fan control
    assert "M107" in block
    assert "M106 S255" in block
    # Pre-wipe anti-ooze retract
    assert "G1 E-0.8 F1800" in block
    # Front silicone brush sweep
    assert "G1 Y{y_stroke} F3000" in block
    assert "G1 Y-{y_stroke} F3000" in block
    # Cutoff blade snap
    assert "INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=2" in block
    # Safe corridor exit
    assert "INNER_DISCARD_FILAMENT_BASE_DISCARD" in block
    assert "MOVE_TO_XY_IDLE_POSITION_EXTRUDER" in block
    assert "G1 Y250 F12000" in block
    # No temperature drop
    assert "M104 S0" not in block
