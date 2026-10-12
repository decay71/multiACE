#!/usr/bin/env bash
set -euo pipefail

TEST_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
UPDATER="$TEST_DIR/../../tools/multiace_update.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat > "$TMP/bin/curl" <<'CURL'
#!/bin/sh
printf '%s\n' "${MULTIACE_TEST_RELEASE_JSON:?missing test release JSON}"
CURL
chmod +x "$TMP/bin/curl"

run_assets() {
    local fixture="$1"
    env \
        PATH="$TMP/bin:$PATH" \
        MULTIACE_TEST_RELEASE_JSON="$fixture" \
        MULTIACE_UPDATE_REPO="fixture/multiACE" \
        MULTIACE_UPDATE_PRERELEASE=1 \
        MULTIACE_MANAGED=0 \
        MULTIACE_DISABLE_UPDATES=0 \
        MULTIACE_MANAGED_MARKER="$TMP/no-managed-marker" \
        MULTIACE_CONFIG_DIR="$TMP/config" \
        sh "$UPDATER" assets
}

RELEASE_JSON="$(cat <<'JSON'
[
  {
    "tag_name": "v1.11b-test.123456",
    "assets": [
      {
        "name": "multiace-managed-1.11b.tar.gz",
        "browser_download_url": "https://example.invalid/multiace-managed-1.11b.tar.gz"
      },
      {
        "name": "multiace-managed-1.11b.tar.gz.sha256",
        "browser_download_url": "https://example.invalid/multiace-managed-1.11b.tar.gz.sha256"
      },
      {
        "name": "multiace-v1.11b-test.123456.tar.gz",
        "browser_download_url": "https://example.invalid/multiace-v1.11b-test.123456.tar.gz"
      },
      {
        "name": "multiace-v1.11b-test.123456.tar.gz.sha256",
        "browser_download_url": "https://example.invalid/multiace-v1.11b-test.123456.tar.gz.sha256"
      }
    ]
  }
]
JSON
)"

OUTPUT="$(run_assets "$RELEASE_JSON")"
case "$OUTPUT" in
    *"TARBALL_URL=https://example.invalid/multiace-v1.11b-test.123456.tar.gz"*);;
    *)
        printf 'FAIL: updater did not select the exact standalone archive:\n%s\n' "$OUTPUT" >&2
        exit 1
        ;;
esac
case "$OUTPUT" in
    *"SHA_URL=https://example.invalid/multiace-v1.11b-test.123456.tar.gz.sha256"*);;
    *)
        printf 'FAIL: updater did not select the matching checksum:\n%s\n' "$OUTPUT" >&2
        exit 1
        ;;
esac

MANAGED_ONLY_JSON="$(cat <<'JSON'
[
  {
    "tag_name": "v1.11b-test.123456",
    "assets": [
      {
        "name": "multiace-managed-1.11b.tar.gz",
        "browser_download_url": "https://example.invalid/multiace-managed-1.11b.tar.gz"
      },
      {
        "name": "multiace-managed-1.11b.tar.gz.sha256",
        "browser_download_url": "https://example.invalid/multiace-managed-1.11b.tar.gz.sha256"
      }
    ]
  }
]
JSON
)"
if OUTPUT="$(run_assets "$MANAGED_ONLY_JSON" 2>&1)"; then
    printf 'FAIL: updater accepted a managed archive without the standalone asset\n%s\n' "$OUTPUT" >&2
    exit 1
fi
case "$OUTPUT" in
    *"missing the exact standalone asset"*) ;;
    *)
        printf 'FAIL: missing-asset error was unclear:\n%s\n' "$OUTPUT" >&2
        exit 1
        ;;
esac



NO_CHECKSUM_JSON="$(cat <<'JSON'
[
  {
    "tag_name": "v1.11b-test.123456",
    "assets": [
      {
        "name": "multiace-v1.11b-test.123456.tar.gz",
        "browser_download_url": "https://example.invalid/multiace-v1.11b-test.123456.tar.gz"
      }
    ]
  }
]
JSON
)"
if OUTPUT="$(run_assets "$NO_CHECKSUM_JSON" 2>&1)"; then
    printf 'FAIL: updater accepted a standalone archive without its checksum\n%s\n' "$OUTPUT" >&2
    exit 1
fi
case "$OUTPUT" in
    *"missing the matching checksum"*) ;;
    *)
        printf 'FAIL: missing-checksum error was unclear:\n%s\n' "$OUTPUT" >&2
        exit 1
        ;;
esac

printf 'Standalone release asset selection tests passed\n'
