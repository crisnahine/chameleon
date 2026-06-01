#!/usr/bin/env bash
set -euo pipefail

[[ "${CHAMELEON_DISABLE:-}" == "1" ]] && exit 0

# The statusline cache lives in a repo-relative path (.claude/), so its values
# are attacker-controllable. Strip control chars (ANSI/OSC escape injection,
# terminal-title rewrite) before emitting anything to the terminal.
strip_ctrl() { tr -d '[:cntrl:]'; }

input=$(cat)
project_dir=""
if command -v jq &>/dev/null; then
  project_dir=$(printf '%s' "$input" | jq -r '.workspace.project_dir // empty' 2>/dev/null || true)
fi
if [[ -z "$project_dir" ]]; then
  project_dir=$(printf '%s' "$input" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('workspace',{}).get('project_dir',''))" 2>/dev/null || true)
fi
[[ -z "$project_dir" ]] && project_dir="${CLAUDE_PROJECT_DIR:-$PWD}"

cache_file="$project_dir/.claude/.chameleon-statusline-cache"
if [[ -f "$cache_file" ]]; then
  if command -v jq &>/dev/null; then
    count=$(jq -r '.profiles | length' "$cache_file" 2>/dev/null || echo 0)
    if [[ "$count" -gt 0 ]]; then
      parts=""
      for i in $(seq 0 $((count - 1))); do
        name=$(jq -r ".profiles[$i].name" "$cache_file" 2>/dev/null | strip_ctrl)
        trust=$(jq -r ".profiles[$i].trust" "$cache_file" 2>/dev/null | strip_ctrl)
        case "$trust" in trusted|untrusted|stale|n/a) ;; *) trust="?" ;; esac
        if [[ -n "$parts" ]]; then
          parts="$parts │ "
        fi
        parts="$parts$name ($trust)"
      done
      activity=$(jq -r '.activity // empty' "$cache_file" 2>/dev/null | strip_ctrl)
      if [[ -n "$activity" ]]; then
        cache_mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || echo 0)
        cache_age=$(( $(date +%s) - cache_mtime ))
        if [[ "$cache_age" -lt 30 ]]; then
          parts="$parts │ $activity"
        fi
      fi
      update=$(jq -r '.update // empty' "$cache_file" 2>/dev/null | strip_ctrl)
      if [[ -n "$update" ]]; then
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
import json, os, re
d=json.load(open(os.environ['CACHE_PATH']))
ps=d.get('profiles',[])
def _s(v): return re.sub(r'[\x00-\x1f\x7f]','',str(v))
def _t(v):
    v=_s(v)
    return v if v in ('trusted','untrusted','stale','n/a') else '?'
if ps:
    parts=' │ '.join(f\"{_s(p.get('name',''))} ({_t(p.get('trust',''))})\" for p in ps)
    act=_s(d.get('activity',''))
    if act:
        import time
        try:
            age=time.time()-os.path.getmtime(os.environ['CACHE_PATH'])
            if age<30: parts+=f' │ {act}'
        except: pass
    upd=_s(d.get('update',''))
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
