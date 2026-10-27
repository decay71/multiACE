#!/bin/bash
# run-dev-ui.sh - start the multiACE web UI locally, against a real
# Moonraker if one is reachable and against tests/fixtures/*.json if not.
#
#   ./scripts/run-dev-ui.sh                 # auto-detect
#   MOONRAKER_URL=http://printer:7125 ./scripts/run-dev-ui.sh
#   MULTIACE_MOCK_MODE=1 ./scripts/run-dev-ui.sh   # force mock
#   MULTIACE_MOCK_STATE_FILE=mock_state_head.json ./scripts/run-dev-ui.sh
#       # dashboard in head mode instead of the default multi-ACE fixture
#
# Mock mode also unlocks POST /api/debug/simulate (inject a fake error
# notification) and GET /api/debug/sample-gcode (a small 4-filament
# sample print, tests/fixtures/sample_4color.gcode).
set -e

PORT="${PORT:-7126}"
MOONRAKER_URL="${MOONRAKER_URL:-http://127.0.0.1:7125}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/multiace/web/backend"

# Git Bash / MSYS hands POSIX paths ("/c/Users/…") to a NATIVE Windows
# Python, which reads them as drive-relative and silently finds nothing -
# every label in the UI then renders as its raw i18n key. Convert every
# path we export. No-op on Linux/macOS, where cygpath does not exist.
winpath() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

if [ -n "$MULTIACE_MOCK_MODE" ]; then
    echo "-> mock mode forced by MULTIACE_MOCK_MODE=$MULTIACE_MOCK_MODE"
elif curl -fsS --max-time 2 "$MOONRAKER_URL/printer/info" >/dev/null 2>&1; then
    echo "OK  Moonraker found at $MOONRAKER_URL - using real data"
    MULTIACE_MOCK_MODE=0
else
    echo "--  Moonraker not reachable at $MOONRAKER_URL - using mock data"
    MULTIACE_MOCK_MODE=1
fi

export MULTIACE_MOCK_MODE
export MOONRAKER_URL
export MULTIACE_MOCK_DIR="${MULTIACE_MOCK_DIR:-$(winpath "$REPO_ROOT/tests/fixtures")}"
# The installer copies the catalogs next to the backend (web/i18n); in a
# checkout they live at multiace/i18n. Without this every label in the UI
# renders as its raw key.
export MULTIACE_I18N_DIR="${MULTIACE_I18N_DIR:-$(winpath "$REPO_ROOT/multiace/i18n")}"
# A dev box has no /home/lava; point the config editor at a scratch copy
# so saving from the UI cannot 404 (and cannot touch a real printer).
if [ -z "$MULTIACE_CFG_PATH" ]; then
    MULTIACE_CFG_PATH="$REPO_ROOT/.devdata/ace.cfg"
    mkdir -p "$(dirname "$MULTIACE_CFG_PATH")"
    [ -f "$MULTIACE_CFG_PATH" ] || cp "$REPO_ROOT/multiace/config/extended/ace.cfg" "$MULTIACE_CFG_PATH"
    MULTIACE_CFG_PATH="$(winpath "$MULTIACE_CFG_PATH")"
    export MULTIACE_CFG_PATH
fi
echo "  config: $MULTIACE_CFG_PATH"

# Pick an interpreter that actually RUNS: a python3 that merely exists on
# PATH is not enough (a broken Windows Store / stale install shim is on
# most dev boxes, and it fails only once uvicorn is already "started").
PY=""
for cand in "$PYTHON" python3 python py; do
    [ -n "$cand" ] || continue
    if "$cand" -c 'import sys' >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
    echo "No working Python found - set PYTHON=/path/to/python" >&2
    exit 1
fi

URL="http://127.0.0.1:$PORT/"
(
    sleep 2
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
    elif command -v open >/dev/null 2>&1; then open "$URL"
    fi
) &

if [ "$MULTIACE_MOCK_MODE" = "1" ]; then
    echo "  mock: sample print at http://127.0.0.1:$PORT/api/debug/sample-gcode"
fi

echo "Dev server on $URL  (Ctrl+C to stop)"
cd "$BACKEND_DIR"
exec "$PY" -m uvicorn main:app --host 127.0.0.1 --port "$PORT" --reload
