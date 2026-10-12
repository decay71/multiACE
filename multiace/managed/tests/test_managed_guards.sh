#!/usr/bin/env bash
set -euo pipefail

TEST_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MULTIACE_ROOT="$(CDPATH= cd -- "$TEST_DIR/../.." && pwd)"
MARKER="$(mktemp)"
trap 'rm -f "$MARKER"' EXIT

GUARDED_SCRIPTS=(
    "install_multiace.sh"
    "uninstall_multiace.sh"
    "tools/multiace_update.sh"
    "config/extended/multiace/ace_mode_switch.sh"
)

run_guard_test() {
    local label="$1"
    shift
    local script output status
    for script in "${GUARDED_SCRIPTS[@]}"; do
        set +e
        output="$(env "$@" bash "$MULTIACE_ROOT/$script" 2>&1)"
        status=$?
        set -e
        if [ "$status" -ne 2 ]; then
            printf 'FAIL: %s guard for %s returned %s\n%s\n' \
                "$label" "$script" "$status" "$output" >&2
            exit 1
        fi
        case "$output" in
            *managed*) ;;
            *)
                printf 'FAIL: %s guard for %s did not explain managed ownership\n' \
                    "$label" "$script" >&2
                exit 1
                ;;
        esac
    done
}

# The marker is the fallback used when a shell session does not inherit the
# activation hook's environment.
run_guard_test marker \
    -u MULTIACE_MANAGED \
    MULTIACE_MANAGED_MARKER="$MARKER"

# The explicit environment contract remains supported as well.
run_guard_test environment \
    MULTIACE_MANAGED=1 \
    MULTIACE_MANAGED_MARKER="/path/that/does/not/exist"

printf 'Managed-install guard tests passed\n'
