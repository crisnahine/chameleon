#!/usr/bin/env bash
# One-command setup for chameleon.
#
# Verifies every prerequisite (with versions and exact per-OS install hints for
# anything missing) and then warms the Python + Node environments so the first
# Claude Code session is instant instead of paying the build cost mid-edit.
#
# Usage:
#   scripts/setup.sh           verify prerequisites, then warm dependencies
#   scripts/setup.sh --check   verify prerequisites only (no install / no warm)
#   scripts/setup.sh --dev     warm with dev/test extras too (for contributors)
#
# The default warm installs RUNTIME deps only (what the MCP server and hooks
# actually need). Contributors who also run the test suite / linters should use
# --dev, which adds pytest and ruff via `uv sync --extra dev`.
#
# Exit status:
#   0  every REQUIRED tool is present (and, without --check, warm-up succeeded)
#   1  a required tool is missing, or warm-up failed
#
# Ruby (only needed for Ruby repos) and timeout(1) are OPTIONAL: when missing
# they warn but never fail the run. Required tools are uv, Node 20+, and npm.
#
# Kept POSIX-bash-3.2 compatible (macOS system bash): no mapfile/readarray, no
# associative arrays.

set -euo pipefail

mode="warm"
case "${1:-}" in
    --check) mode="check" ;;
    --dev) mode="dev" ;;
    "") : ;;
    -h | --help)
        sed -n '2,26p' "$0"
        exit 0
        ;;
    *)
        printf 'setup.sh: unknown argument %s (try --check, --dev, or --help)\n' "$1" >&2
        exit 2
        ;;
esac

# Resolve the repo root from this script's own location, so it works whether run
# from the repo root, from scripts/, or out of the installed plugin cache.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

# Colors only when writing to a terminal that supports them.
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    C_OK="$(tput setaf 2)"
    C_WARN="$(tput setaf 3)"
    C_ERR="$(tput setaf 1)"
    C_DIM="$(tput dim)"
    C_RST="$(tput sgr0)"
else
    C_OK="" C_WARN="" C_ERR="" C_DIM="" C_RST=""
fi

ok() { printf '%s  ok %s %s\n' "$C_OK" "$C_RST" "$1"; }
warn() { printf '%s warn%s %s\n' "$C_WARN" "$C_RST" "$1"; }
err() { printf '%s fail%s %s\n' "$C_ERR" "$C_RST" "$1"; }
hint() { printf '       %s%s%s\n' "$C_DIM" "$1" "$C_RST"; }

case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin) platform=macos ;;
    Linux) platform=linux ;;
    MINGW* | MSYS* | CYGWIN*) platform=windows ;;
    *) platform=unknown ;;
esac

required_missing=0

# Print an install hint for a tool, tailored to the detected platform.
install_hint() {
    case "$1:$platform" in
        uv:macos) hint "install: brew install uv   (or: curl -LsSf https://astral.sh/uv/install.sh | sh)" ;;
        uv:linux) hint "install: curl -LsSf https://astral.sh/uv/install.sh | sh   then open a new terminal" ;;
        uv:windows) hint 'install: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' ;;
        uv:*) hint "install uv: https://docs.astral.sh/uv/getting-started/installation/" ;;
        node:macos) hint "install: brew install node   (gives current Node, well past 20)" ;;
        node:linux) hint "install: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs" ;;
        node:windows) hint "install: winget install OpenJS.NodeJS.LTS   (or download from https://nodejs.org)" ;;
        node:*) hint "install Node 20+: https://nodejs.org" ;;
        ruby:macos) hint "install (Ruby repos only): brew install ruby   then add it to PATH as brew prints" ;;
        ruby:linux) hint "install (Ruby repos only): sudo apt-get install -y ruby-full   then: gem install prism (if Ruby < 3.3)" ;;
        ruby:windows) hint "install (Ruby repos only): RubyInstaller 3.3+ from https://rubyinstaller.org (bundles prism)" ;;
        ruby:*) hint "install Ruby 3.0+ with the prism gem (Ruby repos only)" ;;
        timeout:macos) hint "optional: brew install coreutils   (provides gtimeout; caps stuck hooks)" ;;
        timeout:*) hint "optional: install coreutils for timeout(1) (caps stuck hooks)" ;;
    esac
}

printf '%schameleon setup%s  (platform: %s, repo: %s)\n\n' "$C_DIM" "$C_RST" "$platform" "$repo_root"

# --- Required: uv -----------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    ok "uv        $(uv --version 2>/dev/null | head -1)"
else
    err "uv        not found on PATH"
    install_hint uv
    required_missing=$((required_missing + 1))
fi

# --- Required: Node 20+ -----------------------------------------------------
if command -v node >/dev/null 2>&1; then
    node_ver="$(node --version 2>/dev/null)"
    node_ver_num="${node_ver#v}"
    node_major="${node_ver_num%%.*}"
    if [ -n "$node_major" ] && [ "$node_major" -ge 20 ] 2>/dev/null; then
        ok "node      $node_ver"
    else
        err "node      $node_ver  (need 20 or newer)"
        install_hint node
        required_missing=$((required_missing + 1))
    fi
else
    err "node      not found on PATH"
    install_hint node
    required_missing=$((required_missing + 1))
fi

# --- Required: npm (ships with Node) ----------------------------------------
if command -v npm >/dev/null 2>&1; then
    ok "npm       $(npm --version 2>/dev/null)"
else
    err "npm       not found on PATH (ships with Node)"
    install_hint node
    required_missing=$((required_missing + 1))
fi

# --- Optional: Ruby 3.0+ with prism (only for Ruby repos) -------------------
if command -v ruby >/dev/null 2>&1; then
    ruby_ver="$(ruby -e 'print RUBY_VERSION' 2>/dev/null)"
    if ruby -e 'exit(RUBY_VERSION.split(".")[0].to_i >= 3 ? 0 : 1)' 2>/dev/null; then
        if ruby -e "require 'prism'" 2>/dev/null; then
            ok "ruby      $ruby_ver  (prism available)"
        else
            warn "ruby      $ruby_ver  but the prism gem is missing"
            hint "fix (Ruby repos only): gem install prism"
        fi
    else
        warn "ruby      $ruby_ver  (need 3.0+ for Ruby repos; TS/Python unaffected)"
        install_hint ruby
    fi
else
    warn "ruby      not found  (only needed to edit Ruby repos; TS/Python work without it)"
    install_hint ruby
fi

# --- Optional: timeout(1) / gtimeout ---------------------------------------
if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
    ok "timeout   present  (hooks get an external wall-clock cap)"
else
    warn "timeout   not found  (hooks still run; they just lose the external wall-clock cap)"
    install_hint timeout
fi

printf '\n'

if [ "$required_missing" -gt 0 ]; then
    err "$required_missing required tool(s) missing. Install the item(s) above, open a NEW terminal, then re-run this script."
    exit 1
fi

if [ "$mode" = "check" ]; then
    ok "all required prerequisites present. (--check: skipping the dependency warm-up.)"
    exit 0
fi

# --- Warm the environments so the first session pays no build cost ----------
printf '%swarming dependencies (one-time; later runs are fast)...%s\n' "$C_DIM" "$C_RST"

warm_failed=0

if [ "$mode" = "dev" ]; then
    printf '  - Python env with dev/test extras (uv sync --extra dev in mcp/)...\n'
    if (cd "$repo_root/mcp" && uv sync --extra dev); then
        ok "Python environment ready (with pytest + ruff)"
    else
        err "uv sync --extra dev failed in $repo_root/mcp"
        warm_failed=$((warm_failed + 1))
    fi
else
    printf '  - Python env (uv sync in mcp/)...\n'
    if (cd "$repo_root/mcp" && uv sync); then
        ok "Python environment ready"
    else
        err "uv sync failed in $repo_root/mcp"
        warm_failed=$((warm_failed + 1))
    fi
fi

printf '  - Node TypeScript reader (npm install in mcp/)...\n'
if (cd "$repo_root/mcp" && npm install); then
    ok "Node TypeScript reader ready"
else
    err "npm install failed in $repo_root/mcp"
    warm_failed=$((warm_failed + 1))
fi

printf '\n'
if [ "$warm_failed" -gt 0 ]; then
    err "setup finished with $warm_failed warm-up failure(s). chameleon will still build these on first use, just slower."
    exit 1
fi

ok "chameleon is ready. Open a repo in Claude Code and run /chameleon-init, then /chameleon-trust."
exit 0
