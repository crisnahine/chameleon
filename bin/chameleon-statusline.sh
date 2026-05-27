#!/usr/bin/env bash
# Status line script for the chameleon Claude Code plugin.
# Reads a cache file written by SessionStart + hooks for live state.
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

# --- Read cached state ---
cache_file="$project_dir/.claude/.chameleon-statusline-cache"
if [[ -f "$cache_file" ]]; then
  if command -v jq &>/dev/null; then
    count=$(jq -r '.profiles | length' "$cache_file" 2>/dev/null || echo 0)
    if [[ "$count" -gt 0 ]]; then
      parts=""
      for i in $(seq 0 $((count - 1))); do
        name=$(jq -r ".profiles[$i].name" "$cache_file" 2>/dev/null)
        trust=$(jq -r ".profiles[$i].trust" "$cache_file" 2>/dev/null)
        if [[ -n "$parts" ]]; then
          parts="$parts │ "
        fi
        parts="$parts$name ($trust)"
      done
      activity=$(jq -r '.activity // empty' "$cache_file" 2>/dev/null)
      if [[ -n "$activity" ]]; then
        cache_mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || echo 0)
        cache_age=$(( $(date +%s) - cache_mtime ))
        if [[ "$cache_age" -lt 30 ]]; then
          parts="$parts │ $activity"
        fi
      fi
      update=$(jq -r '.update // empty' "$cache_file" 2>/dev/null)
      if [[ -n "$update" ]]; then
        # Verify the update is still pending by reading the current
        # plugin's version. CLAUDE_PLUGIN_ROOT updates after /reload-plugins,
        # so if our version matches the update target, the reload happened.
        plugin_init="${CLAUDE_PLUGIN_ROOT:-${0%/*}/..}/mcp/chameleon_mcp/__init__.py"
        cur_ver=""
        if [[ -f "$plugin_init" ]]; then
          cur_ver=$(grep '^__version__' "$plugin_init" 2>/dev/null | head -1 | sed 's/.*= *"//;s/".*//')
        fi
        if [[ -n "$cur_ver" && "$cur_ver" != "$update" ]]; then
          parts="$parts │ ⬆ v${update} ready — /reload-plugins or reopen session"
        fi
      fi
      printf '🦎 chameleon │ %s' "$parts"
      exit 0
    fi
  else
    result=$(CACHE_PATH="$cache_file" python3 -c "
import json, os
d=json.load(open(os.environ['CACHE_PATH']))
ps=d.get('profiles',[])
if ps:
    parts=' │ '.join(f\"{p['name']} ({p['trust']})\" for p in ps)
    act=d.get('activity','')
    if act:
        import time
        try:
            age=time.time()-os.path.getmtime(os.environ['CACHE_PATH'])
            if age<30: parts+=f' │ {act}'
        except: pass
    upd=d.get('update','')
    if upd:
        import re as _re
        _pr=os.environ.get('CLAUDE_PLUGIN_ROOT','')
        _pi=os.path.join(_pr,'mcp','chameleon_mcp','__init__.py') if _pr else ''
        _cv=''
        if _pi:
            try:
                for _ln in open(_pi):
                    if _ln.startswith('__version__'):
                        _m=_re.search(r'\"([^\"]+)\"',_ln)
                        if _m: _cv=_m.group(1)
                        break
            except: pass
        if _cv and _cv!=upd: parts+=' │ ⬆ v'+upd+' ready'
    print(f'🦎 chameleon │ {parts}')
" 2>/dev/null || true)
    if [[ -n "$result" ]]; then
      printf '%s' "$result"
      exit 0
    fi
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

exit 0
