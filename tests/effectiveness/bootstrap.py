"""Profile bootstrap-once per (fixture, run), trust grants, env-repo resolution.

Cost control per the spec: the committed fixture is bootstrapped ONCE per run
(in-process, free) and the profile is committed into the fixture repo, so
every per-cell worktree materializes it via checkout instead of re-deriving.
Env-pointed real repos (tier full) re-use their existing committed profile —
it is per-(repo, commit) by construction because it lives in git.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.journey.harness.bash import run_bash

_ENV_VARS = {
    "env-ts": "CHAMELEON_TEST_TS_REPO",
    "env-ruby": "CHAMELEON_TEST_RUBY_REPO",
    "env-py": "CHAMELEON_TEST_PYTHON_REPO",
}
_OK_STATUSES = {"success", "already_bootstrapped"}


class EffBootstrapError(Exception):
    pass


def ensure_chameleon_env(ctx_env: dict[str, str]) -> None:
    """Mirror the run's isolation env into THIS process.

    chameleon_mcp reads CHAMELEON_PLUGIN_DATA / TMPDIR / the HMAC key path at
    call time; the runner process must see the same values the hooks saw or
    scoring would read the wrong trust records and exec logs.
    """
    for key in (
        "CHAMELEON_PLUGIN_DATA",
        "CHAMELEON_HMAC_KEY_PATH",
        "TMPDIR",
        "CHAMELEON_HOOK_ERROR_LOG",
    ):
        if key in ctx_env:
            os.environ[key] = ctx_env[key]


def _bootstrap_repo(path: str) -> dict:
    """Seam: tests monkeypatch this."""
    from chameleon_mcp.tools import bootstrap_repo

    return bootstrap_repo(path)


# Competing imports each fixture's team has "taught", by archetype. Derivation
# cannot infer these -- `competing` is populated solely by teaching -- and
# without them the fixtures have no rule with teeth: a straightforward new file
# conforms trivially, which is precisely why the first turn-depth run measured
# zero violations in BOTH arms and could not have produced a signal in either
# direction (results-published/depth-arm-2026-07-27.md).
#
# Each pair names a real wrapper the fixture already uses and a plausible
# alternative a drifting model reaches for once early context stops steering.
_TAUGHT_COMPETING: dict[str, tuple[tuple[str, str, str], ...]] = {
    "eff_ts": (("component", "../utils/cx", "classnames"),),
}


def _teach_fixture_conventions(work_dir: Path) -> None:
    """Seed the taught competing imports for this fixture, if it has any.

    NOT best-effort. A pair that fails to bind -- a drifted archetype key after a
    clustering change, a corrupt profile -- leaves the fixture with no rule that
    has teeth, and the arm then scores zero violations in BOTH arms. That is the
    same number a perfect run produces and the opposite conclusion, and it is
    exactly the non-result the depth arm published on its first attempt. Failing
    the run is the only outcome that cannot be mistaken for a measurement.
    """
    pairs = _TAUGHT_COMPETING.get(work_dir.name)
    if not pairs:
        return
    from chameleon_mcp.tools import _compute_repo_id, teach_competing_import

    repo_id = _compute_repo_id(work_dir)
    for archetype, preferred, over in pairs:
        resp = teach_competing_import(
            repo_id, archetype=archetype, preferred=preferred, over=over
        )
        status = (resp.get("data") or {}).get("status")
        if status != "success":
            raise EffBootstrapError(
                f"teaching {preferred!r} over {over!r} on archetype {archetype!r} in "
                f"{work_dir.name} returned status {status!r}; the fixture would have no "
                "rule with teeth and the arm would score a meaningless zero"
            )


def bootstrap_fixture(work_dir: Path) -> None:
    """Derive the profile in-process and commit .chameleon into the fixture repo."""
    resp = _bootstrap_repo(str(work_dir))
    status = (resp.get("data") or {}).get("status")
    if status not in _OK_STATUSES:
        raise EffBootstrapError(f"bootstrap of {work_dir} returned status {status!r}")
    # Teach BEFORE the commit so the taught pair rides the committed profile the
    # worktrees read, exactly as it would in a real repo.
    _teach_fixture_conventions(work_dir)
    ident = "-c user.name=effectiveness -c user.email=eff@local"
    r = run_bash(
        f"git {ident} add .chameleon && git {ident} commit -q -m 'chameleon profile'",
        cwd=work_dir,
        timeout_s=60,
    )
    if r.returncode != 0:
        raise EffBootstrapError(f"committing profile failed: {r.stderr.strip()}")


def grant_worktree_trust(worktree: Path) -> str:
    """Grant trust for one worktree root; returns the repo_id.

    Worktrees share the fixture's loopback-origin remote URL, so they share
    one repo_id; grant_trust is additive per resolved root, which is exactly
    the per-worktree coverage scoring needs.
    """
    from chameleon_mcp.profile.trust import grant_trust
    from chameleon_mcp.tools import _compute_repo_id

    repo_id = _compute_repo_id(worktree)
    grant_trust(repo_id, worktree / ".chameleon")
    return repo_id


def env_repo_root(fixture: str) -> tuple[Path | None, str | None]:
    """Resolve an env-pointed tier-full repo. Returns (root, None) or (None, reason)."""
    var = _ENV_VARS.get(fixture)
    if var is None:
        return None, f"unknown env fixture {fixture!r}"
    raw = os.environ.get(var, "")
    if not raw:
        return None, f"{var} not set; tier-full {fixture} tasks skipped"
    root = Path(raw)
    if not root.is_dir():
        return None, f"{var}={raw} is not a directory"
    if not (root / ".chameleon" / "profile.json").is_file():
        return None, f"{var}={raw} has no committed .chameleon profile (bootstrap it first)"
    return root, None
