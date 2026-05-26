#!/usr/bin/env bash
# Status line script for the chameleon Claude Code plugin.
# Reads a cache file written by SessionStart for profile name + trust state.
# Must complete in <100ms.
set -euo pipefail

[[ "${CHAMELEON_DISABLE:-}" == "1" ]] && exit 0

# --- Read project dir from stdin JSON ---
input=$(cat)
project_dir=""
if command -v jq &>/dev/null; then
  project_dir=$(printf '%s' "$input" | jq -r '.workspace.project_dir // empty' 2>/dev/null)
fi
if [[ -z "$project_dir" ]]; then
  project_dir=$(printf '%s' "$input" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('workspace',{}).get('project_dir',''))" 2>/dev/null || true)
fi
[[ -z "$project_dir" ]] && project_dir="${CLAUDE_PROJECT_DIR:-$PWD}"

# --- Read cached state written by SessionStart ---
cache_file="$project_dir/.claude/.chameleon-statusline-cache"
if [[ -f "$cache_file" ]]; then
  if command -v jq &>/dev/null; then
    profile_name=$(jq -r '.profile // empty' "$cache_file" 2>/dev/null)
    trust_state=$(jq -r '.trust // "untrusted"' "$cache_file" 2>/dev/null)
  else
    profile_name=$(python3 -c "import json; d=json.load(open('$cache_file')); print(d.get('profile',''))" 2>/dev/null || true)
    trust_state=$(python3 -c "import json; d=json.load(open('$cache_file')); print(d.get('trust','untrusted'))" 2>/dev/null || echo "untrusted")
  fi
  if [[ -n "$profile_name" ]]; then
    printf '🦎 chameleon │ %s │ %s' "$profile_name" "$trust_state"
    exit 0
  fi
fi

# --- Fallback: check for .chameleon/ profile without trust info ---
dir="$project_dir"
while true; do
  if [[ -f "$dir/.chameleon/profile.json" ]]; then
    printf '🦎 chameleon │ %s' "$(basename "$dir")"
    exit 0
  fi
  parent="$(dirname "$dir")"
  [[ "$parent" == "$dir" ]] && break
  dir="$parent"
done

printf '🦎 chameleon │ no profile'
