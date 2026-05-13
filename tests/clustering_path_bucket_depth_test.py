"""Unit tests for Option 4: path bucket depth = 2 with sub_bucket metadata.

Tests verify that:
  - Non-monorepo paths with 4+ segments use depth-2 bucket (parts[0]/parts[1]).
  - sub_bucket captures the remaining inner directories.
  - Files in app/services/zoom/ and app/services/billing/ produce the same bucket.
  - Monorepo paths (apps/, packages/, workspaces/) are unaffected (always depth-3).
  - CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH=3 env var restores depth-3 behavior.
  - Clusters built by cluster_files carry sub_bucket_counts distributions.
  - merge passes (loose + shape-fuzzy) combine sub_bucket_counts correctly.
  - TS monorepo: apps/admin/... and apps/web/... still produce distinct buckets.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/clustering_path_bucket_depth_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

TMPDATA = tempfile.mkdtemp(prefix="chameleon_path_bucket_depth_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA
# Ensure we start with the default depth=2.
os.environ.pop("CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH", None)

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


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from chameleon_mcp.bootstrap.clustering import Cluster, cluster_files  # noqa: E402
from chameleon_mcp.extractors._base import ParsedFile  # noqa: E402
from chameleon_mcp.signatures import path_pattern_bucket_for  # noqa: E402


def _pf(
    rel_path: str,
    *,
    repo_root: Path,
    head: str = "",
    top_kinds: tuple[str, ...] = ("ClassNode",),
    default_kind: str | None = "ClassNode",
    named_count: int = 0,
    imports: tuple[tuple[str, str], ...] = (),
    has_jsx: bool = False,
) -> ParsedFile:
    return ParsedFile(
        path=repo_root / rel_path,
        content_first_200_bytes=head,
        top_level_node_kinds=top_kinds,
        default_export_kind=default_kind,
        named_export_count=named_count,
        import_specifiers=imports,
        has_jsx=has_jsx,
    )


# ---------------------------------------------------------------------------
section("Depth-2 bucket: path_pattern_bucket_for unit cases")
# ---------------------------------------------------------------------------

# app/services/zoom and app/services/billing should produce the same bucket.
zoom_b, zoom_sub = path_pattern_bucket_for("app/services/zoom/recordings.rb")
billing_b, billing_sub = path_pattern_bucket_for("app/services/billing/invoices.rb")

t(
    "app/services/zoom/recordings.rb bucket is app/services:rb (with ext)",
    path_pattern_bucket_for("app/services/zoom/recordings.rb", include_extension=True)[0]
    == "app/services:rb",
    path_pattern_bucket_for("app/services/zoom/recordings.rb", include_extension=True)[0],
)
t(
    "app/services/zoom and app/services/billing share the same depth-2 bucket",
    zoom_b == billing_b,
    f"zoom={zoom_b!r} billing={billing_b!r}",
)
t(
    "app/services/zoom sub_bucket is 'zoom'",
    zoom_sub == "zoom",
    zoom_sub,
)
t(
    "app/services/billing sub_bucket is 'billing'",
    billing_sub == "billing",
    billing_sub,
)

# Direct file under app/services/ (no subdirectory).
direct_b, direct_sub = path_pattern_bucket_for("app/services/dashboard.rb")
t(
    "app/services/dashboard.rb bucket matches app/services",
    direct_b == "app/services",
    direct_b,
)
t(
    "app/services/dashboard.rb sub_bucket is empty (no inner dir)",
    direct_sub == "",
    repr(direct_sub),
)

# Deep path: app/controllers/api/v1/users.rb
ctrl_b, ctrl_sub = path_pattern_bucket_for("app/controllers/api/v1/users.rb")
t(
    "app/controllers/api/v1 bucket is app/controllers at depth=2",
    ctrl_b == "app/controllers",
    ctrl_b,
)
t(
    "app/controllers/api/v1 sub_bucket is 'api/v1'",
    ctrl_sub == "api/v1",
    ctrl_sub,
)

# Shallow path (3 parts): falls through to v5 shape unchanged.
shallow_b, shallow_sub = path_pattern_bucket_for("app/models/listing.rb")
t(
    "shallow path (3 segments) produces app/models",
    shallow_b == "app/models",
    shallow_b,
)
t(
    "shallow path sub_bucket is empty",
    shallow_sub == "",
    repr(shallow_sub),
)

# Root-level file.
root_b, root_sub = path_pattern_bucket_for("Gemfile")
t(
    "single-segment path produces (root)",
    root_b == "(root)",
    root_b,
)
t(
    "single-segment path sub_bucket is empty",
    root_sub == "",
    repr(root_sub),
)

# app/services/zoom/deep/something.rb — inner has two segments.
deep_b, deep_sub = path_pattern_bucket_for("app/services/zoom/nested/thing.rb")
t(
    "deeply nested path still uses depth-2 bucket",
    deep_b == "app/services",
    deep_b,
)
t(
    "deeply nested path sub_bucket captures all inner dirs",
    deep_sub == "zoom/nested",
    deep_sub,
)


# ---------------------------------------------------------------------------
section("Monorepo paths are unaffected (always depth-3)")
# ---------------------------------------------------------------------------

# apps/ monorepo: apps/web and apps/admin should still produce distinct buckets.
admin_b, admin_sub = path_pattern_bucket_for("apps/admin/components/Header.tsx")
web_b, web_sub = path_pattern_bucket_for("apps/web/components/Header.tsx")
t(
    "apps/admin bucket is apps/admin/components (depth-3, monorepo)",
    admin_b == "apps/admin/components",
    admin_b,
)
t(
    "apps/web bucket is apps/web/components (depth-3, monorepo)",
    web_b == "apps/web/components",
    web_b,
)
t(
    "apps/admin and apps/web produce distinct buckets",
    admin_b != web_b,
    f"admin={admin_b!r} web={web_b!r}",
)
t(
    "monorepo path sub_bucket is empty (depth-3 formula doesn't split further)",
    admin_sub == "",
    repr(admin_sub),
)

# packages/ monorepo also unaffected.
pkg_b, _ = path_pattern_bucket_for("packages/propel/src/services/billing.ts")
pkg2_b, _ = path_pattern_bucket_for("packages/element/src/services/billing.ts")
t(
    "packages/propel bucket is packages/propel/src",
    pkg_b == "packages/propel/src",
    pkg_b,
)
t(
    "packages/propel and packages/element produce distinct buckets",
    pkg_b != pkg2_b,
    f"propel={pkg_b!r} element={pkg2_b!r}",
)


# ---------------------------------------------------------------------------
section("CLUSTER_PATH_BUCKET_DEPTH=3 restores depth-3 behavior")
# ---------------------------------------------------------------------------

os.environ["CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH"] = "3"

# Need to reload the threshold since it's evaluated at call time.
from chameleon_mcp._thresholds import threshold_int  # noqa: E402

assert threshold_int("CLUSTER_PATH_BUCKET_DEPTH") == 3, "env not picked up"

zoom_b3, zoom_sub3 = path_pattern_bucket_for("app/services/zoom/recordings.rb")
billing_b3, billing_sub3 = path_pattern_bucket_for("app/services/billing/invoices.rb")
t(
    "depth=3: app/services/zoom/recordings.rb bucket is app/services/zoom",
    zoom_b3 == "app/services/zoom",
    zoom_b3,
)
t(
    "depth=3: sub_bucket is empty (all info already in bucket)",
    zoom_sub3 == "",
    repr(zoom_sub3),
)
t(
    "depth=3: zoom and billing produce different buckets (old behavior)",
    zoom_b3 != billing_b3,
    f"zoom={zoom_b3!r} billing={billing_b3!r}",
)

# Reset to default.
os.environ.pop("CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH", None)


# ---------------------------------------------------------------------------
section("cluster_files: sub_bucket_counts on Cluster")
# ---------------------------------------------------------------------------

with tempfile.TemporaryDirectory(prefix="cv059b1_") as tmp:
    repo = Path(tmp)
    members = [
        _pf("app/services/zoom/recordings.rb", repo_root=repo),
        _pf("app/services/zoom/transcripts.rb", repo_root=repo),
        _pf("app/services/billing/invoices.rb", repo_root=repo),
        _pf("app/services/billing/charges.rb", repo_root=repo),
        _pf("app/services/dashboard.rb", repo_root=repo),
    ]
    result = cluster_files(members, repo_root=repo, min_cluster_size=1)

    # All 5 files should collapse into ONE cluster (same bucket app/services:rb).
    rb_clusters = [
        c for c in result.clusters
        if c.key.path_pattern_bucket and c.key.path_pattern_bucket.startswith("app/services")
    ]
    t(
        "all app/services/* files form ONE cluster at depth=2",
        len(rb_clusters) == 1,
        f"got {len(rb_clusters)} clusters: {[c.key.path_pattern_bucket for c in result.clusters]}",
    )

    if rb_clusters:
        cl = rb_clusters[0]
        t(
            "cluster bucket is app/services:rb",
            cl.key.path_pattern_bucket == "app/services:rb",
            cl.key.path_pattern_bucket,
        )
        t(
            "cluster has 5 members",
            cl.size == 5,
            str(cl.size),
        )
        t(
            "sub_bucket_counts is non-empty",
            bool(cl.sub_bucket_counts),
            str(cl.sub_bucket_counts),
        )
        t(
            "sub_bucket 'zoom' has count 2",
            cl.sub_bucket_counts.get("zoom") == 2,
            str(cl.sub_bucket_counts),
        )
        t(
            "sub_bucket 'billing' has count 2",
            cl.sub_bucket_counts.get("billing") == 2,
            str(cl.sub_bucket_counts),
        )
        t(
            "sub_bucket '' (direct under app/services) has count 1",
            cl.sub_bucket_counts.get("") == 1,
            str(cl.sub_bucket_counts),
        )
        t(
            "total sub_bucket_counts sum equals cluster size",
            sum(cl.sub_bucket_counts.values()) == cl.size,
            f"sum={sum(cl.sub_bucket_counts.values())} size={cl.size}",
        )


# ---------------------------------------------------------------------------
section("cluster_files with CLUSTER_PATH_BUCKET_DEPTH=3: zoom/billing split")
# ---------------------------------------------------------------------------

os.environ["CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH"] = "3"

with tempfile.TemporaryDirectory(prefix="cv059b2_") as tmp:
    repo = Path(tmp)
    members = [
        _pf("app/services/zoom/recordings.rb", repo_root=repo),
        _pf("app/services/zoom/transcripts.rb", repo_root=repo),
        _pf("app/services/billing/invoices.rb", repo_root=repo),
        _pf("app/services/billing/charges.rb", repo_root=repo),
        _pf("app/services/dashboard.rb", repo_root=repo),
    ]
    result = cluster_files(members, repo_root=repo, min_cluster_size=1)

    svc_clusters = [
        c for c in result.clusters
        if (c.key.path_pattern_bucket or "").startswith("app/services")
    ]
    t(
        "depth=3: zoom/billing/dashboard split into separate clusters",
        len(svc_clusters) >= 2,
        f"got {len(svc_clusters)}: {[c.key.path_pattern_bucket for c in svc_clusters]}",
    )

os.environ.pop("CHAMELEON_CLUSTER_PATH_BUCKET_DEPTH", None)


# ---------------------------------------------------------------------------
section("TS monorepo: apps/admin and apps/web remain distinct at depth=2")
# ---------------------------------------------------------------------------

with tempfile.TemporaryDirectory(prefix="cv059b3_") as tmp:
    repo = Path(tmp)
    ts_members = [
        _pf(
            "apps/admin/components/Header.tsx",
            repo_root=repo,
            top_kinds=("FunctionDeclaration",),
            default_kind="FunctionDeclaration",
            has_jsx=True,
        ),
        _pf(
            "apps/admin/components/Footer.tsx",
            repo_root=repo,
            top_kinds=("FunctionDeclaration",),
            default_kind="FunctionDeclaration",
            has_jsx=True,
        ),
        _pf(
            "apps/web/components/Header.tsx",
            repo_root=repo,
            top_kinds=("FunctionDeclaration",),
            default_kind="FunctionDeclaration",
            has_jsx=True,
        ),
        _pf(
            "apps/web/components/Footer.tsx",
            repo_root=repo,
            top_kinds=("FunctionDeclaration",),
            default_kind="FunctionDeclaration",
            has_jsx=True,
        ),
    ]
    result = cluster_files(ts_members, repo_root=repo, min_cluster_size=1)
    buckets = [c.key.path_pattern_bucket for c in result.clusters]

    t(
        "TS monorepo produces at least 2 clusters (admin and web stay split)",
        len(result.clusters) >= 2,
        f"clusters={buckets}",
    )
    t(
        "apps/admin/components bucket exists",
        any("apps/admin/components" in (b or "") for b in buckets),
        str(buckets),
    )
    t(
        "apps/web/components bucket exists",
        any("apps/web/components" in (b or "") for b in buckets),
        str(buckets),
    )


# ---------------------------------------------------------------------------
section("sub_bucket_counts preserved through merge passes")
# ---------------------------------------------------------------------------

# Two clusters that will loose-merge (same bucket, sparse, similar shapes).
from chameleon_mcp.bootstrap.clustering import _merge_sub_bucket_counts  # noqa: E402
from chameleon_mcp.signatures import ClusterKey  # noqa: E402


def _make_cluster(bucket: str, sub_counts: dict[str, int]) -> Cluster:
    key = ClusterKey(
        path_pattern_bucket=bucket,
        content_signal_match="none",
        top_level_node_kinds=("ClassNode",),
        default_export_kind="ClassNode",
        named_export_count_bucket="0",
        import_module_set_hash="a" * 64,
        jsx_present=False,
    )
    members = [
        ParsedFile(
            path=Path(f"/fake/path_{i}.rb"),
            content_first_200_bytes="",
            top_level_node_kinds=("ClassNode",),
            default_export_kind="ClassNode",
            named_export_count=0,
            import_specifiers=[],
            has_jsx=False,
        )
        for i in range(sum(sub_counts.values()))
    ]
    return Cluster(key=key, members=members, sub_bucket_counts=dict(sub_counts))


c1 = _make_cluster("app/services:rb", {"zoom": 2, "": 1})
c2 = _make_cluster("app/services:rb", {"billing": 3})
merged = _merge_sub_bucket_counts([c1, c2])

t(
    "_merge_sub_bucket_counts combines zoom, billing, and empty-string keys",
    merged == {"zoom": 2, "": 1, "billing": 3},
    str(merged),
)
t(
    "_merge_sub_bucket_counts total count equals sum of inputs",
    sum(merged.values()) == 6,
    str(sum(merged.values())),
)


# ---------------------------------------------------------------------------
# Final results
# ---------------------------------------------------------------------------

print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
