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
REGISTRY="${HOME}/.claude/plugins/installed_plugins.json"

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
        --exclude='.claude' --exclude='.orphaned_at' --exclude='.in_use' \
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

    # Hop 4: the installed-plugin registry. Materializing the cache dir is not
    # enough -- Claude Code resolves which copy to load from this file, so a
    # session started after a deploy that skipped it still loads the PREVIOUS
    # version. That is invisible to a content diff of the cache dir (which is
    # byte-perfect), so a fix could be verified green against a plugin that was
    # never actually running.
    python3 - "${REGISTRY}" "${ver}" "${cache_dir}" <<'PY'
import json, shutil, sys
from pathlib import Path

registry, version, install_path = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
data = json.loads(registry.read_text())
records = data.get("plugins", {}).get("chameleon@chameleon") or []
if not records:
    print("    WARNING: chameleon not in the installed-plugin registry; skipping")
    raise SystemExit
shutil.copy2(registry, registry.with_suffix(".json.bak.qa-deploy"))
before = records[0].get("version")
records[0]["version"] = version
records[0]["installPath"] = install_path
registry.write_text(json.dumps(data, indent=2))
print(f"    installed-plugin registry -> {version} (was {before})")
PY

    cmd_verify
}

cmd_verify() {
    local ver cache_dir rc=0
    ver="$(version "${DEV_ROOT}/plugin")"
    cache_dir="${CACHE_BASE}/${ver}"

    echo "==> verifying the running plugin is the dev plugin (v${ver})"

    # The campaign folds agent evidence (which quotes absolute paths) into the
    # matrix ledger and TESTING.md, so a developer home path can sneak into a
    # tracked file and redden CI's no-personal-paths guard. Run the same guard
    # here so a fold regression is caught before a push, not after.
    if [ -x "${DEV_ROOT}/scripts/check-no-personal-paths.sh" ]; then
        if ! bash "${DEV_ROOT}/scripts/check-no-personal-paths.sh" >/dev/null 2>&1; then
            echo "    FAIL: a tracked file contains a developer home path (CI would redden)." >&2
            echo "          Run scripts/check-no-personal-paths.sh to see which." >&2
            rc=1
        else
            echo "    OK: no personal paths in tracked files"
        fi
    fi

    if [ ! -d "${cache_dir}" ]; then
        echo "    FAIL: no cache dir for v${ver} -- nothing loads this version yet." >&2
        echo "          Run 'qa-deploy.sh deploy'." >&2
        return 1
    fi

    if tree_differs "${DEV_ROOT}/plugin" "${cache_dir}"; then
        echo "    FAIL: running copy differs from the dev tree. Offending paths:" >&2
        diff -rq --exclude='__pycache__' --exclude='.venv' --exclude='node_modules' \
            --exclude='.in_use' --exclude='.pytest_cache' --exclude='.ruff_cache' \
            --exclude='.claude' --exclude='.orphaned_at' --exclude='.in_use' \
            "${DEV_ROOT}/plugin" "${cache_dir}" 2>&1 | head -20 >&2
        rc=1
    else
        echo "    OK: hooks and skills run the dev tree byte-for-byte"
    fi

    # A byte-perfect cache dir proves nothing if no session loads it. The
    # registry is what Claude Code reads to pick a version, so a stale pin here
    # means every new session runs an older plugin while this script reports OK.
    local pinned
    pinned="$(python3 -c '
import json, sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
    print(data["plugins"]["chameleon@chameleon"][0].get("version", "?"))
except Exception:
    print("unreadable")
' "${REGISTRY}" 2>/dev/null)"
    if [ "${pinned}" = "${ver}" ]; then
        echo "    OK: new sessions load v${ver} (installed-plugin registry agrees)"
    else
        echo "    FAIL: registry pins v${pinned}, not v${ver}. A new session would load" >&2
        echo "          the OLD plugin, so any cell re-run against it is a false green." >&2
        echo "          Run 'qa-deploy.sh deploy'." >&2
        rc=1
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
