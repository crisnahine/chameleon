#!/usr/bin/env bash
# Git merge driver wrapper for chameleon profile JSONs.
#
# Register with:
#   git config merge.chameleon.driver \\
#     "${CLAUDE_PLUGIN_ROOT}/scripts/chameleon-merge-driver.sh %O %A %B %P"
#
# And in .gitattributes:
#   .chameleon/archetypes.json merge=chameleon
#   .chameleon/canonicals.json merge=chameleon
#   .chameleon/rules.json      merge=chameleon
#   .chameleon/profile.json    merge=chameleon
#
# Git invokes this with:
#   $1 = %O — path to common ancestor (base) version
#   $2 = %A — path to "ours" version (also the merge target; we WRITE here)
#   $3 = %B — path to "theirs" version
#   $4 = %P — original pathname (e.g. ".chameleon/archetypes.json")
#
# Exit 0 on successful merge. Non-zero leaves git's regular conflict
# markers in $2 for manual resolution.

set -euo pipefail

if [ "$#" -lt 3 ]; then
    echo "chameleon-merge-driver: usage: $0 BASE OURS THEIRS [PATH]" >&2
    exit 2
fi

BASE="$1"
OURS="$2"
THEIRS="$3"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${0%/*}/..}"
MCP_DIR="${PLUGIN_ROOT}/mcp"

if [ -d "${MCP_DIR}/.venv" ]; then
    PYTHON="${MCP_DIR}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
else
    echo "chameleon-merge-driver: no Python interpreter found" >&2
    exit 1
fi

PYTHONPATH="${MCP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON}" -c "
import json, sys
from chameleon_mcp.tools import merge_profiles
result = merge_profiles(repo='', base='$BASE', ours='$OURS', theirs='$THEIRS')
data = result.get('data', {})
status = data.get('status')
if status == 'success':
    sys.exit(0)
print(f'chameleon-merge-driver failed: {data.get(\"error\", status)}', file=sys.stderr)
sys.exit(1)
"
