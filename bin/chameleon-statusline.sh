#!/usr/bin/env bash
# Status line script for the chameleon Claude Code plugin.
# Prints a one-line summary: profile name + trust state.
# Receives session JSON on stdin from Claude Code.
# Must complete in <100ms - no MCP calls, no heavy imports.
set -euo pipefail

# Disabled? Output nothing.
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

# --- Locate .chameleon/profile.json by walking up from project dir ---
profile_dir=""
dir="$project_dir"
while true; do
  if [[ -f "$dir/.chameleon/profile.json" ]]; then
    profile_dir="$dir/.chameleon"
    break
  fi
  parent="$(dirname "$dir")"
  [[ "$parent" == "$dir" ]] && break
  dir="$parent"
done

if [[ -z "$profile_dir" ]]; then
  printf '🦎 chameleon │ no profile'
  exit 0
fi

# --- Extract profile name (repo directory basename) ---
repo_dir="$(dirname "$profile_dir")"
profile_name="$(basename "$repo_dir")"

# --- Determine trust state ---
# repo_id = sha256 of git remote URL (or cwd if no remote)
remote_url="$(git -C "$repo_dir" remote get-url origin 2>/dev/null || echo "$repo_dir")"
repo_id="$(printf '%s' "$remote_url" | shasum -a 256 | cut -d' ' -f1)"

plugin_data="${CHAMELEON_PLUGIN_DATA:-$HOME/.local/share/chameleon}"
trust_file="$plugin_data/$repo_id/.trust"

trust_state="untrusted"
if [[ -f "$trust_file" ]]; then
  trust_state="trusted"
fi

printf '🦎 chameleon │ %s │ %s' "$profile_name" "$trust_state"
