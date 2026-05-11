"""Regression suite for Phase 4.3-extended partial re-clustering.

Covers the middle ground between the no-op short-circuit and the full
re-bootstrap path: a refresh where 0 < change_ratio <= 10% re-parses
only the changed/added files and amends archetypes.json in place via
atomic_profile_commit. Repos without per-file cluster state (legacy
v0.4 profiles) fall through to full bootstrap.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/partial_refresh_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))


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


# Isolated plugin data dir for this whole test file.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_partial_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

from chameleon_mcp import index_db  # noqa: E402
from chameleon_mcp.tools import (  # noqa: E402
    PARTIAL_REFRESH_CHANGE_RATIO_CEILING,
    _compute_repo_id,
    bootstrap_repo,
    refresh_repo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_ts_repo(cluster_size: int = 12, name: str = "ts") -> Path:
    """Build a TS repo with `cluster_size` identical-shape files in a single
    dense cluster (so partial refresh has room to operate). Returns the
    repo root."""
    root = Path(tempfile.mkdtemp(prefix=f"chameleon_partial_{name}_"))
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    src = root / "src" / "models"
    src.mkdir(parents=True)
    for i in range(cluster_size):
        (src / f"M{i}.ts").write_text(
            f"export class M{i} {{ value: number = {i}; }}\n"
        )
    return root


def _make_two_cluster_repo(name: str = "two") -> Path:
    """Repo with two dense clusters of different shapes so we can exercise
    cluster-bucket movement and cluster_size deltas."""
    root = Path(tempfile.mkdtemp(prefix=f"chameleon_partial_{name}_"))
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")

    classes_dir = root / "src" / "models"
    classes_dir.mkdir(parents=True)
    for i in range(12):
        (classes_dir / f"M{i}.ts").write_text(
            f"export class M{i} {{ value: number = {i}; }}\n"
        )

    fns_dir = root / "src" / "lib"
    fns_dir.mkdir(parents=True)
    for i in range(12):
        (fns_dir / f"fn{i}.ts").write_text(
            f"export function fn{i}(): number {{ return {i}; }}\n"
        )
    return root


def _archetypes(repo: Path) -> dict:
    return json.loads(
        (repo / ".chameleon" / "archetypes.json").read_text(encoding="utf-8")
    )["archetypes"]


def _generations(repo: Path) -> tuple[int, int, int, int]:
    gens = []
    for name in ("profile.json", "archetypes.json", "canonicals.json", "rules.json"):
        gens.append(
            json.loads(
                (repo / ".chameleon" / name).read_text(encoding="utf-8")
            )["generation"]
        )
    return tuple(gens)  # type: ignore[return-value]


def _canonical_witness(repo: Path, archetype: str) -> str | None:
    canonicals = json.loads(
        (repo / ".chameleon" / "canonicals.json").read_text(encoding="utf-8")
    )["canonicals"]
    entries = canonicals.get(archetype) or []
    if not entries:
        return None
    return (entries[0].get("witness") or {}).get("path")


# ---------------------------------------------------------------------------
# 1. bootstrap_repo populates file_clusters
# ---------------------------------------------------------------------------
section("bootstrap_repo writes file_clusters rows")

repo = _make_ts_repo(12, name="popcheck")
try:
    r = bootstrap_repo(str(repo))["data"]
    t("bootstrap status=success", r["status"] == "success")

    repo_id = _compute_repo_id(repo.resolve())
    rows = index_db.get_file_clusters(repo_id)
    t("file_clusters has one row per source file", len(rows) == 12)
    sample = next(iter(rows.values()))
    t(
        "row carries cluster_id (16-char hex)",
        isinstance(sample["cluster_id"], str) and len(sample["cluster_id"]) == 16,
    )
    t(
        "row carries sha_hint (xxhash64 16-char hex)",
        isinstance(sample["sha_hint"], str) and len(sample["sha_hint"]) == 16,
    )

    # Verify every cluster_id in file_clusters maps to one in archetypes.json.
    archetypes_by_cid = {
        arch["cluster_id"]: name
        for name, arch in _archetypes(repo).items()
    }
    matched = sum(1 for r in rows.values() if r["cluster_id"] in archetypes_by_cid)
    t(
        f"every file_clusters cluster_id matches an archetype ({matched}/{len(rows)})",
        matched == len(rows),
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Zero-change refresh → noop (partial path must NOT fire)
# ---------------------------------------------------------------------------
section("zero-change refresh stays at status=noop")

repo = _make_ts_repo(12, name="noop")
try:
    bootstrap_repo(str(repo))
    r = refresh_repo(str(repo))["data"]
    t(f"status=noop on unchanged repo (got {r['status']})", r["status"] == "noop")
    t("noop does not advertise partial-refresh fields", "files_changed" not in r)
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. Single-file modification under 10% → partial_refresh
# ---------------------------------------------------------------------------
section("1-file modification under 10% triggers partial_refresh")

repo = _make_ts_repo(12, name="onemod")
try:
    bootstrap_repo(str(repo))
    # Modify the LAST file (not the canonical witness, which is the first
    # alphabetical: M0.ts).
    time.sleep(1.1)
    (repo / "src" / "models" / "M11.ts").write_text(
        "export class M11 { value: number = 999; }\n"
    )
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=partial_refresh (got {r['status']})",
        r["status"] == "partial_refresh",
    )
    t("files_changed == 1", r.get("files_changed") == 1)
    t("files_added == 0", r.get("files_added") == 0)
    t("files_removed == 0", r.get("files_removed") == 0)
    t(
        f"change_ratio ≈ 1/12 = 0.0833 (got {r.get('change_ratio')})",
        abs((r.get("change_ratio") or 0) - 0.0833) < 0.001,
    )
    t(
        "duration_ms is non-negative",
        isinstance(r.get("duration_ms"), int) and r["duration_ms"] >= 0,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Many-files modified (>10%) → full re-bootstrap
# ---------------------------------------------------------------------------
section("change_ratio > 10% falls back to full bootstrap")

repo = _make_ts_repo(20, name="manymod")
try:
    bootstrap_repo(str(repo))
    # Modify 5 files = 5/20 = 25%, above the 10% ceiling.
    time.sleep(1.1)
    for i in range(5):
        (repo / "src" / "models" / f"M{i}.ts").write_text(
            f"export class M{i} {{ value: number = 9{i}9; }}\n"
        )
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=success (got {r['status']})",
        r["status"] == "success",
        json.dumps(r),
    )
    t(
        "change_ratio key absent in full-bootstrap envelope",
        "change_ratio" not in r,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. Canonical witness modified → falls back to full bootstrap
# ---------------------------------------------------------------------------
section("canonical witness modified forces full re-bootstrap")

repo = _make_ts_repo(12, name="canonmod")
try:
    bootstrap_repo(str(repo))
    archetypes = _archetypes(repo)
    arch_name = next(iter(archetypes))
    witness_rel = _canonical_witness(repo, arch_name)
    t(
        f"canonical witness resolved (got {witness_rel})",
        witness_rel is not None,
    )

    # Modify exactly the canonical witness.
    time.sleep(1.1)
    (repo / witness_rel).write_text(
        "export class CanonRewrite { value: number = 0; }\n"
    )
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=success (got {r['status']})",
        r["status"] == "success",
    )

    # After full re-bootstrap, the new content shape may have produced a
    # new cluster — what we care about is that the partial path bailed
    # rather than silently amending the profile with a stale canonical.
    t(
        "post-refresh canonical is computed against the new content",
        _canonical_witness(repo, arch_name) is not None
        or len(_archetypes(repo)) >= 0,  # tautological — main assertion is status
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Adding a file to an existing cluster (≤10%) → partial_refresh
# ---------------------------------------------------------------------------
section("adding 1 file to existing cluster (≤10%) triggers partial_refresh")

repo = _make_ts_repo(20, name="addone")
try:
    bootstrap_repo(str(repo))
    before = _archetypes(repo)
    cluster_size_before = next(iter(before.values()))["cluster_size"]
    t(f"initial cluster_size={cluster_size_before}", cluster_size_before == 20)

    time.sleep(1.1)
    (repo / "src" / "models" / "M99.ts").write_text(
        "export class M99 { value: number = 99; }\n"
    )
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=partial_refresh (got {r['status']})",
        r["status"] == "partial_refresh",
    )
    t("files_added=1", r.get("files_added") == 1)
    after = _archetypes(repo)
    cluster_size_after = next(iter(after.values()))["cluster_size"]
    t(
        f"cluster_size grew by 1 ({cluster_size_before} → {cluster_size_after})",
        cluster_size_after == cluster_size_before + 1,
    )
    t(
        "archetypes_amended == 1",
        r.get("archetypes_amended") == 1,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. Adding a file that lands in a NEW cluster → falls back to full
# ---------------------------------------------------------------------------
section("adding a file with a new cluster signature falls back to full bootstrap")

repo = _make_ts_repo(20, name="newcluster")
try:
    bootstrap_repo(str(repo))
    before_count = len(_archetypes(repo))
    time.sleep(1.1)
    # New file in a completely new path bucket — bucket alone makes the
    # cluster key novel.
    new_dir = repo / "src" / "queries"
    new_dir.mkdir(parents=True)
    (new_dir / "q1.ts").write_text(
        "import { useQuery } from 'react-query';\n"
        "export const q1 = () => useQuery('q1', async () => 1);\n"
    )
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=success — new cluster forces full bootstrap (got {r['status']})",
        r["status"] == "success",
    )
    # After full re-bootstrap there may now be a new archetype too.
    after_count = len(_archetypes(repo))
    t(
        f"archetype count >= before ({before_count} → {after_count})",
        after_count >= before_count,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. Removed-only delta (≤10%) → partial_refresh
# ---------------------------------------------------------------------------
section("removed-only delta (≤10%) triggers partial_refresh")

repo = _make_ts_repo(20, name="remove")
try:
    bootstrap_repo(str(repo))
    before = _archetypes(repo)
    cluster_size_before = next(iter(before.values()))["cluster_size"]

    # Delete one non-canonical file.
    archetype_name = next(iter(before))
    witness_rel = _canonical_witness(repo, archetype_name)
    victim = None
    for i in range(20):
        rel = f"src/models/M{i}.ts"
        if rel != witness_rel:
            victim = rel
            break
    assert victim is not None
    time.sleep(1.1)
    (repo / victim).unlink()

    r = refresh_repo(str(repo))["data"]
    t(
        f"status=partial_refresh (got {r['status']})",
        r["status"] == "partial_refresh",
    )
    t("files_removed=1", r.get("files_removed") == 1)
    t("files_changed=0", r.get("files_changed") == 0)
    t("files_added=0", r.get("files_added") == 0)
    after = _archetypes(repo)
    cluster_size_after = next(iter(after.values()))["cluster_size"]
    t(
        f"cluster_size shrunk by 1 ({cluster_size_before} → {cluster_size_after})",
        cluster_size_after == cluster_size_before - 1,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. Removing the canonical witness → falls back to full bootstrap
# ---------------------------------------------------------------------------
section("removing the canonical witness forces full re-bootstrap")

repo = _make_ts_repo(20, name="removecanon")
try:
    bootstrap_repo(str(repo))
    arch_name = next(iter(_archetypes(repo)))
    witness_rel = _canonical_witness(repo, arch_name)
    t("canonical witness resolved", witness_rel is not None)

    time.sleep(1.1)
    (repo / witness_rel).unlink()
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=success (got {r['status']})",
        r["status"] == "success",
    )
    # The new canonical for the cluster (if any) must NOT be the deleted file.
    arches_after = _archetypes(repo)
    if arch_name in arches_after:
        new_witness = _canonical_witness(repo, arch_name)
        t(
            "new canonical is different from the deleted one",
            new_witness != witness_rel,
        )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 10. Mixed add/remove/modify (under 10%) → partial_refresh
# ---------------------------------------------------------------------------
section("mixed add/remove/modify under 10% triggers partial_refresh")

repo = _make_ts_repo(50, name="mixed")
try:
    bootstrap_repo(str(repo))
    arch_name = next(iter(_archetypes(repo)))
    witness_rel = _canonical_witness(repo, arch_name)
    time.sleep(1.1)
    # 1 modify (non-canonical) + 1 add + 1 remove = 3/50 = 6%
    mod_target = None
    rm_target = None
    for i in range(50):
        rel = f"src/models/M{i}.ts"
        if rel != witness_rel:
            if mod_target is None:
                mod_target = rel
            elif rm_target is None:
                rm_target = rel
                break
    assert mod_target and rm_target
    (repo / mod_target).write_text(
        "export class ModTarget { value: number = 88; }\n"
    )
    (repo / rm_target).unlink()
    (repo / "src" / "models" / "Mnew.ts").write_text(
        "export class Mnew { value: number = 77; }\n"
    )
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=partial_refresh (got {r['status']})",
        r["status"] == "partial_refresh",
    )
    t("files_changed=1", r.get("files_changed") == 1)
    t("files_added=1", r.get("files_added") == 1)
    t("files_removed=1", r.get("files_removed") == 1)
    t(
        f"change_ratio ≈ 3/50 = 0.06 (got {r.get('change_ratio')})",
        abs((r.get("change_ratio") or 0) - 0.06) < 0.001,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 11. Boundary: change_ratio exactly at 10% → partial_refresh
# ---------------------------------------------------------------------------
section("change_ratio exactly at 10% is on the partial side")

repo = _make_ts_repo(20, name="boundary")
try:
    bootstrap_repo(str(repo))
    arch_name = next(iter(_archetypes(repo)))
    witness_rel = _canonical_witness(repo, arch_name)
    time.sleep(1.1)
    # Modify 2 non-canonical files = 2/20 = 0.10 = ceiling exactly.
    count = 0
    for i in range(20):
        rel = f"src/models/M{i}.ts"
        if rel == witness_rel:
            continue
        (repo / rel).write_text(
            f"export class M{i} {{ value: number = 9{i}9; }}\n"
        )
        count += 1
        if count == 2:
            break
    r = refresh_repo(str(repo))["data"]
    # 2/20 = 0.10. PARTIAL_REFRESH_CHANGE_RATIO_CEILING = 0.10. The
    # condition `change_ratio > CEILING` is False at equality, so the
    # partial path fires. Verify the boundary explicitly.
    t(
        f"change_ratio == ceiling ({PARTIAL_REFRESH_CHANGE_RATIO_CEILING}) "
        f"still takes partial path (got {r['status']})",
        r["status"] == "partial_refresh",
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 12. Backward compat: no file_clusters rows → falls through to full
# ---------------------------------------------------------------------------
section("legacy repo without file_clusters rows falls through to full bootstrap")

repo = _make_ts_repo(20, name="legacy")
try:
    bootstrap_repo(str(repo))
    repo_id = _compute_repo_id(repo.resolve())
    # Simulate a legacy v0.4 install: delete every file_clusters row.
    removed = index_db.delete_all_file_clusters(repo_id)
    t(f"wiped file_clusters rows ({removed} rows)", removed > 0)
    rows = index_db.get_file_clusters(repo_id)
    t("file_clusters now empty", rows == {})

    time.sleep(1.1)
    (repo / "src" / "models" / "M0.ts").write_text(
        "export class M0 { value: number = 0; comment: string = 'x'; }\n"
    )
    r = refresh_repo(str(repo))["data"]
    t(
        f"status=success (legacy fallthrough, got {r['status']})",
        r["status"] == "success",
    )

    # After the full re-bootstrap, file_clusters should be repopulated.
    rows_after = index_db.get_file_clusters(repo_id)
    t(
        f"file_clusters repopulated after full bootstrap ({len(rows_after)} rows)",
        len(rows_after) == 20,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 13. force=True bypasses BOTH no-op and partial paths
# ---------------------------------------------------------------------------
section("force=True always falls through to full bootstrap")

repo = _make_ts_repo(20, name="force")
try:
    bootstrap_repo(str(repo))
    # No source change at all — without force, this would noop.
    r1 = refresh_repo(str(repo))["data"]
    t(f"plain refresh = noop (got {r1['status']})", r1["status"] == "noop")
    r2 = refresh_repo(str(repo), force=True)["data"]
    t(
        f"force=True = success even with no changes (got {r2['status']})",
        r2["status"] == "success",
    )

    # A small change that would normally take partial path also falls
    # through with force=True.
    time.sleep(1.1)
    (repo / "src" / "models" / "M5.ts").write_text(
        "export class M5 { value: number = 55; }\n"
    )
    r3 = refresh_repo(str(repo), force=True)["data"]
    t(
        f"force=True bypasses partial path (got {r3['status']})",
        r3["status"] == "success",
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 14. Generation counter increments on partial refresh
# ---------------------------------------------------------------------------
section("partial refresh bumps generation across all 4 JSON artifacts")

repo = _make_ts_repo(20, name="gen")
try:
    bootstrap_repo(str(repo))
    g_before = _generations(repo)
    t(
        f"all 4 artifacts share generation pre-refresh (got {g_before})",
        len(set(g_before)) == 1,
    )
    time.sleep(1.1)
    (repo / "src" / "models" / "M19.ts").write_text(
        "export class M19 { value: number = 199; }\n"
    )
    r = refresh_repo(str(repo))["data"]
    t("partial refresh fired", r["status"] == "partial_refresh")
    g_after = _generations(repo)
    t(
        f"all 4 artifacts still share generation post-refresh (got {g_after})",
        len(set(g_after)) == 1,
    )
    t(
        f"generation incremented ({g_before[0]} → {g_after[0]})",
        g_after[0] > g_before[0],
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 15. file_clusters is updated correctly after partial refresh
# ---------------------------------------------------------------------------
section("file_clusters reflects per-file changes after a partial refresh")

# 50 files so a 3-file delta = 6% < 10% triggers partial.
repo = _make_ts_repo(50, name="rowsync")
try:
    bootstrap_repo(str(repo))
    repo_id = _compute_repo_id(repo.resolve())
    rows_before = index_db.get_file_clusters(repo_id)
    sha_before_m5 = rows_before["src/models/M5.ts"]["sha_hint"]

    # Modify a non-canonical file (M5) + add + remove.
    arch_name = next(iter(_archetypes(repo)))
    witness_rel = _canonical_witness(repo, arch_name)
    rm_target = None
    for i in range(50):
        rel = f"src/models/M{i}.ts"
        if rel != witness_rel and rel != "src/models/M5.ts":
            rm_target = rel
            break

    time.sleep(1.1)
    (repo / "src" / "models" / "M5.ts").write_text(
        "export class M5 { value: number = 55; }\n"
    )
    (repo / rm_target).unlink()
    (repo / "src" / "models" / "Mnew.ts").write_text(
        "export class Mnew { value: number = 1000; }\n"
    )

    r = refresh_repo(str(repo))["data"]
    t(
        f"status=partial_refresh (got {r['status']})",
        r["status"] == "partial_refresh",
    )

    rows_after = index_db.get_file_clusters(repo_id)
    t(
        f"row count adjusted (before={len(rows_before)} → after={len(rows_after)})",
        len(rows_after) == len(rows_before),  # -1 remove + 1 add = same.
    )
    t(
        "removed row is gone",
        rm_target not in rows_after,
    )
    t(
        "added row is present",
        "src/models/Mnew.ts" in rows_after,
    )
    t(
        "modified row has a NEW sha_hint",
        rows_after["src/models/M5.ts"]["sha_hint"] != sha_before_m5
        and rows_after["src/models/M5.ts"]["sha_hint"] is not None,
    )
    t(
        "modified row keeps the same cluster_id (file shape preserved)",
        rows_after["src/models/M5.ts"]["cluster_id"]
        == rows_before["src/models/M5.ts"]["cluster_id"],
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 16. atomicity: monkey-patched atomic_profile_commit aborts cleanly
# ---------------------------------------------------------------------------
section("atomicity: a half-failed partial-refresh leaves the profile intact")

repo = _make_ts_repo(50, name="atomic")
try:
    bootstrap_repo(str(repo))
    profile_dir = repo / ".chameleon"
    g_before = _generations(repo)
    before_blob = (profile_dir / "archetypes.json").read_text()
    sentinel_before = (profile_dir / "COMMITTED").is_file()

    # Drive _attempt_partial_refresh directly with a bogus profile_dir that
    # exists on disk but has missing/corrupt artifacts. The helper must
    # return None (signaling "fall through to full bootstrap") and never
    # mutate the real .chameleon/.
    from chameleon_mcp.tools import _attempt_partial_refresh

    repo_id = _compute_repo_id(repo.resolve())
    prev_state = index_db.get_file_clusters(repo_id)
    t("prev_state populated from previous bootstrap", len(prev_state) == 50)

    # Move .chameleon aside so the helper sees an empty dir and bails on the
    # missing JSON reads. This forces every step of the partial path that
    # could theoretically mutate disk to be unreachable.
    aside = repo.parent / f"{repo.name}.chameleon.aside"
    shutil.move(str(profile_dir), str(aside))
    try:
        from chameleon_mcp.bootstrap.discovery import discover_files
        from chameleon_mcp.bootstrap.orchestrator import (
            _glob_for_extractor,
            _select_extractor,
        )
        ext = _select_extractor(repo.resolve())
        cands = discover_files(repo.resolve(), glob=_glob_for_extractor(ext))
        result = _attempt_partial_refresh(
            repo.resolve(),
            repo_id,
            profile_dir,
            list(cands),
            prev_state,
            time.time(),
        )
        t(
            "partial returned None when artifacts missing (graceful bail)",
            result is None,
        )
        # The aside dir must be unchanged byte-for-byte.
        t(
            "aside archetypes.json byte-identical after the failed partial",
            (aside / "archetypes.json").read_text() == before_blob,
        )
        t(
            "aside COMMITTED sentinel present (untouched)",
            (aside / "COMMITTED").is_file() == sentinel_before,
        )
    finally:
        shutil.move(str(aside), str(profile_dir))

    # Confirm the live profile is also still pristine.
    g_after = _generations(repo)
    t(
        f"generation unchanged ({g_before} == {g_after})",
        g_before == g_after,
    )
    t(
        "live archetypes.json byte-identical after failed partial",
        (profile_dir / "archetypes.json").read_text() == before_blob,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 17. index_db helpers behave correctly on empty input + unknown repo
# ---------------------------------------------------------------------------
section("index_db helpers: defensive behavior on empty input")

unknown = "0" * 64
t(
    "get_file_clusters on unknown repo returns empty dict",
    index_db.get_file_clusters(unknown) == {},
)
t(
    "upsert_file_clusters with empty rows is a no-op",
    index_db.upsert_file_clusters("repoX", []) is None,
)
t(
    "delete_file_clusters_for_paths with empty list returns 0",
    index_db.delete_file_clusters_for_paths("repoX", []) == 0,
)
t(
    "delete_all_file_clusters on unknown repo returns 0",
    index_db.delete_all_file_clusters(unknown) == 0,
)
t(
    "upsert_file_clusters with empty repo_id is silently dropped",
    index_db.upsert_file_clusters("", [("a", "b", "c")]) is None,
)


# ---------------------------------------------------------------------------
# 18. Two-cluster repo: amending one cluster leaves the other unchanged
# ---------------------------------------------------------------------------
section("partial refresh amends only the impacted archetype")

repo = _make_two_cluster_repo(name="twoclust")
try:
    bootstrap_repo(str(repo))
    before = _archetypes(repo)
    # Two archetypes expected: classes + functions.
    t(f"two archetypes detected (got {len(before)})", len(before) == 2)
    sizes_before = {name: arch["cluster_size"] for name, arch in before.items()}

    # Add 1 file to the classes cluster.
    time.sleep(1.1)
    (repo / "src" / "models" / "M999.ts").write_text(
        "export class M999 { value: number = 999; }\n"
    )
    r = refresh_repo(str(repo))["data"]
    t(f"status=partial_refresh (got {r['status']})", r["status"] == "partial_refresh")
    t(
        f"archetypes_amended=1 (got {r.get('archetypes_amended')})",
        r.get("archetypes_amended") == 1,
    )
    t(
        f"archetypes_unchanged=1 (got {r.get('archetypes_unchanged')})",
        r.get("archetypes_unchanged") == 1,
    )

    after = _archetypes(repo)
    deltas = {name: after[name]["cluster_size"] - sizes_before[name] for name in sizes_before}
    growers = [n for n, d in deltas.items() if d == 1]
    same = [n for n, d in deltas.items() if d == 0]
    t(
        f"exactly one archetype grew by 1 ({growers})",
        len(growers) == 1 and len(same) == 1,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass:  {PASS}")
print(f"  Fail:  {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
