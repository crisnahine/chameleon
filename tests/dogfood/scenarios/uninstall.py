"""Phase 15: clean uninstall scenario."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from tests.dogfood.scenario import Result, Scenario

_FIXTURE_REL = "tests/fixtures/eval_repos/ts_minimal"


def _ensure_mcp_on_path(ctx) -> None:
    d = str(ctx.plugin_root / "mcp")
    if d not in sys.path:
        sys.path.insert(0, d)


def _make_fresh_copy(ctx, suffix: str = "ts_minimal") -> Path:
    src = ctx.plugin_root / _FIXTURE_REL
    dest = ctx.plugin_data_dir / suffix
    shutil.copytree(src, dest)
    return dest


def _set_env(ctx, plugin_data_override: Path | None = None) -> dict:
    saved = {
        "CHAMELEON_PLUGIN_DATA": os.environ.get("CHAMELEON_PLUGIN_DATA"),
        "CHAMELEON_ALLOW_TMP_REPO": os.environ.get("CHAMELEON_ALLOW_TMP_REPO"),
    }
    pd = plugin_data_override or ctx.plugin_data_dir
    os.environ["CHAMELEON_PLUGIN_DATA"] = str(pd)
    os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# 15.1  Clean uninstall
# ---------------------------------------------------------------------------

def _run_clean_uninstall(ctx) -> Result:
    """Bootstrap + trust + hook, verify state locations, then 'uninstall' and confirm clean."""
    _ensure_mcp_on_path(ctx)

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    pd = ctx.plugin_data_dir / "pd_uninstall"
    pd.mkdir(parents=True, exist_ok=True)

    repo = _make_fresh_copy(ctx, "uninstall_repo")

    saved = _set_env(ctx, plugin_data_override=pd)
    try:
        from chameleon_mcp.tools import (  # type: ignore[import]
            bootstrap_repo,
            get_pattern_context,
            list_profiles,
            trust_profile,
            _compute_repo_id,
        )

        # Use force=True so the orchestrator runs even though the fixture ships
        # with a pre-committed .chameleon/ profile. This also populates index.db,
        # which is part of what we want to verify and clean up.
        boot = bootstrap_repo(str(repo), force=True)
        if boot.get("data", {}).get("status") not in ("success", "already_bootstrapped"):
            return Result(
                status="FAIL",
                notes=f"bootstrap failed: {boot.get('data', {})!r:.200}",
            )

        trust_profile(str(repo), repo.name)
        repo_id = _compute_repo_id(repo)

        # Run a get_pattern_context to trigger metrics emission
        ts_file = next(repo.rglob("*.ts"), repo / "src" / "index.ts")
        get_pattern_context(str(ts_file))

        # --- Verify state is exactly where expected ---
        chameleon_in_repo = repo / ".chameleon"
        per_repo_data_dir = pd / repo_id
        index_db_path = pd / "index.db"
        metrics_path = pd / "metrics.jsonl"

        failures_pre: list[str] = []
        if not chameleon_in_repo.is_dir():
            failures_pre.append(f".chameleon/ missing inside fixture at {chameleon_in_repo}")
        if not per_repo_data_dir.is_dir():
            failures_pre.append(f"per-repo data dir missing at {per_repo_data_dir}")
        if not index_db_path.is_file():
            failures_pre.append(f"index.db missing at {index_db_path}")
        # metrics.jsonl may or may not be written (best-effort); skip hard check

        if failures_pre:
            return Result(
                status="FAIL",
                notes="pre-uninstall state check failed: " + "; ".join(failures_pre),
            )

        # --- Perform "uninstall" ---
        # 1. Remove repo-embedded state
        shutil.rmtree(chameleon_in_repo, ignore_errors=True)
        # 2. Remove per-repo plugin data
        shutil.rmtree(per_repo_data_dir, ignore_errors=True)
        # 3. Delete index.db row for this repo
        from chameleon_mcp import index_db as _idx  # type: ignore[import]
        _idx.forget_repo(repo_id, db_path=index_db_path)
        # 4. Truncate metrics.jsonl
        if metrics_path.is_file():
            metrics_path.write_bytes(b"")

        # --- Verify post-uninstall ---
        # list_profiles must return no entry for this repo
        lp_after = list_profiles()
        profiles_after = lp_after.get("data", {}).get("profiles", [])
        leftover_ids = [p.get("repo_id") for p in profiles_after if p.get("repo_id") == repo_id]

        # get_pattern_context must return no_profile or no_repo
        resp_after = get_pattern_context(str(ts_file))
        data_after = resp_after.get("data", {})
        repo_info_after = data_after.get("repo", {})
        profile_status_after = repo_info_after.get("profile_status", "")
    finally:
        _restore_env(saved)

    failures: list[str] = []
    if leftover_ids:
        failures.append(
            f"list_profiles still has {len(leftover_ids)} entry/entries for repo_id {repo_id[:8]}..."
        )
    if profile_status_after not in ("no_profile", "no_repo", "profile_corrupted"):
        failures.append(
            f"get_pattern_context after uninstall returned profile_status={profile_status_after!r}; "
            f"expected no_profile / no_repo"
        )

    if failures:
        return Result(status="FAIL", notes="; ".join(failures))

    return Result(
        status="PASS",
        notes=(
            f"uninstall complete: index entry removed, .chameleon/ gone, "
            f"per-repo data dir removed; post-uninstall profile_status={profile_status_after!r}"
        ),
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="15.1",
        name="clean uninstall (verify state isolation)",
        family="uninstall",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_clean_uninstall,
    ),
]
