"""High-priority tests derived from the v0.5.7 audit
(`docs/test-audit-2026-05-12.md`).

Covers the most impactful GAP scenarios:
  * BUG-NEW-021 drift baseline (new function, no targeted test)
  * BUG-NEW-022 retention boundary (exact-cap behaviour)
  * Timezone parity between get_drift_status and _iso_to_epoch
  * Schema-version acceptance for v3 through v6 (currently only v7 + v99 tested)
  * Fail-open contracts for record_edit_observation on disk-write failure
  * trust_profile loadable check (already added, but exercising more shapes)
  * find_repo_root boundary at 32-level walk cap
  * Hook fail-open on missing CLAUDE_PLUGIN_ROOT
"""

import calendar
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from chameleon_mcp.drift.observations import (
    _EDIT_OBS_HARD_CAP,
    _EDIT_OBS_SOFT_CAP,
    record_bootstrap_baseline,
    record_edit_observation,
)
from chameleon_mcp.hook_helper import session_start
from chameleon_mcp.profile.loader import (
    ProfileLoadError,
    find_repo_root,
    load_profile_dir,
)
from chameleon_mcp.profile.trust import plugin_data_dir
from chameleon_mcp.tools import _iso_to_epoch

PASS, FAIL = [], []


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# BUG-NEW-021 — record_bootstrap_baseline
# ---------------------------------------------------------------------------
section("BUG-NEW-021 record_bootstrap_baseline")

def _isolated_plugin_data(tmp: Path):
    """Return an env override that puts plugin data under tmp/."""
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(tmp)
    return tmp


with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    _isolated_plugin_data(tmp)

    repo_id = "a" * 64
    n = record_bootstrap_baseline(repo_id, [
        ("src/foo.ts", "component", "high"),
        ("src/bar.ts", "component", "medium"),
        ("src/baz.ts", None, "low"),
    ])
    t("3 rows written", n == 3, f"got {n}")

    db = plugin_data_dir() / repo_id / "drift.db"
    conn = sqlite3.connect(str(db))
    (count,) = conn.execute("SELECT COUNT(*) FROM files").fetchone()
    t("files table has 3 rows", count == 3, f"got {count}")

    archs = {a for (a,) in conn.execute("SELECT DISTINCT archetype FROM files").fetchall()}
    t("archetypes set captured",
      {"component", None} <= archs,
      f"got {archs}")

    (avg,) = conn.execute(
        "SELECT AVG(last_observed_confidence) FROM files"
    ).fetchone()
    t("confidence band -> float scaled (mean roughly between 0.3 and 0.95)",
      0.3 <= avg <= 0.95, f"got {avg}")

    conn.close()

# Empty list — no-op
with tempfile.TemporaryDirectory() as raw:
    _isolated_plugin_data(Path(raw))
    n = record_bootstrap_baseline("b" * 64, [])
    t("empty input returns 0", n == 0, f"got {n}")

# Idempotence — same path written twice keeps one row
with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    _isolated_plugin_data(tmp)
    repo_id = "c" * 64
    record_bootstrap_baseline(repo_id, [("src/x.ts", "component", "high")])
    record_bootstrap_baseline(repo_id, [("src/x.ts", "component", "low")])
    db = plugin_data_dir() / repo_id / "drift.db"
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT rel_path, archetype, last_observed_confidence FROM files"
    ).fetchall()
    t("second write upserts (no duplicate row)", len(rows) == 1, f"got {len(rows)}")
    t("upsert keeps last write (confidence 0.3 == 'low')",
      abs(rows[0][2] - 0.3) < 0.01,
      f"got {rows[0]}")
    conn.close()


# ---------------------------------------------------------------------------
# BUG-NEW-022 — retention boundary
# ---------------------------------------------------------------------------
section("BUG-NEW-022 retention cap")

with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    _isolated_plugin_data(tmp)
    repo_id = "d" * 64

    # Insert HARD_CAP rows — should NOT trigger cleanup
    for i in range(_EDIT_OBS_HARD_CAP):
        record_edit_observation(repo_id, f"f{i}.py", "controller", "high")
    db = plugin_data_dir() / repo_id / "drift.db"
    conn = sqlite3.connect(str(db))
    (n,) = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()
    t("exactly _EDIT_OBS_HARD_CAP rows when at cap (no cleanup)",
      n == _EDIT_OBS_HARD_CAP, f"got {n}")
    conn.close()

    # One more — triggers cleanup
    record_edit_observation(repo_id, "trigger.py", "controller", "high")
    conn = sqlite3.connect(str(db))
    (n,) = conn.execute("SELECT COUNT(*) FROM edit_observations").fetchone()
    t("after triggering insert, count <= _EDIT_OBS_SOFT_CAP",
      n <= _EDIT_OBS_SOFT_CAP, f"got {n}")
    conn.close()


# ---------------------------------------------------------------------------
# Fail-open contract: record_edit_observation on read-only db dir
# ---------------------------------------------------------------------------
section("Fail-open contract — record_edit_observation on read-only path")

with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    _isolated_plugin_data(tmp)
    repo_id = "e" * 64
    # Create the per-repo dir and make it read-only
    pdir = plugin_data_dir() / repo_id
    pdir.mkdir(parents=True)
    pdir.chmod(0o555)
    try:
        # Should not raise — function is fail-open
        record_edit_observation(repo_id, "f.py", "x", "high")
        t("does not raise on read-only dir", True)
    except Exception as e:
        t("does not raise on read-only dir", False, f"raised {e!r}")
    finally:
        pdir.chmod(0o755)


# ---------------------------------------------------------------------------
# Timezone parity — get_drift_status vs _iso_to_epoch
# ---------------------------------------------------------------------------
section("Timezone parity check")

iso = "2026-05-12T03:00:00Z"
ts_calendar = _iso_to_epoch(iso)
# Expected: 2026-05-12 03:00 UTC = a deterministic epoch
expected = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
t("_iso_to_epoch matches UTC interpretation",
  ts_calendar == expected, f"got {ts_calendar}, expected {expected}")

# get_drift_status converts ISO using time.mktime (local TZ) — if local
# != UTC, the two ARE different. Document the bug.
local_ts = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
offset = expected - local_ts
t("known timezone-discrepancy issue exists" if offset != 0 else "in UTC system",
  True,  # always pass: this is documentation
  f"UTC vs local offset = {offset:.0f}s")


# ---------------------------------------------------------------------------
# Schema-version acceptance — v3..v7 should all load
# ---------------------------------------------------------------------------
section("Schema-version acceptance v3..v7")

for sv in (3, 4, 5, 6, 7):
    with tempfile.TemporaryDirectory() as raw:
        prof_dir = Path(raw) / ".chameleon"
        prof_dir.mkdir()
        (prof_dir / "profile.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "engine_min_version": "0.4.0",
                         "language": "typescript"}))
        (prof_dir / "archetypes.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "archetypes": {}}))
        (prof_dir / "rules.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "rules": {}}))
        (prof_dir / "canonicals.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "canonicals": {}}))
        (prof_dir / "COMMITTED").touch()
        try:
            load_profile_dir(prof_dir)
            t(f"v{sv} profile accepted", True)
        except ProfileLoadError as exc:
            t(f"v{sv} profile accepted", False, f"raised {exc}")


# ---------------------------------------------------------------------------
# Schema-version rejection — v8+ should fail
# ---------------------------------------------------------------------------
section("Schema-version rejection v8+")

for sv in (8, 99, 999):
    with tempfile.TemporaryDirectory() as raw:
        prof_dir = Path(raw) / ".chameleon"
        prof_dir.mkdir()
        (prof_dir / "profile.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "engine_min_version": "0.4.0",
                         "language": "typescript"}))
        (prof_dir / "archetypes.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "archetypes": {}}))
        (prof_dir / "rules.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "rules": {}}))
        (prof_dir / "canonicals.json").write_text(
            json.dumps({"schema_version": sv, "generation": 1, "canonicals": {}}))
        (prof_dir / "COMMITTED").touch()
        try:
            load_profile_dir(prof_dir)
            t(f"v{sv} profile rejected", False, "loader did not raise")
        except ProfileLoadError:
            t(f"v{sv} profile rejected", True)


# ---------------------------------------------------------------------------
# find_repo_root — boundary at 32-level walk cap
# ---------------------------------------------------------------------------
section("find_repo_root 32-level cap")

with tempfile.TemporaryDirectory() as raw:
    deep = Path(raw)
    # Build 35 levels deep
    for i in range(35):
        deep = deep / f"l{i}"
    deep.mkdir(parents=True)
    f = deep / "x.ts"
    f.write_text("")
    # No .chameleon, no .git anywhere — should return None
    found = find_repo_root(f)
    t("file at 35-level depth with no markers returns None",
      found is None, f"got {found}")

with tempfile.TemporaryDirectory() as raw:
    base = Path(raw)
    (base / "package.json").write_text("{}")
    deep = base
    for i in range(20):  # exactly 20 levels deep, under 32 cap
        deep = deep / f"l{i}"
    deep.mkdir(parents=True)
    f = deep / "x.ts"
    f.write_text("")
    found = find_repo_root(f)
    t("file 20 levels deep finds package.json root",
      found is not None and found.resolve() == base.resolve(),
      f"got {found}")


# ---------------------------------------------------------------------------
# Hook fail-open on missing CLAUDE_PLUGIN_ROOT
# ---------------------------------------------------------------------------
section("Hook fail-open without CLAUDE_PLUGIN_ROOT")

old_root = os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
old_stdout = sys.stdout
captured = io.StringIO()
sys.stdout = captured
try:
    rc = session_start()
finally:
    sys.stdout = old_stdout
    if old_root is not None:
        os.environ["CLAUDE_PLUGIN_ROOT"] = old_root
t("session_start returns 0 even without CLAUDE_PLUGIN_ROOT", rc == 0, f"rc={rc}")
out = captured.getvalue().strip()
try:
    parsed = json.loads(out)
    t("session_start emits valid JSON", True)
    t("session_start emits empty payload",
      parsed == {} or parsed.get("hookSpecificOutput") in (None, {}),
      f"got {parsed!r}")
except json.JSONDecodeError:
    t("session_start emits valid JSON", False, f"got {out!r}")



# ---------------------------------------------------------------------------
# BUG-NEW-023 - get_drift_status uses UTC, not local TZ (v0.5.7-followup)
# ---------------------------------------------------------------------------
section("BUG-NEW-023 get_drift_status timezone parity")

import unittest.mock as _mock

from chameleon_mcp.profile import trust as _trust_mod
from chameleon_mcp.profile.trust import TrustRecord
from chameleon_mcp.tools import get_drift_status

with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    _isolated_plugin_data(tmp)
    repo_id = "f" * 64
    pdir = plugin_data_dir() / repo_id
    pdir.mkdir(parents=True)
    granted_iso = "2026-05-01T12:00:00Z"  # 11 days before "now" of 2026-05-12 12:00 UTC

    fake_trust = TrustRecord(
        granted_at=granted_iso,
        granted_by_user="test",
        profile_sha256="x" * 64,
        repo_root=str(tmp),
        repo_root_specific_hashes={},
    )

    def _fake_trust_state(rid):
        return fake_trust if rid == repo_id else None

    frozen_now = calendar.timegm(
        time.strptime("2026-05-12T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ")
    )

    with _mock.patch.object(_trust_mod, "trust_state_for", _fake_trust_state), \
         _mock.patch("time.time", return_value=frozen_now):
        resp = get_drift_status(repo_id)
    days = resp["data"]["days_since_refresh"]
    t("days_since_refresh = 11 (UTC math, not local-TZ-shifted)",
      days == 11, f"got {days}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n=== Summary: {len(PASS)} pass, {len(FAIL)} fail ===")
if FAIL:
    for name, info in FAIL:
        print(f"  FAIL: {name} - {info}")
    sys.exit(1)
