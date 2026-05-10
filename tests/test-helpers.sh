#!/usr/bin/env bash
# Helper functions for Claude Code integration tests.
#
# Adapted from superpowers/tests/claude-code/test-helpers.sh.
#
# Source this file from a bash test script:
#   source "${BASH_SOURCE%/*}/test-helpers.sh"
#
# Then use run_claude / assert_contains / assert_not_contains / etc.

CHAMELEON_PLUGIN_ROOT="${CHAMELEON_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Run Claude Code with a prompt and capture output.
# Usage: run_claude "prompt text" [timeout_seconds] [allowed_tools] [extra_flags]
#
# Always loads chameleon via --plugin-dir; never uses bypassPermissions
# (which silences PreToolUse hooks). Returns 0 on success; non-zero on
# timeout or claude error.
run_claude() {
    local prompt="$1"
    local timeout="${2:-90}"
    local allowed_tools="${3:-}"
    local extra_flags="${4:-}"
    local output_file
    output_file=$(mktemp)

    local cmd
    cmd="claude -p \"$prompt\" --plugin-dir \"$CHAMELEON_PLUGIN_ROOT\""
    if [ -n "$allowed_tools" ]; then
        cmd="$cmd --allowedTools \"$allowed_tools\""
    fi
    if [ -n "$extra_flags" ]; then
        cmd="$cmd $extra_flags"
    fi

    if timeout "$timeout" bash -c "$cmd" > "$output_file" 2>&1; then
        cat "$output_file"
        rm -f "$output_file"
        return 0
    else
        local exit_code=$?
        cat "$output_file" >&2
        rm -f "$output_file"
        return $exit_code
    fi
}

# Run claude in stream-json mode and return the raw JSON event lines.
# Useful when you need to inspect hook_events / tool_uses programmatically.
run_claude_stream() {
    local prompt="$1"
    local timeout="${2:-90}"
    local allowed_tools="${3:-}"
    local max_turns="${4:-3}"
    local extra_flags="${5:-}"

    local cmd
    cmd="claude -p \"$prompt\" --plugin-dir \"$CHAMELEON_PLUGIN_ROOT\""
    cmd="$cmd --output-format stream-json --include-hook-events"
    cmd="$cmd --max-turns $max_turns --verbose"
    if [ -n "$allowed_tools" ]; then
        cmd="$cmd --allowedTools \"$allowed_tools\""
    fi
    if [ -n "$extra_flags" ]; then
        cmd="$cmd $extra_flags"
    fi

    timeout "$timeout" bash -c "$cmd"
}

# Assert that output contains a pattern.
# Usage: assert_contains "$output" "pattern" "test name"
assert_contains() {
    local output="$1"
    local pattern="$2"
    local test_name="${3:-test}"

    if echo "$output" | grep -q -- "$pattern"; then
        echo "  [PASS] $test_name"
        return 0
    else
        echo "  [FAIL] $test_name"
        echo "    Expected to find: $pattern"
        echo "    In output:"
        echo "$output" | sed 's/^/      /'
        return 1
    fi
}

# Assert that output does NOT contain a pattern.
# Usage: assert_not_contains "$output" "pattern" "test name"
assert_not_contains() {
    local output="$1"
    local pattern="$2"
    local test_name="${3:-test}"

    if echo "$output" | grep -q -- "$pattern"; then
        echo "  [FAIL] $test_name"
        echo "    Did not expect to find: $pattern"
        return 1
    else
        echo "  [PASS] $test_name"
        return 0
    fi
}

# Assert pattern A appears before pattern B in output.
# Usage: assert_order "$output" "pattern_a" "pattern_b" "test name"
assert_order() {
    local output="$1"
    local pattern_a="$2"
    local pattern_b="$3"
    local test_name="${4:-test}"

    local line_a line_b
    line_a=$(echo "$output" | grep -n -- "$pattern_a" | head -1 | cut -d: -f1)
    line_b=$(echo "$output" | grep -n -- "$pattern_b" | head -1 | cut -d: -f1)

    if [ -z "$line_a" ]; then
        echo "  [FAIL] $test_name: pattern A not found: $pattern_a"
        return 1
    fi
    if [ -z "$line_b" ]; then
        echo "  [FAIL] $test_name: pattern B not found: $pattern_b"
        return 1
    fi
    if [ "$line_a" -lt "$line_b" ]; then
        echo "  [PASS] $test_name (A at line $line_a, B at line $line_b)"
        return 0
    else
        echo "  [FAIL] $test_name"
        echo "    Expected '$pattern_a' before '$pattern_b'"
        echo "    Got: A at line $line_a, B at line $line_b"
        return 1
    fi
}

# Create + clean up a temporary repo for tests.
create_test_project() {
    mktemp -d
}

cleanup_test_project() {
    local test_dir="$1"
    if [ -d "$test_dir" ]; then
        rm -rf "$test_dir"
    fi
}

# Skip the test file with a message if `claude` CLI is not on PATH.
require_claude_cli() {
    if ! command -v claude >/dev/null 2>&1; then
        echo "SKIP: claude CLI not on PATH"
        exit 0
    fi
}

export -f run_claude
export -f run_claude_stream
export -f assert_contains
export -f assert_not_contains
export -f assert_order
export -f create_test_project
export -f cleanup_test_project
export -f require_claude_cli
