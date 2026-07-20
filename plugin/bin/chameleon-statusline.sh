#!/usr/bin/env bash
set -euo pipefail

[[ "${CHAMELEON_DISABLE:-}" == "1" ]] && exit 0

# Bound the stdin read so a pathological payload cannot blow the <100ms render
# budget. Real Claude Code payloads are tiny JSON; 256 KB is far above any of them.
input=$(head -c 262144 2>/dev/null || true)
project_dir=""
if command -v jq &>/dev/null; then
  project_dir=$(printf '%s' "$input" | jq -r '.workspace.project_dir // empty' 2>/dev/null || true)
fi
if [[ -z "$project_dir" ]]; then
  # Deliberately bare python3, unlike the hooks' _resolve-python.sh ladder: every
  # python call in this script is stdlib-only JSON that runs fine on 3.9 (the
  # macOS system python), each call is fail-silent, and the ladder's version
  # probe alone would blow the <100ms render budget.
  project_dir=$(printf '%s' "$input" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('workspace',{}).get('project_dir') or '')" 2>/dev/null || true)
fi
[[ -z "$project_dir" ]] && project_dir="${CLAUDE_PROJECT_DIR:-$PWD}"

# Surface an active /chameleon-pause-15m window. While `.pause_until` is in
# the future, PreToolUse silently skips BOTH advisory injection AND the
# security-deny gates (secret-in-content, eval-call, import-preference), so a
# live pause is a security-relevant state the user should be able to see
# without guessing. Reads the exact same `${PLUGIN_DATA}/<repo_id>/.pause_until`
# file the hooks check (`is_chameleon_suppressed` in optouts.py) rather than a
# separate cache, so this can never drift from what the hooks actually honor.
pause_suffix=""
if [[ "${CHAMELEON_STATUSLINE_PAUSE:-}" != "0" ]]; then
  plugin_data_dir="${CHAMELEON_PLUGIN_DATA:-$HOME/.local/share/chameleon}"
  # The common no-pause render must stay O(1): a single stat on the
  # machine-wide `.pause_active` sentinel `write_pause` touches, instead of a
  # data-dir walk whose cost grows with every profiled repo. Only while the
  # sentinel exists does the marker sweep below run. A pause is deliberately
  # short-lived (<=240 minutes); once every marker has expired the sweep
  # removes the sentinel itself, closing the gate again. A marker left by a
  # repo no other session ever queries again would otherwise hold the gate
  # open forever (the per-repo read path that unlinks expired markers never
  # runs for a dead repo), so a well-formed marker whose second-precision Z
  # timestamp is already past is removed here -- the same unlink the read
  # path performs. Lexicographic comparison is sound because the writer emits
  # exactly %Y-%m-%dT%H:%M:%SZ; anything unreadable or oddly formatted keeps
  # the gate open and lets the python path decide, never a silent drop of a
  # live pause.
  pause_gate=""
  if [[ -f "$plugin_data_dir/.pause_active" ]]; then
    now_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    while IFS= read -r pause_marker; do
      [[ -n "$pause_marker" ]] || continue
      pause_expiry=""
      IFS= read -r pause_expiry < "$pause_marker" 2>/dev/null || true
      if [[ "$pause_expiry" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
        && [[ ! "$pause_expiry" > "$now_utc" ]]; then
        rm -f -- "$pause_marker" 2>/dev/null || pause_gate=1
      else
        pause_gate=1
      fi
    done < <(find "$plugin_data_dir" -maxdepth 2 -name '.pause_until' 2>/dev/null)
    if [[ -z "$pause_gate" ]]; then
      rm -f -- "$plugin_data_dir/.pause_active" 2>/dev/null || true
    fi
  fi
  if [[ -n "$pause_gate" ]]; then
    pause_root="$project_dir"
    while true; do
      [[ -f "$pause_root/.chameleon/profile.json" ]] && break
      pause_parent="$(dirname "$pause_root")"
      if [[ "$pause_parent" == "$pause_root" ]]; then
        pause_root=""
        break
      fi
      pause_root="$pause_parent"
    done
    if [[ -n "$pause_root" ]]; then
      pause_suffix=$(CHAMELEON_STATUSLINE_ROOT="$pause_root" \
        CHAMELEON_STATUSLINE_MCP_DIR="${CLAUDE_PLUGIN_ROOT:-${0%/*}/..}/mcp" \
        python3 -c "
import os, sys, time
from datetime import datetime
sys.path.insert(0, os.environ['CHAMELEON_STATUSLINE_MCP_DIR'])
try:
    from pathlib import Path
    from chameleon_mcp.repo_id import _compute_repo_id
    from chameleon_mcp.optouts import _pause_has_valid_signature
    repo_id = _compute_repo_id(Path(os.environ['CHAMELEON_STATUSLINE_ROOT']))
    data_dir = os.environ.get('CHAMELEON_PLUGIN_DATA') or os.path.join(os.path.expanduser('~'), '.local', 'share', 'chameleon')
    with open(os.path.join(data_dir, repo_id, '.pause_until'), encoding='utf-8') as f:
        lines = f.read().splitlines()
    expiry_iso = lines[0].strip() if lines else ''
    sig_line = ''
    for line in lines[1:]:
        if line.startswith('sig='):
            sig_line = line[len('sig='):].strip()
    expiry = datetime.fromisoformat(expiry_iso.replace('Z', '+00:00'))
    remaining = expiry.timestamp() - time.time()
    # Verify the signature so the display matches enforcement: a planted or
    # forged marker is_chameleon_suppressed rejects must not read as paused.
    if remaining > 0 and _pause_has_valid_signature(repo_id, expiry_iso, sig_line):
        mins = int(remaining // 60) + (1 if remaining % 60 else 0)
        print(f' │ ⏸ paused — {mins}m left')
except Exception:
    pass
" 2>/dev/null || true)
    fi
  fi
fi

cache_file="$project_dir/.claude/.chameleon-statusline-cache"
if [[ -f "$cache_file" ]]; then
  if command -v jq &>/dev/null; then
    # One jq pass emits every field as a tagged, tab-separated record so the
    # process spawn count stays constant regardless of profile count (the
    # former per-field/per-profile loop spawned 2N+3 jq processes and broke
    # the <100ms budget past ~12 profiles). Each field is stripped of control,
    # bidi, and zero-width chars (plus the framing tab/newline) in the same jq
    # gsub, so embedded content cannot corrupt the line protocol or inject a
    # terminal escape -- locale-independent, and no per-render process spawn.
    records=$(jq -r '
      (.profiles // [])[] as $p
        | "P\t" + (($p.name // "") | gsub("[\u0000-\u001f\u007f\u0080-\u009f\u200b-\u200d\ufeff\u200e-\u200f\u202a-\u202e\u2066-\u2069\u2500-\u259f]"; "")) + "\t" + (($p.trust // "") | gsub("[\u0000-\u001f\u007f\u0080-\u009f\u200b-\u200d\ufeff\u200e-\u200f\u202a-\u202e\u2066-\u2069\u2500-\u259f]"; "")),
      "A\t" + ((.activity // "") | tostring | gsub("[\u0000-\u001f\u007f\u0080-\u009f\u200b-\u200d\ufeff\u200e-\u200f\u202a-\u202e\u2066-\u2069\u2500-\u259f]"; "")),
      "U\t" + ((.update // "") | tostring | gsub("[\u0000-\u001f\u007f\u0080-\u009f\u200b-\u200d\ufeff\u200e-\u200f\u202a-\u202e\u2066-\u2069\u2500-\u259f]"; ""))
    ' "$cache_file" 2>/dev/null || true)
    if [[ -n "$records" ]]; then
      parts=""
      activity=""
      update=""
      while IFS=$'\t' read -r tag f1 f2; do
        case "$tag" in
          P)
            name="$f1"
            trust="$f2"
            case "$trust" in trusted|untrusted|stale|n/a) ;; *) trust="?" ;; esac
            if [[ -n "$parts" ]]; then
              parts="$parts │ "
            fi
            parts="$parts$name ($trust)"
            ;;
          A) activity="$f1" ;;
          U) update="$f1" ;;
        esac
      done <<<"$records"
    fi
    if [[ -n "${parts:-}" ]]; then
      if [[ -n "$activity" ]]; then
        cache_mtime=$(stat -c %Y "$cache_file" 2>/dev/null || stat -f %m "$cache_file" 2>/dev/null || echo 0)
        cache_age=$(( $(date +%s) - cache_mtime ))
        if [[ "$cache_age" -lt 30 ]]; then
          parts="$parts │ $activity"
        fi
      fi
      if [[ -n "$update" ]]; then
        plugin_init="${CLAUDE_PLUGIN_ROOT:-${0%/*}/..}/mcp/chameleon_mcp/__init__.py"
        cur_ver=""
        if [[ -f "$plugin_init" ]]; then
          # grep exits non-zero when the file has no column-0 __version__
          # literal; under pipefail that would abort the whole statusline, so
          # the no-match is absorbed and cur_ver is left empty.
          cur_ver=$({ grep '^__version__' "$plugin_init" 2>/dev/null || true; } | head -1 | sed 's/.*= *"//;s/".*//')
        fi
        if [[ -n "$cur_ver" && "$cur_ver" != "$update" ]]; then
          parts="$parts │ ⬆ v${update} ready — /reload-plugins or reopen session"
        fi
      fi
      # `|| true`: a closed stdout (e.g. a caller reading only stderr) makes
      # printf's write() fail under `-e`, which would otherwise abort the
      # script with a nonzero exit -- the fail-open contract this statusline
      # holds everywhere else (see the jq-miss guard above) requires exit 0
      # regardless of what happened to the fd we tried to write to.
      printf '🦎 chameleon │ %s%s' "$parts" "$pause_suffix" || true
      exit 0
    fi
  else
    result=$(CACHE_PATH="$cache_file" PLUGIN_ROOT_FALLBACK="${0%/*}/.." python3 -c "
import json, os, re
d=json.load(open(os.environ['CACHE_PATH']))
ps=d.get('profiles',[])
_CTRL=re.compile(r'[\x00-\x1f\x7f\x80-\x9f\u200b-\u200d\ufeff\u200e\u200f\u202a-\u202e\u2066-\u2069\u2500-\u259f]')
def _s(v): return _CTRL.sub('',str(v))
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
        _pr=os.environ.get('CLAUDE_PLUGIN_ROOT') or os.environ.get('PLUGIN_ROOT_FALLBACK','')
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
        if _cv and _cv!=upd: parts+=' │ ⬆ v'+upd+' ready — /reload-plugins or reopen session'
    print(parts)
" 2>/dev/null || true)
    if [[ -n "$result" ]]; then
      printf '🦎 chameleon │ %s%s' "$result" "$pause_suffix" || true
      exit 0
    fi
  fi
fi

dir="$project_dir"
while true; do
  if [[ -f "$dir/.chameleon/profile.json" ]]; then
    printf '🦎 chameleon │ %s%s' "$(basename "$dir")" "$pause_suffix" || true
    exit 0
  fi
  parent="$(dirname "$dir")"
  [[ "$parent" == "$dir" ]] && break
  dir="$parent"
done

exit 0
