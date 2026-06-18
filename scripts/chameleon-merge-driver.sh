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
# Git Bash may pass a Windows path; normalize so the resolver path resolves.
PLUGIN_ROOT="${PLUGIN_ROOT//\\//}"
MCP_DIR="${PLUGIN_ROOT}/mcp"

# Resolve a dep-capable Python >=3.11 via the shared hook resolver. This driver
# imports chameleon_mcp, so it needs the deps and the >=3.11 floor — not whatever
# bare python3 resolves to (on macOS that is 3.9.x, below the floor and without
# the deps). Run the resolver as a subprocess so a damaged resolver cannot abort
# this script. Unlike the hooks, the merge driver must NOT fail open: a merge it
# cannot run is left conflicted (exit 1) for manual resolution rather than
# silently overwriting the profile.
CHAMELEON_PY=()
RESOLVER="${PLUGIN_ROOT}/hooks/_resolve-python.sh"
if [ -r "${RESOLVER}" ]; then
    while IFS= read -r _tok || [ -n "${_tok}" ]; do
        if [ -n "${_tok}" ]; then CHAMELEON_PY+=("${_tok}"); fi
    done < <("${BASH:-bash}" "${RESOLVER}" "${MCP_DIR}" 2>/dev/null || true)
fi
if [ "${#CHAMELEON_PY[@]}" -eq 0 ]; then
    echo "chameleon-merge-driver: no Python >=3.11 found (and uv unavailable); leaving conflict" >&2
    exit 1
fi

# Pass the three paths through the environment, never interpolated into the
# Python source. Git-supplied merge paths (and repo checkouts) can contain
# single quotes or other metacharacters; inlining them into a `python -c`
# string literal broke the literal and was a code-injection sink.
CH_MERGE_BASE="${BASE}" CH_MERGE_OURS="${OURS}" CH_MERGE_THEIRS="${THEIRS}" \
PYTHONPATH="${MCP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${CHAMELEON_PY[@]}" -c '
import os, sys
from chameleon_mcp.tools import merge_profiles
result = merge_profiles(
    repo="",
    base=os.environ["CH_MERGE_BASE"],
    ours=os.environ["CH_MERGE_OURS"],
    theirs=os.environ["CH_MERGE_THEIRS"],
)
data = result.get("data", {})
status = data.get("status")
if status == "success":
    sys.exit(0)
print("chameleon-merge-driver failed: {}".format(data.get("error", status)), file=sys.stderr)
sys.exit(1)
'
