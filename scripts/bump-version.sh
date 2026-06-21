#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$REPO_ROOT/.version-bump.json"

if [[ ! -f "$CONFIG" ]]; then
  echo "error: .version-bump.json not found at $CONFIG" >&2
  exit 1
fi

# jq is required for every command (manifest read/write, config parsing, and the
# profile schema scan). Without this guard, --validate-profiles reads
# schema_version via `jq ... 2>/dev/null || sv=""`, which silently skips every
# profile and falsely reports them all compatible. Fail loudly instead of letting
# a missing dependency masquerade as a clean validation.
if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required but was not found on PATH" >&2
  exit 1
fi

read_json_field() {
  local file="$1" field="$2"
  if [[ "$file" == *.toml ]]; then
    _read_toml_field "$file" "$field"
    return
  fi
  if [[ "$file" == *.py ]]; then
    _read_py_field "$file" "$field"
    return
  fi
  local jq_path
  jq_path=$(echo "$field" | sed -E 's/\.([0-9]+)/[\1]/g' | sed 's/^/./' | sed 's/\.\././g')
  jq -r "$jq_path" "$file"
}

write_json_field() {
  local file="$1" field="$2" value="$3"
  if [[ "$file" == *.toml ]]; then
    _write_toml_field "$file" "$field" "$value"
    return
  fi
  if [[ "$file" == *.py ]]; then
    _write_py_field "$file" "$field" "$value"
    return
  fi
  local jq_path
  jq_path=$(echo "$field" | sed -E 's/\.([0-9]+)/[\1]/g' | sed 's/^/./' | sed 's/\.\././g')
  local tmp="${file}.tmp"
  jq --arg v "$value" "$jq_path = \$v" "$file" > "$tmp" && mv "$tmp" "$file"
}

_read_py_field() {
  local file="$1" field="$2"
  awk -v k="$field" '
    $0 ~ "^"k"[[:space:]]*=" {
      sub(/^[^=]*=[[:space:]]*/, "")
      gsub(/^["'\'']|["'\'']$/, "")
      print; exit
    }
  ' "$file"
}

_write_py_field() {
  local file="$1" field="$2" value="$3"
  local tmp="${file}.tmp"
  awk -v k="$field" -v v="$value" '
    !done && $0 ~ "^"k"[[:space:]]*=" {
      print k " = \"" v "\""; done=1; next
    }
    {print}
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

_read_toml_field() {
  local file="$1" field="$2"
  local section key
  section=$(echo "$field" | awk -F. '{NF--; print}' OFS=.)
  key=$(echo "$field" | awk -F. '{print $NF}')
  if [[ -z "$section" ]]; then
    awk -v k="$key" 'BEGIN{FS="[[:space:]]*=[[:space:]]*"} $1==k {gsub(/^"|"$/, "", $2); print $2; exit}' "$file"
    return
  fi
  awk -v sect="[$section]" -v k="$key" '
    $0==sect {in_sect=1; next}
    /^\[/ {in_sect=0}
    in_sect && $0 ~ "^"k"[[:space:]]*=" {
      sub(/^[^=]*=[[:space:]]*/, "")
      gsub(/^"|"$/, "")
      print; exit
    }
  ' "$file"
}

_write_toml_field() {
  local file="$1" field="$2" value="$3"
  local section key
  section=$(echo "$field" | awk -F. '{NF--; print}' OFS=.)
  key=$(echo "$field" | awk -F. '{print $NF}')
  local tmp="${file}.tmp"
  if [[ -z "$section" ]]; then
    awk -v k="$key" -v v="$value" '
      !done && $1==k {sub(/=.*/, "= \""v"\""); done=1}
      {print}
    ' "$file" > "$tmp" && mv "$tmp" "$file"
    return
  fi
  awk -v sect="[$section]" -v k="$key" -v v="$value" '
    $0==sect {in_sect=1; print; next}
    /^\[/ {in_sect=0}
    in_sect && !done && $0 ~ "^"k"[[:space:]]*=" {
      sub(/=.*/, "= \""v"\""); done=1
    }
    {print}
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

declared_files() {
  jq -r '.files[] | "\(.path)\t\(.field)"' "$CONFIG"
}

audit_excludes() {
  jq -r '.audit.exclude[]' "$CONFIG" 2>/dev/null
}

cmd_check() {
  local has_drift=0
  local versions=()

  echo "Version check:"
  echo ""

  while IFS=$'\t' read -r path field; do
    local fullpath="$REPO_ROOT/$path"
    if [[ ! -f "$fullpath" ]]; then
      printf "  %-45s  MISSING\n" "$path ($field)"
      has_drift=1
      continue
    fi
    local ver
    ver=$(read_json_field "$fullpath" "$field")
    printf "  %-45s  %s\n" "$path ($field)" "$ver"
    versions+=("$ver")
  done < <(declared_files)

  echo ""

  # Guard the empty case before expanding "${versions[@]}": macOS bash 3.2
  # errors on an empty-array expansion under `set -u`.
  if [[ ${#versions[@]} -eq 0 ]]; then
    echo "error: no declared files found in .version-bump.json" >&2
    return 1
  fi

  local unique
  unique=$(printf '%s\n' "${versions[@]}" | sort -u | wc -l | tr -d ' ')
  if [[ "$unique" -gt 1 ]]; then
    echo "DRIFT DETECTED — versions are not in sync:"
    printf '%s\n' "${versions[@]}" | sort | uniq -c | sort -rn | while read -r count ver; do
      echo "  $ver ($count files)"
    done
    has_drift=1
  else
    echo "All declared files are in sync at ${versions[0]}"
  fi

  return $has_drift
}

cmd_audit() {
  cmd_check || true
  echo ""

  local current_version
  current_version=$(
    while IFS=$'\t' read -r path field; do
      local fullpath="$REPO_ROOT/$path"
      [[ -f "$fullpath" ]] && read_json_field "$fullpath" "$field"
    done < <(declared_files) | sort | uniq -c | sort -rn | head -1 | awk '{print $2}'
  )

  if [[ -z "$current_version" ]]; then
    echo "error: could not determine current version" >&2
    return 1
  fi

  echo "Audit: scanning repo for version string '$current_version'..."
  echo ""

  local -a exclude_args=()
  while IFS= read -r pattern; do
    exclude_args+=("--exclude=$pattern" "--exclude-dir=$pattern")
  done < <(audit_excludes)

  exclude_args+=("--exclude-dir=.git" "--exclude-dir=node_modules" "--exclude-dir=.venv" "--binary-files=without-match")

  local -a declared_paths=()
  while IFS=$'\t' read -r path _field; do
    declared_paths+=("$path")
  done < <(declared_files)

  local found_undeclared=0
  while IFS= read -r match; do
    local match_file
    match_file=$(echo "$match" | cut -d: -f1)
    local rel_path="${match_file#$REPO_ROOT/}"

    local is_declared=0
    for dp in "${declared_paths[@]}"; do
      if [[ "$rel_path" == "$dp" ]]; then
        is_declared=1
        break
      fi
    done

    if [[ "$is_declared" -eq 0 ]]; then
      if [[ "$found_undeclared" -eq 0 ]]; then
        echo "UNDECLARED files containing '$current_version':"
        found_undeclared=1
      fi
      echo "  $match"
    fi
  done < <(grep -rn "${exclude_args[@]}" -F "$current_version" "$REPO_ROOT" 2>/dev/null || true)

  if [[ "$found_undeclared" -eq 0 ]]; then
    echo "No undeclared files contain the version string. All clear."
  else
    echo ""
    echo "Review the above files — if they should be bumped, add them to .version-bump.json"
    echo "If they should be skipped, add them to the audit.exclude list."
  fi
}

# Read MAX_SUPPORTED_SCHEMA_VERSION from the profile loader so the check tracks
# the engine without a second source of truth.
engine_max_schema() {
  local loader="$REPO_ROOT/mcp/chameleon_mcp/profile/loader.py"
  [[ -f "$loader" ]] || return 1
  awk '
    /^MAX_SUPPORTED_SCHEMA_VERSION[[:space:]]*=/ {
      sub(/^[^=]*=[[:space:]]*/, "")
      sub(/[^0-9].*$/, "")
      print; exit
    }
  ' "$loader"
}

cmd_validate_profiles() {
  local scan_dir="${1:-$REPO_ROOT}"

  local max_schema
  max_schema=$(engine_max_schema)
  if [[ -z "$max_schema" ]]; then
    echo "WARN: could not read MAX_SUPPORTED_SCHEMA_VERSION from loader.py; skipping" >&2
    return 0
  fi

  echo "Validating committed .chameleon profiles against engine schema <= $max_schema..."
  echo ""

  local found_incompatible=0
  local scanned=0
  while IFS= read -r profile; do
    local sv
    # jq returns "null" on a missing key and exits non-zero on malformed JSON.
    sv=$(jq -r '.schema_version // empty' "$profile" 2>/dev/null) || sv=""
    [[ -z "$sv" ]] && continue
    [[ "$sv" =~ ^[0-9]+$ ]] || continue
    # Count only profiles actually validated (a real integer schema_version), so
    # the summary's "All N scanned" excludes skipped/malformed files.
    scanned=$((scanned + 1))
    if (( sv > max_schema )); then
      if [[ "$found_incompatible" -eq 0 ]]; then
        echo "INCOMPATIBLE profiles (schema newer than engine supports):"
        found_incompatible=1
      fi
      local rel="${profile#"$scan_dir"/}"
      echo "  $rel (schema_version=$sv > $max_schema)"
    fi
  done < <(find "$scan_dir" -type f -path '*/.chameleon/profile.json' 2>/dev/null)

  if [[ "$found_incompatible" -eq 0 ]]; then
    echo "All $scanned scanned profile(s) load under the current engine schema."
    return 0
  fi

  echo ""
  echo "These profiles must be regenerated with /chameleon-refresh after the bump."
  return 0
}

cmd_bump() {
  local new_version="$1"

  if ! echo "$new_version" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "error: '$new_version' doesn't look like a version (expected exactly X.Y.Z)" >&2
    exit 1
  fi

  echo "Bumping all declared files to $new_version..."
  echo ""

  while IFS=$'\t' read -r path field; do
    local fullpath="$REPO_ROOT/$path"
    if [[ ! -f "$fullpath" ]]; then
      echo "  SKIP (missing): $path"
      continue
    fi
    local old_ver
    old_ver=$(read_json_field "$fullpath" "$field")
    write_json_field "$fullpath" "$field" "$new_version"
    printf "  %-45s  %s -> %s\n" "$path ($field)" "$old_ver" "$new_version"
  done < <(declared_files)

  local venv_site="$REPO_ROOT/mcp/.venv/lib"
  if [[ -d "$venv_site" ]]; then
    local cleaned=0
    while IFS= read -r -d '' stale_dir; do
      if [[ "$(basename "$stale_dir")" != "chameleon_mcp-${new_version}.dist-info" ]]; then
        rm -rf "$stale_dir"
        ((cleaned++)) || true
      fi
    done < <(find "$venv_site" -maxdepth 3 -type d -name 'chameleon_mcp-*.dist-info' -print0 2>/dev/null)
    if [[ $cleaned -gt 0 ]]; then
      echo "Cleaned $cleaned stale dist-info dir(s) from venv."
    fi
  fi

  # Regenerate lockfiles so they carry the new version. CI runs
  # `uv sync --frozen` and `npm ci`, both of which fail if the lock is stale
  # after a version bump. Non-fatal: warn and continue if a tool is absent or
  # offline, so a bump never hard-fails on lock regeneration.
  if command -v uv >/dev/null 2>&1; then
    if (cd "$REPO_ROOT/mcp" && uv lock >/dev/null 2>&1); then
      echo "Regenerated mcp/uv.lock"
    else
      echo "WARN: 'uv lock' failed; run it in mcp/ before pushing (CI uses --frozen)" >&2
    fi
  else
    echo "WARN: uv not found; mcp/uv.lock not regenerated (CI uses --frozen)" >&2
  fi
  if command -v npm >/dev/null 2>&1; then
    if (cd "$REPO_ROOT/mcp" && npm install --package-lock-only --silent >/dev/null 2>&1); then
      echo "Regenerated mcp/package-lock.json"
    else
      echo "WARN: 'npm install --package-lock-only' failed; regenerate mcp/package-lock.json before pushing" >&2
    fi
  else
    echo "WARN: npm not found; mcp/package-lock.json not regenerated" >&2
  fi

  echo ""
  echo "Done. Running audit to check for missed files..."
  echo ""
  cmd_audit
}

case "${1:-}" in
  --check)
    cmd_check
    ;;
  --audit)
    cmd_audit
    ;;
  --validate-profiles)
    cmd_validate_profiles "${2:-}"
    ;;
  --help|-h|"")
    echo "Usage: bump-version.sh <new-version> | --check | --audit | --validate-profiles [dir]"
    echo ""
    echo "  <new-version>         Bump all declared files to the given version"
    echo "  --check               Show current versions, detect drift"
    echo "  --audit               Check + scan repo for undeclared version references"
    echo "  --validate-profiles   Warn if any committed .chameleon profile has a"
    echo "                        schema_version newer than this engine supports"
    exit 0
    ;;
  --*)
    echo "error: unknown flag '$1'" >&2
    exit 1
    ;;
  *)
    cmd_bump "$1"
    ;;
esac
