"""A linked git worktree inherits the main checkout's profile and trust.

End-to-end through detect_repo and get_pattern_context: before the fix, a
worktree (whose .chameleon/ is gitignored and absent) reported no_profile / n/a
and every hook silently no-opped. After it, the worktree resolves the main
worktree's committed profile and inherits its trust grant — with no extra
/chameleon-trust and regardless of where the worktree lives on disk.

A standalone sibling repo (its own .git directory, no .chameleon) must still
report no_profile: the repo-boundary behavior for real separate repos is
unchanged.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chameleon_mcp import index_db, tools
from chameleon_mcp.profile.trust import grant_trust
from chameleon_mcp.repo_id import _compute_repo_id


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    index_db.close_index_connections()
    tools._clear_repo_id_cache()
    from chameleon_mcp.profile import loader as _loader

    _loader._REPO_ROOT_CACHE.clear()
    yield
    index_db.close_index_connections()
    tools._clear_repo_id_cache()
    _loader._REPO_ROOT_CACHE.clear()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_main_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@t.t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("remote", "add", "origin", "https://github.com/x/cham-wt-int.git", cwd=root)
    (root / "app" / "models").mkdir(parents=True)
    (root / "app" / "models" / "listing.rb").write_text("class Listing; end\n", encoding="utf-8")
    (root / ".gitignore").write_text(".chameleon/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    cham = root / ".chameleon"
    cham.mkdir()
    (cham / "profile.json").write_text(
        json.dumps({"schema_version": 8, "generation": 1, "language": "ruby"}), encoding="utf-8"
    )
    (cham / "archetypes.json").write_text(
        json.dumps({"schema_version": 8, "generation": 1, "archetypes": {}}), encoding="utf-8"
    )
    (cham / "canonicals.json").write_text(
        json.dumps({"schema_version": 8, "generation": 1, "canonicals": {}}), encoding="utf-8"
    )
    (cham / "rules.json").write_text(
        json.dumps({"schema_version": 8, "generation": 1, "rules": {}}), encoding="utf-8"
    )
    (cham / "COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")
    return root


def _detect(path: Path) -> dict:
    env = tools.detect_repo(str(path))
    if isinstance(env, str):
        env = json.loads(env)
    return env.get("data", env)


class TestWorktreeInheritsProfileAndTrust:
    def test_sibling_worktree_inherits_trusted_profile(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        grant_trust(_compute_repo_id(main), main / ".chameleon")
        wt = tmp_path / "sibling-wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)

        d = _detect(wt / "app" / "models" / "listing.rb")
        assert d["profile_status"] == "profile_present"
        assert d["trust_state"] == "trusted"
        # same git-remote-derived repo_id as the main checkout
        assert d["repo_id"] == _detect(main / "app" / "models" / "listing.rb")["repo_id"]

    def test_custom_path_worktree_inherits_trusted_profile(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        grant_trust(_compute_repo_id(main), main / ".chameleon")
        wt = tmp_path / "anywhere" / "deep" / "feature"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)

        d = _detect(wt / "app" / "models" / "listing.rb")
        assert d["profile_status"] == "profile_present"
        assert d["trust_state"] == "trusted"

    def test_get_pattern_context_resolves_in_worktree(self, tmp_path):
        main = _make_main_repo(tmp_path / "main")
        grant_trust(_compute_repo_id(main), main / ".chameleon")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)

        env = tools.get_pattern_context(str(wt / "app" / "models" / "listing.rb"))
        if isinstance(env, str):
            env = json.loads(env)
        data = env.get("data", env)
        repo = data.get("repo", {})
        # the profile was found (the bug was a no_profile no-op) and trust carried
        assert repo.get("profile_status") != "no_profile"
        assert repo.get("trust_state") == "trusted"

    def test_worktree_untrusted_until_main_granted(self, tmp_path):
        # No grant anywhere: the worktree finds the profile but is untrusted,
        # exactly like the main checkout would be. (Not n/a, not silently off.)
        main = _make_main_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=main)

        d = _detect(wt / "app" / "models" / "listing.rb")
        assert d["profile_status"] == "profile_present"
        assert d["trust_state"] == "untrusted"


class TestStandaloneRepoBoundaryUnchanged:
    def test_separate_sibling_repo_without_profile_still_no_profile(self, tmp_path):
        # A real separate repo (own .git DIRECTORY, no .chameleon) must NOT
        # borrow a profile from anywhere: the hard repo boundary is preserved.
        other = tmp_path / "other-repo"
        other.mkdir()
        _git("init", "-q", cwd=other)
        (other / "x.rb").write_text("class X; end\n", encoding="utf-8")
        d = _detect(other / "x.rb")
        assert d["profile_status"] == "no_profile"
