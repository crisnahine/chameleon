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

_ENV_VARS = {"env-ts": "CHAMELEON_TEST_TS_REPO", "env-ruby": "CHAMELEON_TEST_RUBY_REPO"}
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


def bootstrap_fixture(work_dir: Path) -> None:
    """Derive the profile in-process and commit .chameleon into the fixture repo."""
    resp = _bootstrap_repo(str(work_dir))
    status = (resp.get("data") or {}).get("status")
    if status not in _OK_STATUSES:
        raise EffBootstrapError(f"bootstrap of {work_dir} returned status {status!r}")
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
