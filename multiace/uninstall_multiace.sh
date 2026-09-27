#!/bin/bash
sed -i 's/\r$//' "$0" 2>/dev/null
set -e
HOME_DIR="/home/lava"
EXTRAS_DIR="${HOME_DIR}/klipper/klippy/extras"
KINEMATICS_DIR="${HOME_DIR}/klipper/klippy/kinematics"
CONFIG_DIR="${HOME_DIR}/printer_data/config/extended"
MULTIACE_DIR="${CONFIG_DIR}/multiace"
ACE_VARS="${MULTIACE_DIR}/ace_vars.cfg"
PRINTER_CFG="${HOME_DIR}/printer_data/config/printer.cfg"
LOG_DIR="${HOME_DIR}/printer_data/logs"
mkdir -p "$LOG_DIR" 2>/dev/null
LOGFILE="${LOG_DIR}/multiace_uninstall.log"
# Dual-user log self-heal + never abort on a failed log write (set -e;
# same fix as install_multiace.sh - root-owned log vs lava-context run).
( touch "$LOGFILE" && chmod 0666 "$LOGFILE" ) 2>/dev/null || true
FORCE=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes|--force) FORCE=1 ;;
    esac
done
log() {
    _line="$(date '+%Y-%m-%d %H:%M:%S') [multiACE] $1"
    echo "$_line"
    { echo "$_line" >> "$LOGFILE"; } 2>/dev/null || true
}
log "=== multiACE Uninstall ==="
LOADED_HEADS=""
LOADED_COUNT=0
if [ -f "$ACE_VARS" ]; then
    HS_LINE=$(grep '^ace__head_source' "$ACE_VARS" 2>/dev/null || true)
    if [ -n "$HS_LINE" ]; then
        for i in 0 1 2 3; do
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
log "Restoring original files..."
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
rm -f "$CONFIG_DIR/ace.cfg"
rm -f "$CONFIG_DIR/ace_pre_multiace.cfg"
log "  Removed ace.cfg"
log "Removing multiACE files..."
rm -f "$EXTRAS_DIR/ace.py"
rm -f "$EXTRAS_DIR/ace_tipform.py"
rm -f "$EXTRAS_DIR/ace_protocol.py"
rm -f "$EXTRAS_DIR/ace_protocol_v1.py"
rm -f "$EXTRAS_DIR/ace_protocol_v2.py"
rm -f "$EXTRAS_DIR/filament_feed_ace.py"
rm -f "$EXTRAS_DIR/filament_switch_sensor_ace.py"
rm -f "$EXTRAS_DIR/filament_feed_pre_multiace.py"
rm -f "$EXTRAS_DIR/filament_switch_sensor_pre_multiace.py"
rm -f "$KINEMATICS_DIR/extruder_ace.py"
rm -f "$KINEMATICS_DIR/extruder_pre_multiace.py"
rm -f "$CONFIG_DIR/ace_pre_multiace.cfg"
log "  multiACE files removed"
for init_path in /etc/init.d/S55multiace_v2d /etc/init.d/multiace_v2d; do
    if [ -x "$init_path" ]; then
        "$init_path" stop 2>/dev/null || true
        rm -f "$init_path"
        log "  Legacy V2 daemon init script removed: $init_path"
    fi
done
if pgrep -f multiace_v2d.py >/dev/null 2>&1; then
    pkill -TERM -f multiace_v2d.py 2>/dev/null || true
    sleep 0.5
    pkill -KILL -f multiace_v2d.py 2>/dev/null || true
    log "  Running V2 daemon stopped"
fi
rm -f /usr/local/bin/multiace_v2d.py
rm -f /tmp/multiace_v2.sock
rm -f /var/run/multiace_v2d.pid
WEB_INITD="/etc/init.d/S98multiace-web"
if [ -x "$WEB_INITD" ]; then
    "$WEB_INITD" stop 2>/dev/null || true
    rm -f "$WEB_INITD"
    log "  Web init script stopped and removed"
fi
if [ -f /tmp/multiace_web.pid ]; then
    kill -TERM "$(cat /tmp/multiace_web.pid 2>/dev/null)" 2>/dev/null || true
    rm -f /tmp/multiace_web.pid
fi
pkill -TERM -f "uvicorn main:app" 2>/dev/null || true
WEB_NGINX="/etc/nginx/fluidd.d/multiace-web.conf"
if [ -f "$WEB_NGINX" ]; then
    rm -f "$WEB_NGINX"
    log "  Nginx drop-in removed: $WEB_NGINX"
    if command -v nginx >/dev/null 2>&1; then
        nginx -s reload 2>/dev/null || true
    fi
fi
# 1.4 layout: the installer injects the location /multiace/ block directly
# into sites-available/fluidd (no fluidd.d dir). Strip it back out.
FLUIDD_SITE="/etc/nginx/sites-available/fluidd"
if [ -f "$FLUIDD_SITE" ] && grep -q 'location /multiace/' "$FLUIDD_SITE"; then
    cp "$FLUIDD_SITE" "${FLUIDD_SITE}.bak.multiace-uninstall" 2>/dev/null || true
    python3 - "$FLUIDD_SITE" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
# Remove the whole 'location /multiace/ { ... }' block (brace-balanced),
# plus trailing blank line.
out, i, n = [], 0, len(s)
key = s.find('location /multiace/')
while key != -1:
    brace = s.find('{', key)
    depth, j = 0, brace
    while j < n:
        if s[j] == '{': depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    # swallow leading indentation of the block and one trailing newline/blank
    start = s.rfind('\n', 0, key) + 1
    while j < n and s[j] in ' \t': j += 1
    if j < n and s[j] == '\n': j += 1
    if j < n and s[j] == '\n': j += 1
    s = s[:start] + s[j:]
    n = len(s)
    key = s.find('location /multiace/')
open(p, 'w').write(s)
print('removed /multiace/ block')
PYEOF
    log "  Removed /multiace/ block from $FLUIDD_SITE"
    if command -v nginx >/dev/null 2>&1; then
        nginx -t >/dev/null 2>&1 && nginx -s reload 2>/dev/null || true
    fi
fi
if [ -d /home/lava/multiace_web ]; then
    rm -rf /home/lava/multiace_web
    log "  /home/lava/multiace_web removed"
fi
rm -f /home/lava/printer_data/logs/multiace_web.log
if [ -d "$MULTIACE_DIR" ]; then
    rm -rf "$MULTIACE_DIR"
    log "  multiace config directory removed"
fi
if [ -f "$PRINTER_CFG" ]; then
    if grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
        sed -i '/\[include extended\/ace.cfg\]/d' "$PRINTER_CFG"
        sed -i '/^$/N;/^\n$/d' "$PRINTER_CFG"
        log "  Removed [include extended/ace.cfg] from printer.cfg"
    fi
fi
find "$EXTRAS_DIR/__pycache__" -name "ace*" -delete 2>/dev/null
find "$EXTRAS_DIR/__pycache__" -name "filament_feed*" -delete 2>/dev/null
find "$EXTRAS_DIR/__pycache__" -name "filament_switch_sensor*" -delete 2>/dev/null
find "$KINEMATICS_DIR/__pycache__" -name "extruder*" -delete 2>/dev/null
log "Python cache cleared"
log ""
log "=== Uninstall complete ==="
log "Please reboot the printer to restore stock operation."
log ""
