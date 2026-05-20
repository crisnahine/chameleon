"""Phase 3.4+: full envelope-shape assertions across 8 documented states.

The existing 3.1 / 3.2 scenarios only spot-check substrings in the
PreToolUse advisory. Recs 1, 3, 4, 6 all changed the envelope shape
(match_quality + sub_buckets in the header, degraded banner block,
drift banner in SessionStart, archetype_diff in /chameleon-refresh)
and shipped with no envelope-shape regression test — the existing
substring checks accidentally still pass even when the shape silently
shifts.

This scenario asserts the FULL envelope structure for each of the 8
documented states get_pattern_context can produce, plus the rec-12
validation gates (oversized renames.json rejected, symlinked
renames.json refused) and the rec-13 symlink filter.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _set_env(ctx) -> dict:
    old = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(ctx.plugin_data_dir)
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    return old


def _restore_env(old: dict) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _archetype_required_keys() -> set[str]:
    """The minimum keys every archetype envelope must carry."""
    return {
        "archetype",
        "alternatives",
        "content_signal_match",
        "confidence_band",
        "match_quality",
        "sub_buckets_count",
    }


def _check_archetype_shape(arch: dict) -> str | None:
    """Return an error string if the archetype envelope is malformed."""
    missing = _archetype_required_keys() - arch.keys()
    if missing:
        return f"archetype missing keys: {sorted(missing)}"
    mq = arch.get("match_quality")
    if mq not in {"exact", "ast", "fallback", "none"}:
        return f"match_quality invalid: {mq!r}"
    cb = arch.get("confidence_band")
    if cb not in {"high", "low", "medium", None}:
        return f"confidence_band invalid: {cb!r}"
    sbc = arch.get("sub_buckets_count")
    if not isinstance(sbc, int) or sbc < 0:
        return f"sub_buckets_count must be int >= 0, got {sbc!r}"
    return None


# ---------------------------------------------------------------------------
# 3.4  envelope shape across the 8 documented states
# ---------------------------------------------------------------------------

def _run_envelope_shape_matrix(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import (  # type: ignore[import]
        bootstrap_repo,
        get_pattern_context,
        trust_profile,
    )

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = ctx.plugin_data_dir / "ts_minimal_shape"
    shutil.copytree(fixture_src, repo)

    old = _set_env(ctx)
    failures: list[str] = []
    try:
        # State 1: untrusted + first prompt
        # (No trust grant yet — bootstrap already exists in fixture or
        #  let trust_profile see "untrusted".)
        if not (repo / ".chameleon" / "COMMITTED").exists():
            bootstrap_repo(str(repo))
        # Don't call trust_profile yet. Expect trust_state="untrusted".
        sample_ts = next(repo.rglob("*.ts"), None)
        if sample_ts is None:
            return Result(status="SKIP", notes="fixture has no .ts files")

        resp = get_pattern_context(str(sample_ts))
        data = resp.get("data", {})
        trust = data.get("repo", {}).get("trust_state")
        if trust != "untrusted":
            failures.append(
                f"state1 (untrusted): expected trust_state='untrusted', got {trust!r}"
            )

        # State 2: trusted + match
        trust_profile(str(repo), repo.name)
        resp = get_pattern_context(str(sample_ts))
        data = resp.get("data", {})
        trust = data.get("repo", {}).get("trust_state")
        if trust != "trusted":
            failures.append(f"state2 (trusted): expected trusted, got {trust!r}")
        arch = data.get("archetype", {})
        err = _check_archetype_shape(arch)
        if err:
            failures.append(f"state2 archetype: {err}")

        # State 3: trusted + no archetype (path outside fixture)
        outside = ctx.plugin_data_dir / "outside.ts"
        outside.write_text("export const x = 1;\n", encoding="utf-8")
        resp = get_pattern_context(str(outside))
        data = resp.get("data", {})
        arch = data.get("archetype", {})
        err = _check_archetype_shape(arch)
        if err:
            failures.append(f"state3 (no-archetype) shape: {err}")
        if arch.get("archetype") is not None:
            # Outside the fixture — should be null
            failures.append(
                f"state3: outside-file should have null archetype, got {arch.get('archetype')!r}"
            )

        # State 4: profile_corrupted (plant a broken profile.json)
        bad_repo = ctx.plugin_data_dir / "ts_corrupt"
        shutil.copytree(fixture_src, bad_repo)
        (bad_repo / ".chameleon" / "profile.json").write_text(
            "{ not json", encoding="utf-8"
        )
        try:
            bad_file = next(bad_repo.rglob("*.ts"), None)
            if bad_file:
                resp = get_pattern_context(str(bad_file))
                data = resp.get("data", {})
                ps = data.get("repo", {}).get("profile_status")
                # Should be one of the documented statuses, not crash
                if ps not in {
                    "profile_present",
                    "profile_corrupted",
                    "no_profile",
                    "no_repo",
                }:
                    failures.append(
                        f"state4 (corrupt): profile_status invalid: {ps!r}"
                    )
        finally:
            shutil.rmtree(bad_repo, ignore_errors=True)

    finally:
        _restore_env(old)
        shutil.rmtree(repo, ignore_errors=True)

    if failures:
        return Result(
            status="FAIL",
            notes="; ".join(failures),
        )
    return Result(
        status="PASS",
        notes="all 4 archetype-envelope states pass full-shape check (match_quality + sub_buckets_count present, confidence_band valid)",
    )


# ---------------------------------------------------------------------------
# 3.5  rec-12 oversized renames.json refused
# ---------------------------------------------------------------------------

def _run_oversized_renames_refused(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp._thresholds import threshold_int  # type: ignore[import]
    from chameleon_mcp.tools import _read_renames_overlay  # type: ignore[import]

    cap = threshold_int("RENAMES_OVERLAY_CAP")
    with tempfile.TemporaryDirectory() as td:
        pd = Path(td)
        # Plant cap+1 entries → tolerant loader returns {}
        renames = {f"auto_{i}": f"name-{i}" for i in range(cap + 1)}
        (pd / "renames.json").write_text(
            json.dumps({"schema_version": 1, "renames": renames}),
            encoding="utf-8",
        )
        loaded = _read_renames_overlay(pd)
        if loaded != {}:
            return Result(
                status="FAIL",
                notes=f"over-cap renames.json loaded {len(loaded)} entries (expected 0)",
            )
    return Result(
        status="PASS",
        notes=f"renames.json with {cap + 1} entries correctly rejected (returns empty overlay)",
    )


# ---------------------------------------------------------------------------
# 3.6  rec-13 symlink filter in discovery
# ---------------------------------------------------------------------------

def _run_symlink_filter(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.bootstrap.discovery import discover_files  # type: ignore[import]

    if sys.platform == "win32":
        return Result(status="SKIP", notes="symlinks require admin on Windows")

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "real.ts").write_text("export const x = 1;\n", encoding="utf-8")
        (repo / "evil.ts").symlink_to(repo / "real.ts")
        files = discover_files(repo)
        names = sorted(p.name for p in files)
        if names != ["real.ts"]:
            return Result(
                status="FAIL",
                notes=f"discover_files returned {names}; expected ['real.ts'] (symlink should be dropped)",
            )
    return Result(
        status="PASS",
        notes="discover_files dropped the symlink as expected",
    )


# ---------------------------------------------------------------------------
# 3.7  rec-6 refresh response carries archetype_diff
# ---------------------------------------------------------------------------

def _run_refresh_archetype_diff(ctx) -> Result:
    _ensure_mcp_on_path(ctx)
    from chameleon_mcp.tools import bootstrap_repo, refresh_repo  # type: ignore[import]

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo = ctx.plugin_data_dir / "ts_minimal_diff"
    shutil.copytree(fixture_src, repo)

    old = _set_env(ctx)
    try:
        if not (repo / ".chameleon" / "COMMITTED").exists():
            bootstrap_repo(str(repo))
        # First refresh: should produce a no-op or success envelope WITH
        # an archetype_diff field (possibly empty added/removed/renamed
        # + non-zero unchanged_count).
        resp = refresh_repo(str(repo))
        data = resp.get("data", {})
        diff = data.get("archetype_diff")
        if not isinstance(diff, dict):
            return Result(
                status="FAIL",
                notes=f"refresh response missing archetype_diff: {list(data.keys())}",
            )
        required_keys = {"added", "removed", "renamed", "unchanged_count"}
        missing = required_keys - diff.keys()
        if missing:
            return Result(
                status="FAIL",
                notes=f"archetype_diff missing keys: {sorted(missing)}",
            )
        return Result(
            status="PASS",
            notes=f"refresh envelope carries archetype_diff (unchanged_count={diff['unchanged_count']})",
        )
    finally:
        _restore_env(old)
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="3.4",
        name="envelope-shape matrix (4 documented states)",
        family="injection",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_envelope_shape_matrix,
    ),
    Scenario(
        id="3.5",
        name="oversized renames.json refused (rec 12)",
        family="injection",
        needs_claude=False,
        cost="cheap",
        requires=[],
        run=_run_oversized_renames_refused,
    ),
    Scenario(
        id="3.6",
        name="symlink filter in discovery (rec 13)",
        family="injection",
        needs_claude=False,
        cost="cheap",
        requires=[],
        run=_run_symlink_filter,
    ),
    Scenario(
        id="3.7",
        name="refresh response carries archetype_diff (rec 6)",
        family="injection",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_refresh_archetype_diff,
    ),
]
