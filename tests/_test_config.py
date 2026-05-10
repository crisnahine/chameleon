"""Shared test configuration: resolves CHAMELEON_TEST_TS_REPO /
CHAMELEON_TEST_RUBY_REPO from the environment (or .env in the repo root)
so test files don't hardcode user-specific paths.

Usage from a test file:

    from _test_config import TS_REPO, RUBY_REPO, require_repo

    repo = require_repo(TS_REPO, "TypeScript")  # SKIPs the file if unset
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Best-effort .env loader (no python-dotenv dependency)."""
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _resolve(env_var: str) -> Path | None:
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


TS_REPO: Path | None = _resolve("CHAMELEON_TEST_TS_REPO")
RUBY_REPO: Path | None = _resolve("CHAMELEON_TEST_RUBY_REPO")
PLUGIN_ROOT: Path = REPO_ROOT


def require_repo(repo: Path | None, label: str) -> Path:
    """Return the repo path or `sys.exit(0)` with a SKIP message if unset.

    Tests that absolutely require a repo path call this at the top to
    skip the file gracefully when the env var isn't set.
    """
    if repo is None:
        env_name = (
            "CHAMELEON_TEST_TS_REPO" if "TypeScript" in label
            else "CHAMELEON_TEST_RUBY_REPO"
        )
        print(f"SKIP: {label} repo not configured (set {env_name} in .env)")
        sys.exit(0)
    return repo


def require_any_repo() -> tuple[Path | None, Path | None]:
    """Return (TS_REPO, RUBY_REPO); skip the test file only if BOTH are unset."""
    if TS_REPO is None and RUBY_REPO is None:
        print(
            "SKIP: no test repos configured "
            "(set CHAMELEON_TEST_TS_REPO and/or CHAMELEON_TEST_RUBY_REPO in .env)"
        )
        sys.exit(0)
    return TS_REPO, RUBY_REPO
