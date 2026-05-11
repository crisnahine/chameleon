"""Regression test for BUG-002: loose-merge clustering tier.

Pre-v0.5.6 the seven-tuple cluster signature split files with minor
AST-shape differences into separate clusters. Real codebases ended up
with 90%+ singleton clusters and 1 archetype per ~150 files. Most
files then returned archetype=null from get_pattern_context.

The loose-merge tier (cluster_tier='loose') groups same paths_pattern
sparse clusters whose AST shapes Jaccard >= 0.5 and folds them into
one cluster — recovering the long tail of meaningful patterns.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_6_loose_merge_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_6_loose_data_")
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
    print("=== BUG-002: loose-merge clustering tier ===")

    # Build a repo where strict signature clustering would split api files
    # into singletons but they share the same paths_pattern.
    with tempfile.TemporaryDirectory(prefix="bug002_") as td:
        root = Path(td)
        (root / "package.json").write_text(
            '{"name":"x","dependencies":{"typescript":"5"}}'
        )
        (root / "tsconfig.json").write_text("{}")
        api_dir = root / "src" / "features" / "comments" / "api"
        api_dir.mkdir(parents=True)
        # 5 files in the same paths_pattern but slightly different AST shapes
        # (one has an extra type, one has an interface, etc.)
        api_dir_contents = [
            "import {axios} from '../client';\nexport async function getOne() {\n  return axios.get('/one');\n}\n",
            "import {axios} from '../client';\ntype Result = {id: number};\nexport async function getTwo(): Promise<Result> {\n  return axios.get('/two');\n}\n",
            "import {axios} from '../client';\ninterface Item {id: number}\nexport async function getThree(): Promise<Item[]> {\n  return axios.get('/three');\n}\n",
            "import {axios} from '../client';\nexport const ENDPOINT = '/four';\nexport async function getFour() {\n  return axios.get(ENDPOINT);\n}\n",
            "import {axios} from '../client';\nexport async function deleteOne(id: number) {\n  return axios.delete('/one/' + id);\n}\n",
        ]
        for i, content in enumerate(api_dir_contents):
            (api_dir / f"file{i}.ts").write_text(content)

        resp = bootstrap_repo(str(root))
        data = resp["data"]
        t(
            "bootstrap success",
            data.get("status") == "success",
            f"got {data.get('status')!r}",
        )
        # Number of detected archetypes (not warnings)
        arch_count = int(data.get("archetypes_detected") or 0)
        t(
            "at least 1 archetype detected after loose-merge",
            arch_count >= 1,
            f"got archetypes_detected={arch_count}",
        )

    # Verify cluster_tier shows up on the orchestrator's result
    from chameleon_mcp.bootstrap.clustering import (
        Cluster,
        _loose_merge_sparse_clusters,
    )
    from chameleon_mcp.signatures import ClusterKey

    class FakeMember:
        def __init__(self, kinds):
            self.top_level_node_kinds = kinds

    # 3 singletons sharing path bucket + similar (>=0.5 Jaccard) shapes
    def _mk(name, kinds):
        key = ClusterKey(
            path_pattern_bucket=name,
            content_signal_match="none",
            top_level_node_kinds=tuple(kinds),
            default_export_kind=None,
            named_export_count_bucket="0",
            import_module_set_hash=name,
            jsx_present=False,
        )
        return Cluster(key=key, members=[FakeMember(kinds)], sparse_threshold=3)

    sparse_inputs = [
        _mk("src/api:ts", ["ImportDeclaration", "ExportNamedDeclaration"]),
        _mk("src/api:ts", ["ImportDeclaration", "TypeAliasDeclaration", "ExportNamedDeclaration"]),
        _mk("src/api:ts", ["ImportDeclaration", "InterfaceDeclaration", "ExportNamedDeclaration"]),
    ]
    merged = _loose_merge_sparse_clusters(sparse_inputs, sparse_threshold=3)
    t(
        "3 same-bucket singletons merge into one cluster",
        len(merged) == 1,
        f"got {len(merged)} clusters",
    )
    if merged:
        t(
            "merged cluster has tier='loose'",
            merged[0].cluster_tier == "loose",
            f"got tier={merged[0].cluster_tier!r}",
        )
        t(
            "merged cluster has 3 members",
            merged[0].size == 3,
            f"got size={merged[0].size}",
        )

    # Negative: very different shapes should NOT merge
    dissimilar = [
        _mk("src/api:ts", ["ImportDeclaration"]),
        _mk("src/api:ts", ["ClassDeclaration", "ExportDefaultDeclaration"]),
    ]
    not_merged = _loose_merge_sparse_clusters(dissimilar, sparse_threshold=3)
    t(
        "dissimilar singletons stay separate",
        len(not_merged) == 2,
        f"got {len(not_merged)} clusters",
    )

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
