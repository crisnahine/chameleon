# shellcheck shell=bash
# Single source of truth for the Python interpreter every chameleon hook runs.
#
# The hook scripts RUN this as a subprocess and read the printed argv back, one
# token per line, into their CHAMELEON_PY array:
#     bash _resolve-python.sh <mcp_dir>   # prints argv tokens, one per line
# (running it as a subprocess, not sourcing it, means a corrupt or truncated copy
# degrades the hook cleanly instead of a parse error aborting it). The same
# command is what /chameleon-doctor reports. Sourcing still works as a secondary
# mode — it defines _cham_resolve_python and sets CHAMELEON_PY without printing.
#
# Why a validated ladder instead of a blind `python3` fallback: macOS ships
# /usr/bin/python3 = 3.9.x, below chameleon's >=3.11 floor (mcp/pyproject.toml)
# and without its third-party deps. A rung that trusts whatever `python3`
# resolves to lands every hook on an interpreter that fail-opens — silently
# disabling enforcement for the whole session. Each rung below is either >=3.11
# by construction (the bundled venv, version-named binaries) or version-probed
# before it wins; `uv run` (the same resolver the MCP server uses via uvx)
# supplies a dep-complete >=3.11 interpreter when no system one is on PATH.
# CHAMELEON_PY is left empty and a non-zero status returned only when nothing
# viable exists, so the caller can surface a degraded banner rather than running
# a doomed interpreter.
#
# Kept POSIX-bash-3.2 compatible (macOS system bash): no mapfile/readarray, no
# associative arrays.

# True iff the given interpreter reports Python >=3.11.
#
# This is the only rung that executes an external binary (the others are shell
# builtins: command -v, [ -x ]), so it is the only place resolution can hang on a
# pathological interpreter. Bound it with timeout/gtimeout when available — even a
# generous cap turns a hung probe into a clean "not >=3.11" instead of stalling
# every hook. Degrades to uncapped when no timeout binary exists (in-process work
# is trivial) and on Git Bash / MSYS, where `timeout` is Windows' timeout.exe.
_cham_py_ge_311() {
    local t=""
    case "$(uname -s 2>/dev/null)" in
        MINGW* | MSYS* | CYGWIN*) t="" ;;
        *) t="$(command -v timeout || command -v gtimeout || true)" ;;
    esac
    ${t:+"$t" 5} "$1" \
        -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

# Probe the uv rung the way it will actually be invoked: the full
# `uv run --project <dir> python` argv, not a single interpreter path. A broken
# or locked lockfile, an offline first-materialization, or a shadowing
# non-chameleon `uv` then fails here and the resolver falls through to rung 4 /
# the degraded banner, instead of accepting a uv that fails at every later hook
# call (silent enforcement-off for the session). `uv run` may materialize or
# download an interpreter on a cold cache, so this needs a far more generous cap
# than the 5s single-interpreter probe (mirrors doctor's 30s uv probe); a fast
# non-zero exit (the broken-uv cases) returns immediately well under it.
_cham_uv_ge_311() {
    local uv="$1" mcp_dir="$2" t=""
    case "$(uname -s 2>/dev/null)" in
        MINGW* | MSYS* | CYGWIN*) t="" ;;
        *) t="$(command -v timeout || command -v gtimeout || true)" ;;
    esac
    ${t:+"$t" 30} "$uv" run --project "$mcp_dir" python \
        -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

# Resolve into the CHAMELEON_PY array. Returns 0 on success (array set to the
# interpreter argv), 1 when no viable interpreter exists (array left empty).
_cham_resolve_python() {
    local mcp_dir="$1"
    local cand uv name

    CHAMELEON_PY=()

    # 1. Bundled dev venv. Trusted as >=3.11 (chameleon builds it via uv); the
    #    `-x` guard skips the common installed case where it is absent.
    for cand in "${mcp_dir}/.venv/bin/python" "${mcp_dir}/.venv/Scripts/python.exe"; do
        if [ -x "$cand" ]; then
            CHAMELEON_PY=("$cand")
            return 0
        fi
    done

    # 2. Version-named system interpreters: >=3.11 by their own name, no probe.
    for name in python3.13 python3.12 python3.11; do
        cand="$(command -v "$name" 2>/dev/null || true)"
        if [ -n "$cand" ]; then
            CHAMELEON_PY=("$cand")
            return 0
        fi
    done

    # 3. uv: the dep-complete resolver the MCP server already relies on. Covers
    #    the box whose only python3 is < 3.11. uv honors requires-python, so the
    #    interpreter it materializes is >=3.11 with chameleon's deps available.
    uv="$(command -v uv 2>/dev/null || true)"
    if [ -n "$uv" ] && [ -d "$mcp_dir" ] && _cham_uv_ge_311 "$uv" "$mcp_dir"; then
        CHAMELEON_PY=("$uv" run --project "$mcp_dir" python)
        return 0
    fi

    # 4. Unversioned python3 / python ONLY when a probe confirms >=3.11 — never
    #    blindly, since this is exactly where /usr/bin/python3 = 3.9.x sneaks in.
    for name in python3 python; do
        cand="$(command -v "$name" 2>/dev/null || true)"
        if [ -n "$cand" ] && _cham_py_ge_311 "$cand"; then
            CHAMELEON_PY=("$cand")
            return 0
        fi
    done

    return 1
}

# Direct execution (doctor probe): print the resolved argv, one token per line,
# so a non-bash caller can read it back. Exit 1 when nothing resolves.
if [ "${BASH_SOURCE[0]:-$0}" = "${0}" ]; then
    if _cham_resolve_python "${1:-${MCP_DIR:-}}"; then
        printf '%s\n' "${CHAMELEON_PY[@]}"
        exit 0
    fi
    exit 1
fi
