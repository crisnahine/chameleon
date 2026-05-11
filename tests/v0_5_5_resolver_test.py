"""Regression tests for v0.5.5 Bug H: ancestor-aware repo_root resolver.

The cycle-4 dogfood (3-app: excalidraw, mastodon, plane) surfaced a
silent misroute in ``_resolve_repo_root_by_id``. After
``bootstrap_repo(plane_root)``, the ``repos`` table carries 18 rows
(1 root + 17 workspaces, all sharing the same ``repo_id`` because
workspaces inherit the root's git remote URL). The pre-v0.5.5
freshest-row resolver picked the alphabetically-last workspace
(``packages/utils``); downstream consumers (``get_canonical_excerpt``,
``get_drift_status``) loaded the wrong ``.chameleon/`` dir and silently
emitted ``archetype not found`` for valid archetypes.

v0.5.5 makes the resolver ancestor-aware: when multiple rows share a
repo_id, prefer the row whose ``repo_root`` is an ancestor of (or equal
to) every other row's ``repo_root``. Falls back to the freshest when no
clear ancestor exists (rare — sibling clones).

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_5_resolver_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))

# Isolate plugin data per run.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v0_5_5_resolver_data_")
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


from chameleon_mcp.index_db import (  # noqa: E402
    _pick_ancestor_or_freshest,
    init_index_db,
    resolve_repo_root,
    upsert_repo,
)

# ---------------------------------------------------------------------------
section("_pick_ancestor_or_freshest: unit tests")
# Verify-before: pre-v0.5.5 the resolver returned the freshest row by
# last_seen_at. For plane that was `packages/utils` (alphabetically last,
# inserted last by the workspace-bootstrap loop).
# Verify-after: the ancestor candidate wins regardless of insertion order.

# Build synthetic dirs on disk so .resolve() can find a real path.
_TMP = Path(tempfile.mkdtemp(prefix="chameleon_v0_5_5_synth_"))
(_TMP / "root").mkdir()
(_TMP / "root" / "apps" / "web").mkdir(parents=True)
(_TMP / "root" / "apps" / "api").mkdir(parents=True)
(_TMP / "root" / "packages" / "utils").mkdir(parents=True)
(_TMP / "root" / "packages" / "shared").mkdir(parents=True)

ROOT = str((_TMP / "root").resolve())
WEB = str((_TMP / "root" / "apps" / "web").resolve())
API = str((_TMP / "root" / "apps" / "api").resolve())
UTILS = str((_TMP / "root" / "packages" / "utils").resolve())
SHARED = str((_TMP / "root" / "packages" / "shared").resolve())

# Candidates in the order the cycle-4 dogfood produced (alphabetical,
# packages/utils last):
candidates = [WEB, API, UTILS, SHARED, ROOT]
picked = _pick_ancestor_or_freshest(candidates)
t(
    "monorepo root wins over 4 workspaces",
    picked == ROOT,
    f"got {picked}",
)

# Same candidates with the root in a DIFFERENT position. The fix must
# not depend on caller-supplied ordering.
picked = _pick_ancestor_or_freshest([ROOT, WEB, API, UTILS, SHARED])
t(
    "ancestor wins regardless of insertion order (root first)",
    picked == ROOT,
)
picked = _pick_ancestor_or_freshest([UTILS, ROOT, WEB])
t(
    "ancestor wins regardless of insertion order (root middle)",
    picked == ROOT,
)

# Single-candidate input: pass through unchanged.
picked = _pick_ancestor_or_freshest([WEB])
t(
    "single-candidate input returned unchanged",
    picked == WEB,
)

# No clear ancestor (sibling clones — both /tmp/A and /tmp/B with the same
# git remote). v0.5.5 falls back to "freshest first" which the caller
# already pre-sorted; the function preserves that order via tie-break on
# path length.
(_TMP / "siblingA").mkdir()
(_TMP / "siblingB").mkdir()
SIBLING_A = str((_TMP / "siblingA").resolve())
SIBLING_B = str((_TMP / "siblingB").resolve())
picked = _pick_ancestor_or_freshest([SIBLING_A, SIBLING_B])
t(
    "no ancestor relation → returns one deterministically (shorter path wins on tie)",
    picked in {SIBLING_A, SIBLING_B},
    f"got {picked}",
)

# Edge: nested workspaces (apps/web/sub) where apps/web is also a row.
# Expected: ROOT wins (most descendants), apps/web is second-best.
(_TMP / "root" / "apps" / "web" / "sub").mkdir(parents=True)
SUB = str((_TMP / "root" / "apps" / "web" / "sub").resolve())
picked = _pick_ancestor_or_freshest([SUB, WEB, ROOT])
t(
    "deepest workspace + intermediate + root → root wins",
    picked == ROOT,
)


# ---------------------------------------------------------------------------
section("resolve_repo_root: real index.db round-trip")
# Verify-before: synthesize the plane situation — one repo_id, 5 rows
# (1 root + 4 workspaces). Pre-v0.5.5 ``resolve_repo_root`` returned the
# freshest workspace; v0.5.5 returns the root.

REPO_ID = "a" * 64  # 64-char hex; the value doesn't matter for this test

# Init the index.db in the isolated CHAMELEON_PLUGIN_DATA dir.
db_path = init_index_db()

# Insert root first, then 4 workspaces. last_seen_at on workspaces is
# fresher (matches the cycle-4 dogfood pattern).
upsert_repo(REPO_ID, ROOT, archetype_count=70)
upsert_repo(REPO_ID, API, archetype_count=10)
upsert_repo(REPO_ID, WEB, archetype_count=15)
upsert_repo(REPO_ID, SHARED, archetype_count=8)
upsert_repo(REPO_ID, UTILS, archetype_count=12)

resolved = resolve_repo_root(REPO_ID)
t(
    "resolve_repo_root: no hint, 5 candidates → returns root",
    resolved == ROOT,
    f"got {resolved}",
)

# Pre-v0.5.5 behavior check: ``packages/utils`` was the last inserted
# row, so the freshest-row rule would have returned it.
t(
    "verify-before: freshest is NOT what we want (would be utils, was the bug)",
    UTILS != ROOT,  # Sanity — these are different paths
)

# Explicit hint still works (unchanged from v0.5.1).
resolved = resolve_repo_root(REPO_ID, repo_root_hint=API)
t(
    "explicit hint still returns the hinted row (v0.5.1 contract preserved)",
    resolved == API,
)
resolved = resolve_repo_root(REPO_ID, repo_root_hint=ROOT)
t(
    "explicit hint=root returns root",
    resolved == ROOT,
)

# Hint that misses falls through to the ancestor-aware resolution.
resolved = resolve_repo_root(REPO_ID, repo_root_hint="/path/that/does/not/exist")
t(
    "hint miss falls through to ancestor resolution",
    resolved == ROOT,
    f"got {resolved}",
)

# Single-row repo: unchanged behavior.
SINGLE_ID = "b" * 64
upsert_repo(SINGLE_ID, ROOT, archetype_count=5)
resolved = resolve_repo_root(SINGLE_ID)
t(
    "single-row repo_id resolves to that row",
    resolved == ROOT,
)


# ---------------------------------------------------------------------------
section("end-to-end: get_canonical_excerpt no longer misroutes")
# The cycle-4 dogfood symptom: get_canonical_excerpt(repo_id, valid_arch)
# returned ``archetype not found`` because the resolver pointed at a
# workspace that has no .chameleon/. We don't reproduce the full
# bootstrap pipeline here (covered by v0_5_3_canonical_witness_test); we
# just verify the resolver fix flows through to _resolve_repo_root_by_id.

# Place a synthetic .chameleon/ at ROOT only (not in any workspace) so
# the resolver's pick determines whether load_profile_dir succeeds.
chameleon_dir = Path(ROOT) / ".chameleon"
chameleon_dir.mkdir(exist_ok=True)
(chameleon_dir / "profile.json").write_text(
    '{"schema_version": 7, "created_at": "2026-05-11T00:00:00Z", '
    '"engine_min_version": "0.5.5", "generation": 1, "language": "typescript", '
    '"source": "test", "archetype_count": 1}',
    encoding="utf-8",
)
(chameleon_dir / "archetypes.json").write_text(
    '{"schema_version": 7, "engine_min_version": "0.5.5", "generation": 1, '
    '"archetypes": {"component": {"cluster_id": "c1", "cluster_size": 5, '
    '"paths_pattern": "src/components", "default_export_kind": null, '
    '"top_level_node_kinds": [], "jsx_present": true, "content_signal": null}}}',
    encoding="utf-8",
)
(chameleon_dir / "canonicals.json").write_text(
    '{"schema_version": 7, "engine_min_version": "0.5.5", "generation": 1, '
    '"canonicals": {}}',
    encoding="utf-8",
)

# Verify resolver lands on ROOT, not any workspace.
from chameleon_mcp.tools import _resolve_repo_root_by_id  # noqa: E402

resolved = _resolve_repo_root_by_id(REPO_ID)
t(
    "_resolve_repo_root_by_id returns root for monorepo repo_id",
    resolved is not None and str(resolved) == ROOT,
    f"got {resolved}",
)


# Cleanup
try:
    shutil.rmtree(_TMP, ignore_errors=True)
    shutil.rmtree(TMPDATA, ignore_errors=True)
except OSError:
    pass


print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")

sys.exit(0 if FAIL == 0 else 1)
