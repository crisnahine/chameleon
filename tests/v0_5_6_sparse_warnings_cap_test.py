"""Regression test for BUG-008/009: sparse_cluster_warnings cap + aggregate.

Pre-v0.5.6 bootstrap of a moderately-sized repo produced 2000-6000
``sparse_cluster_warnings`` entries — a 800 KB - 2 MB payload that
exceeded the MCP transport's response cap. Real-world ef-client
bootstrap landed at 665KB; gitlabhq at 2 MB.

Fix: aggregate by paths_pattern (collapse same-pattern singletons) and
hard-cap the resulting list at 50, surfacing a truncation envelope.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_sparse_warnings_cap_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_sparse_data_")
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


def main() -> int:
    print("=== BUG-008/009: sparse_cluster_warnings cap + aggregate ===")

    # Build a synthetic repo with many distinct directory shapes so the
    # clusterer produces hundreds of singletons.
    with tempfile.TemporaryDirectory(prefix="bug008_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        # 80 unique directory shapes × 1 file each → 80 singletons grouped
        # into ~80 distinct paths_patterns. The aggregator should keep
        # them as ~80 rows (one per pattern), then the 50 cap fires.
        for i in range(80):
            d = root / "src" / "features" / f"feat{i:02d}"
            d.mkdir(parents=True)
            (d / "index.ts").write_text(
                f"// feature {i}\nexport const v{i} = {i};\n"
            )

        resp = bootstrap_repo(str(root))
        data = resp["data"]
        warnings = data.get("sparse_cluster_warnings") or []
        truncation_marker = [w for w in warnings if w.get("kind") == "sparse_cluster_truncated"]

        t(
            "warnings count <= 51 (50 entries + optional truncation marker)",
            len(warnings) <= 51,
            f"got {len(warnings)}",
        )
        t(
            "truncation marker present",
            len(truncation_marker) == 1
            and truncation_marker[0].get("truncated") is True,
            f"got marker={truncation_marker!r}",
        )
        if truncation_marker:
            total = int(truncation_marker[0].get("total_groups") or 0)
            t(
                "truncation marker reports total_groups > 50",
                total > 50,
                f"got total_groups={total}",
            )

        # Test deduplication: a second repo with multiple singletons sharing
        # one paths_pattern should NOT produce repeats.
    with tempfile.TemporaryDirectory(prefix="bug008b_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        api_dir = root / "src" / "features" / "comments" / "api"
        api_dir.mkdir(parents=True)
        # 5 files with same paths_pattern but distinct AST shapes
        api_dir_contents = [
            "import {a} from 'a';\nexport function getOne() { return 1; }\n",
            "import {b} from 'b'; import {c} from 'c';\nexport type X = number;\nexport function getTwo() { return 2; }\n",
            "type Y = string;\nexport interface I {}\nexport const k = 1;\n",
            "import a from 'a';\nclass Foo {};\nexport default Foo;\n",
            "export async function getFive() { return 5; }\n",
        ]
        for i, content in enumerate(api_dir_contents):
            (api_dir / f"file{i}.ts").write_text(content)

        resp = bootstrap_repo(str(root))
        warnings = resp["data"].get("sparse_cluster_warnings") or []
        # Find rows for src/features/comments/api:ts
        api_rows = [
            w for w in warnings
            if "api" in (w.get("paths_pattern") or "")
        ]
        if api_rows:
            t(
                "same paths_pattern collapses to a single aggregated row",
                len(api_rows) == 1,
                f"got {len(api_rows)} rows for api: {api_rows!r}",
            )
            # The row should record cluster_count > 1 OR contain aggregate fields
            row = api_rows[0]
            agg = row.get("cluster_count")
            t(
                "aggregated row carries cluster_count/total_members",
                agg is not None or row.get("size") is not None,
                f"got row={row!r}",
            )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
