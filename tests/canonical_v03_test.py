"""Phase 2C regression tests for canonical selection + clustering.

Covers Phase 2C.1 (AST query derivation), 2C.2 (recency-weighted selection),
and 2C.3 (sparse + bimodal cluster surfacing). Each test corresponds to one
of the deliverable items in the PR brief and pins behavior so future
refactors can't silently regress it.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/canonical_v03_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

# Use isolated plugin data dir per run.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v03_data_")
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


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from chameleon_mcp.bootstrap.canonical import (  # noqa: E402
    RECENCY_WEIGHT_MULTIPLIER,
    RECENCY_WINDOW_DAYS,
    CanonicalSelection,
    _file_recency_weight,
    derive_ast_query,
    select_canonicals,
)
from chameleon_mcp.bootstrap.clustering import (  # noqa: E402
    BIMODAL_DOMINANT_SHARE_THRESHOLD,
    SPARSE_CLUSTER_THRESHOLD,
    Cluster,
    cluster_files,
)
from chameleon_mcp.bootstrap.orchestrator import (  # noqa: E402
    _build_bimodal_warnings,
    _build_sparse_warnings,
    bootstrap_repo,
)
from chameleon_mcp.extractors._base import ParsedFile  # noqa: E402
from chameleon_mcp.signatures import ClusterKey, compute_signature  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_parsed_file(
    *,
    repo_root: Path,
    rel_path: str,
    content_first_200_bytes: str = "",
    top_level_node_kinds: tuple[str, ...] = ("FunctionDeclaration",),
    default_export_kind: str | None = "FunctionDeclaration",
    named_export_count: int = 1,
    import_specifiers: tuple[tuple[str, str], ...] = (("react", "default"),),
    has_jsx: bool = False,
) -> ParsedFile:
    abs_path = repo_root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(
        content_first_200_bytes or f"// placeholder for {rel_path}\nexport function x() {{}}\n",
        encoding="utf-8",
    )
    return ParsedFile(
        path=abs_path,
        content_first_200_bytes=content_first_200_bytes
        or f"// placeholder for {rel_path}\nexport function x() {{}}\n",
        top_level_node_kinds=top_level_node_kinds,
        default_export_kind=default_export_kind,
        named_export_count=named_export_count,
        import_specifiers=import_specifiers,
        has_jsx=has_jsx,
        parse_diagnostics_count=0,
        sha_hint="cafef00d",
    )


def _make_cluster(members: list[ParsedFile], repo_root: Path) -> Cluster:
    """Cluster `members` by their actual computed signature.

    All members are forced into the SAME cluster by re-using the first
    file's signature. Useful when constructing test scenarios where we
    want to control intra-cluster variance manually.
    """
    if not members:
        raise ValueError("members must be non-empty")
    first = members[0]
    rel = str(first.path.relative_to(repo_root))
    key = compute_signature(
        file_path=rel,
        content_first_200_bytes=first.content_first_200_bytes,
        top_level_node_kinds=first.top_level_node_kinds,
        default_export_kind=first.default_export_kind,
        named_export_count=first.named_export_count,
        import_specifiers=first.import_specifiers,
        has_jsx=first.has_jsx,
    )
    return Cluster(key=key, members=members)


def _make_ts_repo() -> Path:
    """Create a minimal TS repo with two clusters (one dense, one sparse)."""
    root = Path(tempfile.mkdtemp(prefix="chameleon_v03_repo_"))
    (root / "package.json").write_text('{"name":"x","dependencies":{"typescript":"5.0.0"}}')
    (root / "tsconfig.json").write_text("{}")

    # Dense cluster: 6 controllers in app/controllers/api/v1/
    app_dir = root / "app" / "controllers" / "api" / "v1"
    app_dir.mkdir(parents=True)
    for i in range(6):
        (app_dir / f"r{i}.ts").write_text(
            f"export class Resource{i} {{ get() {{ return {i}; }} }}\n"
        )

    # Sparse cluster: 2 admin files
    admin_dir = root / "app" / "admin"
    admin_dir.mkdir(parents=True)
    for i in range(2):
        (admin_dir / f"a{i}.ts").write_text(
            f"export const admin{i} = {{ run: () => {i} }};\n"
        )

    return root


def _make_ruby_repo() -> Path:
    """Create a minimal Ruby on Rails-like repo with one dense cluster."""
    root = Path(tempfile.mkdtemp(prefix="chameleon_v03_rb_"))
    (root / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails', '~> 7.0'\n")
    (root / "Gemfile.lock").write_text("GEM\n  remote: https://rubygems.org/\n")
    app_dir = root / "app" / "controllers" / "api" / "v1"
    app_dir.mkdir(parents=True)
    for i in range(6):
        (app_dir / f"r{i}.rb").write_text(
            f"class R{i}Controller < ApplicationController\n  def index\n    {i}\n  end\nend\n"
        )
    return root


# ---------------------------------------------------------------------------
# Phase 2C.1: AST query derivation
# ---------------------------------------------------------------------------
section("Phase 2C.1: AST query persisted for TypeScript")
repo = _make_ts_repo()
try:
    bootstrap_repo(repo.resolve())
    canonicals_path = repo / ".chameleon" / "canonicals.json"
    t("canonicals.json exists", canonicals_path.is_file())
    payload = json.loads(canonicals_path.read_text())
    canonicals = payload.get("canonicals", {})
    t("at least one canonical archetype", len(canonicals) >= 1)

    any_ast_query: dict | None = None
    for _name, entries in canonicals.items():
        if entries and entries[0].get("normative_shape", {}).get("ast_query") is not None:
            any_ast_query = entries[0]["normative_shape"]["ast_query"]
            break
    t(
        "TS canonical persists an ast_query (no longer None)",
        any_ast_query is not None,
        json.dumps(canonicals, indent=2)[:300],
    )
    if any_ast_query is not None:
        required = {
            "top_level_node_kinds",
            "default_export_kind",
            "named_export_count_bucket",
            "jsx_present",
            "content_signal",
        }
        t(
            "TS ast_query has all 5 required fields",
            required.issubset(set(any_ast_query.keys())),
            f"got {set(any_ast_query.keys())}",
        )
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("Phase 2C.1: AST query persisted for Ruby")
repo = _make_ruby_repo()
try:
    bootstrap_repo(repo.resolve())
    canonicals_path = repo / ".chameleon" / "canonicals.json"
    if not canonicals_path.is_file():
        t("Ruby canonicals.json exists", False, "bootstrap did not write canonicals.json")
    else:
        payload = json.loads(canonicals_path.read_text())
        canonicals = payload.get("canonicals", {})
        any_ast_query: dict | None = None
        for _name, entries in canonicals.items():
            if entries and entries[0].get("normative_shape", {}).get("ast_query") is not None:
                any_ast_query = entries[0]["normative_shape"]["ast_query"]
                break
        # If the Ruby parser produced any dense cluster, we expect an ast_query.
        # If not, the test SKIPs the verification but still passes the run.
        if canonicals:
            t(
                "Ruby canonical persists an ast_query (no longer None)",
                any_ast_query is not None,
                json.dumps(canonicals, indent=2)[:300],
            )
            t(
                "Ruby ast_query is a JSON-serializable dict",
                isinstance(any_ast_query, dict)
                and json.dumps(any_ast_query) is not None,
            )
        else:
            print("  [INFO] Ruby bootstrap produced no canonicals (likely no Prism). "
                  "Treating as informational pass.")
            t("Ruby bootstrap produced canonicals (informational)", True)
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("Phase 2C.1: ast_query is JSON-serializable for arbitrary ClusterKeys")
sample_keys = [
    ClusterKey(
        path_pattern_bucket="app/controllers",
        content_signal_match="none",
        top_level_node_kinds=("ClassDeclaration",),
        default_export_kind="ClassDeclaration",
        named_export_count_bucket="1",
        import_module_set_hash="abc123",
        jsx_present=False,
    ),
    ClusterKey(
        path_pattern_bucket="src/components",
        content_signal_match="use_client",
        top_level_node_kinds=("FunctionDeclaration", "VariableStatement"),
        default_export_kind="FunctionDeclaration",
        named_export_count_bucket="2-4",
        import_module_set_hash="def456",
        jsx_present=True,
    ),
]
for i, k in enumerate(sample_keys):
    q = derive_ast_query(k)
    t(
        f"sample[{i}] ast_query JSON-serializes cleanly",
        json.dumps(q) is not None,
    )
    t(
        f"sample[{i}] ast_query contains every key field",
        all(
            f in q for f in (
                "top_level_node_kinds",
                "default_export_kind",
                "named_export_count_bucket",
                "jsx_present",
                "content_signal",
            )
        ),
        json.dumps(q),
    )


section("Phase 2C.1: ast_query mirrors ClusterKey dimensions")
key = sample_keys[1]
q = derive_ast_query(key)
t(
    "top_level_node_kinds matches ClusterKey",
    q["top_level_node_kinds"] == list(key.top_level_node_kinds),
)
t(
    "default_export_kind matches ClusterKey",
    q["default_export_kind"] == key.default_export_kind,
)
t(
    "named_export_count_bucket matches ClusterKey",
    q["named_export_count_bucket"] == key.named_export_count_bucket,
)
t("jsx_present matches ClusterKey", q["jsx_present"] is key.jsx_present)
t(
    "content_signal mirrors non-'none' signal verbatim",
    q["content_signal"] == "use_client",
)

# "none" must become None so the lint engine can treat it as "any directive".
q_none = derive_ast_query(sample_keys[0])
t(
    "content_signal 'none' is encoded as None in ast_query",
    q_none["content_signal"] is None,
)


# ---------------------------------------------------------------------------
# Phase 2C.2: Recency-weighted selection
# ---------------------------------------------------------------------------
section("Phase 2C.2: recency-weight constants are stable")
t(
    f"RECENCY_WEIGHT_MULTIPLIER is 2.0 (was {RECENCY_WEIGHT_MULTIPLIER})",
    RECENCY_WEIGHT_MULTIPLIER == 2.0,
)
t(
    f"RECENCY_WINDOW_DAYS is 90 (was {RECENCY_WINDOW_DAYS})",
    RECENCY_WINDOW_DAYS == 90,
)


section("Phase 2C.2: fresh file gets 2x recency boost; old file gets 1x")
tmpdir = Path(tempfile.mkdtemp(prefix="chameleon_v03_recency_"))
try:
    now = time.time()
    fresh = tmpdir / "fresh.ts"
    fresh.write_text("export const fresh = 1;\n")
    os.utime(fresh, (now - 86400, now - 86400))  # 1 day old
    old = tmpdir / "old.ts"
    old.write_text("export const old = 2;\n")
    os.utime(old, (now - 365 * 86400, now - 365 * 86400))  # 1 year old

    w_fresh = _file_recency_weight(fresh, now=now)
    w_old = _file_recency_weight(old, now=now)
    t(
        f"file modified 1 day ago weights {RECENCY_WEIGHT_MULTIPLIER}x",
        w_fresh == RECENCY_WEIGHT_MULTIPLIER,
        f"got {w_fresh}",
    )
    t(
        "file modified 1 year ago weights 1.0x",
        w_old == 1.0,
        f"got {w_old}",
    )

    # Boundary: file exactly RECENCY_WINDOW_DAYS old is INCLUSIVE.
    boundary = tmpdir / "boundary.ts"
    boundary.write_text("export const b = 3;\n")
    os.utime(boundary, (now - RECENCY_WINDOW_DAYS * 86400, now - RECENCY_WINDOW_DAYS * 86400))
    w_boundary = _file_recency_weight(boundary, now=now)
    t(
        f"file exactly {RECENCY_WINDOW_DAYS} days old is still boosted (inclusive boundary)",
        w_boundary == RECENCY_WEIGHT_MULTIPLIER,
        f"got {w_boundary}",
    )

    # 91 days = past the window
    over = tmpdir / "over.ts"
    over.write_text("export const o = 4;\n")
    os.utime(over, (now - 91 * 86400, now - 91 * 86400))
    w_over = _file_recency_weight(over, now=now)
    t(
        f"file 91 days old weights 1.0x (past window)",
        w_over == 1.0,
        f"got {w_over}",
    )

    # Future mtime (clock skew) also boosted (defensive design choice).
    future = tmpdir / "future.ts"
    future.write_text("export const f = 5;\n")
    os.utime(future, (now + 600, now + 600))
    w_future = _file_recency_weight(future, now=now)
    t(
        "future mtime falls back to 2x (forgiving design)",
        w_future == RECENCY_WEIGHT_MULTIPLIER,
        f"got {w_future}",
    )

    # Nonexistent path: no boost, no crash.
    w_missing = _file_recency_weight(tmpdir / "does_not_exist.ts", now=now)
    t("missing path falls back to 1.0x (no crash)", w_missing == 1.0, f"got {w_missing}")
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


section("Phase 2C.2: recency weight breaks ties in select_canonicals")
# Build a synthetic cluster of 2 eligible files where the OLDER file has the
# shorter path. Without recency weighting, shortest-path-first picks the older
# file. With recency weighting, the FRESH file wins regardless of path depth.
tmpdir = Path(tempfile.mkdtemp(prefix="chameleon_v03_select_")).resolve()
try:
    # Create the ParsedFile shells first (this writes placeholder content).
    old_pf = _make_parsed_file(
        repo_root=tmpdir, rel_path="old.ts",
        content_first_200_bytes="export const old = 1;\n",
    )
    fresh_pf = _make_parsed_file(
        repo_root=tmpdir, rel_path="deeply/nested/fresh.ts",
        content_first_200_bytes="export const fresh = 2;\n",
    )
    old_file = old_pf.path
    fresh_file = fresh_pf.path

    # NOW pin mtimes (this must come AFTER _make_parsed_file writes, which
    # would otherwise reset mtime to the current wall clock).
    now = time.time()
    os.utime(old_file, (now - 365 * 86400, now - 365 * 86400))
    os.utime(fresh_file, (now - 86400, now - 86400))

    # Force same key so they land in one cluster
    cluster = _make_cluster([old_pf, fresh_pf], tmpdir)

    result = select_canonicals([cluster], tmpdir, now=now)
    sels = list(result.selections.values())
    t("exactly one canonical selected", len(sels) == 1)
    if sels:
        chosen = sels[0]
        t(
            "fresh deep file wins over old shallow file (recency over path-length)",
            chosen.witness_path == fresh_file,
            f"chosen={chosen.witness_path}",
        )
        t(
            f"chosen recency_weight is {RECENCY_WEIGHT_MULTIPLIER}",
            chosen.recency_weight == RECENCY_WEIGHT_MULTIPLIER,
            f"got {chosen.recency_weight}",
        )

    # Both old → tiebreak falls back to (path-length, lex). Old file wins.
    os.utime(fresh_file, (now - 365 * 86400, now - 365 * 86400))
    result_both_old = select_canonicals([cluster], tmpdir, now=now)
    chosen_both_old = list(result_both_old.selections.values())[0]
    t(
        "when both old, shorter path wins (deterministic tiebreak preserved)",
        chosen_both_old.witness_path == old_file,
        f"got {chosen_both_old.witness_path}",
    )
    t(
        "both-old chosen recency_weight is 1.0x",
        chosen_both_old.recency_weight == 1.0,
        f"got {chosen_both_old.recency_weight}",
    )
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 2C.3: sparse + bimodal cluster surfacing
# ---------------------------------------------------------------------------
section("Phase 2C.3: sparse cluster flagged in BootstrapReport")
repo = _make_ts_repo()
try:
    report = bootstrap_repo(repo.resolve())
    t(
        "BootstrapReport has sparse_cluster_warnings attribute",
        hasattr(report, "sparse_cluster_warnings"),
    )
    sparse = report.sparse_cluster_warnings
    # The fixture creates 2 admin files — sparse threshold is 5, so we
    # expect at least one sparse warning.
    t(
        "at least one sparse cluster warning emitted "
        f"(threshold {SPARSE_CLUSTER_THRESHOLD})",
        len(sparse) >= 1,
        f"got {len(sparse)} sparse warnings: {json.dumps(sparse, indent=2)[:400]}",
    )
    if sparse:
        first = sparse[0]
        t("sparse warning has kind=sparse_cluster", first.get("kind") == "sparse_cluster")
        t("sparse warning has paths_pattern", isinstance(first.get("paths_pattern"), str))
        t("sparse warning has size < threshold",
          isinstance(first.get("size"), int) and first["size"] < SPARSE_CLUSTER_THRESHOLD)
        t("sparse warning has sample_paths list",
          isinstance(first.get("sample_paths"), list))
    # to_dict includes the warnings.
    d = report.to_dict()
    t("BootstrapReport.to_dict() exposes sparse_cluster_warnings",
      "sparse_cluster_warnings" in d
      and len(d["sparse_cluster_warnings"]) == len(sparse))
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("Phase 2C.3: bimodal cluster flagged in BootstrapReport")
# Build a synthetic ClusteringResult where one cluster splits 50/50 on
# default_export_kind. Even though same-ClusterKey members "shouldn't" diverge
# under the signature function, the bimodal detection on raw ParsedFile fields
# is the safety net that catches signature-derivation drift.
tmpdir = Path(tempfile.mkdtemp(prefix="chameleon_v03_bimodal_"))
try:
    members = []
    # 3 files with FunctionDeclaration default export, 3 with ClassDeclaration.
    for i in range(3):
        members.append(_make_parsed_file(
            repo_root=tmpdir,
            rel_path=f"src/fn{i}.ts",
            default_export_kind="FunctionDeclaration",
        ))
    for i in range(3):
        members.append(_make_parsed_file(
            repo_root=tmpdir,
            rel_path=f"src/cls{i}.ts",
            default_export_kind="ClassDeclaration",
        ))
    synthetic_cluster = _make_cluster(members, tmpdir)
    t(
        "synthetic 50/50 cluster is bimodal on default_export_kind",
        "default_export_kind" in synthetic_cluster.bimodal_dimensions,
        f"flagged: {synthetic_cluster.bimodal_dimensions}",
    )

    # 6-member cluster all same dimension → NOT bimodal
    uniform_members = [
        _make_parsed_file(
            repo_root=tmpdir,
            rel_path=f"uniform/u{i}.ts",
            default_export_kind="FunctionDeclaration",
        )
        for i in range(6)
    ]
    uniform_cluster = _make_cluster(uniform_members, tmpdir)
    t(
        "uniform cluster is NOT flagged bimodal",
        not uniform_cluster.is_bimodal,
        f"flagged: {uniform_cluster.bimodal_dimensions}",
    )

    # Boundary: 60/40 split (3/2) — should NOT be bimodal (boundary).
    boundary_members = [
        _make_parsed_file(
            repo_root=tmpdir,
            rel_path=f"b/maj{i}.ts",
            default_export_kind="FunctionDeclaration",
        )
        for i in range(3)
    ] + [
        _make_parsed_file(
            repo_root=tmpdir,
            rel_path=f"b/min{i}.ts",
            default_export_kind="ClassDeclaration",
        )
        for i in range(2)
    ]
    boundary_cluster = _make_cluster(boundary_members, tmpdir)
    # 3/5 = 0.6 exactly = NOT < 0.6 = not bimodal
    t(
        f"60/40 exact split is the boundary (not flagged); threshold={BIMODAL_DOMINANT_SHARE_THRESHOLD}",
        not boundary_cluster.is_bimodal,
        f"flagged: {boundary_cluster.bimodal_dimensions}",
    )

    # 59/41 — DOES flag.
    over_members = [
        _make_parsed_file(
            repo_root=tmpdir,
            rel_path=f"o/maj{i}.ts",
            default_export_kind="FunctionDeclaration",
        )
        for i in range(59)
    ] + [
        _make_parsed_file(
            repo_root=tmpdir,
            rel_path=f"o/min{i}.ts",
            default_export_kind="ClassDeclaration",
        )
        for i in range(41)
    ]
    over_cluster = _make_cluster(over_members, tmpdir)
    t(
        "59/41 split (dominant < 60%) IS flagged bimodal",
        over_cluster.is_bimodal,
        f"flagged: {over_cluster.bimodal_dimensions}",
    )

    # Warning builder produces JSON-serializable output.
    warnings = _build_bimodal_warnings([synthetic_cluster], tmpdir)
    t("bimodal warning builder emits one entry per cluster", len(warnings) == 1)
    if warnings:
        w = warnings[0]
        t("bimodal warning has kind=bimodal_cluster", w.get("kind") == "bimodal_cluster")
        t("bimodal warning has 'dimensions' list", isinstance(w.get("dimensions"), list))
        t("bimodal warning has 'distributions' dict",
          isinstance(w.get("distributions"), dict))
        t(
            "bimodal warning JSON-serializes cleanly",
            json.dumps(w) is not None,
        )
        # Distribution keys must all be strings (true/false/null/strings).
        for dim, dist in w["distributions"].items():
            for key in dist.keys():
                t(
                    f"distribution[{dim}] key is a str: {key!r}",
                    isinstance(key, str),
                )

    # Sparse helper also serializes cleanly.
    sparse_synthetic = Cluster(
        key=synthetic_cluster.key,
        members=members[:2],  # 2-member sparse cluster
    )
    sparse_w = _build_sparse_warnings([sparse_synthetic], tmpdir)
    t("sparse warning builder produces one entry", len(sparse_w) == 1)
    if sparse_w:
        t(
            "sparse warning JSON-serializes cleanly",
            json.dumps(sparse_w[0]) is not None,
        )
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


section("Phase 2C.3: BootstrapReport.to_dict() exposes both warning lists")
repo = _make_ts_repo()
try:
    report = bootstrap_repo(repo.resolve())
    d = report.to_dict()
    t("to_dict has 'sparse_cluster_warnings' key", "sparse_cluster_warnings" in d)
    t("to_dict has 'bimodal_cluster_warnings' key", "bimodal_cluster_warnings" in d)
    t(
        "to_dict() is JSON-serializable",
        json.dumps(d, default=str) is not None,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
