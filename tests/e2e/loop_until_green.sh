#!/usr/bin/env bash
# Wraps the comprehensive E2E in an automated loop. Re-runs until the
# script exits 0 (all assertions pass). Operator must interrupt with
# Ctrl-C if a particular failure keeps recurring after manual fix.
#
# Usage:
#   ./tests/e2e/loop_until_green.sh                    # cheap only
#   ./tests/e2e/loop_until_green.sh --include-real-claude
#
# Each iteration's full log lands in tests/e2e/iter_NNN.log.

set -u
cd "$(dirname "$0")/../.."

MAX_ITER=${MAX_ITER:-10}
EXTRA="$*"

for i in $(seq 1 "$MAX_ITER"); do
  echo "==================== E2E ITERATION $i / $MAX_ITER ===================="
  log_path="tests/e2e/iter_$(printf '%03d' "$i").log"
  PYTHONPATH=mcp:tests mcp/.venv/bin/python tests/e2e/comprehensive_e2e.py $EXTRA \
    > "$log_path" 2>&1
  rc=$?
  tail -20 "$log_path"
  if [[ $rc -eq 0 ]]; then
    echo
    echo "✓ E2E ITERATION $i PASSED. Loop terminates."
    exit 0
  fi
  echo
  echo "✗ E2E iteration $i exited $rc. Full log: $log_path"
  echo "  (loop_until_green.sh does NOT auto-fix bugs — the operator"
  echo "   examines the log and either fixes code or re-runs with the"
  echo "   same args. Sleeping 5s before the next iteration so an"
  echo "   intervening Ctrl-C is easy to land.)"
  sleep 5
done

echo
echo "✗ E2E failed $MAX_ITER iterations in a row. Bailing."
exit 1
