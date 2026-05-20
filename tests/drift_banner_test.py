"""Tests for rec 4: drift banner at SessionStart.

The banner fires when:
  - drift score >= 0.4 (default)
  - observation count >= 10 in the 14-day window
  - the per-repo cooldown marker is older than 7 days (or absent)

All three gates must hold or the banner stays silent. The cooldown
marker lives in plugin_data_dir, NOT in-repo, so a shared filesystem
or git checkout doesn't race.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import chameleon_mcp.drift.observations as drift_obs
from chameleon_mcp._thresholds import threshold_float, threshold_int
from chameleon_mcp.drift.observations import (
    compute_drift_score,
    compute_drift_stats,
    record_edit_observation,
)
from chameleon_mcp.hook_helper import (
    _DRIFT_BANNER_FILENAME,
    _drift_banner_for_repo,
    _plugin_data_dir,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _make_repo(td: Path) -> Path:
    repo = td / "repo"
    repo.mkdir()
    # Sentinel marker so find_repo_root would identify this as a repo.
    (repo / "package.json").write_text("{}", encoding="utf-8")
    return repo


def _seed_observations(repo_id: str, *, count: int, band: str) -> None:
    """Seed `count` rows in drift.db with the given confidence_band."""
    now = int(time.time())
    for i in range(count):
        record_edit_observation(
            repo_id,
            f"src/file_{i}.ts",
            "component",
            band,
            matched_canonical=False,
            observed_at=now - i,
        )


def _wipe_plugin_data() -> None:
    """Clean up the per-user plugin data dir between scenarios so the
    cooldown marker doesn't leak between tests."""
    pd = _plugin_data_dir()
    if pd.is_dir():
        shutil.rmtree(pd, ignore_errors=True)


# Run all scenarios under an isolated CHAMELEON_PLUGIN_DATA so the test
# never touches the user's real plugin data dir.
_TMP = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP.name

# Reload affected modules so they pick up the override. The drift
# module reads CHAMELEON_PLUGIN_DATA via plugin_paths at call time,
# so no reload needed there.

section("compute_drift_stats returns score + count when populated")
with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    from chameleon_mcp.tools import _compute_repo_id

    repo_id = _compute_repo_id(repo)
    _seed_observations(repo_id, count=15, band="low")
    stats = compute_drift_stats(repo_id)
    t("stats not None", stats is not None)
    t("count matches", stats and stats["count"] == 15, str(stats))
    t("score is in [0,1]", stats and 0.0 <= stats["score"] <= 1.0, str(stats))
    t(
        "compute_drift_score returns same score",
        compute_drift_score(repo_id) == stats["score"],
    )
    _wipe_plugin_data()


section("banner SKIPS when observation count < min floor")
with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    repo_id = _compute_repo_id(repo)
    # Below floor: 9 observations
    _seed_observations(repo_id, count=threshold_int("DRIFT_BANNER_MIN_OBSERVATIONS") - 1, band="low")
    banner = _drift_banner_for_repo(repo)
    t("banner is None below floor", banner is None, str(banner)[:60] if banner else "None")
    _wipe_plugin_data()


section("banner SKIPS when score < threshold")
with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    repo_id = _compute_repo_id(repo)
    # 15 observations all "high" confidence (~0.95): score ~0.05 < 0.4
    _seed_observations(repo_id, count=15, band="high")
    banner = _drift_banner_for_repo(repo)
    t("banner is None below threshold", banner is None, str(banner)[:60] if banner else "None")
    _wipe_plugin_data()


section("banner FIRES when both gates pass + cooldown is fresh after first fire")
with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    repo_id = _compute_repo_id(repo)
    _seed_observations(repo_id, count=15, band="low")  # score ~0.7, count 15
    banner1 = _drift_banner_for_repo(repo)
    t(
        "first call returns a banner",
        banner1 is not None and "drift" in (banner1 or ""),
        banner1[:80] if banner1 else "None",
    )
    # Second call within cooldown: silent
    banner2 = _drift_banner_for_repo(repo)
    t(
        "second call within 7-day cooldown returns None",
        banner2 is None,
        str(banner2)[:60] if banner2 else "None",
    )
    # Marker is in plugin_data_dir, NOT in-repo
    marker = _plugin_data_dir() / repo_id / _DRIFT_BANNER_FILENAME
    t("cooldown marker landed in plugin_data_dir", marker.is_file(), str(marker))
    in_repo_marker = repo / ".chameleon" / _DRIFT_BANNER_FILENAME
    t(
        "no in-repo marker written (no shared-filesystem race)",
        not in_repo_marker.exists(),
        str(in_repo_marker),
    )
    _wipe_plugin_data()


section("banner re-fires after cooldown expires")
with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    repo_id = _compute_repo_id(repo)
    _seed_observations(repo_id, count=15, band="low")
    # Fire once, then manually expire the marker by setting its mtime to 8 days ago
    b1 = _drift_banner_for_repo(repo)
    marker = _plugin_data_dir() / repo_id / _DRIFT_BANNER_FILENAME
    ancient = time.time() - 8 * 24 * 3600
    os.utime(marker, (ancient, ancient))
    b2 = _drift_banner_for_repo(repo)
    t("re-fires after cooldown expiry", b1 is not None and b2 is not None)
    _wipe_plugin_data()


section("banner is fail-open if drift compute raises")
# Force the import inside _drift_banner_for_repo to fail by passing a
# bogus repo path that isn't a real directory — _compute_repo_id may
# still return a hash, compute_drift_stats returns None for missing DB.
# Either way, the helper must not raise.
banner = _drift_banner_for_repo(Path("/nonexistent/repo/xxx"))
t("fail-open returns None instead of raising", banner is None)


section("thresholds module exposes the gates (env-overridable)")
t(
    "DRIFT_BANNER_THRESHOLD lives in _thresholds.py",
    threshold_float("DRIFT_BANNER_THRESHOLD") == 0.4,
)
t(
    "DRIFT_BANNER_MIN_OBSERVATIONS lives in _thresholds.py",
    threshold_int("DRIFT_BANNER_MIN_OBSERVATIONS") == 10,
)
t(
    "DRIFT_BANNER_TTL_SECONDS lives in _thresholds.py",
    threshold_int("DRIFT_BANNER_TTL_SECONDS") == 7 * 24 * 3600,
)


section("banner respects opt-outs (CHAMELEON_DISABLE)")
with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    repo_id = _compute_repo_id(repo)
    _seed_observations(repo_id, count=15, band="low")
    saved_disable = os.environ.pop("CHAMELEON_DISABLE", None)
    try:
        os.environ["CHAMELEON_DISABLE"] = "1"
        banner = _drift_banner_for_repo(repo)
        t("CHAMELEON_DISABLE=1 silences the banner", banner is None, str(banner)[:60] if banner else "None")
        marker = _plugin_data_dir() / repo_id / _DRIFT_BANNER_FILENAME
        t(
            "cooldown marker NOT written while opted out",
            not marker.is_file(),
            str(marker),
        )
    finally:
        if saved_disable is None:
            os.environ.pop("CHAMELEON_DISABLE", None)
        else:
            os.environ["CHAMELEON_DISABLE"] = saved_disable
    _wipe_plugin_data()


section("banner respects opt-outs (.chameleon/.skip)")
with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    repo_id = _compute_repo_id(repo)
    _seed_observations(repo_id, count=15, band="low")
    (repo / ".chameleon").mkdir(exist_ok=True)
    (repo / ".chameleon" / ".skip").write_text("", encoding="utf-8")
    # find_repo_root prefers .chameleon when present, so the resolved
    # root for the suppression check is the same dir.
    banner = _drift_banner_for_repo(repo)
    t(".chameleon/.skip silences the banner", banner is None, str(banner)[:60] if banner else "None")
    _wipe_plugin_data()


section("banner is fail-open if drift compute raises (real monkey-patch)")
def _boom_stats(*_a, **_kw):
    raise RuntimeError("simulated drift compute crash")


with tempfile.TemporaryDirectory() as td:
    repo = _make_repo(Path(td))
    with patch.object(drift_obs, "compute_drift_stats", _boom_stats):
        # The import inside _drift_banner_for_repo is fresh, so patch
        # the symbol at the call-site source module too.
        import chameleon_mcp.hook_helper as hh_module

        with patch.object(hh_module, "_drift_banner_for_repo", hh_module._drift_banner_for_repo):
            # Direct call exercises the outer try/except.
            banner = hh_module._drift_banner_for_repo(repo)
    t("raise inside drift compute -> banner is None (fail-open)", banner is None)
    _wipe_plugin_data()


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
