#!/usr/bin/env bash
# Deploy the dev tree to the copy of the plugin that Claude Code actually runs,
# and assert the two agree. Dev-only tooling for the full-matrix test campaign.
#
# A fix authored in the dev tree reaches nothing until it travels three hops:
#
#   dev tree  ->  marketplace clone  ->  version-keyed cache  ->  hooks + MCP
#
# The cache is keyed by the version in plugin/.claude-plugin/plugin.json, so a
# fix without a version bump lands in a directory nothing loads. Re-running a
# test cell against that stale copy reports a false green -- which is the exact
# failure this script exists to make impossible.
#
# Usage:
#   qa-deploy.sh deploy   propagate dev HEAD to the marketplace clone and
#                         materialize the version-keyed cache dir
#   qa-deploy.sh verify   assert the running copy matches the dev tree; non-zero
#                         exit means no cell may be marked green
#
# Hooks re-resolve their interpreter per invocation, so a deployed hook-path fix
# takes effect on the next hook fire. The MCP server is launched once per
# session from the cache dir, so MCP-surface changes additionally need the
# session/MCP connection restarted -- `verify` reports when that is the case.

set -euo pipefail

DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKET="${HOME}/.claude/plugins/marketplaces/chameleon"
CACHE_BASE="${HOME}/.claude/plugins/cache/chameleon/chameleon"

version() {
    # The single source of truth the cache directory is keyed by.
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
        "${1}/.claude-plugin/plugin.json"
}

# Files whose content decides whether the running plugin is the dev plugin.
# Compared as a tree rather than by git SHA: the cache is a plain copy with no
# git metadata of its own, so a SHA has nothing to compare against there.
tree_differs() {
    ! diff -rq --exclude='__pycache__' --exclude='.venv' --exclude='node_modules' \
        --exclude='.in_use' --exclude='.pytest_cache' --exclude='.ruff_cache' \
        --exclude='.claude' \
        "$1" "$2" >/dev/null 2>&1
}

cmd_deploy() {
    local ver branch cache_dir
    ver="$(version "${DEV_ROOT}/plugin")"
    branch="$(git -C "${DEV_ROOT}" rev-parse --abbrev-ref HEAD)"
    cache_dir="${CACHE_BASE}/${ver}"

    if [ -n "$(git -C "${DEV_ROOT}" status --porcelain)" ]; then
        echo "REFUSING: dev tree has uncommitted changes. Commit first -- the" >&2
        echo "marketplace hop propagates commits, so anything uncommitted would" >&2
        echo "be silently left behind and the cache would not match the tree." >&2
        return 1
    fi

    echo "==> deploying ${branch} @ $(git -C "${DEV_ROOT}" rev-parse --short HEAD) (v${ver})"

    # Hop 2: the marketplace clone shares an origin with the dev tree, so it can
    # fetch straight from the local path -- no network, no push to a shared remote.
    git -C "${MARKET}" fetch --quiet "${DEV_ROOT}" "${branch}"
    git -C "${MARKET}" reset --quiet --hard FETCH_HEAD
    echo "    marketplace clone -> $(git -C "${MARKET}" rev-parse --short HEAD)"

    # Hop 3: materialize exactly what Claude Code materializes -- a copy of
    # plugin/. Preserve .in_use, which Claude Code owns, not us.
    mkdir -p "${cache_dir}"
    rsync -a --delete \
        --exclude='.in_use' --exclude='__pycache__' \
        --exclude='.venv' --exclude='node_modules' \
        --exclude='.pytest_cache' --exclude='.ruff_cache' \
        "${MARKET}/plugin/" "${cache_dir}/"
    echo "    version-keyed cache -> ${cache_dir}"

    cmd_verify
}

cmd_verify() {
    local ver cache_dir rc=0
    ver="$(version "${DEV_ROOT}/plugin")"
    cache_dir="${CACHE_BASE}/${ver}"

    echo "==> verifying the running plugin is the dev plugin (v${ver})"

    if [ ! -d "${cache_dir}" ]; then
        echo "    FAIL: no cache dir for v${ver} -- nothing loads this version yet." >&2
        echo "          Run 'qa-deploy.sh deploy'." >&2
        return 1
    fi

    if tree_differs "${DEV_ROOT}/plugin" "${cache_dir}"; then
        echo "    FAIL: running copy differs from the dev tree. Offending paths:" >&2
        diff -rq --exclude='__pycache__' --exclude='.venv' --exclude='node_modules' \
            --exclude='.in_use' --exclude='.pytest_cache' --exclude='.ruff_cache' \
            --exclude='.claude' \
            "${DEV_ROOT}/plugin" "${cache_dir}" 2>&1 | head -20 >&2
        rc=1
    else
        echo "    OK: hooks and skills run the dev tree byte-for-byte"
    fi

    # The MCP server is a long-lived process started from the cache dir at
    # session start; a fresh copy on disk does not restart it.
    if [ -e "${cache_dir}/.in_use" ]; then
        echo "    NOTE: this version is loaded by a live session. Hook-path fixes are"
        echo "          live now; MCP-tool-surface fixes need the MCP server restarted."
    fi

    return "${rc}"
}

case "${1:-verify}" in
    deploy) cmd_deploy ;;
    verify) cmd_verify ;;
    *) echo "usage: $0 {deploy|verify}" >&2; exit 2 ;;
esac
