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
# associative arrays, no `wait -n` (bash 4.3+).
#
# Resolution is CACHED: the winning argv is persisted (atomic tmp + mv) to
# `${CHAMELEON_PLUGIN_DATA:-$HOME/.local/share/chameleon}/interp.cache` — line 1
# is the mcp_dir the argv was resolved for (the installed plugin path embeds the
# version, so the cache is version-keyed for free), the following lines are the
# argv tokens. A hit requires the recorded mcp_dir to match AND the leading argv
# binary to still resolve; every other state (corrupt, unreadable, mismatched,
# stale) is a miss that re-runs the ladder and rewrites the cache. The hit path
# is pure shell builtins — no fork — so a warm per-edit resolve costs
# microseconds. `CHAMELEON_INTERP_CACHE=0` bypasses read AND write.
#
# `CHAMELEON_RESOLVE_FAST=1` (set by the per-edit/per-turn hooks) drops the uv
# probe cap 30s -> 5s and, where no timeout(1)/gtimeout(1) exists, bounds the
# probe with a background poll loop instead of running it uncapped. SessionStart
# and doctor stay on the generous path (a cold uv materialization may take that
# long) and warm the cache for the fast hooks.

# Path of the interpreter cache, or empty when caching is unavailable. With
# neither CHAMELEON_PLUGIN_DATA nor HOME set, the only fallback would be a
# world-writable temp dir, where a planted cache could hand every hook an
# attacker-controlled "interpreter" argv — so that degenerate environment gets
# no cache at all (the ladder still resolves normally).
_cham_interp_cache_file() {
    if [ -n "${CHAMELEON_PLUGIN_DATA:-}" ]; then
        printf '%s/interp.cache' "${CHAMELEON_PLUGIN_DATA}"
    elif [ -n "${HOME:-}" ]; then
        printf '%s/.local/share/chameleon/interp.cache' "${HOME}"
    fi
}

# Fill CHAMELEON_PY from the cache. Returns 0 only on a validated hit: the
# recorded mcp_dir matches and the argv's leading binary still resolves
# ([ -x ] for a path, command -v for a bare name) — a deleted venv or an
# uninstalled uv turns the entry stale and the ladder repairs it. Deliberately
# NO version re-probe on a hit (that fork is the cost the cache removes): a
# binary downgraded in place below 3.11 is caught downstream by the hook's
# import failure, which fail-opens and logs, and the next plugin version
# re-keys the cache anyway. Everything else (kill switch, no cache path,
# absent/unreadable/non-regular file, empty or mismatched content) is a miss,
# never an error.
_cham_cache_read() {
    local mcp_dir="$1" cache line first n=0
    if [ "${CHAMELEON_INTERP_CACHE:-1}" = "0" ]; then return 1; fi
    # An empty key must never hit (a corrupt cache with an empty first line
    # would otherwise match a caller that passed no mcp_dir).
    if [ -z "$mcp_dir" ]; then return 1; fi
    cache="$(_cham_interp_cache_file)"
    # -f rejects FIFOs/devices (opening a reader-less FIFO would block forever).
    if [ -z "$cache" ] || [ ! -f "$cache" ] || [ ! -r "$cache" ]; then return 1; fi
    CHAMELEON_PY=()
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$n" -eq 0 ]; then
            if [ "$line" != "$mcp_dir" ]; then return 1; fi
        elif [ -n "$line" ]; then
            CHAMELEON_PY+=("$line")
        fi
        n=$((n + 1))
    done <"$cache"
    if [ "${#CHAMELEON_PY[@]}" -eq 0 ]; then return 1; fi
    first="${CHAMELEON_PY[0]}"
    if [ -x "$first" ] || command -v "$first" >/dev/null 2>&1; then
        return 0
    fi
    CHAMELEON_PY=()
    return 1
}

# Persist the resolved argv (write tmp + rename, atomic on one filesystem) so
# the next hook invocation skips the ladder. Best-effort: every failure is
# swallowed — caching is an optimization and must never affect resolution.
_cham_cache_write() {
    local mcp_dir="$1" cache tmp
    if [ "${CHAMELEON_INTERP_CACHE:-1}" = "0" ]; then return 0; fi
    if [ -z "$mcp_dir" ] || [ "${#CHAMELEON_PY[@]}" -eq 0 ]; then return 0; fi
    cache="$(_cham_interp_cache_file)"
    if [ -z "$cache" ]; then return 0; fi
    mkdir -p "${cache%/*}" 2>/dev/null || return 0
    tmp="${cache}.tmp.$$"
    { printf '%s\n' "$mcp_dir" "${CHAMELEON_PY[@]}"; } >"$tmp" 2>/dev/null \
        || { rm -f "$tmp" 2>/dev/null; return 0; }
    mv -f "$tmp" "$cache" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    return 0
}

# Run a probe bounded to ~5s on hosts with no usable timeout(1): background it,
# poll `kill -0` every 0.2s, hard-kill after 25 iterations. Returns the probe's
# exit status, or 124 on kill (mirrors timeout(1)). POSIX bash 3.2 — no
# `wait -n`; the waits' stderr is discarded so bash's job-kill notice never
# leaks into a hook's output.
_cham_probe_bounded() {
    local pid i=0 rc=0
    "$@" >/dev/null 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$i" -ge 25 ]; then
            kill -9 "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
            return 124
        fi
        # `|| true` keeps a sourced `set -e` caller alive if sleep is absent;
        # the loop then degrades to killing the probe early, still fail-open.
        sleep 0.2 2>/dev/null || true
        i=$((i + 1))
    done
    wait "$pid" 2>/dev/null || rc=$?
    return "$rc"
}

# True iff the given interpreter reports Python >=3.11.
#
# This rung executes an external binary, so it can hang on a pathological
# interpreter. Bound it with timeout/gtimeout when available — even a generous
# cap turns a hung probe into a clean "not >=3.11" instead of stalling every
# hook. Where no timeout binary exists (minimal PATHs, and Git Bash / MSYS,
# whose `timeout` is Windows' timeout.exe): fast mode bounds it with the poll
# loop; the generous path degrades to uncapped (in-process work is trivial).
_cham_py_ge_311() {
    local t="" probe='import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)'
    case "$(uname -s 2>/dev/null)" in
        MINGW* | MSYS* | CYGWIN*) t="" ;;
        *) t="$(command -v timeout || command -v gtimeout || true)" ;;
    esac
    if [ -n "$t" ]; then
        "$t" 5 "$1" -c "$probe" >/dev/null 2>&1
    elif [ "${CHAMELEON_RESOLVE_FAST:-}" = "1" ]; then
        _cham_probe_bounded "$1" -c "$probe"
    else
        "$1" -c "$probe" >/dev/null 2>&1
    fi
}

# Probe the uv rung the way it will actually be invoked: the full
# `uv run --project <dir> python` argv, not a single interpreter path. A broken
# or locked lockfile, an offline first-materialization, or a shadowing
# non-chameleon `uv` then fails here and the resolver falls through to rung 4 /
# the degraded banner, instead of accepting a uv that fails at every later hook
# call (silent enforcement-off for the session). `uv run` may materialize or
# download an interpreter on a cold cache, so the generous path needs a far
# bigger cap than the 5s single-interpreter probe (mirrors doctor's 30s uv
# probe); a fast non-zero exit (the broken-uv cases) returns immediately well
# under it. Fast mode (the per-edit hooks) cannot afford 30s: the cap drops to
# 5s, and where no timeout binary exists the probe is bounded by the poll loop
# instead of running uncapped — a cold uv materialization then fails fast and
# per-edit resolution degrades for the turn, until SessionStart or doctor pays
# the generous probe and warms the cache.
_cham_uv_ge_311() {
    local uv="$1" mcp_dir="$2" t="" cap=30
    local probe='import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)'
    if [ "${CHAMELEON_RESOLVE_FAST:-}" = "1" ]; then cap=5; fi
    case "$(uname -s 2>/dev/null)" in
        MINGW* | MSYS* | CYGWIN*) t="" ;;
        *) t="$(command -v timeout || command -v gtimeout || true)" ;;
    esac
    if [ -n "$t" ]; then
        "$t" "$cap" "$uv" run --project "$mcp_dir" python -c "$probe" >/dev/null 2>&1
    elif [ "${CHAMELEON_RESOLVE_FAST:-}" = "1" ]; then
        _cham_probe_bounded "$uv" run --project "$mcp_dir" python -c "$probe"
    else
        "$uv" run --project "$mcp_dir" python -c "$probe" >/dev/null 2>&1
    fi
}

# The validated ladder, cache-blind. Returns 0 on success (CHAMELEON_PY set to
# the interpreter argv), 1 when no viable interpreter exists (array left empty).
_cham_resolve_python_uncached() {
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

# Public entry point (same contract as always: sets CHAMELEON_PY, returns 0/1).
# Serve a validated cache hit without running the ladder; on a miss run the
# full ladder and persist the winner. A failed ladder writes nothing, so a
# stale-but-keyed entry stays put for the next attempt to re-validate.
_cham_resolve_python() {
    local mcp_dir="$1"
    if _cham_cache_read "$mcp_dir"; then
        return 0
    fi
    CHAMELEON_PY=()
    if _cham_resolve_python_uncached "$mcp_dir"; then
        _cham_cache_write "$mcp_dir"
        return 0
    fi
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
