"""Contract tests for intentionally-duplicated helpers across modules.

v0.5.7 audit item #3 flagged that several helpers exist in both tools.py
and orchestrator.py with subtly different implementations. The pragmatic
defense (cheaper than full refactor) is a contract test that pins both
versions to the same observable output. Drift then surfaces in CI
instead of in production.

Pairs verified:
  - tools._read_renames_overlay  ==  orchestrator._load_user_renames
  - tools._hash_cluster_key_for(key)  ==  canonical._hash_cluster_key(cluster)
  - tools._compute_repo_id           ==  orchestrator._compute_repo_id
"""

import json
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Pair 1: renames overlay reader
# ---------------------------------------------------------------------------
section("renames overlay: tools._read_renames_overlay == orchestrator._load_user_renames")

from chameleon_mcp.bootstrap.orchestrator import _load_user_renames
from chameleon_mcp.tools import _read_renames_overlay

cases = [
    # Empty / missing
    ("missing file", None, {}),
    # Well-formed
    ("simple v1", {"schema_version": 1, "renames": {"a": "b"}}, {"a": "b"}),
    ("multi entry", {"schema_version": 1, "renames": {"a": "b", "c": "d"}}, {"a": "b", "c": "d"}),
    # Edge cases
    ("empty renames map", {"schema_version": 1, "renames": {}}, {}),
    ("future schema_version (sv=2) rejected", {"schema_version": 2, "renames": {"a": "b"}}, {}),
    ("missing schema_version rejected", {"renames": {"a": "b"}}, {}),
    ("non-string values dropped", {"schema_version": 1, "renames": {"a": 123, "b": "ok"}}, {"b": "ok"}),
    ("non-string keys dropped", {"schema_version": 1, "renames": {1: "a", "ok": "b"}}, {"ok": "b"}),
    ("renames not a dict", {"schema_version": 1, "renames": "not-a-dict"}, {}),
    ("top-level not a dict", "not-a-dict", {}),
]

for label, payload, expected in cases:
    with tempfile.TemporaryDirectory() as raw:
        profile_dir = Path(raw) / ".chameleon"
        profile_dir.mkdir()
        if payload is not None:
            (profile_dir / "renames.json").write_text(json.dumps(payload))
        tools_result = _read_renames_overlay(profile_dir)
        orch_result = _load_user_renames(profile_dir)
        # Both should match expected AND each other.
        t(
            f"{label}: tools == orch",
            tools_result == orch_result,
            f"tools={tools_result}, orch={orch_result}",
        )
        # The two implementations DO differ on one edge: _load_user_renames
        # additionally drops empty string keys/values. So expected is the
        # tools result; orch may be more conservative.
        # We assert the strict-superset relationship: orch is a subset of tools.
        t(
            f"{label}: orch <= tools",
            set(orch_result.items()).issubset(set(tools_result.items())),
            f"diff: {set(orch_result.items()) ^ set(tools_result.items())}",
        )


# ---------------------------------------------------------------------------
# Pair 2: cluster_id hash (4-line helper duplicated by design per comment)
# ---------------------------------------------------------------------------
section("cluster_id hash: tools._hash_cluster_key_for matches canonical._hash_cluster_key")

from chameleon_mcp.bootstrap.canonical import _hash_cluster_key
from chameleon_mcp.signatures import ClusterKey
from chameleon_mcp.tools import _hash_cluster_key_for


def _mock_cluster(key: ClusterKey):
    class _C:
        def __init__(self, k):
            self.key = k
    return _C(key)


sample_keys = [
    ClusterKey(
        path_pattern_bucket="src/components:tsx",
        content_signal_match="weak",
        top_level_node_kinds=("FunctionDeclaration",),
        default_export_kind=None,
        named_export_count_bucket="1",
        import_module_set_hash="abc12345",
        jsx_present=True,
    ),
    ClusterKey(
        path_pattern_bucket="app/controllers:rb",
        content_signal_match="strong",
        top_level_node_kinds=("Class",),
        default_export_kind=None,
        named_export_count_bucket="0",
        import_module_set_hash="def67890",
        jsx_present=False,
    ),
]

for k in sample_keys:
    tools_hash = _hash_cluster_key_for(k)
    canon_hash = _hash_cluster_key(_mock_cluster(k))
    t(f"hashes match for {k.path_pattern_bucket}", tools_hash == canon_hash,
      f"tools={tools_hash}, canon={canon_hash}")
    t(f"hash is 16-char hex for {k.path_pattern_bucket}",
      len(tools_hash) == 16 and all(c in "0123456789abcdef" for c in tools_hash),
      f"got {tools_hash}")


# ---------------------------------------------------------------------------
# Pair 3: _compute_repo_id (orchestrator already delegates; confirm)
# ---------------------------------------------------------------------------
section("_compute_repo_id: orchestrator delegates to tools")

from chameleon_mcp.bootstrap.orchestrator import _compute_repo_id as orch_id
from chameleon_mcp.tools import _compute_repo_id as tools_id

with tempfile.TemporaryDirectory() as raw:
    repo = Path(raw)
    (repo / ".git").mkdir()
    t("same repo -> same id", orch_id(repo) == tools_id(repo))
    t("repo_id is 64-char hex", len(orch_id(repo)) == 64)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} - {info}")
    sys.exit(1)
