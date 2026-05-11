"""Regression tests for the four v0.5.2 clustering / signature bugs.

Bug 1 — Path bucketing is extension-blind (`.tsx` vs `.ts` collapse)
    `path_pattern_bucket_for` ignored the file extension, so `.tsx` and
    `.ts` files in the same directory shared a bucket and were clustered
    together. JSX vs non-JSX re-discrimination happened downstream, AFTER
    the cluster had been forced. v0.5.2 adds an opt-in
    `include_extension: bool = False` arg; clustering flips it on so
    siblings split into distinct clusters. The runtime archetype lookup
    in `get_archetype` keeps the extension-blind default so v0.5.x
    profiles still match without migration.

Bug 2 — Path bucket drops middle segments for monorepos
    The schema-v5/v6 formula `parts[0]/parts[-3]/parts[-2]` dropped
    `parts[1]` (the workspace name) for any ≥5-part monorepo path, so
    files from `packages/excalidraw/components/X/Y.tsx`,
    `packages/element/components/X/Y.tsx`, and
    `packages/math/components/X/Y.tsx` all collapsed to
    "packages/components/X". v0.5.2 detects a monorepo workspace root
    (`packages`, `apps`, `workspaces`) at `parts[0]` and keeps
    `parts[1]` in the bucket.

Bug 3 — `content_signal_match` is dead code
    `tools.get_archetype` and `tools.get_pattern_context` hardcoded
    `content_signal_match: None` in every return branch even though
    `signatures.content_signal_match_for` returned a meaningful directive
    string. v0.5.2 reads the first 200 bytes up-front and surfaces the
    result in every `get_archetype` return path.

Bug 4 — Adaptive sparse-cluster threshold
    The hard-coded `SPARSE_CLUSTER_THRESHOLD = 5` killed recall on
    feature-per-folder layouts: excalidraw produced 94.8% sparse warnings,
    mastodon got 0 archetypes from 856 files. v0.5.2 adds an
    `min_cluster_size: int | None = None` arg to `cluster_files`; when
    None, an adaptive heuristic picks 3 for repos <1k files, 4 for 1k–5k,
    5 for >=5k. Tests can pass explicit values for determinism.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_2_clustering_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

# Isolate plugin data so any trust grants / drift dbs we touch don't leak
# into the rest of the test suite.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_2_clustering_data_")
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


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# Eager imports so a syntax error surfaces before fixture setup.
from chameleon_mcp.bootstrap.clustering import (  # noqa: E402
    Cluster,
    SPARSE_CLUSTER_THRESHOLD,
    _adaptive_sparse_threshold,
    cluster_files,
)
from chameleon_mcp.extractors._base import ParsedFile  # noqa: E402
from chameleon_mcp.signatures import (  # noqa: E402
    _MONOREPO_WORKSPACE_ROOTS,
    compute_signature,
    content_signal_match_for,
    path_pattern_bucket_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pf(
    rel_path: str,
    *,
    repo_root: Path,
    head: str = "",
    top_kinds: tuple[str, ...] = ("FunctionDeclaration",),
    default_kind: str | None = "FunctionDeclaration",
    named_count: int = 1,
    imports: tuple[tuple[str, str], ...] = (),
    has_jsx: bool = False,
) -> ParsedFile:
    """Build a minimal ParsedFile rooted under `repo_root`.

    Default shape mirrors an everyday React component so most clusters
    line up; callers override the relevant fields.
    """
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
# Bug 1 — Extension-blind path bucketing
# ---------------------------------------------------------------------------
section("Bug 1 (verify-before) — default bucket ignores extension")

# Default (include_extension=False) — v0.5.x behavior.
tsx_default = path_pattern_bucket_for("packages/excalidraw/components/Foo.tsx")
ts_default = path_pattern_bucket_for("packages/excalidraw/components/helper.ts")
t(
    "default bucket of .tsx and .ts in same dir collapse (verify-before)",
    tsx_default == ts_default,
    f"tsx={tsx_default!r} ts={ts_default!r}",
)


section("Bug 1 (verify-after) — opt-in extension makes them distinct")

tsx_ext = path_pattern_bucket_for(
    "packages/excalidraw/components/Foo.tsx", include_extension=True
)
ts_ext = path_pattern_bucket_for(
    "packages/excalidraw/components/helper.ts", include_extension=True
)
t(
    ".tsx bucket ends with :tsx when include_extension=True",
    tsx_ext.endswith(":tsx"),
    tsx_ext,
)
t(
    ".ts bucket ends with :ts when include_extension=True",
    ts_ext.endswith(":ts"),
    ts_ext,
)
t(
    ".tsx and .ts buckets diverge when include_extension=True",
    tsx_ext != ts_ext,
    f"tsx={tsx_ext!r} ts={ts_ext!r}",
)
t(
    "extension-aware bucket is a superstring of the extension-blind one",
    tsx_ext.startswith(tsx_default + ":"),
    f"got {tsx_ext!r} expected to start with {tsx_default!r}:",
)
t(
    "extensionless files leave the bucket unsuffixed",
    path_pattern_bucket_for("scripts/Makefile", include_extension=True)
    == path_pattern_bucket_for("scripts/Makefile"),
)
t(
    "dotfile (.gitignore) leaves the bucket unsuffixed",
    path_pattern_bucket_for("src/.gitignore", include_extension=True)
    == path_pattern_bucket_for("src/.gitignore"),
)
t(
    ".test.tsx tracks the FINAL extension (tsx not test)",
    path_pattern_bucket_for("src/page.test.tsx", include_extension=True).endswith(
        ":tsx"
    ),
)
t(
    ".jsx tracks correctly",
    path_pattern_bucket_for("src/components/Foo.jsx", include_extension=True).endswith(
        ":jsx"
    ),
)
t(
    ".d.ts tracks the literal final extension 'ts'",
    path_pattern_bucket_for("src/types/api.d.ts", include_extension=True).endswith(
        ":ts"
    ),
)

# end-to-end: clustering pipeline keeps them in DIFFERENT clusters
with tempfile.TemporaryDirectory(prefix="cv052b1_") as tmp:
    repo = Path(tmp)
    members = [
        _pf("packages/excalidraw/components/Foo.tsx", repo_root=repo, has_jsx=True),
        _pf("packages/excalidraw/components/Bar.tsx", repo_root=repo, has_jsx=True),
        _pf("packages/excalidraw/components/Baz.tsx", repo_root=repo, has_jsx=True),
        _pf("packages/excalidraw/components/helper.ts", repo_root=repo, has_jsx=False),
        _pf("packages/excalidraw/components/util.ts", repo_root=repo, has_jsx=False),
        _pf("packages/excalidraw/components/format.ts", repo_root=repo, has_jsx=False),
    ]
    result = cluster_files(members, repo_root=repo, min_cluster_size=2)
    # Make sure NO cluster mixes .tsx and .ts members.
    mixed_clusters = []
    for cl in result.clusters:
        exts = {m.path.suffix for m in cl.members}
        if exts == {".tsx", ".ts"}:
            mixed_clusters.append(cl)
    t(
        "clustering pipeline does not mix .tsx with .ts in any cluster",
        not mixed_clusters,
        f"mixed={len(mixed_clusters)}",
    )
    # At least two distinct buckets exist, one ending :tsx and one :ts.
    buckets = {cl.key.path_pattern_bucket for cl in result.clusters}
    t(
        "clustering pipeline produces a :tsx bucket",
        any(b.endswith(":tsx") for b in buckets),
        str(sorted(buckets)),
    )
    t(
        "clustering pipeline produces a :ts bucket",
        any(b.endswith(":ts") and not b.endswith(":tsx") for b in buckets),
        str(sorted(buckets)),
    )


# ---------------------------------------------------------------------------
# Bug 2 — Monorepo bucket drops the workspace name
# ---------------------------------------------------------------------------
section("Bug 2 (verify-before) — sibling workspaces would collide (v6 formula)")

# Reconstruct the v6 formula for documentation purposes; the live function
# must NOT match its output any more.
def _v6_bucket(file_path: str) -> str:
    parts = [p for p in file_path.split("/") if p and p not in (".", "..")]
    if len(parts) < 2:
        return "(root)"
    if len(parts) >= 4:
        return f"{parts[0]}/{parts[-3]}/{parts[-2]}"
    return f"{parts[0]}/{parts[-2]}"


a = "packages/excalidraw/components/TTDDialog/X.tsx"
b = "packages/element/components/TTDDialog/X.tsx"
c = "packages/math/components/TTDDialog/X.tsx"
t(
    "v6 formula collides excalidraw / element / math (documented bug)",
    _v6_bucket(a) == _v6_bucket(b) == _v6_bucket(c),
    f"a={_v6_bucket(a)!r} b={_v6_bucket(b)!r} c={_v6_bucket(c)!r}",
)


section("Bug 2 (verify-after) — workspace name is preserved")

ba = path_pattern_bucket_for(a)
bb = path_pattern_bucket_for(b)
bc = path_pattern_bucket_for(c)
t(
    "excalidraw bucket contains 'excalidraw'",
    "/excalidraw/" in ba or ba.endswith("excalidraw"),
    ba,
)
t(
    "element bucket contains 'element'",
    "/element/" in bb or bb.endswith("element"),
    bb,
)
t(
    "math bucket contains 'math'",
    "/math/" in bc or bc.endswith("math"),
    bc,
)
t(
    "three workspace buckets are mutually distinct",
    len({ba, bb, bc}) == 3,
    f"{ba!r} / {bb!r} / {bc!r}",
)
t(
    "bucket of packages/excalidraw/components/TTDDialog/X.tsx matches spec",
    ba == "packages/excalidraw/components",
    ba,
)
t(
    "apps/ heuristic anchors on workspace + workspace-internal top dir",
    path_pattern_bucket_for("apps/web/routes/marketing/page.tsx")
    == "apps/web/routes",
)
t(
    "workspaces/ heuristic also kicks in",
    "/foo/" in path_pattern_bucket_for("workspaces/foo/src/main/Bar.ts"),
)
t(
    "non-monorepo prefix (src/) keeps the v6 enclosing-dir formula",
    path_pattern_bucket_for("src/components/base/Button.tsx") == "src/components/base",
)
t(
    "Rails app/ paths unchanged (no workspace heuristic)",
    path_pattern_bucket_for("app/controllers/api/v1/foo.rb") == "app/api/v1",
)
t(
    "shallow monorepo path (2 segments) falls through to v6 shape",
    path_pattern_bucket_for("packages/x.ts") == "packages/x.ts".split("/")[0]
    + "/"
    + "packages/x.ts".split("/")[-2],
)
t(
    "_MONOREPO_WORKSPACE_ROOTS contains the three documented roots",
    {"packages", "apps", "workspaces"}.issubset(_MONOREPO_WORKSPACE_ROOTS),
    str(sorted(_MONOREPO_WORKSPACE_ROOTS)),
)

# Round-trip via clustering: three sibling workspaces should produce
# three distinct clusters even when every file has byte-identical AST
# shape and imports.
with tempfile.TemporaryDirectory(prefix="cv052b2_") as tmp:
    repo = Path(tmp)
    members = []
    for ws in ("excalidraw", "element", "math"):
        for i in range(3):
            members.append(
                _pf(
                    f"packages/{ws}/components/TTDDialog/Item{i}.tsx",
                    repo_root=repo,
                    has_jsx=True,
                )
            )
    result = cluster_files(members, repo_root=repo, min_cluster_size=2)
    t(
        "three sibling workspaces produce three distinct clusters",
        len(result.clusters) == 3,
        f"got {len(result.clusters)} buckets={[c.key.path_pattern_bucket for c in result.clusters]}",
    )
    t(
        "each workspace's cluster contains exactly 3 members",
        all(c.size == 3 for c in result.clusters),
        str([c.size for c in result.clusters]),
    )


# ---------------------------------------------------------------------------
# Bug 3 — content_signal_match wire-through
# ---------------------------------------------------------------------------
section("Bug 3 (verify-before) — content_signal_match_for returns directive but tools hardcoded None")

t(
    "content_signal_match_for returns 'use_client' on Next-style header",
    content_signal_match_for('"use client";\nimport React from "react";') == "use_client",
)
t(
    "content_signal_match_for returns 'use_server'",
    content_signal_match_for('"use server";\nexport async function action() {}') == "use_server",
)
t(
    "content_signal_match_for returns 'shebang'",
    content_signal_match_for("#!/usr/bin/env node\nconsole.log('hi');") == "shebang",
)
t(
    "content_signal_match_for returns 'ts_pragma'",
    content_signal_match_for("// @ts-nocheck\nfunction foo() {}") == "ts_pragma",
)
t(
    "content_signal_match_for returns 'none' on plain content",
    content_signal_match_for("import React from 'react';\nexport default function Foo() {}") == "none",
)


section("Bug 3 (verify-after) — get_archetype surfaces content_signal_match in every branch")

import json  # noqa: E402

from chameleon_mcp.tools import (  # noqa: E402
    bootstrap_repo,
    get_archetype,
    trust_profile,
    _compute_repo_id,
)


def _make_ts_repo(tmp: Path, with_use_client: bool = True) -> Path:
    """Materialize a tiny TS repo with a use-client component."""
    repo = tmp / "tsrepo"
    repo.mkdir()
    # Minimal package.json so the TS extractor `can_handle` returns True.
    (repo / "package.json").write_text(
        json.dumps({"name": "tsrepo", "dependencies": {"typescript": "5.0.0"}}),
        encoding="utf-8",
    )
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    src = repo / "src" / "components"
    src.mkdir(parents=True)
    # Build enough sibling files to clear the adaptive sparse threshold (3
    # at this corpus size).
    header = '"use client";\n' if with_use_client else ""
    for i in range(5):
        (src / f"Comp{i}.tsx").write_text(
            f"{header}import React from 'react';\nexport default function Comp{i}() "
            "{ return <div>hi</div>; }\n",
            encoding="utf-8",
        )
    return repo


with tempfile.TemporaryDirectory(prefix="cv052b3_") as tmp:
    repo = _make_ts_repo(Path(tmp), with_use_client=True)
    bootstrap_report = bootstrap_repo(str(repo))
    if bootstrap_report["data"]["status"] != "success":
        t(
            "bootstrap_repo for use_client fixture succeeds",
            False,
            json.dumps(bootstrap_report["data"]),
        )
    else:
        t(
            "bootstrap_repo for use_client fixture succeeds",
            True,
        )
        # Trust so callers get the full envelope (not strictly required for
        # get_archetype, but matches what real consumers do).
        trust_profile(str(repo), "tsrepo-use-client")
        repo_id = _compute_repo_id(repo)
        sample = repo / "src" / "components" / "Comp0.tsx"
        r = get_archetype(repo_id, str(sample))["data"]
        t(
            "get_archetype surfaces 'use_client' for a use-client file",
            r["content_signal_match"] == "use_client",
            json.dumps(r),
        )

        # Test a file that exists on disk but is OUTSIDE any matched
        # archetype bucket. The wire-through must still populate the
        # signal because the file head was readable.
        rogue = repo / "src" / "rogue.ts"
        rogue.write_text("#!/usr/bin/env node\nconsole.log('hi');\n", encoding="utf-8")
        r2 = get_archetype(repo_id, str(rogue))["data"]
        t(
            "get_archetype surfaces 'shebang' even when archetype is None",
            r2["content_signal_match"] == "shebang",
            json.dumps(r2),
        )

        # Test the "missing file" branch — content_signal_match stays
        # None because we never looked.
        ghost = repo / "src" / "components" / "GhostThatDoesNotExist.tsx"
        r3 = get_archetype(repo_id, str(ghost))["data"]
        t(
            "get_archetype returns None when file is missing on disk",
            r3["content_signal_match"] is None,
            json.dumps(r3),
        )

        # Test the "wrong repo_id" branch — but the file IS readable on
        # disk. The signal must still come back because the read happens
        # up-front.
        r4 = get_archetype("notarealrepo" * 8, str(sample))["data"]
        t(
            "get_archetype surfaces signal even when repo_id mismatch",
            r4["content_signal_match"] == "use_client",
            json.dumps(r4),
        )

with tempfile.TemporaryDirectory(prefix="cv052b3plain_") as tmp:
    repo = _make_ts_repo(Path(tmp), with_use_client=False)
    bootstrap_report = bootstrap_repo(str(repo))
    if bootstrap_report["data"]["status"] == "success":
        trust_profile(str(repo), "tsrepo-plain")
        repo_id = _compute_repo_id(repo)
        sample = repo / "src" / "components" / "Comp0.tsx"
        r = get_archetype(repo_id, str(sample))["data"]
        # Plain file with no directive: signal must be the string "none"
        # so callers can distinguish "we looked, nothing matched" from
        # "we never looked".
        t(
            "get_archetype emits 'none' (string) when no directive matches",
            r["content_signal_match"] == "none",
            json.dumps(r),
        )


# ---------------------------------------------------------------------------
# Bug 4 — Adaptive sparse threshold
# ---------------------------------------------------------------------------
section("Bug 4 (verify-before) — fixed threshold of 5 is too rigid")

t(
    "module-level SPARSE_CLUSTER_THRESHOLD constant still equals 5",
    SPARSE_CLUSTER_THRESHOLD == 5,
    str(SPARSE_CLUSTER_THRESHOLD),
)


section("Bug 4 (verify-after) — adaptive thresholds resolve by corpus size")

t(
    "_adaptive_sparse_threshold returns 3 for tiny repos (n=10)",
    _adaptive_sparse_threshold(10) == 3,
    str(_adaptive_sparse_threshold(10)),
)
t(
    "_adaptive_sparse_threshold returns 3 just below the 1000-file boundary",
    _adaptive_sparse_threshold(999) == 3,
)
t(
    "_adaptive_sparse_threshold returns 4 at the 1000-file boundary",
    _adaptive_sparse_threshold(1000) == 4,
)
t(
    "_adaptive_sparse_threshold returns 4 just below the 5000-file boundary",
    _adaptive_sparse_threshold(4999) == 4,
)
t(
    "_adaptive_sparse_threshold returns 5 at the 5000-file boundary",
    _adaptive_sparse_threshold(5000) == 5,
)
t(
    "_adaptive_sparse_threshold returns 5 for huge repos (n=100000)",
    _adaptive_sparse_threshold(100_000) == 5,
)


# end-to-end on tiny repos: cluster of 3 must NOT be sparse under the
# adaptive heuristic (whereas under the legacy threshold-5 it was).
with tempfile.TemporaryDirectory(prefix="cv052b4_") as tmp:
    repo = Path(tmp)
    members = [
        _pf(f"src/services/User{i}.ts", repo_root=repo, has_jsx=False)
        for i in range(3)
    ]
    result = cluster_files(members, repo_root=repo)
    t(
        "adaptive: a 3-member cluster on a 3-file repo is NOT sparse",
        len(result.dense_clusters) == 1 and not result.dense_clusters[0].is_sparse,
        f"clusters={[(c.size, c.is_sparse, c.sparse_threshold) for c in result.clusters]}",
    )
    t(
        "adaptive: 3-file corpus resolves threshold to 3",
        result.clusters[0].sparse_threshold == 3,
        str(result.clusters[0].sparse_threshold),
    )

# explicit min_cluster_size override wins over the heuristic
with tempfile.TemporaryDirectory(prefix="cv052b4ex_") as tmp:
    repo = Path(tmp)
    members = [
        _pf(f"src/services/User{i}.ts", repo_root=repo, has_jsx=False)
        for i in range(3)
    ]
    result_strict = cluster_files(members, repo_root=repo, min_cluster_size=5)
    t(
        "explicit min_cluster_size=5 overrides adaptive heuristic",
        result_strict.clusters[0].sparse_threshold == 5,
        str(result_strict.clusters[0].sparse_threshold),
    )
    t(
        "explicit min_cluster_size=5 surfaces a 3-cluster as sparse",
        result_strict.clusters[0].is_sparse,
    )
    result_loose = cluster_files(members, repo_root=repo, min_cluster_size=1)
    t(
        "explicit min_cluster_size=1 keeps everything dense",
        not result_loose.clusters[0].is_sparse,
    )
    # min_cluster_size=0 should clamp to 1 (sparse_threshold >= 1 by docstring)
    result_zero = cluster_files(members, repo_root=repo, min_cluster_size=0)
    t(
        "min_cluster_size=0 clamps up to 1 (never zero)",
        result_zero.clusters[0].sparse_threshold >= 1,
        str(result_zero.clusters[0].sparse_threshold),
    )

# A legacy Cluster built directly (no cluster_files plumbing) preserves
# the v0.5.1 threshold-5 default for backward compat.
direct = Cluster(
    key=compute_signature(
        file_path="src/foo.ts",
        content_first_200_bytes="",
        top_level_node_kinds=("FunctionDeclaration",),
        default_export_kind="FunctionDeclaration",
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    ),
    members=[],
)
t(
    "direct Cluster() construction inherits SPARSE_CLUSTER_THRESHOLD=5",
    direct.sparse_threshold == SPARSE_CLUSTER_THRESHOLD == 5,
    str(direct.sparse_threshold),
)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
print("=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
if FAIL:
    sys.exit(1)
sys.exit(0)
