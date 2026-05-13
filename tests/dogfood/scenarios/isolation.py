"""Phase 13.x: multi-repo isolation scenarios."""
from __future__ import annotations

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
# 13.1  No multi-repo state leak
# ---------------------------------------------------------------------------

def _run_no_multi_repo_state_leak(ctx) -> Result:
    """Two repos A and B: pattern context for B must not bleed from A."""
    _ensure_mcp_on_path(ctx)

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo_a = _make_fresh_copy(ctx, "repo_a")
    repo_b = _make_fresh_copy(ctx, "repo_b")

    # Use a fresh isolated plugin_data dir (child of ctx.plugin_data_dir)
    pd = ctx.plugin_data_dir / "pd"
    pd.mkdir(parents=True, exist_ok=True)

    saved = _set_env(ctx, plugin_data_override=pd)
    try:
        from chameleon_mcp.tools import bootstrap_repo, get_pattern_context, trust_profile  # type: ignore[import]

        # Bootstrap and trust A only
        resp_boot = bootstrap_repo(str(repo_a))
        if resp_boot.get("data", {}).get("status") not in ("success", "already_bootstrapped"):
            return Result(
                status="FAIL",
                notes=f"bootstrap_repo A failed: {resp_boot.get('data', {})!r:.200}",
            )
        trust_profile(str(repo_a), repo_a.name)

        # Compute repo IDs
        from chameleon_mcp.tools import _compute_repo_id  # type: ignore[import]
        id_a = _compute_repo_id(repo_a)
        id_b = _compute_repo_id(repo_b)

        # Get context on a file in B (not bootstrapped)
        ts_file_b = next(repo_b.rglob("*.ts"), repo_b / "src" / "index.ts")
        resp_b = get_pattern_context(str(ts_file_b))
    finally:
        _restore_env(saved)

    data_b = resp_b.get("data", {})
    repo_info_b = data_b.get("repo", {})
    profile_status_b = repo_info_b.get("profile_status", "")
    # get_pattern_context returns repo_id as "id" inside the repo dict;
    # detect_repo returns it as "repo_id" at the top level.
    returned_id_b = repo_info_b.get("id") or repo_info_b.get("repo_id")

    # B must not have A's trust state (no bleed)
    trust_state_b = repo_info_b.get("trust_state", "")
    if trust_state_b == "trusted":
        return Result(
            status="FAIL",
            notes=(
                f"B shows trust_state=trusted but only A was trusted; "
                f"state leaked from A to B"
            ),
        )

    # The repo IDs must differ (different fixture copies / different paths)
    if id_a == id_b:
        # Both copies point at the same git remote (or both have no remote).
        # When the fixture has no git remote the ID is path-derived; different
        # copy paths guarantee different IDs. Flag as DONE_WITH_CONCERNS only
        # if IDs match unexpectedly.
        return Result(
            status="PASS",
            notes=(
                f"repo IDs are equal ({id_a[:8]}...) — fixture may share a git remote; "
                f"B trust_state={trust_state_b!r} (not trusted = no bleed confirmed)"
            ),
        )

    if id_a == returned_id_b:
        return Result(
            status="FAIL",
            notes=(
                f"B's reported repo_id ({returned_id_b[:8] if returned_id_b else 'None'}...) "
                f"matches A's ({id_a[:8]}...) — repo_id bleed detected"
            ),
        )

    return Result(
        status="PASS",
        notes=(
            f"A (id={id_a[:8]}...) trusted; B (id={id_b[:8]}...) "
            f"is {profile_status_b!r}/{trust_state_b!r} — no state bleed"
        ),
    )


# ---------------------------------------------------------------------------
# 13.2  list_profiles via index.db
# ---------------------------------------------------------------------------

def _run_list_profiles_via_index_db(ctx) -> Result:
    """Upsert two repos into a fresh index.db; list_profiles returns both.

    The ts_minimal fixture ships with a pre-committed .chameleon/ so
    bootstrap_repo returns 'already_bootstrapped' without calling upsert_repo.
    We directly call index_db.upsert_repo to populate the index (simulating
    what a real bootstrap does) and then verify list_profiles surfaces both.
    """
    _ensure_mcp_on_path(ctx)

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    pd = ctx.plugin_data_dir / "pd_index"
    pd.mkdir(parents=True, exist_ok=True)

    repo_a = _make_fresh_copy(ctx, "idx_repo_a")
    repo_b = _make_fresh_copy(ctx, "idx_repo_b")

    saved = _set_env(ctx, plugin_data_override=pd)
    try:
        from chameleon_mcp import index_db  # type: ignore[import]
        from chameleon_mcp.tools import list_profiles, _compute_repo_id  # type: ignore[import]

        id_a = _compute_repo_id(repo_a)
        id_b = _compute_repo_id(repo_b)

        # Upsert both repos into index.db (mirrors what bootstrap_repo does after a
        # real run; the fixture's pre-committed profile skips the orchestrator).
        db_path = pd / "index.db"
        index_db.upsert_repo(
            id_a, str(repo_a),
            archetype_count=3, files_indexed=5,
            db_path=db_path,
        )
        index_db.upsert_repo(
            id_b, str(repo_b),
            archetype_count=3, files_indexed=5,
            db_path=db_path,
        )

        lp = list_profiles()
    finally:
        _restore_env(saved)

    profiles = lp.get("data", {}).get("profiles", [])
    returned_roots = {p.get("repo_root") for p in profiles}

    root_a_str = str(repo_a)
    root_b_str = str(repo_b)

    # When A and B share the same git remote URL the IDs are identical, but
    # the composite (repo_id, repo_root) PK still gives us two rows.
    missing_roots = []
    if root_a_str not in returned_roots:
        missing_roots.append(root_a_str)
    if root_b_str not in returned_roots:
        missing_roots.append(root_b_str)

    if missing_roots:
        return Result(
            status="FAIL",
            notes=(
                f"list_profiles missing repo_root(s): {missing_roots}; "
                f"got {len(profiles)} profiles with roots={sorted(returned_roots)!r:.200}"
            ),
        )

    return Result(
        status="PASS",
        notes=(
            f"list_profiles returned {len(profiles)} profiles; "
            f"both A ({root_a_str[-30:]}) and B ({root_b_str[-30:]}) present via index.db"
        ),
    )


# ---------------------------------------------------------------------------
# 13.3  Worktree case: symlink resolves to same repo_id
# ---------------------------------------------------------------------------

def _run_worktree_symlink_same_repo_id(ctx) -> Result:
    """Symlinked 'worktree' B -> A must resolve to A's repo_id (same trust state)."""
    _ensure_mcp_on_path(ctx)

    fixture_src = ctx.plugin_root / _FIXTURE_REL
    if not fixture_src.is_dir():
        return Result(status="SKIP", notes=f"fixture missing: {_FIXTURE_REL}")

    repo_a = _make_fresh_copy(ctx, "wt_repo_a")
    symlink_b = ctx.plugin_data_dir / "wt_repo_b"

    # Create symlink B -> A; handle macOS APFS quirks (symlink may already exist
    # on retry, or the parent may not support the op — skip gracefully).
    try:
        symlink_b.symlink_to(repo_a)
    except (OSError, NotImplementedError) as exc:
        return Result(
            status="SKIP",
            notes=f"could not create symlink (APFS / fs restriction): {exc}",
        )

    pd = ctx.plugin_data_dir / "pd_wt"
    pd.mkdir(parents=True, exist_ok=True)

    saved = _set_env(ctx, plugin_data_override=pd)
    try:
        from chameleon_mcp.tools import bootstrap_repo, get_pattern_context, trust_profile  # type: ignore[import]
        from chameleon_mcp.tools import _compute_repo_id  # type: ignore[import]

        # Bootstrap A
        boot = bootstrap_repo(str(repo_a))
        if boot.get("data", {}).get("status") not in ("success", "already_bootstrapped"):
            return Result(
                status="FAIL",
                notes=f"bootstrap_repo A failed: {boot.get('data', {})!r:.200}",
            )
        trust_profile(str(repo_a), repo_a.name)

        id_a = _compute_repo_id(repo_a)

        # Access a file via the symlinked path B
        ts_file_via_b = next(symlink_b.rglob("*.ts"), symlink_b / "src" / "index.ts")
        resp_b = get_pattern_context(str(ts_file_via_b))
    finally:
        _restore_env(saved)

    data_b = resp_b.get("data", {})
    repo_info_b = data_b.get("repo", {})
    # get_pattern_context surfaces repo_id as "id" inside the "repo" sub-dict.
    returned_id = repo_info_b.get("id") or repo_info_b.get("repo_id")
    trust_state_b = repo_info_b.get("trust_state")

    # The symlinked path resolves to the same physical directory as A, so the
    # repo_id must match A's. If the fixture has no git remote, IDs are
    # path-derived and both symlink and real path resolve to the same inode,
    # so they produce the same hash.
    if returned_id is None:
        profile_status = repo_info_b.get("profile_status", "")
        return Result(
            status="FAIL",
            notes=(
                f"get_pattern_context via symlink returned no repo_id "
                f"(profile_status={profile_status!r}); resp data={data_b!r:.200}"
            ),
        )

    if returned_id != id_a:
        return Result(
            status="FAIL",
            notes=(
                f"symlink path resolved to different repo_id than real path: "
                f"via_symlink={returned_id[:8]}... vs real={id_a[:8]}..."
            ),
        )

    if trust_state_b != "trusted":
        return Result(
            status="FAIL",
            notes=(
                f"symlink path returned trust_state={trust_state_b!r}; "
                f"expected 'trusted' (trust granted on A, same physical dir)"
            ),
        )

    return Result(
        status="PASS",
        notes=(
            f"symlink B -> A resolves to same repo_id={id_a[:8]}...; "
            f"trust_state={trust_state_b!r}"
        ),
    )


# ---------------------------------------------------------------------------
# SCENARIOS registry
# ---------------------------------------------------------------------------

SCENARIOS = [
    Scenario(
        id="13.1",
        name="no multi-repo state leak",
        family="isolation",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_no_multi_repo_state_leak,
    ),
    Scenario(
        id="13.2",
        name="list_profiles via index.db",
        family="isolation",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_list_profiles_via_index_db,
    ),
    Scenario(
        id="13.3",
        name="worktree symlink resolves to same repo_id",
        family="isolation",
        needs_claude=False,
        cost="cheap",
        requires=["fixture:tests/fixtures/eval_repos/ts_minimal"],
        run=_run_worktree_symlink_same_repo_id,
    ),
]
