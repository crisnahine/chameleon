"""Bootstrap-once wiring (chameleon calls stubbed; git is real)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.effectiveness import bootstrap
from tests.journey.harness.bash import run_bash
from tests.journey.harness.fixtures import setup_fixture


def _seed_repo(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "a.ts").write_text("export const a = 1;\n")
    work_dir, _ = setup_fixture("fix", seed, tmp_path / "working")
    return work_dir


def test_bootstrap_fixture_commits_profile(tmp_path, monkeypatch):
    work_dir = _seed_repo(tmp_path)

    def fake_bootstrap(path: str) -> dict:
        cham = Path(path) / ".chameleon"
        cham.mkdir(exist_ok=True)
        (cham / "profile.json").write_text(json.dumps({"schema_version": 8}))
        (cham / "calls_index.json").write_text(json.dumps({"schema_version": 1, "callees": {}}))
        return {"api_version": "1", "data": {"status": "success"}}

    monkeypatch.setattr(bootstrap, "_bootstrap_repo", fake_bootstrap)
    bootstrap.bootstrap_fixture(work_dir)
    # profile committed -> a fresh worktree carries it
    r = run_bash("git show HEAD:.chameleon/profile.json", cwd=work_dir)
    assert r.returncode == 0 and "schema_version" in r.stdout
    # clean tree afterwards
    assert run_bash("git status --porcelain", cwd=work_dir).stdout.strip() == ""


def test_bootstrap_fixture_raises_on_failure_status(tmp_path, monkeypatch):
    work_dir = _seed_repo(tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "_bootstrap_repo",
        lambda path: {"api_version": "1", "data": {"status": "failed_unsupported_language"}},
    )
    with pytest.raises(bootstrap.EffBootstrapError, match="failed_unsupported_language"):
        bootstrap.bootstrap_fixture(work_dir)


def test_env_repo_root_resolution(tmp_path, monkeypatch):
    repo = tmp_path / "real-repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "profile.json").write_text("{}")
    monkeypatch.setenv("CHAMELEON_TEST_TS_REPO", str(repo))
    monkeypatch.delenv("CHAMELEON_TEST_RUBY_REPO", raising=False)

    root, reason = bootstrap.env_repo_root("env-ts")
    assert root == repo and reason is None

    root, reason = bootstrap.env_repo_root("env-ruby")
    assert root is None and "CHAMELEON_TEST_RUBY_REPO" in reason


def test_env_repo_without_profile_reports_reason(tmp_path, monkeypatch):
    repo = tmp_path / "bare-repo"
    repo.mkdir()
    monkeypatch.setenv("CHAMELEON_TEST_TS_REPO", str(repo))
    root, reason = bootstrap.env_repo_root("env-ts")
    assert root is None and ".chameleon" in reason
