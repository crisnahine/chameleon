"""Regression test for BUG-006: rename candidates exclude the current name.

Pre-v0.5.6, ``propose_archetype_renames`` returned a candidate list that
included the current archetype name (so "no rename" was a visible
option) and ``cluster-<hex>-<stem>`` style combos that just decorated
the placeholder name. Users complained the candidates were noisy.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_rename_candidates_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_renames_data_")
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


from chameleon_mcp.tools import _propose_alternatives_for  # noqa: E402


def main() -> int:
    print("=== BUG-006: rename candidates exclude current name ===")

    alts = _propose_alternatives_for(
        current_name="cluster-a2cfb565",
        archetype={
            "paths_pattern": "src/mocks/handlers:ts",
            "top_level_node_kinds": ["ImportDeclaration"],
        },
        canonical={"witness": {"path": "src/testing/mocks/handlers/comments.ts"}},
    )
    t(
        "current cluster name absent from alternatives",
        "cluster-a2cfb565" not in alts,
        f"got {alts!r}",
    )
    t(
        "no alternative carries the cluster-<hex> prefix",
        all(not a.startswith("cluster-a2cfb565") for a in alts),
        f"got {alts!r}",
    )
    t(
        "alternatives include meaningful candidate (e.g. handlers/comments/handlers-ts)",
        any(c in alts for c in ("comments", "handlers", "handlers-ts")),
        f"got {alts!r}",
    )

    # Non-cluster-id current names still drop themselves but keep useful combos.
    alts2 = _propose_alternatives_for(
        current_name="react-component",
        archetype={
            "paths_pattern": "src/components/ui:tsx",
            "top_level_node_kinds": ["ExportNamedDeclaration"],
            "jsx_present": True,
        },
        canonical={"witness": {"path": "src/components/ui/button/button.tsx"}},
    )
    t(
        "non-cluster current name still absent",
        "react-component" not in alts2,
        f"got {alts2!r}",
    )
    t(
        "candidates non-empty for a real archetype",
        len(alts2) >= 1,
        f"got {alts2!r}",
    )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
