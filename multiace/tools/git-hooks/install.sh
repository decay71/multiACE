#!/bin/bash
# Install multiACE git hooks into this repo's .git/hooks/ directory.
# Idempotent — safe to re-run.

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

install_hook() {
    local name="$1"
    local src="$HERE/$name"
    local dst="$HOOKS_DIR/$name"
    if [ ! -f "$src" ]; then
        echo "  skip: $name (source not found at $src)"
        return
    fi
    cp "$src" "$dst"
    chmod +x "$dst"
    echo "  installed: $name"
}

echo "Installing multiACE git hooks into $HOOKS_DIR"
install_hook post-commit
echo "Done."
