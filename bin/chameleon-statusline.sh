#!/usr/bin/env bash
# Status line script for the chameleon Claude Code plugin.
# Prints a one-line summary: profile name + trust state.
# Must complete in <100ms - no MCP calls, no Python imports.
set -euo pipefail

# Disabled? Output nothing.
[[ "${CHAMELEON_DISABLE:-}" == "1" ]] && exit 0

# --- Locate .chameleon/profile.json by walking up from project dir ---
start_dir="${CLAUDE_PROJECT_DIR:-$PWD}"
profile_dir=""
dir="$start_dir"
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
