#!/usr/bin/env bash
# Generates mcp/typescript-checksums.json: SHA-256 manifest of vendored TS files.
# Run quarterly after bumping mcp/node_modules/typescript or whenever TS is updated.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS_DIR="${REPO_ROOT}/mcp/node_modules/typescript"
OUT="${REPO_ROOT}/mcp/typescript-checksums.json"

if [ ! -d "${TS_DIR}" ]; then
  echo "ERROR: ${TS_DIR} not found; run 'cd mcp && npm install typescript' first" >&2
  exit 1
fi

# Walk every file under TS_DIR, compute SHA-256, emit as JSON object keyed by rel path.
python3 - "${TS_DIR}" "${OUT}" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

ts_dir = Path(sys.argv[1])
out_path = Path(sys.argv[2])

checksums: dict[str, str] = {}
for root, _, files in os.walk(ts_dir):
    for fname in files:
        path = Path(root) / fname
        rel = path.relative_to(ts_dir).as_posix()
        h = hashlib.sha256()
        h.update(path.read_bytes())
        checksums[rel] = h.hexdigest()

out_path.write_text(
    json.dumps({"version": 1, "generator": "scripts/generate-typescript-checksums.sh", "files": checksums}, indent=2, sort_keys=True),
    encoding="utf-8",
)
print(f"wrote {len(checksums)} checksums to {out_path}")
PY
