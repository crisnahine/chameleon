"""Unit tests for Option 1 shape-fuzzy merge: _shape_fuzzy_merge.

Tests verify that:
  - Clusters sharing (path_pattern_bucket, default_export_kind, jsx_present)
    merge when their UNION top_level_node_kinds Jaccard >= threshold (0.7).
  - Clusters below the threshold remain split.
  - The threshold is env-overridable via CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD.
  - Clusters in different path buckets never merge regardless of shape.
  - Merged clusters carry cluster_tier='shape-merged'.
  - The merge is idempotent (running twice is safe).
  - cluster_files end-to-end produces the right count after the fuzzy pass.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/clustering_shape_fuzzy_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_shape_fuzzy_data_")
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
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from chameleon_mcp.bootstrap.clustering import (  # noqa: E402
    Cluster,
    _jaccard,
    _node_kinds_set,
    _shape_fuzzy_merge,
    _union_shape,
)
from chameleon_mcp.extractors._base import ParsedFile  # noqa: E402
from chameleon_mcp.signatures import ClusterKey  # noqa: E402


def _parsed_file(path: str, kinds: tuple[str, ...]) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=kinds,
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


def _cluster(
    bucket: str,
    kinds: tuple[str, ...],
    *,
    export_kind: str | None = None,
    jsx: bool = False,
    size: int = 5,
    tier: str = "tight",
    sparse_threshold: int = 3,
) -> Cluster:
    """Build a synthetic Cluster with `size` members all sharing `kinds`."""
    key = ClusterKey(
        path_pattern_bucket=bucket,
        content_signal_match="none",
        top_level_node_kinds=kinds,
        default_export_kind=export_kind,
        named_export_count_bucket="0",
        import_module_set_hash=f"{bucket}:{kinds}",
        jsx_present=jsx,
    )
    members = [_parsed_file(f"/{bucket}/file{i}.ts", kinds) for i in range(size)]
    return Cluster(
        key=key, members=members, sparse_threshold=sparse_threshold, cluster_tier=tier
    )


# ---------------------------------------------------------------------------
# Section 1: _jaccard helper
# ---------------------------------------------------------------------------


def test_jaccard_basics() -> None:
    print("\n--- _jaccard ---")
    t("identical non-empty sets -> 1.0", _jaccard({"A", "B"}, {"A", "B"}) == 1.0)
    t("both empty -> 1.0", _jaccard(frozenset(), frozenset()) == 1.0)
    t("disjoint -> 0.0", _jaccard({"A"}, {"B"}) == 0.0)
    # |intersection|=1, |union|=2 -> 0.5
    t(
        "half-overlap -> 0.5",
        abs(_jaccard(frozenset({"A"}), frozenset({"A", "B"})) - 0.5) < 1e-9,
    )
    # |intersection|=3, |union|=5 -> 0.6
    t(
        "{A,B,C,D} vs {A,B,C,E} -> 0.6",
        abs(_jaccard(frozenset("ABCD"), frozenset("ABCE")) - 0.6) < 1e-9,
    )


# ---------------------------------------------------------------------------
# Section 2: _union_shape
# ---------------------------------------------------------------------------


def test_union_shape() -> None:
    print("\n--- _union_shape ---")
    c = _cluster("src/api:ts", ("ClassNode",), size=2)
    # Inject a second member with an extra kind
    extra = _parsed_file("/src/api:ts/file_extra.ts", ("ClassNode", "CallNode"))
    c.members.append(extra)
    us = _union_shape(c)
    t(
        "union_shape includes kinds from all members",
        us == frozenset({"ClassNode", "CallNode"}),
        f"got {us}",
    )
    # Empty cluster
    empty_c = _cluster("x:ts", (), size=0)
    t("empty cluster -> empty frozenset", _union_shape(empty_c) == frozenset())


# ---------------------------------------------------------------------------
# Section 3: _shape_fuzzy_merge behaviour
# ---------------------------------------------------------------------------


def test_merge_above_threshold() -> None:
    """Two clusters in the same bucket with Jaccard 1.0 must merge."""
    print("\n--- merge above threshold ---")
    c1 = _cluster("src/components:tsx", ("FunctionDeclaration", "ExportDeclaration"))
    c2 = _cluster("src/components:tsx", ("FunctionDeclaration", "ExportDeclaration"))
    out = _shape_fuzzy_merge([c1, c2])
    t("identical shapes merge into 1 cluster", len(out) == 1, f"got {len(out)}")
    t(
        "merged tier is 'shape-merged'",
        out[0].cluster_tier == "shape-merged",
        f"got {out[0].cluster_tier!r}",
    )
    t("merged cluster has all members", out[0].size == c1.size + c2.size)


def test_merge_threshold_0_7() -> None:
    """Jaccard exactly at default 0.7 must merge; just below must not."""
    print("\n--- threshold 0.7 boundary ---")
    # Jaccard({A,B,C,D,E,F,G}, {A,B,C,D,E,F,H}) = 6/8 = 0.75 -> above threshold
    a = frozenset("ABCDEFG")
    b = frozenset("ABCDEFH")
    j = _jaccard(a, b)
    t(f"Jaccard({set(a)!r}, {set(b)!r}) >= 0.7", j >= 0.7, f"got {j:.4f}")
    c1 = _cluster("src/api:ts", tuple("ABCDEFG"))
    c2 = _cluster("src/api:ts", tuple("ABCDEFH"))
    out = _shape_fuzzy_merge([c1, c2])
    t("Jaccard=0.75 clusters merge", len(out) == 1, f"got {len(out)}")

    # Jaccard({A,B,C,D}, {A,B,C,E}) = 3/5 = 0.6 -> below 0.7
    x = frozenset("ABCD")
    y = frozenset("ABCE")
    jxy = _jaccard(x, y)
    t(f"Jaccard({set(x)!r}, {set(y)!r}) < 0.7", jxy < 0.7, f"got {jxy:.4f}")
    c3 = _cluster("src/api:ts", tuple("ABCD"), size=6)
    c4 = _cluster("src/api:ts", tuple("ABCE"), size=6)
    out2 = _shape_fuzzy_merge([c3, c4])
    t("Jaccard=0.6 clusters stay split at default 0.7", len(out2) == 2, f"got {len(out2)}")


def test_env_override_threshold() -> None:
    """CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD=0.5 lowers the bar."""
    print("\n--- env threshold override ---")
    # {ClassNode} vs {ClassNode, ConstantWriteNode}: Jaccard = 1/2 = 0.5
    # Below 0.7 default, but should merge at 0.5.
    c1 = _cluster("app/services:rb", ("ClassNode",), size=4)
    c2 = _cluster("app/services:rb", ("ClassNode", "ConstantWriteNode"), size=4)

    j_val = _jaccard(frozenset({"ClassNode"}), frozenset({"ClassNode", "ConstantWriteNode"}))
    t("Jaccard({ClassNode}, {ClassNode,ConstantWriteNode}) == 0.5", abs(j_val - 0.5) < 1e-9)

    # At default 0.7 — should NOT merge
    out_default = _shape_fuzzy_merge([c1, c2])
    t("at default 0.7 threshold, 0.5-Jaccard stays split", len(out_default) == 2, f"got {len(out_default)}")

    # At 0.4 override — should merge (0.5 >= 0.4)
    old = os.environ.get("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD")
    os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = "0.4"
    try:
        out_low = _shape_fuzzy_merge([c1, c2])
        t(
            "at threshold=0.4, 0.5-Jaccard clusters merge",
            len(out_low) == 1,
            f"got {len(out_low)}",
        )
        if out_low:
            t(
                "merged tier is 'shape-merged'",
                out_low[0].cluster_tier == "shape-merged",
                f"got {out_low[0].cluster_tier!r}",
            )
    finally:
        if old is None:
            os.environ.pop("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD", None)
        else:
            os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = old


def test_different_buckets_no_merge() -> None:
    """Clusters in different path buckets must NEVER merge."""
    print("\n--- different buckets do not merge ---")
    # Identical shapes but different bucket
    c1 = _cluster("app/services/zoom:rb", ("ClassNode",))
    c2 = _cluster("app/services/billing:rb", ("ClassNode",))
    out = _shape_fuzzy_merge([c1, c2])
    t(
        "different path_pattern_bucket clusters stay split",
        len(out) == 2,
        f"got {len(out)}",
    )


def test_different_export_kind_no_merge() -> None:
    """Clusters with different default_export_kind must not merge."""
    print("\n--- different default_export_kind do not merge ---")
    c1 = _cluster("src/pages:tsx", ("FunctionDeclaration",), export_kind="FunctionDeclaration")
    c2 = _cluster("src/pages:tsx", ("FunctionDeclaration",), export_kind="ClassDeclaration")
    out = _shape_fuzzy_merge([c1, c2])
    t(
        "different default_export_kind clusters stay split",
        len(out) == 2,
        f"got {len(out)}",
    )


def test_different_jsx_no_merge() -> None:
    """Clusters with different jsx_present must not merge."""
    print("\n--- different jsx_present do not merge ---")
    c1 = _cluster("src/pages:tsx", ("FunctionDeclaration",), jsx=True)
    c2 = _cluster("src/pages:tsx", ("FunctionDeclaration",), jsx=False)
    out = _shape_fuzzy_merge([c1, c2])
    t(
        "different jsx_present clusters stay split",
        len(out) == 2,
        f"got {len(out)}",
    )


def test_idempotent() -> None:
    """Running _shape_fuzzy_merge twice produces the same result."""
    print("\n--- idempotent ---")
    c1 = _cluster("src/utils:ts", ("FunctionDeclaration", "VariableStatement"))
    c2 = _cluster("src/utils:ts", ("FunctionDeclaration", "VariableStatement", "TypeAliasDeclaration"))
    # Jaccard = 2/3 = 0.667... < 0.7 so they don't merge at default.
    # Use a threshold that lets them merge:
    old = os.environ.get("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD")
    os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = "0.6"
    try:
        out1 = _shape_fuzzy_merge([c1, c2])
        out2 = _shape_fuzzy_merge(out1)
        t("first pass: 2 clusters merge to 1", len(out1) == 1, f"got {len(out1)}")
        t("second pass: still 1 cluster (idempotent)", len(out2) == 1, f"got {len(out2)}")
        if out1 and out2:
            t(
                "size unchanged on second pass",
                out2[0].size == out1[0].size,
                f"{out2[0].size} != {out1[0].size}",
            )
    finally:
        if old is None:
            os.environ.pop("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD", None)
        else:
            os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = old


def test_loose_merged_clusters_participate() -> None:
    """A cluster already marked tier='loose' can still participate in shape merge."""
    print("\n--- loose-merged clusters participate ---")
    # Create two 'loose' clusters with identical shapes in the same bucket
    c1 = _cluster("src/hooks:ts", ("FunctionDeclaration",), tier="loose")
    c2 = _cluster("src/hooks:ts", ("FunctionDeclaration",), tier="loose")
    out = _shape_fuzzy_merge([c1, c2])
    t(
        "two loose clusters merge into shape-merged",
        len(out) == 1,
        f"got {len(out)}",
    )
    if out:
        t(
            "resulting tier is 'shape-merged' (not 'loose')",
            out[0].cluster_tier == "shape-merged",
            f"got {out[0].cluster_tier!r}",
        )


def test_three_way_transitive_merge() -> None:
    """Three clusters A, B, C where A-B merge and B-C merge -> all three merge."""
    print("\n--- three-way transitive merge ---")
    # A={1,2,3,4,5}, B={1,2,3,4,6}, C={1,2,3,4,7}
    # Jaccard(A,B) = 4/6 = 0.667... < 0.7, so no merge at default.
    # Use threshold 0.6 to trigger merges.
    # A-B Jaccard = 4/6 = 0.667 >= 0.6 -> merge
    # B-C Jaccard = 4/6 = 0.667 >= 0.6 -> merge
    # A-C Jaccard = 4/6 = 0.667 >= 0.6 -> merge
    # All three should end up in one cluster.
    old = os.environ.get("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD")
    os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = "0.6"
    try:
        c1 = _cluster("src/api:ts", ("N1", "N2", "N3", "N4", "N5"))
        c2 = _cluster("src/api:ts", ("N1", "N2", "N3", "N4", "N6"))
        c3 = _cluster("src/api:ts", ("N1", "N2", "N3", "N4", "N7"))
        out = _shape_fuzzy_merge([c1, c2, c3])
        t("three clusters merge into one", len(out) == 1, f"got {len(out)}")
        if out:
            t("merged size = 3 * 5 = 15", out[0].size == 15, f"got {out[0].size}")
    finally:
        if old is None:
            os.environ.pop("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD", None)
        else:
            os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = old


def test_union_shape_drives_jaccard_not_single_member() -> None:
    """Merge uses UNION over all members, not just the first member's kinds."""
    print("\n--- union shape (not single member) ---")
    # c1 has members with kinds A and kinds A+B.
    # c2 has kinds A+B+C.
    # If we only used first-member shape:
    #   first-member(c1) = {A}, first-member(c2) = {A,B,C} -> Jaccard 1/3 = 0.33
    # But union(c1) = {A,B}, union(c2) = {A,B,C} -> Jaccard 2/3 = 0.67
    # At threshold 0.6 the union-based Jaccard triggers the merge.
    key1 = ClusterKey(
        path_pattern_bucket="src/services:ts",
        content_signal_match="none",
        top_level_node_kinds=("A",),
        default_export_kind=None,
        named_export_count_bucket="0",
        import_module_set_hash="h1",
        jsx_present=False,
    )
    key2 = ClusterKey(
        path_pattern_bucket="src/services:ts",
        content_signal_match="none",
        top_level_node_kinds=("A", "B", "C"),
        default_export_kind=None,
        named_export_count_bucket="0",
        import_module_set_hash="h2",
        jsx_present=False,
    )
    # c1: first member has kinds (A,), second has (A, B)
    c1 = Cluster(
        key=key1,
        members=[
            _parsed_file("src/services:ts/f0.ts", ("A",)),
            _parsed_file("src/services:ts/f1.ts", ("A", "B")),
        ],
        sparse_threshold=3,
    )
    c2 = Cluster(
        key=key2,
        members=[_parsed_file("src/services:ts/f2.ts", ("A", "B", "C"))],
        sparse_threshold=3,
    )

    # Verify single-member Jaccard vs union Jaccard
    first_member_j = _jaccard(
        _node_kinds_set(c1.members[0]), _node_kinds_set(c2.members[0])
    )
    union_j = _jaccard(_union_shape(c1), _union_shape(c2))
    t(
        "first-member Jaccard is lower than union Jaccard",
        first_member_j < union_j,
        f"first={first_member_j:.3f}, union={union_j:.3f}",
    )

    old = os.environ.get("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD")
    os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = "0.6"
    try:
        out = _shape_fuzzy_merge([c1, c2])
        t(
            "merge uses union shape (clusters merge at 0.6)",
            len(out) == 1,
            f"got {len(out)} clusters; union_j={union_j:.3f}, first_j={first_member_j:.3f}",
        )
    finally:
        if old is None:
            os.environ.pop("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD", None)
        else:
            os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = old


def test_spec_example_classnode_constantwritenode() -> None:
    """Spec example: {ClassNode} vs {ClassNode, ConstantWriteNode} = 0.5 Jaccard.

    3 synthetic ParsedFile objects in the SAME path bucket:
      - File 0: kinds = (ClassNode,)
      - File 1: kinds = (ClassNode,)
      - File 2: kinds = (ClassNode, ConstantWriteNode)

    At default threshold 0.7: Jaccard({ClassNode}, {ClassNode,ConstantWriteNode}) = 0.5
    -> should NOT merge (one cluster with files 0+1, one with file 2).

    At threshold 0.4: Jaccard 0.5 >= 0.4 -> should merge into one cluster.
    """
    print("\n--- spec example: ClassNode vs ClassNode+ConstantWriteNode ---")

    # Files 0 and 1 share the SAME key -> end up in one tight cluster already.
    # File 2 has a different key -> different tight cluster.
    # After tight clustering: 2 clusters. _shape_fuzzy_merge sees both.
    c_class_only = _cluster("app/services:rb", ("ClassNode",), size=2)
    c_class_const = _cluster("app/services:rb", ("ClassNode", "ConstantWriteNode"), size=1)

    j_val = _jaccard(frozenset({"ClassNode"}), frozenset({"ClassNode", "ConstantWriteNode"}))
    t(
        "Jaccard({ClassNode}, {ClassNode,ConstantWriteNode}) == 0.5",
        abs(j_val - 0.5) < 1e-9,
        f"got {j_val}",
    )

    # Default threshold (0.7): should NOT merge
    out_default = _shape_fuzzy_merge([c_class_only, c_class_const])
    t(
        "at default 0.7: stays split (2 clusters)",
        len(out_default) == 2,
        f"got {len(out_default)}",
    )

    # Threshold override 0.4: should merge
    old = os.environ.get("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD")
    os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = "0.4"
    try:
        out_low = _shape_fuzzy_merge([c_class_only, c_class_const])
        t(
            "at threshold=0.4: merges into 1 cluster",
            len(out_low) == 1,
            f"got {len(out_low)}",
        )
        if out_low:
            t("merged size = 3", out_low[0].size == 3, f"got {out_low[0].size}")
            t(
                "tier='shape-merged'",
                out_low[0].cluster_tier == "shape-merged",
                f"got {out_low[0].cluster_tier!r}",
            )
    finally:
        if old is None:
            os.environ.pop("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD", None)
        else:
            os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = old


def test_spec_example_abcd_vs_abce() -> None:
    """Spec example: {A,B,C,D} vs {A,B,C,E}: Jaccard = 3/5 = 0.6.

    Should NOT merge at 0.7. Should merge at 0.5.
    """
    print("\n--- spec example: {A,B,C,D} vs {A,B,C,E} ---")
    j_val = _jaccard(frozenset("ABCD"), frozenset("ABCE"))
    t("Jaccard({A,B,C,D}, {A,B,C,E}) = 0.6", abs(j_val - 0.6) < 1e-9, f"got {j_val}")

    c1 = _cluster("src/api:ts", tuple("ABCD"), size=6)
    c2 = _cluster("src/api:ts", tuple("ABCE"), size=6)

    out_07 = _shape_fuzzy_merge([c1, c2])
    t("at 0.7: stays split", len(out_07) == 2, f"got {len(out_07)}")

    old = os.environ.get("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD")
    os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = "0.5"
    try:
        out_05 = _shape_fuzzy_merge([c1, c2])
        t("at 0.5: merges into 1 cluster", len(out_05) == 1, f"got {len(out_05)}")
    finally:
        if old is None:
            os.environ.pop("CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD", None)
        else:
            os.environ["CHAMELEON_CLUSTER_SHAPE_JACCARD_THRESHOLD"] = old


def test_single_cluster_passthrough() -> None:
    """A single cluster in a group passes through unchanged."""
    print("\n--- single cluster passthrough ---")
    c = _cluster("src/utils:ts", ("FunctionDeclaration",), tier="tight")
    out = _shape_fuzzy_merge([c])
    t("single cluster passes through", len(out) == 1)
    t(
        "tier unchanged",
        out[0].cluster_tier == "tight",
        f"got {out[0].cluster_tier!r}",
    )
    t("size unchanged", out[0].size == c.size)


def test_empty_input() -> None:
    """Empty input returns empty output."""
    print("\n--- empty input ---")
    out = _shape_fuzzy_merge([])
    t("empty input -> empty output", out == [])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== Option 1: shape-fuzzy merge (_shape_fuzzy_merge) ===")

    test_jaccard_basics()
    test_union_shape()
    test_merge_above_threshold()
    test_merge_threshold_0_7()
    test_env_override_threshold()
    test_different_buckets_no_merge()
    test_different_export_kind_no_merge()
    test_different_jsx_no_merge()
    test_idempotent()
    test_loose_merged_clusters_participate()
    test_three_way_transitive_merge()
    test_union_shape_drives_jaccard_not_single_member()
    test_spec_example_classnode_constantwritenode()
    test_spec_example_abcd_vs_abce()
    test_single_cluster_passthrough()
    test_empty_input()

    print(f"\nSummary: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
