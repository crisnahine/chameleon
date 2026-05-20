"""Tests for rec 2: split clusters with semantic sub_bucket suffix.

A Rails ``app/models`` cluster that absorbed 36 concerns under
``app/models/concerns/`` should split into ``model`` + ``model-concern``
instead of one heterogenous cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path

from chameleon_mcp.bootstrap.clustering import (
    BIMODAL_DOMINANT_SHARE_THRESHOLD,
    Cluster,
    _split_by_sub_bucket,
)
from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.signatures import ClusterKey

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _mk_member(path: str) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        default_export_kind="ClassNode",
        named_export_count=1,
        top_level_node_kinds=("ClassNode",),
        import_specifiers=(),
        has_jsx=False,
        parse_diagnostics_count=0,
        content_first_200_bytes="class Foo\nend\n",
    )


def _mk_cluster(paths: list[str], threshold: int = 5) -> Cluster:
    key = ClusterKey(
        path_pattern_bucket="app/models",
        content_signal_match="none",
        top_level_node_kinds=("ClassNode",),
        default_export_kind="ClassNode",
        named_export_count_bucket="1",
        import_module_set_hash="x",
        jsx_present=False,
    )
    return Cluster(
        key=key,
        members=[_mk_member(p) for p in paths],
        sparse_threshold=threshold,
        sub_bucket_counts={},  # filled by upstream; not needed for split test
    )


section("splits Rails model cluster with concerns subdir")
# 20 non-concern models + 8 concerns; threshold 5
non_concerns = [f"app/models/foo_{i}.rb" for i in range(20)]
concerns = [f"app/models/concerns/bar_{i}.rb" for i in range(8)]
cluster = _mk_cluster(non_concerns + concerns, threshold=5)
splits = _split_by_sub_bucket([cluster], sparse_threshold=5)
t("produces 2 clusters", len(splits) == 2, f"got {len(splits)}")
sizes = sorted(c.size for c in splits)
t("sizes are [8, 20]", sizes == [8, 20], str(sizes))


section("does NOT split when concerns count is below sparse threshold")
non_concerns = [f"app/models/foo_{i}.rb" for i in range(20)]
concerns = [f"app/models/concerns/bar_{i}.rb" for i in range(2)]  # only 2
cluster = _mk_cluster(non_concerns + concerns, threshold=5)
splits = _split_by_sub_bucket([cluster], sparse_threshold=5)
t("stays a single cluster", len(splits) == 1, f"got {len(splits)}")


section("does NOT split when non-concern partition is heterogenous")
# 8 concerns + heterogenous non-concerns (so dominant non-concern share < 60%)
non_concerns = (
    [f"app/models/a/foo_{i}.rb" for i in range(5)]
    + [f"app/models/b/foo_{i}.rb" for i in range(5)]
    + [f"app/models/c/foo_{i}.rb" for i in range(5)]
)
concerns = [f"app/models/concerns/bar_{i}.rb" for i in range(8)]
cluster = _mk_cluster(non_concerns + concerns, threshold=5)
splits = _split_by_sub_bucket([cluster], sparse_threshold=5)
# Each sub-bucket holds 5/15 ~= 33%, well below 0.6 threshold.
t(
    "stays a single cluster when non-suffix bucket is fragmented",
    len(splits) == 1,
    f"got {len(splits)}",
)


section("idempotent: a cluster with no semantic suffix is unaffected")
non_concerns = [f"app/models/foo_{i}.rb" for i in range(20)]
cluster = _mk_cluster(non_concerns, threshold=5)
splits = _split_by_sub_bucket([cluster], sparse_threshold=5)
t("returns the same single cluster", len(splits) == 1 and splits[0].size == 20)


section("splits TypeScript __tests__ cluster")
prod = [f"src/components/Button_{i}.ts" for i in range(15)]
tests = [f"src/components/__tests__/Button_{i}.test.ts" for i in range(7)]
key = ClusterKey(
    path_pattern_bucket="src/components",
    content_signal_match="none",
    top_level_node_kinds=("FunctionDeclaration",),
    default_export_kind="ArrowFunction",
    named_export_count_bucket="1",
    import_module_set_hash="x",
    jsx_present=False,
)
cluster = Cluster(
    key=key,
    members=[_mk_member(p) for p in prod + tests],
    sparse_threshold=5,
    sub_bucket_counts={},
)
splits = _split_by_sub_bucket([cluster], sparse_threshold=5)
t("produces 2 clusters for prod + __tests__ split", len(splits) == 2, f"got {len(splits)}")


section("BIMODAL_DOMINANT_SHARE_THRESHOLD is the gate (sanity)")
t(
    "default 0.6 (matches the dominant-share check used by _split_by_sub_bucket)",
    BIMODAL_DOMINANT_SHARE_THRESHOLD == 0.6,
)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {len(PASS) + len(FAIL)}")
print(f"  Pass: {len(PASS)}")
print(f"  Fail: {len(FAIL)}")
if FAIL:
    print("\n  FAILURES:")
    for name, info in FAIL:
        print(f"    - {name}{(': ' + info) if info else ''}")
    sys.exit(1)
sys.exit(0)
