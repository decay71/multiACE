#!/bin/bash
set -e
INSTALL_WEB=0
KEEP_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --install-web) INSTALL_WEB=1 ;;
        --keep-config) KEEP_CONFIG=1 ;;
        --help|-h)
            echo "Usage: $0 [--install-web] [--keep-config]"
            echo "  --install-web   Also install multiACE Web (FastAPI + Vue UI)"
            echo "  --keep-config   Don't touch existing ace.cfg at all (default: merge user values from old cfg into new shipped defaults)"
            exit 0
            ;;
    esac
done
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
find "$INSTALL_DIR" -name "*.sh" -exec sed -i 's/\r$//' {} +
HOME_DIR="/home/lava"
EXTRAS_DIR="${HOME_DIR}/klipper/klippy/extras"
KINEMATICS_DIR="${HOME_DIR}/klipper/klippy/kinematics"
CONFIG_DIR="${HOME_DIR}/printer_data/config/extended"
MULTIACE_DIR="${CONFIG_DIR}/multiace"
PRINTER_CFG="${HOME_DIR}/printer_data/config/printer.cfg"
LOG_DIR="${HOME_DIR}/printer_data/logs"
mkdir -p "$LOG_DIR" 2>/dev/null
LOGFILE="${LOG_DIR}/multiace_install.log"
# Dual-user log self-heal (same class as ace_mode_switch.log): a root SSH
# install creates this file root-owned, the web self-update then runs as
# lava and tee could not append -> the pipeline failed -> set -e killed
# the update right after the first log line. Make the file appendable for
# both users; never let a failed log write abort an install (see log()).
( touch "$LOGFILE" && chmod 0666 "$LOGFILE" ) 2>/dev/null || true
IS_ROOT=0
if [ "$(id -u)" = "0" ]; then
    IS_ROOT=1
fi
run_as_lava() {
    if [ "$IS_ROOT" = "1" ]; then
        su - lava -c "$1"
    elif [ "$(id -un 2>/dev/null)" = "lava" ]; then
        sh -c "$1"
    else
        return 127
    fi
}
log() {
    _line="$(date '+%Y-%m-%d %H:%M:%S') [multiACE] $1"
    echo "$_line"
    { echo "$_line" >> "$LOGFILE"; } 2>/dev/null || true
}
log "=== multiACE Installation ==="
log "Install from: $INSTALL_DIR"
log "Klipper extras: $EXTRAS_DIR"
log "Klipper kinematics: $KINEMATICS_DIR"
log "Config dir: $CONFIG_DIR"
for f in \
    "klipper/extras/ace.py" \
    "klipper/extras/filament_feed_ace.py" \
    "klipper/extras/filament_switch_sensor_ace.py" \
    "klipper/extras/ace_bg_swap.py" \
    "klipper/extras/ace_tipform.py" \
    "klipper/kinematics/extruder_ace.py" \
    "config/extended/ace.cfg" \
    "config/extended/multiace/ace_mode_switch.sh" \
    "config/extended/multiace/ace_vars.cfg"
do
    if [ ! -f "$INSTALL_DIR/$f" ]; then
        log "ERROR: Missing file: $f"
        exit 1
    fi
done
log "All source files found"
# CONFIG_DIR is OURS to create - the klipper dirs are not. printer_data/
# config/extended exists on a PAXX box (S49extended-config mirrors it) and
# on any box that ran an earlier install, but NOT on clean stock firmware:
# there the installer aborted with "Target directory not found" and the user
# had to mkdir it by hand (forum report, stock 1.5.2, 2026-08-19). Our
# include points straight at extended/ace.cfg, so the directory needs no
# PAXX machinery behind it - creating it is enough.
if [ ! -d "$CONFIG_DIR" ]; then
    if mkdir -p "$CONFIG_DIR" 2>/dev/null; then
        log "Created config directory: $CONFIG_DIR"
    fi
fi
for d in "$EXTRAS_DIR" "$KINEMATICS_DIR" "$CONFIG_DIR"; do
    if [ ! -d "$d" ]; then
        log "ERROR: Target directory not found: $d"
        exit 1
    fi
done
log "Target directories verified"
# OVERLAY PERSISTENCE GATE. Root is an overlay whose upper layer lives at
# /oem/overlay/upper, and the firmware WIPES that layer on every boot unless
# /oem/.debug exists. Everything we write under /home/lava/klipper/ is in
# that layer; ace.cfg is not (printer_data is persistent). So without the
# flag an install looks perfect, survives until the next reboot, and then
# Klipper halts with "Section 'ace' is not a valid config section" - the
# config is still included but the extras are gone. Clean stock firmware
# ships WITHOUT the flag (HW 2026-08-20, stock 1.5.2), so this bit people
# who never had to think about it. Creating the flag only takes effect at
# the NEXT boot, which is why we set it and say so rather than trying to
# work around it.
if [ ! -e /oem/.debug ]; then
    if touch /oem/.debug 2>/dev/null; then
        sync 2>/dev/null || true
        OVERLAY_FLAG_CREATED=1
        log "  NOTE: /oem/.debug was missing - created it."
        log "        Without it the firmware wipes /home/lava/klipper on"
        log "        every boot and multiACE would vanish after a reboot."
    else
        log "  WARNING: /oem/.debug is missing and could not be created"
        log "           (run the installer as root). Without it this"
        log "           install will NOT survive a reboot."
    fi
fi
log "Backing up current files..."
for f in "filament_feed.py" "filament_switch_sensor.py"; do
    if [ -f "$EXTRAS_DIR/$f" ] && [ ! -f "$EXTRAS_DIR/${f%.py}_pre_multiace.py" ]; then
        cp "$EXTRAS_DIR/$f" "$EXTRAS_DIR/${f%.py}_pre_multiace.py"
        chmod 644 "$EXTRAS_DIR/${f%.py}_pre_multiace.py"
        log "  Backed up $f -> ${f%.py}_pre_multiace.py"
    fi
done
if [ -f "$KINEMATICS_DIR/extruder.py" ] && [ ! -f "$KINEMATICS_DIR/extruder_pre_multiace.py" ]; then
    cp "$KINEMATICS_DIR/extruder.py" "$KINEMATICS_DIR/extruder_pre_multiace.py"
    chmod 644 "$KINEMATICS_DIR/extruder_pre_multiace.py"
    log "  Backed up extruder.py -> extruder_pre_multiace.py"
fi
if [ -f "$CONFIG_DIR/ace.cfg" ] && [ ! -f "$CONFIG_DIR/ace_pre_multiace.cfg" ]; then
    cp "$CONFIG_DIR/ace.cfg" "$CONFIG_DIR/ace_pre_multiace.cfg"
    log "  Backed up ace.cfg -> ace_pre_multiace.cfg"
fi
log "Installing multiACE files..."
cp "$INSTALL_DIR/klipper/extras/ace.py" "$EXTRAS_DIR/ace.py"
cp "$INSTALL_DIR/klipper/extras/ace_protocol.py" "$EXTRAS_DIR/ace_protocol.py"
cp "$INSTALL_DIR/klipper/extras/ace_protocol_v1.py" "$EXTRAS_DIR/ace_protocol_v1.py"
cp "$INSTALL_DIR/klipper/extras/ace_protocol_v2.py" "$EXTRAS_DIR/ace_protocol_v2.py"
cp "$INSTALL_DIR/klipper/extras/filament_feed_ace.py" "$EXTRAS_DIR/filament_feed_ace.py"
cp "$INSTALL_DIR/klipper/extras/filament_switch_sensor_ace.py" "$EXTRAS_DIR/filament_switch_sensor_ace.py"
# Background-swap module (EXPERIMENTAL): inert without its config section
# ([ace_bg_swap]) - always shipped so opting in is a config-only step and a
# stale copy can never shadow a new ace.py.
cp "$INSTALL_DIR/klipper/extras/ace_bg_swap.py" "$EXTRAS_DIR/ace_bg_swap.py"
# Per-material tip-forming tables ([ace_tipform] section, inert on 'stock')
cp "$INSTALL_DIR/klipper/extras/ace_tipform.py" "$EXTRAS_DIR/ace_tipform.py"
chmod 644 "$EXTRAS_DIR/ace.py" "$EXTRAS_DIR/ace_protocol.py" "$EXTRAS_DIR/ace_protocol_v1.py" "$EXTRAS_DIR/ace_protocol_v2.py" "$EXTRAS_DIR/filament_feed_ace.py" "$EXTRAS_DIR/filament_switch_sensor_ace.py" "$EXTRAS_DIR/ace_bg_swap.py" "$EXTRAS_DIR/ace_tipform.py"
log "  Klipper extras installed"
cp "$INSTALL_DIR/klipper/kinematics/extruder_ace.py" "$KINEMATICS_DIR/extruder_ace.py"
chmod 644 "$KINEMATICS_DIR/extruder_ace.py"
log "  Klipper kinematics installed"
# Multi-MCU homing trsync window. Stock 1.4.1 sets TRSYNC_TIMEOUT=0.050; the
# eddy-current bed probe intermittently exceeds it on a toolhead MCU and trips
# "Communication timeout during homing" (0003-0528, index:3). We raise it to
# 0.350 (stock 1.5's own default) to mask it. The standalone web-head daemon
# (S98multiace-web) reduces but does not eliminate the 0003 (web-preflight
# bedmesh still trips it), so the mitigation is re-armed here. The root cause -
# klippy code-page eviction on overlay/SSH installs during the multi-MCU
# homing-probe window - is tracked separately.
#
# ONLY-RAISE: stock 1.5 already ships 0.350 (and a future stock may go higher);
# never pull the window back DOWN. Patch only when the live value is LOWER than
# our floor (e.g. 1.4.1's 0.050).
# TESTING: set TRSYNC_VALUE to "0.050" to observe the real 0003 signal.
TRSYNC_VALUE="0.350"
MCU_PY="${HOME_DIR}/klipper/klippy/mcu.py"
if [ -f "$MCU_PY" ] && grep -qE '^TRSYNC_TIMEOUT = ' "$MCU_PY"; then
    TRSYNC_CUR="$(grep -oE '^TRSYNC_TIMEOUT = [0-9.]+' "$MCU_PY" | awk '{print $3}')"
    if [ -n "$TRSYNC_CUR" ] && awk "BEGIN{exit !($TRSYNC_CUR < $TRSYNC_VALUE)}"; then
        [ -f "${MCU_PY}.pre_multiace" ] || cp "$MCU_PY" "${MCU_PY}.pre_multiace" 2>/dev/null || true
        sed -i -E "s/^TRSYNC_TIMEOUT = .*/TRSYNC_TIMEOUT = ${TRSYNC_VALUE}/" "$MCU_PY"
        log "  TRSYNC_TIMEOUT raised ${TRSYNC_CUR} -> ${TRSYNC_VALUE} in mcu.py (backup: ${MCU_PY}.pre_multiace)"
    else
        log "  TRSYNC_TIMEOUT already >= ${TRSYNC_VALUE} (${TRSYNC_CUR}) - leaving stock untouched"
    fi
fi
# Same 0003/"Timer too close" family as the trsync raise above: pre-warm the
# klipper page cache at every boot so the homing/probe window never takes a
# cold page fault (see the script header for details). Needs root for
# /etc/init.d; a lava-context web update skips it with a note (the previously
# installed copy keeps running). Also run it ONCE right now so the current
# session is warmed without a reboot.
if [ -f "$INSTALL_DIR/deploy/S59multiace-prewarm" ]; then
    if [ "$IS_ROOT" = "1" ]; then
        cp "$INSTALL_DIR/deploy/S59multiace-prewarm" /etc/init.d/S59multiace-prewarm
        sed -i 's/\r$//' /etc/init.d/S59multiace-prewarm
        chmod 0755 /etc/init.d/S59multiace-prewarm
        log "  Installed init script: /etc/init.d/S59multiace-prewarm (klipper page-cache prewarm)"
        /etc/init.d/S59multiace-prewarm start >>"$LOGFILE" 2>&1 || true
    else
        log "  SKIP: S59multiace-prewarm install needs root (existing copy stays active)"
    fi
fi
prune_cfg_backups() {
    # Keep only the newest 3 timestamped ace.cfg backups - the names sort
    # chronologically, so this needs no stat/find. Without it every install
    # adds one forever (Dirk: the config already grows such a list).
    ls -1 "$ACTIVE_CFG".bak.* 2>/dev/null | sort | head -n -3 | while read -r old_bak; do
        rm -f "$old_bak" && log "  pruned old config backup: $old_bak"
    done
}

NEW_CFG="$INSTALL_DIR/config/extended/ace.cfg"
ACTIVE_CFG="$CONFIG_DIR/ace.cfg"
MERGER="$INSTALL_DIR/tools/merge_ace_cfg.py"
ACE_CFG_MERGED=0
if [ "$KEEP_CONFIG" -eq 1 ] && [ -f "$ACTIVE_CFG" ]; then
    log "  ace.cfg kept (--keep-config)"
elif [ -f "$ACTIVE_CFG" ] && [ -f "$MERGER" ]; then
    ts=$(date -u '+%Y%m%d-%H%M%S')
    backup="$ACTIVE_CFG.bak.$ts"
    cp "$ACTIVE_CFG" "$backup"
    prune_cfg_backups
    tmp_out="$ACTIVE_CFG.merged.$$"
    if python3 "$MERGER" "$ACTIVE_CFG" "$NEW_CFG" "$tmp_out"; then
        mv "$tmp_out" "$ACTIVE_CFG"
        chmod 644 "$ACTIVE_CFG"
        ACE_CFG_MERGED=1
        log "  ace.cfg merged with shipped defaults (backup: $backup)"
    else
        rm -f "$tmp_out"
        cp "$NEW_CFG" "$ACTIVE_CFG"
        chmod 644 "$ACTIVE_CFG"
        log "  ace.cfg merge failed - clean install (backup: $backup)"
    fi
else
    if [ -f "$ACTIVE_CFG" ]; then
        ts=$(date -u '+%Y%m%d-%H%M%S')
        backup="$ACTIVE_CFG.bak.$ts"
        cp "$ACTIVE_CFG" "$backup"
        prune_cfg_backups
        log "  existing ace.cfg backed up to $backup"
    fi
    cp "$NEW_CFG" "$ACTIVE_CFG"
    chmod 644 "$ACTIVE_CFG"
    log "  ace.cfg installed"
fi
mkdir -p "$MULTIACE_DIR"
cp "$INSTALL_DIR/config/extended/multiace/ace_mode_switch.sh" "$MULTIACE_DIR/ace_mode_switch.sh"
chmod +x "$MULTIACE_DIR/ace_mode_switch.sh"
if [ ! -f "$MULTIACE_DIR/ace_vars.cfg" ]; then
    cp "$INSTALL_DIR/config/extended/multiace/ace_vars.cfg" "$MULTIACE_DIR/ace_vars.cfg"
    log "  ace_vars.cfg created (fresh)"
else
    log "  ace_vars.cfg exists, keeping current settings"
fi
# Filament material list is no longer a static file: the web backend reads
# the selectable materials + subtypes straight from the firmware filament DB
# (klippy/extras/filament_parameters.py), so there is nothing to seed here.
log "  multiace config installed"
if [ -d "$INSTALL_DIR/i18n" ]; then
    mkdir -p "$MULTIACE_DIR/i18n"
    cp -a "$INSTALL_DIR/i18n/." "$MULTIACE_DIR/i18n/"
    chown -R lava:lava "$MULTIACE_DIR/i18n"
    log "  i18n catalogs installed to $MULTIACE_DIR/i18n"
fi
if [ -f "$INSTALL_DIR/uninstall_multiace.sh" ]; then
    cp "$INSTALL_DIR/uninstall_multiace.sh" "$MULTIACE_DIR/uninstall_multiace.sh"
    chmod +x "$MULTIACE_DIR/uninstall_multiace.sh"
    log "  Uninstall script installed"
fi
# $MULTIACE_DIR is created with `mkdir -p` above; in the root (SSH) install
# context that leaves it root:root, so the web backend (lava) cannot write
# its runtime files (slot_overrides.json, filament_snapshots/) and Klipper
# (lava) cannot read them -> "[Errno 13] Permission denied" on
# slot_overrides.json. Hand the whole tree back to lava. Self-heals any
# pre-existing root-owned files from an earlier SSH overwrite. Lava context
# already owns them, so this is root-only.
if [ "$IS_ROOT" = "1" ]; then
    chown -R lava:lava "$MULTIACE_DIR" 2>/dev/null || true
    log "  multiace config dir ownership set to lava:lava"
fi
if [ -d "$INSTALL_DIR/tools" ]; then
    mkdir -p "${HOME_DIR}/printer_data/config/tools"
    cp "$INSTALL_DIR/tools/"*.py "${HOME_DIR}/printer_data/config/tools/" 2>/dev/null || true
    log "  Tools installed"
fi
if [ -f "$INSTALL_DIR/tools/multiace_update.sh" ]; then
    mkdir -p "$MULTIACE_DIR/.."
    cp "$INSTALL_DIR/tools/multiace_update.sh" "$HOME_DIR/multiace_update.sh"
    chmod 755 "$HOME_DIR/multiace_update.sh"
    chown lava:lava "$HOME_DIR/multiace_update.sh" 2>/dev/null || true
    log "  Updater installed at $HOME_DIR/multiace_update.sh"
    # PAXX-baked firmware ships a copy at /home/lava/multiace/tools/
    # from the squashfs. Refresh it too so the Web backend's fallback
    # path doesn't lag behind the canonical one.
    if [ -d "$HOME_DIR/multiace/tools" ]; then
        cp "$INSTALL_DIR/tools/multiace_update.sh" \
            "$HOME_DIR/multiace/tools/multiace_update.sh" 2>/dev/null && {
            chmod 755 "$HOME_DIR/multiace/tools/multiace_update.sh" 2>/dev/null || true
            chown lava:lava "$HOME_DIR/multiace/tools/multiace_update.sh" 2>/dev/null || true
            log "  Updater also refreshed at $HOME_DIR/multiace/tools/multiace_update.sh"
        } || log "  WARN: could not refresh $HOME_DIR/multiace/tools/multiace_update.sh"
    fi
fi
if [ -f "$INSTALL_DIR/tools/merge_ace_cfg.py" ]; then
    MERGER_TARGET_DIR=/usr/local/bin
    if ! mkdir -p "$MERGER_TARGET_DIR" 2>/dev/null \
       || ! [ -w "$MERGER_TARGET_DIR" ]; then
        MERGER_TARGET_DIR="${HOME_DIR}/bin"
        mkdir -p "$MERGER_TARGET_DIR" 2>/dev/null || true
    fi
    if [ -d "$MERGER_TARGET_DIR" ] && [ -w "$MERGER_TARGET_DIR" ]; then
        cp "$INSTALL_DIR/tools/merge_ace_cfg.py" \
            "$MERGER_TARGET_DIR/multiace_merge_cfg.py"
        chmod 755 "$MERGER_TARGET_DIR/multiace_merge_cfg.py"
        log "  cfg merger installed at $MERGER_TARGET_DIR/multiace_merge_cfg.py"
    else
        log "  WARN: skipping merger install - no writable target"
    fi
fi
if pgrep -f multiace_v2d.py >/dev/null 2>&1; then
    pkill -TERM -f multiace_v2d.py 2>/dev/null || true
    sleep 1
    pkill -KILL -f multiace_v2d.py 2>/dev/null || true
    log "  Stopped legacy multiace_v2d daemon"
fi
for old_init in /etc/init.d/S55multiace_v2d /etc/init.d/multiace_v2d; do
    if [ -e "$old_init" ]; then
        "$old_init" stop 2>/dev/null || true
        rm -f "$old_init"
        log "  Removed obsolete init script: $old_init"
    fi
done
rm -f /usr/local/bin/multiace_v2d.py 2>/dev/null || true
rm -f /var/run/multiace_v2d.pid /tmp/multiace_v2.sock 2>/dev/null || true
find "$EXTRAS_DIR/__pycache__" -name "ace*" -delete 2>/dev/null || true
find "$EXTRAS_DIR/__pycache__" -name "filament_feed*" -delete 2>/dev/null || true
find "$EXTRAS_DIR/__pycache__" -name "filament_switch_sensor*" -delete 2>/dev/null || true
find "$KINEMATICS_DIR/__pycache__" -name "extruder*" -delete 2>/dev/null || true
log "Python cache cleared"
if [ -f "$PRINTER_CFG" ]; then
    if ! grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
        if grep -q '^\[' "$PRINTER_CFG"; then
            sed -i '0,/^\[/{s/^\[/[include extended\/ace.cfg]\n\n[/}' "$PRINTER_CFG"
        else
            sed -i '1i [include extended/ace.cfg]\n' "$PRINTER_CFG"
        fi
        if grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
            log "Added [include extended/ace.cfg] to printer.cfg"
        else
            echo -e '\n[include extended/ace.cfg]' >> "$PRINTER_CFG"
            log "Added [include extended/ace.cfg] to end of printer.cfg"
        fi
    else
        log "printer.cfg already includes ace.cfg"
    fi
else
    log "WARNING: printer.cfg not found at $PRINTER_CFG"
fi
sed -i 's/\r$//' "$MULTIACE_DIR/ace_mode_switch.sh"
chmod +x "$MULTIACE_DIR/ace_mode_switch.sh"
log "Mode switch script prepared"
log "Activating ACE file swap..."
bash "$MULTIACE_DIR/ace_mode_switch.sh" ace
log "ACE files activated"
rm -rf "$EXTRAS_DIR/__pycache__" 2>/dev/null || true
rm -rf "$KINEMATICS_DIR/__pycache__" 2>/dev/null || true
log "Python cache deleted"
log ""
log "Verifying install integrity..."
VERIFY_FAILED=0
verify_match() {
    local src="$1"
    local dst="$2"
    local label="$3"
    if [ ! -f "$dst" ]; then
        log "  FAIL: $label: not found at $dst"
        VERIFY_FAILED=1
        return
    fi
    if ! cmp -s "$src" "$dst"; then
        local src_size dst_size
        src_size=$(wc -c < "$src" 2>/dev/null || echo "?")
        dst_size=$(wc -c < "$dst" 2>/dev/null || echo "?")
        log "  FAIL: $label: content mismatch (src=$src_size, dst=$dst_size bytes)"
        VERIFY_FAILED=1
    else
        log "  OK:   $label"
    fi
}
verify_match "$INSTALL_DIR/klipper/extras/ace.py" \
             "$EXTRAS_DIR/ace.py" "ace.py"
verify_match "$INSTALL_DIR/klipper/extras/ace_protocol.py" \
             "$EXTRAS_DIR/ace_protocol.py" "ace_protocol.py"
verify_match "$INSTALL_DIR/klipper/extras/ace_protocol_v1.py" \
             "$EXTRAS_DIR/ace_protocol_v1.py" "ace_protocol_v1.py"
verify_match "$INSTALL_DIR/klipper/extras/ace_protocol_v2.py" \
             "$EXTRAS_DIR/ace_protocol_v2.py" "ace_protocol_v2.py"
verify_match "$INSTALL_DIR/klipper/extras/filament_feed_ace.py" \
             "$EXTRAS_DIR/filament_feed_ace.py" "filament_feed_ace.py"
verify_match "$INSTALL_DIR/klipper/extras/filament_switch_sensor_ace.py" \
             "$EXTRAS_DIR/filament_switch_sensor_ace.py" "filament_switch_sensor_ace.py"
verify_match "$INSTALL_DIR/klipper/kinematics/extruder_ace.py" \
             "$KINEMATICS_DIR/extruder_ace.py" "extruder_ace.py"
if [ "$KEEP_CONFIG" -eq 1 ] && [ -f "$CONFIG_DIR/ace.cfg" ]; then
    log "  SKIP: ace.cfg (--keep-config, user edits preserved)"
elif [ "$ACE_CFG_MERGED" -eq 1 ]; then
    if [ -s "$CONFIG_DIR/ace.cfg" ]; then
        log "  OK:   ace.cfg (merged with user values)"
    else
        log "  FAIL: ace.cfg merged but file is empty or missing"
        VERIFY_FAILED=1
    fi
else
    verify_match "$INSTALL_DIR/config/extended/ace.cfg" \
                 "$CONFIG_DIR/ace.cfg" "ace.cfg"
fi
if [ -f "$INSTALL_DIR/tools/merge_ace_cfg.py" ] \
   && [ -n "${MERGER_TARGET_DIR:-}" ] \
   && [ -f "$MERGER_TARGET_DIR/multiace_merge_cfg.py" ]; then
    verify_match "$INSTALL_DIR/tools/merge_ace_cfg.py" \
                 "$MERGER_TARGET_DIR/multiace_merge_cfg.py" \
                 "multiace_merge_cfg.py"
fi
verify_match "$EXTRAS_DIR/filament_feed_ace.py" \
             "$EXTRAS_DIR/filament_feed.py" "filament_feed.py (mode swap)"
verify_match "$EXTRAS_DIR/filament_switch_sensor_ace.py" \
             "$EXTRAS_DIR/filament_switch_sensor.py" "filament_switch_sensor.py (mode swap)"
verify_match "$KINEMATICS_DIR/extruder_ace.py" \
             "$KINEMATICS_DIR/extruder.py" "extruder.py (mode swap)"
if [ -f "$PRINTER_CFG" ]; then
    if grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
        log "  OK:   printer.cfg include"
    else
        log "  FAIL: printer.cfg missing [include extended/ace.cfg]"
        VERIFY_FAILED=1
    fi
fi
if [ "$VERIFY_FAILED" = "1" ]; then
    log ""
    log "========================================================"
    log "  INSTALL VERIFICATION FAILED"
    log ""
    log "  One or more files did not persist after copy."
    log "  This almost always means ADVANCED MODE is NOT enabled"
    log "  on the Snapmaker U1 display."
    log ""
    log "  To enable:"
    log "    Settings > About > tap firmware version 10 times"
    log "    > Advanced Mode > Root Access"
    log ""
    log "  Then re-run: bash install_multiace.sh"
    log "========================================================"
    log ""
    exit 1
fi
log "All files verified OK."
if [ "$INSTALL_WEB" = "1" ]; then
    log ""
    log "=== Installing multiACE Web ==="
    WEB_SRC="$INSTALL_DIR/web"
    WEB_DEST="${HOME_DIR}/multiace_web"
    NGINX_DROPIN="/etc/nginx/fluidd.d/multiace-web.conf"
    INITD_SCRIPT="/etc/init.d/S98multiace-web"
    if [ ! -d "$WEB_SRC" ]; then
        log "ERROR: $WEB_SRC not found - multiace/web/ missing in install bundle"
        exit 1
    fi
    mkdir -p "$WEB_DEST/backend" "$WEB_DEST/frontend" "$WEB_DEST/i18n"
    cp -a "$WEB_SRC/backend/."  "$WEB_DEST/backend/"
    cp -a "$WEB_SRC/frontend/." "$WEB_DEST/frontend/"
    if [ -d "$INSTALL_DIR/i18n" ]; then
        cp -a "$INSTALL_DIR/i18n/." "$WEB_DEST/i18n/"
    fi
    # Drop stale bytecode. May be root-owned (left over from when the web
    # head ran as root) while the updater runs as lava - so this can fail;
    # never let it abort the install (set -e). Stale .pyc is harmless:
    # Python recompiles when the .py is newer.
    rm -rf "$WEB_DEST/backend/__pycache__" 2>/dev/null || true
    find "$WEB_DEST/backend/__pycache__" -type f -delete 2>/dev/null || true
    chown -R lava:lava "$WEB_DEST" 2>/dev/null || true
    log "  Copied web/ to $WEB_DEST (incl. i18n catalogs)"
    if run_as_lava "command -v pip3 >/dev/null" 2>/dev/null; then
        log "  Installing Python deps (fastapi, uvicorn, httpx) for user lava ..."
        run_as_lava "pip3 install --user --upgrade -r '$WEB_DEST/backend/requirements.txt'" \
            >>"$LOGFILE" 2>&1 || log "  WARN: pip install reported errors - see $LOGFILE"
    else
        log "  WARN: pip3 not reachable in lava context - install backend dependencies manually"
    fi
    mkdir -p "${HOME_DIR}/printer_data/logs"
    touch    "${HOME_DIR}/printer_data/logs/multiace_web.log"
    chown lava:lava "${HOME_DIR}/printer_data/logs/multiace_web.log" 2>/dev/null || true
    if [ "$IS_ROOT" = "1" ]; then
        if [ -d /etc/nginx/fluidd.d ]; then
            cp "$WEB_SRC/deploy/multiace-web.nginx.conf" "$NGINX_DROPIN"
            log "  Installed nginx drop-in: $NGINX_DROPIN"
            if nginx -t >>"$LOGFILE" 2>&1; then
                nginx -s reload >>"$LOGFILE" 2>&1 && log "  nginx reloaded"
            else
                log "  WARN: nginx -t failed - drop-in installed but not active"
            fi
        elif [ -f /etc/nginx/sites-available/fluidd ]; then
            # Firmware 1.4 layout: no fluidd.d include dir; inject the
            # /multiace/ location straight into the fluidd server block.
            FLUIDD_SITE="/etc/nginx/sites-available/fluidd"
            if grep -q 'location /multiace/' "$FLUIDD_SITE"; then
                log "  nginx: /multiace/ already present in $FLUIDD_SITE"
            else
                if grep -rqs 'auth_check' /etc/nginx/ 2>/dev/null; then
                    AUTHLINE='        auth_request /auth_check;'
                else
                    AUTHLINE=''
                fi
                cp "$FLUIDD_SITE" "${FLUIDD_SITE}.bak.multiace" 2>/dev/null || true
                python3 - "$FLUIDD_SITE" "$AUTHLINE" <<'PYEOF'
import sys
p, authline = sys.argv[1], sys.argv[2]
s = open(p).read()
auth = (authline + "\n") if authline else ""
block = ("    location /multiace/ {\n" + auth +
         "        proxy_pass http://127.0.0.1:7126/;\n"
         "        proxy_http_version 1.1;\n"
         "        proxy_set_header Host $host;\n"
         "        proxy_set_header X-Real-IP $remote_addr;\n"
         "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
         "        proxy_set_header X-Forwarded-Proto $scheme;\n"
         "        proxy_set_header Upgrade $http_upgrade;\n"
         '        proxy_set_header Connection "upgrade";\n'
         "        proxy_buffering off;\n"
         "        proxy_read_timeout 3600s;\n"
         "        proxy_send_timeout 3600s;\n"
         "    }\n\n")
if '/multiace/' not in s and '    location / {' in s:
    open(p, 'w').write(s.replace('    location / {', block + '    location / {', 1))
    print('inserted')
else:
    print('skipped')
PYEOF
                log "  nginx: injected /multiace/ block into $FLUIDD_SITE"
            fi
            if nginx -t >>"$LOGFILE" 2>&1; then
                nginx -s reload >>"$LOGFILE" 2>&1 && log "  nginx reloaded"
            else
                log "  WARN: nginx -t failed - see $LOGFILE"
            fi
        else
            log "  WARN: neither /etc/nginx/fluidd.d nor sites-available/fluidd - nginx proxy not configured"
        fi
    else
        log "  Skipped nginx drop-in update (non-root context - already in place from first install)"
    fi
    if [ "$IS_ROOT" = "1" ]; then
        cp "$WEB_SRC/deploy/S98multiace-web" "$INITD_SCRIPT"
        sed -i 's/\r$//' "$INITD_SCRIPT"
        # 0755 (not just +x): ace.py runs as lava and starts this script
        # via sudo; a root-only -rwx------ is fine for sudo but 0755 keeps
        # it consistent and inspectable.
        chmod 0755 "$INITD_SCRIPT"
        log "  Installed init script: $INITD_SCRIPT"
    else
        log "  Skipped init script update (non-root context)"
    fi
    if [ "$IS_ROOT" = "1" ]; then
        SUDOERS_SRC="$WEB_SRC/deploy/multiace-debug.sudoers"
        SUDOERS_DST="/etc/sudoers.d/multiace-debug"
        if [ -f "$SUDOERS_SRC" ] && [ -d "/etc/sudoers.d" ]; then
            TMP_SUDO="$(mktemp)"
            cp "$SUDOERS_SRC" "$TMP_SUDO"
            sed -i 's/\r$//' "$TMP_SUDO"
            if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$TMP_SUDO" >>"$LOGFILE" 2>&1; then
                log "  WARN: visudo refused multiace-debug sudoers drop-in - skipping install"
                rm -f "$TMP_SUDO"
            else
                install -m 0440 -o root -g root "$TMP_SUDO" "$SUDOERS_DST" 2>>"$LOGFILE" \
                    || cp "$TMP_SUDO" "$SUDOERS_DST"
                chmod 0440 "$SUDOERS_DST" 2>/dev/null || true
                rm -f "$TMP_SUDO"
                log "  Installed sudoers drop-in: $SUDOERS_DST"
            fi
        elif [ ! -d "/etc/sudoers.d" ]; then
            # The Snapmaker U1 ships without sudo, so there is no
            # /etc/sudoers.d - this is normal, not an error. The web head
            # runs as the printer user and the in-place updater works via
            # the chowned klipper dirs (no sudo needed). Nothing to do.
            log "  sudo not present on this system - skipping sudoers drop-in (normal on U1)"
        else
            log "  WARN: sudoers template missing - skipping sudoers drop-in"
        fi
    else
        log "  Skipped sudoers drop-in update (non-root context)"
    fi
    if [ "$IS_ROOT" = "1" ] && [ -x "$INITD_SCRIPT" ]; then
        "$INITD_SCRIPT" stop  >>"$LOGFILE" 2>&1 || true
        "$INITD_SCRIPT" start >>"$LOGFILE" 2>&1 || log "  WARN: start failed - see $LOGFILE"
        sleep 1
        # Judge by the status EXIT CODE, not by grepping the text: the old
        # `grep -q "running"` also matched "not running", so the installer
        # reported "multiACE Web running" unconditionally - on 2026-08-05
        # that green light hid an Errno-98 bind failure while the
        # pre-update backend kept serving stale code all evening.
        if "$INITD_SCRIPT" status >>"$LOGFILE" 2>&1; then
            log "  multiACE Web running"
            log "  -> http://<printer-ip>/multiace/"
        else
            log "  WARN: multiACE Web NOT healthy - check $LOGFILE and $WEB_DEST/backend/"
        fi
    else
        if pgrep -u lava -f 'uvicorn.*main:app' >/dev/null 2>&1; then
            pkill -TERM -u lava -f 'uvicorn.*main:app' 2>/dev/null || true
            log "  Sent SIGTERM to running uvicorn (restart needed for new code)"
        fi
        # A root-owned instance (S98 boot / web-update context) is out of
        # lava's reach - the pkill above cannot touch it and the fresh code
        # will NOT serve until a reboot. Say so instead of staying silent.
        sleep 2
        if netstat -tln 2>/dev/null | grep -q ':7126 '; then
            log "  WARN: :7126 still served by an instance we cannot replace"
            log "        (root-owned?) - new web code is NOT live until reboot"
        fi
        log "  Skipped service restart (non-root context) - reboot or use Web Restart"
    fi
fi
DATA_DIR="${HOME_DIR}/printer_data"
if [ -d "$DATA_DIR" ]; then
    DATA_OWNER="$(stat -c '%U:%G' "$DATA_DIR" 2>/dev/null)"
    if [ -n "$DATA_OWNER" ] && [ "$DATA_OWNER" != "root:root" ]; then
        log "Restoring ownership ($DATA_OWNER) on $CONFIG_DIR ..."
        chown -R "$DATA_OWNER" "$CONFIG_DIR" 2>>"$LOGFILE" || true
        if [ -d "$EXTRAS_DIR" ]; then
            chown "$DATA_OWNER" "$EXTRAS_DIR" 2>>"$LOGFILE" || true
            chown "$DATA_OWNER" "$EXTRAS_DIR"/ace*.py \
                "$EXTRAS_DIR"/filament_feed_ace.py \
                "$EXTRAS_DIR"/filament_switch_sensor_ace.py \
                2>>"$LOGFILE" || true
        fi
        if [ -d "$KINEMATICS_DIR" ]; then
            chown "$DATA_OWNER" "$KINEMATICS_DIR" 2>>"$LOGFILE" || true
            chown "$DATA_OWNER" "$KINEMATICS_DIR"/extruder_ace.py \
                2>>"$LOGFILE" || true
        fi
        if [ -f "${HOME_DIR}/multiace_update.sh" ]; then
            chown "$DATA_OWNER" "${HOME_DIR}/multiace_update.sh" \
                2>>"$LOGFILE" || true
        fi
        if [ -f /tmp/multiace_web.pid ]; then
            chown "$DATA_OWNER" /tmp/multiace_web.pid 2>>"$LOGFILE" || true
        fi
    fi
fi
log ""
log "=== Installation complete ==="
log "Please reboot the printer to activate multiACE."
# Repeat the overlay note at the END too: the install output is long and a
# line from the top scrolls away long before the user reads the result.
if [ "${OVERLAY_FLAG_CREATED:-0}" = "1" ]; then
    log ""
    log "NOTE: /oem/.debug did not exist and was created by this install."
    log "      It makes the Klipper-side files survive a reboot; it takes"
    log "      effect from the next boot on. Reboot now."
fi
log ""
