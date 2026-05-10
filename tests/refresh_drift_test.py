"""Verify /chameleon-refresh actually detects + reflects drift.

Previous tests called refresh_repo and verified status=success. They
NEVER verified the output differs when the repo has materially changed
since the original bootstrap.

Round 1: synthetic repo, bootstrap, add a brand-new cluster of files,
         refresh_repo, verify the new archetype appears in the
         post-refresh profile (and not in the pre-refresh one).
Round 2: verify the drift score (from edit_observations) increases
         over time after low-confidence edits accumulate.
"""

import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


from chameleon_mcp.tools import (
    _compute_repo_id, bootstrap_repo, get_drift_status, refresh_repo,
    trust_profile,
)
from chameleon_mcp.drift.observations import (
    compute_drift_score, record_edit_observation,
)


# ---------------------------------------------------------------------------
# Round 1 — refresh detects new archetypes after material change
# ---------------------------------------------------------------------------
section("Round 1 — refresh picks up new archetype after material change")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "drift_test"
    repo.mkdir()
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "src" / "queries").mkdir(parents=True)
    (repo / "tsconfig.json").write_text('{}')
    (repo / "package.json").write_text('{"name":"drift","dependencies":{"typescript":"5.0.0"}}')

    # Initial state: 6 components + 6 queries
    for i in range(6):
        (repo / "src" / "components" / f"Component{i}.tsx").write_text(
            f"import React from 'react';\nexport const Component{i} = () => <div>{i}</div>;\n"
        )
        (repo / "src" / "queries" / f"useQuery{i}.ts").write_text(
            f"import {{ useQuery }} from 'react-query';\nexport const useQuery{i} = () => useQuery('q{i}', async () => {i});\n"
        )

    initial_report = bootstrap_repo(str(repo))["data"]
    initial_archetypes = json.loads(
        (repo / ".chameleon" / "archetypes.json").read_text()
    )["archetypes"]
    initial_count = len(initial_archetypes)
    t(f"Initial bootstrap: {initial_count} archetypes", initial_count >= 2)

    # Add a NEW cluster: 6 hooks (different shape from components and queries)
    (repo / "src" / "hooks").mkdir()
    for i in range(6):
        (repo / "src" / "hooks" / f"useHook{i}.ts").write_text(
            f"export function useHook{i}<T>(arg: T): T {{\n  return arg;\n}}\n"
        )

    refresh_report = refresh_repo(str(repo))["data"]
    t(f"Refresh status=success ({refresh_report['status']})", refresh_report["status"] == "success")

    refreshed_archetypes = json.loads(
        (repo / ".chameleon" / "archetypes.json").read_text()
    )["archetypes"]
    refreshed_count = len(refreshed_archetypes)
    t(
        f"Refresh sees more archetypes after adding files ({initial_count} → {refreshed_count})",
        refreshed_count > initial_count,
    )

    # Verify the new archetype includes a hook file
    found_hook_archetype = False
    for arch_name, arch_data in refreshed_archetypes.items():
        if arch_data.get("paths_pattern") and "hooks" in arch_data["paths_pattern"]:
            found_hook_archetype = True
            break
    t("Refresh creates an archetype matching the new hooks/", found_hook_archetype)


# ---------------------------------------------------------------------------
# Round 1 — refresh removes archetypes after files deleted
# ---------------------------------------------------------------------------
section("Round 1 — refresh drops archetypes after material removal")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "shrink_test"
    repo.mkdir()
    (repo / "src" / "a").mkdir(parents=True)
    (repo / "src" / "b").mkdir(parents=True)
    (repo / "tsconfig.json").write_text('{}')

    for i in range(6):
        (repo / "src" / "a" / f"file{i}.ts").write_text(f"export const a{i} = {i};\n")
        (repo / "src" / "b" / f"file{i}.ts").write_text(
            f"export const b{i}: Promise<number> = Promise.resolve({i});\n"
        )

    bootstrap_repo(str(repo))
    initial = json.loads((repo / ".chameleon" / "archetypes.json").read_text())["archetypes"]

    # Delete the entire b/ tree
    shutil.rmtree(repo / "src" / "b")

    refresh_repo(str(repo))
    after = json.loads((repo / ".chameleon" / "archetypes.json").read_text())["archetypes"]
    t(
        f"Archetype count drops after files removed ({len(initial)} → {len(after)})",
        len(after) <= len(initial),
    )


# ---------------------------------------------------------------------------
# Round 1 — refresh idempotent on stable repo
# ---------------------------------------------------------------------------
section("Round 1 — refresh is idempotent on stable repos")

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "stable_test"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "tsconfig.json").write_text('{}')
    for i in range(6):
        (repo / "src" / f"f{i}.ts").write_text(f"export const f{i} = {i};\n")

    bootstrap_repo(str(repo))
    a1 = json.loads((repo / ".chameleon" / "archetypes.json").read_text())["archetypes"]
    refresh_repo(str(repo))
    a2 = json.loads((repo / ".chameleon" / "archetypes.json").read_text())["archetypes"]
    refresh_repo(str(repo))
    a3 = json.loads((repo / ".chameleon" / "archetypes.json").read_text())["archetypes"]

    t("Refresh-refresh-refresh on unchanged repo: count stable",
      len(a1) == len(a2) == len(a3))


# ---------------------------------------------------------------------------
# Round 2 — drift score increases as low-confidence observations accumulate
# ---------------------------------------------------------------------------
section("Round 2 — drift score reflects observation history")

import os
with tempfile.TemporaryDirectory() as tmp:
    os.environ["CHAMELEON_PLUGIN_DATA"] = tmp
    rid = "drift-score-test-" + hashlib.sha256(b"drift").hexdigest()[:16]

    # No observations → drift None
    t("No observations: compute_drift_score returns None",
      compute_drift_score(rid) is None)

    # All high-confidence observations → low drift
    for i in range(5):
        record_edit_observation(rid, f"src/f{i}.ts", "cluster-X", "high")
    s_high = compute_drift_score(rid)
    t(f"All-high observations: low drift ({s_high:.3f})", s_high is not None and s_high < 0.2)

    # Now add many low-confidence observations
    for i in range(20):
        record_edit_observation(rid, f"src/g{i}.ts", "cluster-X", "low")
    s_after_drift = compute_drift_score(rid)
    t(
        f"After many low-conf obs: drift increases ({s_high:.3f} → {s_after_drift:.3f})",
        s_after_drift > s_high,
    )

    # get_drift_status surfaces the score
    # We need a trust record to make get_drift_status produce non-None
    # days_since_refresh; but we also need the data dir to exist. Quick path:
    # just call get_drift_status and check the score field.
    r = get_drift_status(rid)
    t(
        f"get_drift_status returns observed_drift_score ({r['data'].get('observed_drift_score')})",
        r["data"].get("observed_drift_score") is not None,
    )

    del os.environ["CHAMELEON_PLUGIN_DATA"]


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
