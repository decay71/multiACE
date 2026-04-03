#!/bin/bash
# multiACE Uninstaller for Snapmaker U1
# Restores original files from _pre_multiace or _stock backups
# Usage: bash uninstall_multiace.sh

# Fix Windows line endings
sed -i 's/\r$//' "$0" 2>/dev/null

set -e

HOME_DIR="/home/lava"
EXTRAS_DIR="${HOME_DIR}/klipper/klippy/extras"
KINEMATICS_DIR="${HOME_DIR}/klipper/klippy/kinematics"
CONFIG_DIR="${HOME_DIR}/printer_data/config/extended"
MULTIACE_DIR="${CONFIG_DIR}/multiace"
PRINTER_CFG="${HOME_DIR}/printer_data/config/printer.cfg"
LOGFILE="/tmp/multiace_uninstall.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [multiACE] $1" | tee -a "$LOGFILE"
}

log "=== multiACE Uninstall ==="

# --- Restore original files from backups ---
log "Restoring original files..."

# Helper: restore from _pre_multiace or _stock backup
restore_file() {
    local dir="$1"
    local name="$2"
    local pre_multiace="${dir}/${name}_pre_multiace.py"
    local stock="${dir}/${name}_stock.py"

    if [ -f "$pre_multiace" ]; then
        cp "$pre_multiace" "${dir}/${name}.py"
        log "  Restored ${name}.py from _pre_multiace backup"
    elif [ -f "$stock" ]; then
        cp "$stock" "${dir}/${name}.py"
        log "  Restored ${name}.py from _stock backup"
    else
        log "  WARNING: No backup found for ${name}.py, skipping"
    fi
}

restore_file "$EXTRAS_DIR" "filament_feed"
restore_file "$EXTRAS_DIR" "filament_switch_sensor"
restore_file "$KINEMATICS_DIR" "extruder"

# Always remove ace.cfg — it references multiace/ which will be deleted
rm -f "$CONFIG_DIR/ace.cfg"
rm -f "$CONFIG_DIR/ace_pre_multiace.cfg"
log "  Removed ace.cfg"

# --- Remove multiACE files ---
log "Removing multiACE files..."
rm -f "$EXTRAS_DIR/filament_feed_ace.py"
rm -f "$EXTRAS_DIR/filament_switch_sensor_ace.py"
rm -f "$EXTRAS_DIR/filament_feed_pre_multiace.py"
rm -f "$EXTRAS_DIR/filament_switch_sensor_pre_multiace.py"
rm -f "$KINEMATICS_DIR/extruder_ace.py"
rm -f "$KINEMATICS_DIR/extruder_pre_multiace.py"
rm -f "$CONFIG_DIR/ace_pre_multiace.cfg"
log "  multiACE files removed"

# --- Remove multiace config directory ---
if [ -d "$MULTIACE_DIR" ]; then
    rm -rf "$MULTIACE_DIR"
    log "  multiace config directory removed"
fi

# --- Remove include from printer.cfg ---
if [ -f "$PRINTER_CFG" ]; then
    if grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
        sed -i '/\[include extended\/ace.cfg\]/d' "$PRINTER_CFG"
        # Remove blank line left behind
        sed -i '/^$/N;/^\n$/d' "$PRINTER_CFG"
        log "  Removed [include extended/ace.cfg] from printer.cfg"
    fi
fi

# --- Clear Python cache ---
find "$EXTRAS_DIR/__pycache__" -name "ace*" -delete 2>/dev/null
find "$EXTRAS_DIR/__pycache__" -name "filament_feed*" -delete 2>/dev/null
find "$EXTRAS_DIR/__pycache__" -name "filament_switch_sensor*" -delete 2>/dev/null
find "$KINEMATICS_DIR/__pycache__" -name "extruder*" -delete 2>/dev/null
log "Python cache cleared"

log ""
log "=== Uninstall complete ==="
log "Please reboot the printer to restore stock operation."
log ""
