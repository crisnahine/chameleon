"""Regression tests for the repo index (Phase 4.4) + incremental refresh
(Phase 4.3) wired through `tools.refresh_repo` and `tools.list_profiles`.

Targets `chameleon_mcp.index_db` directly plus the integration points in
`tools.py`. Uses an isolated CHAMELEON_PLUGIN_DATA per run so there's no
cross-test contamination with the real user index.db.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/index_db_test.py
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
TMPDATA = tempfile.mkdtemp(prefix="chameleon_index_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

from chameleon_mcp import index_db  # noqa: E402
from chameleon_mcp.profile.trust import (  # noqa: E402
    grant_trust,
    plugin_data_dir,
    repo_data_dir,
)
from chameleon_mcp.tools import (  # noqa: E402
    _compute_repo_id,
    _resolve_repo_root_by_id,
    bootstrap_repo,
    list_profiles,
    refresh_repo,
    teach_profile,
    trust_profile,
)


# ---------------------------------------------------------------------------
# Repo fixtures
# ---------------------------------------------------------------------------
def _make_ts_repo(name: str = "ts_repo") -> Path:
    """Build a tiny but bootstrapable TypeScript repo."""
    root = Path(tempfile.mkdtemp(prefix=f"chameleon_index_{name}_"))
    (root / "package.json").write_text(
        '{"name":"x","dependencies":{"typescript":"5.0.0"}}'
    )
    (root / "tsconfig.json").write_text("{}")
    src = root / "app" / "controllers" / "api" / "v1"
    src.mkdir(parents=True)
    for i in range(6):
        (src / f"r{i}.ts").write_text(
            f"export class Resource{i} {{ get() {{ return {i}; }} }}\n"
        )
    spec = root / "spec" / "controllers" / "api" / "v1"
    spec.mkdir(parents=True)
    for i in range(6):
        (spec / f"r{i}.test.ts").write_text(
            f"import {{ Resource{i} }} from '../../app/controllers/api/v1/r{i}';\n"
            f"test('r{i}', () => {{ expect(new Resource{i}().get()).toBe({i}); }});\n"
        )
    return root


# ---------------------------------------------------------------------------
# 1. Schema init is idempotent
# ---------------------------------------------------------------------------
section("Schema init is idempotent + uses a clean tmp DB")

db_dir = Path(tempfile.mkdtemp(prefix="chameleon_indexdb_init_"))
db_path = db_dir / "index.db"

conn = index_db.init_index_db(db_path)
t("init_index_db creates the file on first call", db_path.is_file())
rows_first = list(conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
conn.close()

# Re-run init — must NOT error and must keep the same tables.
conn = index_db.init_index_db(db_path)
rows_second = list(conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
t(
    "init_index_db is idempotent (re-run preserves schema)",
    [r["name"] for r in rows_first] == [r["name"] for r in rows_second],
)

# Confirm WAL mode is on.
mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
t(f"journal_mode is WAL (got {mode})", mode.lower() == "wal")

# Verify schema_meta carries the version.
ver = conn.execute(
    "SELECT v FROM schema_meta WHERE k = 'schema_version'"
).fetchone()
t("schema_meta records schema_version", ver is not None and ver["v"] == "1")
conn.close()
shutil.rmtree(db_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. upsert + resolve roundtrip
# ---------------------------------------------------------------------------
section("upsert_repo + resolve_repo_root roundtrip")

# Override the path so we don't share state with the integration tests below.
db_dir = Path(tempfile.mkdtemp(prefix="chameleon_indexdb_upsert_"))
db_path = db_dir / "index.db"

index_db.upsert_repo(
    "repo_abc",
    "/tmp/abc",
    profile_sha256="aaaa",
    archetype_count=3,
    files_indexed=42,
    bootstrap_ms=1234,
    db_path=db_path,
)
t(
    "resolve_repo_root returns the inserted repo_root",
    index_db.resolve_repo_root("repo_abc", db_path=db_path) == "/tmp/abc",
)
got = index_db.get_repo("repo_abc", db_path=db_path)
t("get_repo returns a row dict", isinstance(got, dict))
t("archetype_count round-trips", got["archetype_count"] == 3)
t("files_indexed round-trips", got["files_indexed"] == 42)
t("bootstrap_ms round-trips", got["bootstrap_ms"] == 1234)
t("profile_sha256 round-trips", got["profile_sha256"] == "aaaa")

# Upsert with a NEW set of values updates the existing row.
index_db.upsert_repo(
    "repo_abc",
    "/tmp/abc-moved",
    archetype_count=5,
    files_indexed=50,
    bootstrap_ms=2000,
    db_path=db_path,
)
got = index_db.get_repo("repo_abc", db_path=db_path)
t("upsert with same repo_id updates repo_root", got["repo_root"] == "/tmp/abc-moved")
t("upsert updates archetype_count", got["archetype_count"] == 5)
# COALESCE behavior: profile_sha256 was None in the new call, so it preserves
# the old value.
t(
    "upsert preserves profile_sha256 when omitted (COALESCE)",
    got["profile_sha256"] == "aaaa",
)

# resolve_repo_root returns None for unknown repos.
t(
    "resolve_repo_root returns None for unknown repo_id",
    index_db.resolve_repo_root("repo_does_not_exist", db_path=db_path) is None,
)
t(
    "get_repo returns None for unknown repo_id",
    index_db.get_repo("repo_does_not_exist", db_path=db_path) is None,
)
t(
    "resolve_repo_root returns None on empty repo_id",
    index_db.resolve_repo_root("", db_path=db_path) is None,
)


# ---------------------------------------------------------------------------
# 3. list_repos pagination + ordering
# ---------------------------------------------------------------------------
section("list_repos: ordering by last_seen_at DESC, then repo_id ASC")

# Wipe + re-seed with deterministic last_seen_at timestamps.
db_path.unlink(missing_ok=True)
fixture = [
    ("rep_1", "/tmp/r1", "2024-01-01T00:00:00Z"),
    ("rep_2", "/tmp/r2", "2024-03-01T00:00:00Z"),
    ("rep_3", "/tmp/r3", "2024-02-01T00:00:00Z"),
    ("rep_4", "/tmp/r4", "2024-03-01T00:00:00Z"),  # same ts as rep_2
    ("rep_5", "/tmp/r5", "2024-05-01T00:00:00Z"),
]
for rid, root, ts in fixture:
    index_db.upsert_repo(rid, root, last_seen_at=ts, db_path=db_path)

page, next_cursor, total = index_db.list_repos(None, 100, db_path=db_path)
t("list_repos returns all 5 rows on a wide page", len(page) == 5)
t("total_known reports 5", total == 5)
t("first page has no next_cursor", next_cursor is None)

order = [r["repo_id"] for r in page]
expected = ["rep_5", "rep_2", "rep_4", "rep_3", "rep_1"]
t(
    f"order is last_seen_at DESC then repo_id ASC (got {order})",
    order == expected,
)

# Paginated reads must reconstruct the same order.
page1, c1, _ = index_db.list_repos(None, 2, db_path=db_path)
t("page1 size = 2", len(page1) == 2)
t("page1 has next_cursor", c1 is not None)
page2, c2, _ = index_db.list_repos(c1, 2, db_path=db_path)
t("page2 size = 2", len(page2) == 2)
page3, c3, _ = index_db.list_repos(c2, 2, db_path=db_path)
t("page3 size = 1 (last page)", len(page3) == 1)
t("page3 has no next_cursor", c3 is None)
combined = [r["repo_id"] for r in page1 + page2 + page3]
t(
    f"paginated walk reconstructs full order (got {combined})",
    combined == expected,
)

# Unknown cursor must raise ValueError so callers can return the v0.2
# "unknown cursor" envelope verbatim.
caught = False
try:
    index_db.list_repos("totally-bogus", 10, db_path=db_path)
except ValueError:
    caught = True
t("unknown cursor raises ValueError", caught)


# ---------------------------------------------------------------------------
# 4. forget_repo removes the row
# ---------------------------------------------------------------------------
section("forget_repo")

t("forget_repo on known repo returns True",
  index_db.forget_repo("rep_1", db_path=db_path) is True)
t(
    "forget_repo on already-gone repo returns False",
    index_db.forget_repo("rep_1", db_path=db_path) is False,
)
t(
    "forget_repo leaves other rows intact",
    index_db.get_repo("rep_2", db_path=db_path) is not None,
)
t(
    "forget_repo on missing db file returns False",
    index_db.forget_repo("xx", db_path=Path("/tmp/__chameleon_definitely_missing.db"))
    is False,
)

shutil.rmtree(db_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. _resolve_repo_root_by_id wiring (index → trust fallback)
# ---------------------------------------------------------------------------
section("_resolve_repo_root_by_id: index preferred, falls back to trust")

# A repo that exists on disk so the "is_dir()" gate is satisfied.
fake_repo = Path(tempfile.mkdtemp(prefix="chameleon_resolve_"))
(fake_repo / ".chameleon").mkdir()
(fake_repo / ".chameleon" / "profile.json").write_text(
    json.dumps({"schema_version": 5, "engine_min_version": "0.2.0"})
)

# Use a fixed repo_id (sha256 of the resolved path) to feed the trust record.
resolved_repo_id = _compute_repo_id(fake_repo)
record = grant_trust(resolved_repo_id, fake_repo / ".chameleon")

t(
    "trust-only path resolves before index.db has anything",
    _resolve_repo_root_by_id(resolved_repo_id) == fake_repo.resolve(),
)

# Now drop a row pointing at a DIFFERENT path into index.db and confirm the
# index value wins.
alt_repo = Path(tempfile.mkdtemp(prefix="chameleon_resolve_alt_"))
index_db.upsert_repo(resolved_repo_id, str(alt_repo))
t(
    "index_db row wins over trust record when both present",
    _resolve_repo_root_by_id(resolved_repo_id) == alt_repo.resolve(),
)

# If the indexed path no longer exists, fall back to trust.
shutil.rmtree(alt_repo, ignore_errors=True)
t(
    "stale index entry falls back to trust record",
    _resolve_repo_root_by_id(resolved_repo_id) == fake_repo.resolve(),
)

# Unknown repo_id → None.
t(
    "unknown repo_id resolves to None",
    _resolve_repo_root_by_id("0" * 64) is None,
)

# Clear out the index row to keep later test stages clean.
index_db.forget_repo(resolved_repo_id)
shutil.rmtree(fake_repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. bootstrap_repo populates index.db
# ---------------------------------------------------------------------------
section("bootstrap_repo upserts an index.db row")

repo = _make_ts_repo("bootstrap")
try:
    rep = bootstrap_repo(str(repo))["data"]
    t("bootstrap_repo status=success", rep["status"] == "success")
    repo_id = _compute_repo_id(repo.resolve())
    row = index_db.get_repo(repo_id)
    t("bootstrap creates an index.db row", row is not None)
    if row is not None:
        t(
            f"row.repo_root matches the bootstrap path (got {row['repo_root']})",
            row["repo_root"] == str(repo.resolve()),
        )
        t(
            f"row.archetype_count matches report ({rep['archetypes_detected']} vs {row['archetype_count']})",
            row["archetype_count"] == rep["archetypes_detected"],
        )
        t(
            f"row.files_indexed > 0 (got {row['files_indexed']})",
            (row["files_indexed"] or 0) > 0,
        )
        t(
            f"row.bootstrap_ms is set (got {row['bootstrap_ms']})",
            row["bootstrap_ms"] is not None,
        )
        t(
            "row.profile_sha256 is set",
            row["profile_sha256"] and len(row["profile_sha256"]) == 64,
        )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. refresh_repo no-op short-circuit
# ---------------------------------------------------------------------------
section("refresh_repo no-op short-circuit when nothing has changed")

repo = _make_ts_repo("noop")
try:
    rep1 = bootstrap_repo(str(repo))["data"]
    t("initial bootstrap success", rep1["status"] == "success")

    # Sleep briefly so the bootstrap's last_seen_at is strictly older than the
    # source file mtimes were when they were written. Actually we want the
    # opposite — last_seen_at AFTER file mtimes — so nothing here. The fact
    # that the source files were created before bootstrap means their mtimes
    # are <= last_seen_at and the no-op should fire on the next refresh.

    rep2 = refresh_repo(str(repo))["data"]
    t(
        f"refresh after no change fires no-op (got {rep2['status']})",
        rep2["status"] == "noop",
    )
    t(
        "no-op response carries archetypes_detected",
        "archetypes_detected" in rep2,
    )
    t(
        "no-op archetypes_detected matches bootstrap report",
        rep2["archetypes_detected"] == rep1["archetypes_detected"],
    )
    t(
        "no-op reason is human-readable",
        isinstance(rep2.get("reason"), str) and "no files changed" in rep2["reason"],
    )

    # Mutating any source file invalidates the no-op.
    src_file = repo / "app" / "controllers" / "api" / "v1" / "r0.ts"
    # Force mtime newer than last_seen_at (1-second granularity safety margin).
    time.sleep(1.1)
    src_file.write_text("export class Resource0Changed { get() { return 99; } }\n")
    rep3 = refresh_repo(str(repo))["data"]
    t(
        f"refresh after edit re-bootstraps (got {rep3['status']})",
        rep3["status"] == "success",
    )

    # force=True must bypass the no-op even when nothing changed.
    rep4 = refresh_repo(str(repo), force=True)["data"]
    t(
        f"refresh with force=True always re-bootstraps (got {rep4['status']})",
        rep4["status"] == "success",
    )

    # Adding a brand new file (cardinality change) invalidates the no-op.
    repo_after_force = refresh_repo(str(repo))["data"]
    # After the force re-bootstrap there's no diff yet, so this is no-op...
    t(
        f"after force-bootstrap, immediate refresh is noop (got {repo_after_force['status']})",
        repo_after_force["status"] == "noop",
    )
    new_file = repo / "app" / "controllers" / "api" / "v1" / "r99.ts"
    new_file.write_text("export class Resource99 { get() { return 99; } }\n")
    rep5 = refresh_repo(str(repo))["data"]
    t(
        f"refresh after new file re-bootstraps (cardinality change, got {rep5['status']})",
        rep5["status"] == "success",
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. teach_profile invalidates the refresh no-op so summary.md re-renders
# ---------------------------------------------------------------------------
section("teach_profile invalidates the no-op (idioms.md mtime check)")

repo = _make_ts_repo("teach_noop")
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    time.sleep(1.1)
    teach_profile(str(repo), "always export interfaces, never type aliases for objects")
    rep = refresh_repo(str(repo))["data"]
    t(
        f"refresh after teach re-bootstraps so summary.md updates (got {rep['status']})",
        rep["status"] == "success",
    )
    summary = (repo / ".chameleon" / "profile.summary.md").read_text()
    t(
        "summary.md surfaces the new idiom body after the forced re-bootstrap",
        "always export interfaces" in summary,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9. list_profiles is backed by index.db (shape preserved)
# ---------------------------------------------------------------------------
section("list_profiles: backwards-compatible response shape, sourced from index.db")

# Build two fresh repos.
repo_a = _make_ts_repo("listing_a")
repo_b = _make_ts_repo("listing_b")
try:
    bootstrap_repo(str(repo_a))
    bootstrap_repo(str(repo_b))
    trust_profile(str(repo_a), repo_a.name)

    r = list_profiles(limit=100)
    data = r["data"]
    t("envelope carries api_version", r.get("api_version") == "1")
    t("data has 'profiles' key", "profiles" in data)
    t("data has 'total_known' key", "total_known" in data)
    profile_ids = {p["repo_id"] for p in data["profiles"]}
    t(
        f"both bootstrapped repos surface in list_profiles (got {len(profile_ids)} ids)",
        _compute_repo_id(repo_a.resolve()) in profile_ids
        and _compute_repo_id(repo_b.resolve()) in profile_ids,
    )
    # repo_a is trusted; repo_b isn't.
    a_entry = next(p for p in data["profiles"]
                   if p["repo_id"] == _compute_repo_id(repo_a.resolve()))
    b_entry = next(p for p in data["profiles"]
                   if p["repo_id"] == _compute_repo_id(repo_b.resolve()))
    t("repo_a entry has trust_state=trusted", a_entry["trust_state"] == "trusted")
    t("repo_a entry has trusted_at populated",
      isinstance(a_entry["trusted_at"], str) and a_entry["trusted_at"])
    t("repo_b entry has trust_state=untrusted", b_entry["trust_state"] == "untrusted")
    t("repo_b entry has trusted_at = None", b_entry["trusted_at"] is None)
finally:
    shutil.rmtree(repo_a, ignore_errors=True)
    shutil.rmtree(repo_b, ignore_errors=True)


# ---------------------------------------------------------------------------
# 10. list_profiles validates limit + cursor exactly like v0.2
# ---------------------------------------------------------------------------
section("list_profiles validation: limit + cursor errors are preserved")

r = list_profiles(limit=0)["data"]
t("limit=0 returns failed", r.get("status") == "failed", json.dumps(r))
r = list_profiles(limit=-1)["data"]
t("limit=-1 returns failed", r.get("status") == "failed")
r = list_profiles(limit=1001)["data"]
t("limit=1001 returns failed", r.get("status") == "failed")
r = list_profiles(cursor="totally-bogus")["data"]
t("unknown cursor returns failed", r.get("status") == "failed")
t(
    "unknown cursor error mentions next_cursor",
    "next_cursor" in (r.get("error") or "").lower() or "cursor" in (r.get("error") or "").lower(),
)


# ---------------------------------------------------------------------------
# 11. legacy backfill: trust record without index.db row gets mirrored
# ---------------------------------------------------------------------------
section("Legacy backfill: v0.1/v0.2 trust records become index.db rows")

# Spin up a fresh CHAMELEON_PLUGIN_DATA so this section is hermetic.
legacy_data = Path(tempfile.mkdtemp(prefix="chameleon_legacy_"))
old_env = os.environ.get("CHAMELEON_PLUGIN_DATA")
os.environ["CHAMELEON_PLUGIN_DATA"] = str(legacy_data)
try:
    legacy_repo = _make_ts_repo("legacy")
    # Manually write the .chameleon dir so we can hand-craft a trust record
    # without invoking bootstrap_repo (which would also create the index row).
    bootstrap_repo(str(legacy_repo))
    legacy_repo_id = _compute_repo_id(legacy_repo.resolve())

    # Delete the index.db to simulate a fresh upgrade from v0.2.
    index_path = legacy_data / "index.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = legacy_data / f"index.db{suffix}"
        if candidate.exists():
            candidate.unlink()
    t("index.db wiped to simulate v0.2 install", not index_path.is_file())

    # Trust record still exists.
    trust_profile(str(legacy_repo), legacy_repo.name)
    repo_dir = repo_data_dir(legacy_repo_id)
    t(
        "trust record present in per-repo dir",
        (repo_dir / ".trust").is_file(),
    )

    # list_profiles must backfill on first call.
    r = list_profiles(limit=100)["data"]
    ids = {p["repo_id"] for p in r["profiles"]}
    t(
        "list_profiles surfaces the repo via backfill",
        legacy_repo_id in ids,
    )
    t(
        "index.db now has a row for the legacy repo",
        index_db.resolve_repo_root(legacy_repo_id) == str(legacy_repo.resolve()),
    )

    # _resolve_repo_root_by_id picks up the backfilled row.
    resolved = _resolve_repo_root_by_id(legacy_repo_id)
    t(
        "_resolve_repo_root_by_id uses the backfilled index row",
        resolved == legacy_repo.resolve(),
    )

    shutil.rmtree(legacy_repo, ignore_errors=True)
finally:
    if old_env is None:
        del os.environ["CHAMELEON_PLUGIN_DATA"]
    else:
        os.environ["CHAMELEON_PLUGIN_DATA"] = old_env
    shutil.rmtree(legacy_data, ignore_errors=True)


# ---------------------------------------------------------------------------
# 12. forget_repo via index_db keeps trust record untouched
# ---------------------------------------------------------------------------
section("forget_repo(index_db) does not touch the trust record")

repo = _make_ts_repo("forget")
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    rid = _compute_repo_id(repo.resolve())

    t("trust record exists pre-forget", (plugin_data_dir() / rid / ".trust").is_file())
    t("index row exists pre-forget", index_db.resolve_repo_root(rid) is not None)

    removed = index_db.forget_repo(rid)
    t("forget_repo reports a row was removed", removed is True)
    t("trust record still present after forget", (plugin_data_dir() / rid / ".trust").is_file())
    # _resolve_repo_root_by_id should fall back to the trust record.
    t(
        "_resolve_repo_root_by_id falls back to trust after forget",
        _resolve_repo_root_by_id(rid) == repo.resolve(),
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
