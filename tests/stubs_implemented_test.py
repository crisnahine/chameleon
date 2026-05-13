"""Verification of #3: drift.db population + merge_profiles + daemon deferral.

Round 1: unit tests for record_edit_observation, compute_drift_score, and
         merge_profiles real implementation.
Round 2: end-to-end — run hook with a real test file, verify drift.db row
         appears, verify get_drift_status reflects observation, exercise
         merge_profiles on real profile JSONs.
"""

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from _test_config import RUBY_REPO, TS_REPO

PASS, FAIL = [], []
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


# Preconditions — order-independent: ensure both test repos are bootstrapped
# + trusted before any test that reads their .chameleon state.
import os as _os

if _os.environ.get("CHAMELEON_PLUGIN_DATA"):
    del _os.environ["CHAMELEON_PLUGIN_DATA"]
from chameleon_mcp.tools import bootstrap_repo as _bs
from chameleon_mcp.tools import trust_profile as _tp

if not (TS_REPO / ".chameleon" / "profile.json").is_file():
    _bs(str(TS_REPO))
_tp(str(TS_REPO), "client")
if RUBY_REPO.is_dir() and not (RUBY_REPO / ".chameleon" / "profile.json").is_file():
    _bs(str(RUBY_REPO))
if RUBY_REPO.is_dir():
    _tp(str(RUBY_REPO), "api")


# ---------------------------------------------------------------------------
# Round 1 — drift.db unit tests
# ---------------------------------------------------------------------------
section("Round 1 — drift.db unit tests")

import os

from chameleon_mcp.drift.observations import (
    _drift_db_path,
    compute_drift_score,
    record_edit_observation,
)

with tempfile.TemporaryDirectory() as tmp:
    os.environ["CHAMELEON_PLUGIN_DATA"] = tmp
    repo_id = "drift-test-" + hashlib.sha256(b"x").hexdigest()[:16]

    record_edit_observation(repo_id, "src/foo.ts", "cluster-A", "high")
    record_edit_observation(repo_id, "src/foo.ts", "cluster-A", "high", matched_canonical=True)
    record_edit_observation(repo_id, "src/bar.ts", "cluster-B", "low")

    db_path = _drift_db_path(repo_id)
    t("drift.db file created", db_path.is_file())

    conn = sqlite3.connect(str(db_path))
    obs_count = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()[0]
    files_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    t(f"3 edit_observations rows written (got {obs_count})", obs_count == 3)
    t(f"2 files rows (deduped by rel_path) (got {files_count})", files_count == 2)

    score = compute_drift_score(repo_id)
    # Three observations: confidence 0.95, 0.95, 0.3 → mean ~0.73 → drift ~0.27
    t(f"compute_drift_score returns non-None ({score})", score is not None)
    t(f"compute_drift_score in expected range (got {score:.2f})", 0.15 < score < 0.4)

    del os.environ["CHAMELEON_PLUGIN_DATA"]


# ---------------------------------------------------------------------------
# Round 1 — empty observations returns None
# ---------------------------------------------------------------------------
section("Round 1 — drift.db edge cases")

with tempfile.TemporaryDirectory() as tmp:
    os.environ["CHAMELEON_PLUGIN_DATA"] = tmp
    repo_id = "empty-" + hashlib.sha256(b"y").hexdigest()[:16]

    score = compute_drift_score(repo_id)
    t("compute_drift_score returns None when no observations", score is None)

    # record_edit_observation with empty repo_id is a no-op
    record_edit_observation("", "src/foo.ts", "cluster-A", "high")
    t(
        "record_edit_observation no-op on empty repo_id",
        not (Path(tmp) / "" / "drift.db").exists(),
    )

    del os.environ["CHAMELEON_PLUGIN_DATA"]


# ---------------------------------------------------------------------------
# Round 1 — merge_profiles unit tests
# ---------------------------------------------------------------------------
section("Round 1 — merge_profiles unit tests")

from chameleon_mcp.tools import merge_profiles

with tempfile.TemporaryDirectory() as tmp:
    base_path = Path(tmp) / "base.json"
    ours_path = Path(tmp) / "ours.json"
    theirs_path = Path(tmp) / "theirs.json"

    base_path.write_text(json.dumps({
        "schema_version": 4,
        "generation": 1,
        "archetypes": {"cluster-A": {"cluster_size": 3, "canonical_witness": "a.ts"}},
    }))
    ours_path.write_text(json.dumps({
        "schema_version": 4,
        "generation": 2,
        "archetypes": {
            "cluster-A": {"cluster_size": 5, "canonical_witness": "a.ts"},
            "cluster-B": {"cluster_size": 4, "canonical_witness": "b.ts"},
        },
    }))
    theirs_path.write_text(json.dumps({
        "schema_version": 4,
        "generation": 3,
        "archetypes": {
            "cluster-A": {"cluster_size": 7, "canonical_witness": "a.ts"},  # higher size; should win
            "cluster-C": {"cluster_size": 2, "canonical_witness": "c.ts"},
        },
    }))

    r = merge_profiles("repo-x", str(base_path), str(ours_path), str(theirs_path))
    t("merge_profiles returns success", r["data"]["status"] == "success")
    t(f"merged archetype count = 3 (got {r['data']['merged_archetype_count']})",
      r["data"]["merged_archetype_count"] == 3)

    merged = json.loads(ours_path.read_text())
    t(
        "Conflicting cluster-A resolved by higher cluster_size (theirs wins)",
        merged["archetypes"]["cluster-A"]["cluster_size"] == 7,
    )
    t(
        "Non-conflicting cluster-B preserved from ours",
        "cluster-B" in merged["archetypes"],
    )
    t(
        "Non-conflicting cluster-C added from theirs",
        "cluster-C" in merged["archetypes"],
    )


# ---------------------------------------------------------------------------
# Round 1 — merge_profiles failure paths
# ---------------------------------------------------------------------------
section("Round 1 — merge_profiles failure paths")

with tempfile.TemporaryDirectory() as tmp:
    # Missing files
    r = merge_profiles("repo-x", "/none1", "/none2", "/none3")
    t("merge_profiles fails on missing files", r["data"]["status"] == "failed")

    # Malformed JSON
    bad = Path(tmp) / "bad.json"
    bad.write_text("{not valid json")
    good = Path(tmp) / "good.json"
    good.write_text('{"archetypes": {}}')
    r = merge_profiles("repo-x", str(good), str(good), str(bad))
    t("merge_profiles fails on malformed JSON", r["data"]["status"] == "failed")


# ---------------------------------------------------------------------------
# Round 2 — end-to-end: hook records observation, get_drift_status reflects
# ---------------------------------------------------------------------------
section("Round 2 — end-to-end drift recording via preflight-and-advise")

import subprocess

env = os.environ.copy()
env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)

# Use a fresh PLUGIN_DATA so we can count observations precisely
with tempfile.TemporaryDirectory() as plugin_data_tmp:
    env["CHAMELEON_PLUGIN_DATA"] = plugin_data_tmp

    # Re-grant trust so trust record lands in the test plugin_data
    from chameleon_mcp.tools import trust_profile
    # We need to use the same env for the in-process call too
    os.environ["CHAMELEON_PLUGIN_DATA"] = plugin_data_tmp
    trust_profile(str(TS_REPO), TS_REPO.name)

    test_files = [
        TS_REPO / "src" / "components" / "base" / "SelectVettingStatus.tsx",
        TS_REPO / "src" / "queries" / "admin" / "users" / "create.ts",
        TS_REPO / "src" / "utils" / "balanceTransaction.ts",
    ]
    for f in test_files:
        hook_input = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(f)},
            "session_id": "drift-end2end",
        })
        subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "preflight-and-advise")],
            input=hook_input,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    from chameleon_mcp.tools import _compute_repo_id as _compute_repo_id_v6
    repo_id = _compute_repo_id_v6(TS_REPO)
    db_path = Path(plugin_data_tmp) / repo_id / "drift.db"
    t(f"drift.db created at expected path ({db_path})", db_path.is_file())

    if db_path.is_file():
        conn = sqlite3.connect(str(db_path))
        obs = conn.execute(
            "SELECT rel_path, archetype, confidence_observed FROM edit_observations"
        ).fetchall()
        conn.close()
        t(
            f"3 observations recorded from 3 hook invocations (got {len(obs)})",
            len(obs) == 3,
        )
        t(
            "Each observation has non-empty archetype",
            all(row[1] for row in obs),
        )
        t(
            "Each observation has non-zero confidence",
            all(row[2] > 0 for row in obs),
        )

    # get_drift_status should now reflect observations
    from chameleon_mcp.tools import get_drift_status
    r = get_drift_status(repo_id)
    score = r["data"].get("observed_drift_score")
    t(f"get_drift_status reports observed_drift_score (got {score})", score is not None)

    del os.environ["CHAMELEON_PLUGIN_DATA"]


# ---------------------------------------------------------------------------
# Round 2 — merge_profiles on real profile copies
# ---------------------------------------------------------------------------
section("Round 2 — merge_profiles on the test repo profile JSONs")

with tempfile.TemporaryDirectory() as tmp:
    # Copy real the TypeScript repo + the Ruby on Rails repo archetypes.json side by side
    ours = Path(tmp) / "ours.json"
    theirs = Path(tmp) / "theirs.json"
    base = Path(tmp) / "base.json"
    shutil.copy(TS_REPO / ".chameleon" / "archetypes.json", ours)
    shutil.copy(RUBY_REPO / ".chameleon" / "archetypes.json", theirs)
    shutil.copy(TS_REPO / ".chameleon" / "archetypes.json", base)

    ours_data = json.loads(ours.read_text())
    theirs_data = json.loads(theirs.read_text())
    ours_count = len(ours_data.get("archetypes", {}))
    theirs_count = len(theirs_data.get("archetypes", {}))

    r = merge_profiles("test-repo", str(base), str(ours), str(theirs))
    t("merge_profiles on real profiles succeeds", r["data"]["status"] == "success")
    merged_count = r["data"]["merged_archetype_count"]
    t(
        f"merged count >= max(ours={ours_count}, theirs={theirs_count}) (got {merged_count})",
        merged_count >= max(ours_count, theirs_count),
    )

    # Re-merging should be idempotent (same input → same output)
    r2 = merge_profiles("test-repo", str(base), str(ours), str(theirs))
    t(
        "merge_profiles is idempotent",
        r["data"]["merged_archetype_count"] == r2["data"]["merged_archetype_count"],
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
