#!/bin/bash
# multiACE Uninstaller for Snapmaker U1
# Restores original files from _pre_multiace or _stock backups
# Usage: bash uninstall_multiace.sh [-y|--yes|--force]

# Fix Windows line endings
sed -i 's/\r$//' "$0" 2>/dev/null

set -e

HOME_DIR="/home/lava"
EXTRAS_DIR="${HOME_DIR}/klipper/klippy/extras"
KINEMATICS_DIR="${HOME_DIR}/klipper/klippy/kinematics"
CONFIG_DIR="${HOME_DIR}/printer_data/config/extended"
MULTIACE_DIR="${CONFIG_DIR}/multiace"
ACE_VARS="${MULTIACE_DIR}/ace_vars.cfg"
PRINTER_CFG="${HOME_DIR}/printer_data/config/printer.cfg"
LOGFILE="/tmp/multiace_uninstall.log"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes|--force) FORCE=1 ;;
    esac
done

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [multiACE] $1" | tee -a "$LOGFILE"
}

log "=== multiACE Uninstall ==="

# --- Pre-flight: warn if any toolheads are currently registered as loaded ---
LOADED_HEADS=""
LOADED_COUNT=0
if [ -f "$ACE_VARS" ]; then
    HS_LINE=$(grep '^ace__head_source' "$ACE_VARS" 2>/dev/null || true)
    if [ -n "$HS_LINE" ]; then
        for i in 0 1 2 3; do
            # Loaded entry looks like '0': {ace_index: ...}, empty is '0': None
            # Klipper save_variables uses single quotes (Python repr)
            if echo "$HS_LINE" | grep -q "'$i': {"; then
                LOADED_COUNT=$((LOADED_COUNT + 1))
                LOADED_HEADS="$LOADED_HEADS T$i"
            fi
        done
    fi
fi

if [ "$LOADED_COUNT" -gt 0 ]; then
    echo ""
    echo "================================================================"
    echo "  WARNING: $LOADED_COUNT toolhead(s) registered as loaded:$LOADED_HEADS"
    echo "================================================================"
    echo ""
    echo "  Uninstalling will delete the head_source mapping in"
    echo "  ace_vars.cfg. The actual filament will remain physically"
    echo "  loaded in the toolheads, but multiACE will no longer know"
    echo "  which ACE/slot each head was loaded from."
    echo ""
    echo "  Recommended: run the Unload All macro from Fluidd before"
    echo "  uninstalling, then re-run this script."
    echo ""
    if [ "$FORCE" -eq 1 ]; then
        echo "  --force given, continuing anyway."
        echo ""
    else
        printf "  Continue with uninstall? [y/N]: "
        read -r reply
        echo ""
        case "$reply" in
            y|Y|yes|YES)
                log "User confirmed uninstall with $LOADED_COUNT loaded heads"
                ;;
            *)
                log "Uninstall aborted by user (loaded heads:$LOADED_HEADS)"
                echo "Uninstall aborted. No changes made."
                exit 0
                ;;
        esac
    fi
fi

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
rm -f "$EXTRAS_DIR/ace.py"
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
