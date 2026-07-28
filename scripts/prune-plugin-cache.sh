#!/usr/bin/env bash

set -euo pipefail

# Both paths are overridable so the liveness logic below can be exercised
# against a fixture tree instead of the developer's real plugin cache.
INSTALLED_JSON="${CHAMELEON_INSTALLED_PLUGINS_JSON:-${HOME}/.claude/plugins/installed_plugins.json}"
CACHE_DIR="${CHAMELEON_PLUGIN_CACHE_DIR:-${HOME}/.claude/plugins/cache/chameleon/chameleon}"

# `ps` is how a recorded session is checked for liveness. Without it nothing
# can be proven dead, so the script keeps every marked build (the pre-liveness
# behaviour) rather than guessing.
HAVE_PS=0
if command -v ps >/dev/null 2>&1; then
    HAVE_PS=1
fi

# Is any session recorded in this .in_use marker directory still running?
#
# Claude Code owns these markers -- chameleon never writes them. It drops one
# file per session, named after the session's pid and holding
# {"pid":N,"procStart":"..."}, but it does not remove them when a session ends.
# So their mere EXISTENCE means only "some session once loaded this build",
# which on a long-lived machine is every build ever installed -- the reason a
# cache reaches gigabytes that this script then refuses to reclaim.
#
# Every uncertain answer is "live". Reclaiming disk is never worth deleting the
# plugin a running session is executing from.
_marker_has_live_session() {
    local marker="$1" f pid
    if [[ ! -e "${marker}" ]]; then
        return 1
    fi
    # Anything but the directory-of-pid-files shape is one this check does not
    # understand, as is a directory it cannot list.
    if (( HAVE_PS == 0 )) || [[ ! -d "${marker}" ]]; then
        return 0
    fi
    if [[ ! -r "${marker}" || ! -x "${marker}" ]]; then
        return 0
    fi
    local -a files=()
    while IFS= read -r -d '' f; do
        files+=("${f}")
    done < <(find "${marker}" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
    # No session recorded at all: nothing can be running from it.
    if (( ${#files[@]} == 0 )); then
        return 1
    fi
    for f in "${files[@]}"; do
        pid="$(basename "${f}")"
        if [[ ! "${pid}" =~ ^[1-9][0-9]*$ ]]; then
            return 0
        fi
        # Existence is the whole test, deliberately. Two sharper signals were
        # considered and both can delete a build a live session is running:
        #
        #   - the recorded procStart, compared to `ps -o lstart=`. These are not
        #     the same clock: the marker stores UTC while `ps` prints local
        #     time, so on any machine east or west of UTC every LIVE session
        #     mismatches and its plugin gets reclaimed out from under it.
        #   - the program name from `ps -o comm=`, which truncates (15 chars on
        #     Linux, column-width on macOS), so a real claude process can fail
        #     a name match and be read as dead.
        #
        # Existence errs the other way: a recycled pid inherited by an
        # unrelated process pins its build until the next reboot. That costs
        # disk, which is the trade this script's own header already makes.
        if ps -p "${pid}" >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

if [[ ! -f "${INSTALLED_JSON}" ]]; then
    echo "chameleon: installed_plugins.json not found at ${INSTALLED_JSON}" >&2
    exit 1
fi
if [[ ! -d "${CACHE_DIR}" ]]; then
    echo "chameleon: cache dir not found at ${CACHE_DIR} (nothing to prune)" >&2
    exit 0
fi

current_version=$(
    python3 -c "
import json, sys
with open('${INSTALLED_JSON}') as fh:
    data = json.load(fh)
plugins = data.get('plugins', data)
entry = plugins.get('chameleon@chameleon')
if isinstance(entry, list) and entry:
    print(entry[0].get('version', ''))
else:
    print('', end='')
" 2>/dev/null
)

if [[ -z "${current_version}" ]]; then
    echo "chameleon: could not read current version from installed_plugins.json" >&2
    exit 1
fi

echo "chameleon: current installed version is v${current_version}"

apply=0
if [[ "${1:-}" == "--apply" ]]; then
    apply=1
fi

removed=0
held=0
for dir in "${CACHE_DIR}"/*/; do
    [[ -d "${dir}" ]] || continue
    version=$(basename "${dir}")
    if [[ "${version}" == "${current_version}" ]]; then
        echo "  keep ${version} (current)"
        continue
    fi
    # Claude Code marks the build a session loaded with .in_use, and a session
    # keeps running the version it STARTED on -- installing a new one re-pins
    # the registry for future sessions without moving the live one. Deleting by
    # registry version alone therefore rm -rf's the plugin a running session is
    # executing from, taking its hooks and MCP server out mid-turn.
    #
    # But the marker is never cleaned up when a session dies, so testing it for
    # bare existence pins every build that was ever loaded -- which after a few
    # weeks is every build ever installed, and the cache this script exists to
    # reclaim never shrinks. Ask whether a recorded session is still RUNNING.
    if _marker_has_live_session "${dir}.in_use"; then
        echo "  keep ${version} (.in_use -- a session is still running it)"
        held=$((held + 1))
        continue
    fi
    if (( apply == 1 )); then
        echo "  prune ${version} (deleting)"
        rm -rf "${dir}"
    else
        echo "  prune ${version} (dry run; pass --apply to delete)"
    fi
    removed=$((removed + 1))
done

if (( held > 0 )); then
    echo
    echo "chameleon: kept ${held} version(s) with a live session. A pid the OS has"
    echo "           since reused for an unrelated process also counts as live, so"
    echo "           a version held across a reboot is safe to remove by hand."
fi

if (( removed == 0 )); then
    echo "chameleon: no stale versions to prune."
elif (( apply == 0 )); then
    echo
    echo "chameleon: dry run — pass --apply to remove ${removed} stale versions."
fi
