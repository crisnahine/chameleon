#!/usr/bin/env bash
# Bash-driven skill-triggering smoke test.
#
# Verifies each user-invocable chameleon slash command produces a
# response when invoked via real Claude Code. Mirrors superpowers'
# tests/skill-triggering/ pattern but condensed into a single script
# (chameleon has fewer skills than superpowers).
#
# Run with:
#   bash tests/skill_triggering_test.sh
#
# Costs ~$0.05 per skill x 7 skills = ~$0.35 per run. Skip on every-
# commit; use before releases.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/test-helpers.sh"

require_claude_cli

EF_CLIENT="/Users/crisn/Documents/Projects/empire-flippers/client"

if [ ! -d "$EF_CLIENT" ]; then
    echo "SKIP: EF_CLIENT not found at $EF_CLIENT"
    exit 0
fi

# /chameleon-pause-15m and /chameleon-disable invocations through real
# Claude Code will actually write pause / session-disable markers in the
# user-level plugin data dir. Clean them up on exit so the markers don't
# linger and silently suppress later test runs.
EF_CLIENT_REPO_ID="$(python3 -c '
import hashlib, sys
print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest())
' "$EF_CLIENT")"
PLUGIN_DATA="${HOME}/.local/share/chameleon/${EF_CLIENT_REPO_ID}"
cleanup_test_markers() {
    rm -f "${PLUGIN_DATA}/.pause_until" 2>/dev/null || true
    rm -f "${PLUGIN_DATA}"/.session_disabled.* 2>/dev/null || true
}
trap cleanup_test_markers EXIT
cleanup_test_markers  # also clear any leftovers from prior run

cd "$EF_CLIENT"

PASS=0
FAIL=0
RESULTS=()

run_skill_test() {
    local skill_name="$1"
    local expected_pattern="$2"
    local test_label="$3"

    echo ""
    echo "Testing: /$skill_name"

    local output
    output=$(run_claude "/chameleon:$skill_name" 120 \
        "Bash Read mcp__plugin_chameleon_chameleon-mcp__detect_repo mcp__plugin_chameleon_chameleon-mcp__get_drift_status mcp__plugin_chameleon_chameleon-mcp__list_profiles mcp__plugin_chameleon_chameleon-mcp__bootstrap_repo mcp__plugin_chameleon_chameleon-mcp__refresh_repo mcp__plugin_chameleon_chameleon-mcp__teach_profile mcp__plugin_chameleon_chameleon-mcp__trust_profile mcp__plugin_chameleon_chameleon-mcp__disable_session mcp__plugin_chameleon_chameleon-mcp__pause_session" \
        "")

    if assert_contains "$output" "$expected_pattern" "$test_label"; then
        PASS=$((PASS + 1))
        RESULTS+=("PASS  /$skill_name")
    else
        FAIL=$((FAIL + 1))
        RESULTS+=("FAIL  /$skill_name")
    fi
}

# (skill, regex pattern that should appear in claude output, label)
run_skill_test "chameleon-status" "drift\|status\|profile" \
    "/chameleon-status mentions drift/status/profile"
run_skill_test "chameleon-init" "bootstrap\|already\|profile" \
    "/chameleon-init mentions bootstrap or existing profile"
run_skill_test "chameleon-refresh" "refresh\|profile\|drift" \
    "/chameleon-refresh mentions refresh"
run_skill_test "chameleon-trust" "trust\|repo\|confirm" \
    "/chameleon-trust mentions trust"
run_skill_test "chameleon-teach" "idiom\|teach\|feedback\|capture" \
    "/chameleon-teach mentions idiom/teach/capture"
run_skill_test "chameleon-disable" "disable\|session\|suppress" \
    "/chameleon-disable mentions disable/suppress"
run_skill_test "chameleon-pause-15m" "pause\|15\|minute" \
    "/chameleon-pause-15m mentions pause"

echo ""
echo "=== Summary ==="
echo "  Total: $((PASS + FAIL))"
echo "  Pass:  $PASS"
echo "  Fail:  $FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo ""
    for r in "${RESULTS[@]}"; do
        echo "  $r"
    done
    exit 1
fi
exit 0
