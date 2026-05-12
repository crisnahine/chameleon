#!/usr/bin/env bash
# Re-bootstrap eval-fixture profiles with a pinned `now` for deterministic
# witness selection. Default mode is --check (dry run); --apply writes.
#
# Usage:
#   scripts/refresh_eval_fixtures.sh            # check, exit non-zero if diff
#   scripts/refresh_eval_fixtures.sh --check    # same
#   scripts/refresh_eval_fixtures.sh --apply    # write the regenerated .chameleon/
#
# Note: --check uses a tmpdir for the comparison, so wall-clock fields
# (generation, created_at, pid, committed-at, repo_id) will always diff.
# Use --apply when you actually want to regenerate.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PINNED_NOW=1700000000.0
MODE="check"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check) MODE="check" ;;
        --apply) MODE="apply" ;;
        -h|--help)
            grep -E "^# " "$0" | sed 's/^# //'
            exit 0
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
    shift
done

FIXTURES=(
    "tests/fixtures/eval_repos/ts_minimal"
    "tests/fixtures/eval_repos/ruby_minimal"
)

cd "${REPO_ROOT}/mcp"

DIRTY=0

for fixture in "${FIXTURES[@]}"; do
    abs="${REPO_ROOT}/${fixture}"
    echo "==> ${fixture}"
    if [ ! -d "${abs}" ]; then
        echo "    missing fixture directory: ${abs}" >&2
        exit 1
    fi

    if [ "${MODE}" = "check" ]; then
        scratch="$(mktemp -d)"
        trap 'rm -rf "${scratch}"' EXIT
        cp -R "${abs}/." "${scratch}/"
        rm -rf "${scratch}/.chameleon"
        PYTHONPATH=.:../tests .venv/bin/python -c "
from chameleon_mcp.tools import bootstrap_repo
import sys, json
r = bootstrap_repo('${scratch}', now=${PINNED_NOW}, force=True)
if r['data'].get('status') != 'success':
    print(json.dumps(r['data'], indent=2))
    sys.exit(1)
"
        if ! diff -rq "${abs}/.chameleon" "${scratch}/.chameleon" > /tmp/refresh_diff_$$.txt 2>&1; then
            echo "    DIFF detected:" >&2
            diff -ru "${abs}/.chameleon" "${scratch}/.chameleon" >&2 || true
            DIRTY=1
        else
            echo "    clean"
        fi
        rm -f /tmp/refresh_diff_$$.txt
        rm -rf "${scratch}"
        trap - EXIT
    else
        rm -rf "${abs}/.chameleon"
        PYTHONPATH=.:../tests .venv/bin/python -c "
from chameleon_mcp.tools import bootstrap_repo
import sys, json
r = bootstrap_repo('${abs}', now=${PINNED_NOW}, force=True)
if r['data'].get('status') != 'success':
    print(json.dumps(r['data'], indent=2))
    sys.exit(1)
"
        echo "    regenerated"
    fi
done

if [ "${MODE}" = "check" ] && [ "${DIRTY}" -eq 1 ]; then
    echo "Refresh would change checked-in files. Run with --apply to commit." >&2
    exit 1
fi

echo "Summary: refresh ${MODE} complete"
exit 0
