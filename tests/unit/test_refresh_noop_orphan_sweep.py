"""A noop refresh must still sweep orphaned profile backups.

A crash between the atomic swap and the backup rmtree leaves a stray
`.chameleon.backup-<txn>` beside an INTACT live profile. The orphan sweep is
wired into the full-bootstrap and partial-refresh paths, but a day-to-day
refresh that noops (nothing changed) used to return before ever reaching it,
so the debris survived indefinitely under normal usage. The working-tree noop
envelope must sweep before returning; the production-pinned noop case lives in
test_production_derivation.py.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp import tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import index_db
    from chameleon_mcp.profile import loader as _loader

    monkeypatch.setattr(index_db, "_INDEX_CONN", None)
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()
    yield
    _loader._PROFILE_CACHE.clear()
    _loader._REPO_ROOT_CACHE.clear()
    tools._clear_repo_id_cache()


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _make_ts_repo(root: Path) -> Path:
    """Minimal TypeScript git repo a bootstrap can profile."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "package.json").write_text('{"name": "fixture", "private": true}\n', encoding="utf-8")
    (root / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}\n', encoding="utf-8")
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    services = root / "src" / "services"
    services.mkdir(parents=True, exist_ok=True)
    for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"):
        (services / f"{name.lower()}Service.ts").write_text(
            f"export class {name}Service {{\n  run(x: string) {{ return x }}\n}}\n",
            encoding="utf-8",
        )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "baseline")
    return root


def _plant_stray_backup(repo: Path) -> Path:
    """The post-swap crash state: the OLD profile, COMMITTED sentinel and all,
    still sitting in its backup dir next to the intact live one."""
    stray = repo / ".chameleon.backup-0-deadbeef-1"
    stray.mkdir()
    (stray / "COMMITTED").write_text("committed-at=1.0\npid=1\n", encoding="utf-8")
    (stray / "profile.json").write_text(json.dumps({"schema_version": 8}), encoding="utf-8")
    return stray


def test_noop_refresh_sweeps_stray_backup(tmp_path):
    repo = _make_ts_repo(tmp_path / "repo")
    assert tools.bootstrap_repo(str(repo))["data"]["status"] == "success"
    # Control: nothing changed, so the refresh takes the noop early-return --
    # the exact path that used to skip the orphan sweep.
    assert tools.refresh_repo(str(repo))["data"]["status"] == "noop"
    stray = _plant_stray_backup(repo)
    out = tools.refresh_repo(str(repo))
    assert out["data"]["status"] == "noop", "fixture must still hit the noop path"
    assert not stray.exists(), "noop refresh left the stray backup dir in place"
