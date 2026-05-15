#!/usr/bin/env bash
#
# check-no-personal-paths.sh - fail if any tracked file contains an
# absolute developer home path (/Users/<name> or /home/<name>).
#
# Shipped skills, hooks, docs, and tests must be machine-independent. A
# hardcoded /Users/<someone> path breaks the moment another developer or
# a CI runner clones the repo. This class regressed twice before, so it
# is now gated.
#
# Generic placeholders (/Users/you, /home/user, ...) are allowed so docs
# can show example paths. Use a relative path, ~, an env var, or a
# placeholder instead of a real home directory.
#
# Run locally before pushing:
#   scripts/check-no-personal-paths.sh
#
# CI runs it on every PR (see .github/workflows/ci.yml).

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

SELF="scripts/check-no-personal-paths.sh"

# Names that are obviously placeholders, not a real account. Matched
# case-insensitively against the path segment right after /Users/ or
# /home/. "runner" covers GitHub-hosted CI ($HOME=/home/runner).
ALLOW='^(you|your-user|youruser|user|username|name|me|runner|ci|example)$'

violations=0

while IFS= read -r -d '' file; do
  [ "$file" = "$SELF" ] && continue
  case "$file" in
    *.lock | *.png | *.jpg | *.jpeg | *.gif | *.ico | *.pdf | *.svg) continue ;;
  esac

  # grep -o emits every match on its own line, prefixed with the line
  # number, so a line carrying two paths is fully checked (not just the
  # first). Each token looks like "<lineno>:/Users/<name>".
  while IFS=: read -r lineno token; do
    [ -z "$token" ] && continue
    seg=${token##*/}
    if printf '%s\n' "$seg" | grep -qiE "$ALLOW"; then
      continue
    fi
    printf '%s:%s: %s\n' "$file" "$lineno" "$token"
    violations=$((violations + 1))
  done < <(grep -noIE '/(Users|home)/[A-Za-z0-9._-]+' -- "$file" 2>/dev/null || true)
done < <(git ls-files -z)

if [ "$violations" -gt 0 ]; then
  echo ""
  echo "FAIL: $violations personal path(s) in tracked files."
  echo "Replace with a relative path, ~, an env var, or a placeholder"
  echo "such as /Users/you/... (see scripts/check-no-personal-paths.sh)."
  exit 1
fi

echo "OK: no personal paths in tracked files."
