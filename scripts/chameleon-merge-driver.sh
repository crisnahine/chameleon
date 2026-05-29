#!/usr/bin/env bash

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
