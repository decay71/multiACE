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

    # Verify all 3 macros exist in ace.cfg
    assert "[gcode_macro TEST_WIPE_MOVE]" in content
    assert "[gcode_macro TEST_LEAN_PURGE]" in content
    assert "[gcode_macro TEST_LEAN_FLUSH_AND_WIPE]" in content


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


def test_lean_flush_and_wipe_full_cycle():
    cfg_path = Path(__file__).resolve().parents[2] / "multiace" / "config" / "extended" / "ace.cfg"
    content = cfg_path.read_text(encoding="utf-8")

    block = content.split("[gcode_macro TEST_LEAN_FLUSH_AND_WIPE]")[1].split("[gcode_macro")[0]

    # Pre-wipe anti-ooze retract
    assert "G1 E-0.8 F1800" in block
    # Front-to-back dual brush sweep along Y axis (calibrated 30mm stroke)
    assert "G1 Y{y_stroke} F3000" in block
    assert "G1 Y-{y_stroke} F3000" in block
    # Final blade / cutoff flick
    assert "INNER_ROUGHLY_CLEAN_NOZZLE_BASE_DISCARD ACTION=2" in block
    # Travel to prime tower at fast speed (F12000)
    assert "G0 X{tower_x} Y{tower_y} F12000" in block
    # No temperature shutdown
    assert "M104 S0" not in block
