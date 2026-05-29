#!/usr/bin/env bash

set -euo pipefail

INSTALLED_JSON="${HOME}/.claude/plugins/installed_plugins.json"
CACHE_DIR="${HOME}/.claude/plugins/cache/chameleon/chameleon"

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
for dir in "${CACHE_DIR}"/*/; do
    [[ -d "${dir}" ]] || continue
    version=$(basename "${dir}")
    if [[ "${version}" == "${current_version}" ]]; then
        echo "  keep ${version} (current)"
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

if (( removed == 0 )); then
    echo "chameleon: no stale versions to prune."
elif (( apply == 0 )); then
    echo
    echo "chameleon: dry run — pass --apply to remove ${removed} stale versions."
fi
