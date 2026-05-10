#!/usr/bin/env bash
# verify-vendor-checksums.sh — CI gate verifying integrity of vendored deps.
#
# Per ARCHITECTURE.md "Security mitigations" #2 (Vendor integrity checksums):
#   - mcp/typescript-checksums.json lists SHA-256 of every file under
#     mcp/node_modules/typescript/.
#   - This script verifies actual checksums match the manifest.
#   - Run by CI on every build; fails if mismatch (supply-chain compromise signal).
#
# Phase 1C placeholder. Phase 4 implements the real verification.
#
# Pre-requisite: TypeScript must be vendored (Phase 2). Until then, this
# script is a no-op.

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS_DIR="${PLUGIN_ROOT}/mcp/node_modules/typescript"
CHECKSUMS_FILE="${PLUGIN_ROOT}/mcp/typescript-checksums.json"

if [ ! -d "$TS_DIR" ]; then
    echo "verify-vendor-checksums.sh: TypeScript not yet vendored (Phase 2). Skipping."
    exit 0
fi

if [ ! -f "$CHECKSUMS_FILE" ]; then
    echo "verify-vendor-checksums.sh: Checksums file not found at $CHECKSUMS_FILE"
    echo "Phase 4 will generate this during the vendoring step."
    exit 0
fi

echo "verify-vendor-checksums.sh: Phase 4 implementation pending."
echo ""
echo "Phase 4 implementation will:"
echo "  1. Load $CHECKSUMS_FILE"
echo "  2. For each entry: compute SHA-256 of $TS_DIR/<path>"
echo "  3. Compare to manifest; fail with non-zero exit on mismatch"
echo "  4. Report any extra files in $TS_DIR not in manifest"
echo ""
echo "Run by CI before every build. Run manually after quarterly TS bump."
exit 0
