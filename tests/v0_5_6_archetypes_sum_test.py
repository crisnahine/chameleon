"""Regression test for BUG-011: archetypes_detected sums across workspaces.

Pre-v0.5.6, the top-level archetypes_detected only reflected the root
workspace's count. For a monorepo where the root has 0 archetypes but
each sub-workspace has N, the user-visible bootstrap summary said
"archetypes_detected: 0" — accurate for the root but misleading at a
glance. The per-workspace counts were buried in the ``workspaces``
array. Now we sum and surface ``archetypes_per_workspace`` as a map.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_archetypes_sum_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_arch_sum_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


from chameleon_mcp.tools import bootstrap_repo  # noqa: E402


def _mk_workspace(parent: Path, ws: str, n_files: int) -> None:
    d = parent / ws
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(
        '{"name":"' + ws.replace("/", "-") + '","dependencies":{"typescript":"5"}}'
    )
    (d / "tsconfig.json").write_text("{}")
    for i in range(n_files):
        (d / f"f{i}.ts").write_text(
            f"export const v{i} = {i}; import {{x}} from './lib';\n"
        )


def main() -> int:
    print("=== BUG-011: archetypes_detected sums across workspaces ===")

    with tempfile.TemporaryDirectory(prefix="bug011_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"root","private":true,"workspaces":["apps/*","packages/*"]}'
        )
        # Root has no tsconfig and no TS deps in root pkg → root processes 0 files
        _mk_workspace(root, "apps/web", 8)
        _mk_workspace(root, "apps/api", 6)
        _mk_workspace(root, "packages/utils", 5)

        resp = bootstrap_repo(str(root))
        data = resp["data"]
        # Some sub-workspace must produce >= 1 archetype to make this meaningful
        per_ws = data.get("archetypes_per_workspace") or {}
        t(
            "archetypes_per_workspace map present",
            isinstance(per_ws, dict),
            f"got {per_ws!r}",
        )
        t(
            "at least one workspace has > 0 archetypes",
            any(v > 0 for v in per_ws.values()),
            f"got {per_ws!r}",
        )
        ws_sum = sum(per_ws.values())
        root_count = int(data.get("archetypes_detected_root") or 0)
        total = int(data.get("archetypes_detected") or 0)
        t(
            "archetypes_detected equals root + sum(workspaces)",
            total == root_count + ws_sum,
            f"total={total} root={root_count} ws_sum={ws_sum}",
        )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
