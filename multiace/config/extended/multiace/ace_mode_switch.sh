#!/bin/bash
# [mUlt1ACE] Mode Switch Script
# Swaps between stock Snapmaker and ACE filament handling
# Usage: ace_mode_switch.sh [ace|normal]

# Auto-detect paths from script location or HOME
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="/home/lava"
EXTRAS_DIR="${HOME_DIR}/klipper/klippy/extras"
KINEMATICS_DIR="${HOME_DIR}/klipper/klippy/kinematics"
VARS_FILE="${SCRIPT_DIR}/ace_vars.cfg"
LOGFILE="/tmp/ace_mode_switch.log"

MODE="$1"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [mUlt1ACE] $1" | tee -a "$LOGFILE"
}

if [ "$MODE" != "ace" ] && [ "$MODE" != "normal" ]; then
    echo "Usage: $0 [ace|normal]"
    exit 1
fi

log "=== Mode switch to: $MODE ==="
log "EXTRAS_DIR=$EXTRAS_DIR"
log "KINEMATICS_DIR=$KINEMATICS_DIR"

# --- Safety check: ACE versions must exist ---
if [ ! -f "$EXTRAS_DIR/filament_feed_ace.py" ]; then
    log "ERROR: filament_feed_ace.py not found in $EXTRAS_DIR! Aborting."
    exit 1
fi
if [ ! -f "$KINEMATICS_DIR/extruder_ace.py" ]; then
    log "ERROR: extruder_ace.py not found in $KINEMATICS_DIR! Aborting."
    exit 1
fi
if [ ! -f "$EXTRAS_DIR/filament_switch_sensor_ace.py" ]; then
    log "ERROR: filament_switch_sensor_ace.py not found in $EXTRAS_DIR! Aborting."
    exit 1
fi

# --- First run: backup stock files if not done yet ---
if [ ! -f "$EXTRAS_DIR/filament_feed_pre_multiace.py" ]; then
    log "First run: backing up stock filament_feed.py"
    cp "$EXTRAS_DIR/filament_feed.py" "$EXTRAS_DIR/filament_feed_pre_multiace.py"
fi
if [ ! -f "$KINEMATICS_DIR/extruder_pre_multiace.py" ]; then
    log "First run: backing up stock extruder.py"
    cp "$KINEMATICS_DIR/extruder.py" "$KINEMATICS_DIR/extruder_pre_multiace.py"
fi
if [ ! -f "$EXTRAS_DIR/filament_switch_sensor_pre_multiace.py" ]; then
    log "First run: backing up stock filament_switch_sensor.py"
    cp "$EXTRAS_DIR/filament_switch_sensor.py" "$EXTRAS_DIR/filament_switch_sensor_pre_multiace.py"
fi

# --- Swap files ---
if [ "$MODE" = "ace" ]; then
    log "Activating ACE mode..."
    cp "$EXTRAS_DIR/filament_feed_ace.py" "$EXTRAS_DIR/filament_feed.py"
    cp "$KINEMATICS_DIR/extruder_ace.py" "$KINEMATICS_DIR/extruder.py"
    cp "$EXTRAS_DIR/filament_switch_sensor_ace.py" "$EXTRAS_DIR/filament_switch_sensor.py"
    log "ACE files activated"
elif [ "$MODE" = "normal" ]; then
    log "Activating NORMAL mode..."
    cp "$EXTRAS_DIR/filament_feed_pre_multiace.py" "$EXTRAS_DIR/filament_feed.py"
    cp "$KINEMATICS_DIR/extruder_pre_multiace.py" "$KINEMATICS_DIR/extruder.py"
    cp "$EXTRAS_DIR/filament_switch_sensor_pre_multiace.py" "$EXTRAS_DIR/filament_switch_sensor.py"
    log "Stock files restored"
fi

# --- Clear Python cache to force reload ---
find "$EXTRAS_DIR/__pycache__" -name "filament_feed*" -delete 2>/dev/null
find "$EXTRAS_DIR/__pycache__" -name "filament_switch_sensor*" -delete 2>/dev/null
find "$KINEMATICS_DIR/__pycache__" -name "extruder*" -delete 2>/dev/null
log "Python cache cleared"

# --- Done ---
log "Files swapped. Manual reboot required!"
log "=== Mode switch complete ==="
