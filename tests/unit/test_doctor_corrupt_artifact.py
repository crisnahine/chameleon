"""doctor's known_repos check must detect a corrupt committed profile.

The per-repo health check only verified the COMMITTED sentinel via is_committed,
so a profile that is committed but whose JSON is corrupt (load_profile_dir
rejects it on every edit, leaving the user with silent advisory degradation)
was reported as healthy "profile_present". doctor must actually load the
profile and surface corruption.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import index_db, tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    # The index.db connection is cached at module level; drop it so this test
    # binds to its own CHAMELEON_PLUGIN_DATA instead of a prior test's tmp dir.
    index_db.close_index_connections()
    tools._clear_repo_id_cache()
    yield
    index_db.close_index_connections()
    tools._clear_repo_id_cache()


def _register_committed_repo(repo, *, canonicals: str) -> None:
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / "COMMITTED").write_text("committed-at=1.0\npid=1\n", encoding="utf-8")
    # profile.json must exist or list_profiles prunes the repo as a dead
    # temp leftover before doctor ever inspects it.
    (cham / "profile.json").write_text(
        json.dumps(
            {"schema_version": 8, "repo_id": "x", "language": "typescript", "generation": 1}
        ),
        encoding="utf-8",
    )
    (cham / "canonicals.json").write_text(canonicals, encoding="utf-8")
    index_db.upsert_repo(
        tools._compute_repo_id(repo), str(repo), archetype_count=1, files_indexed=1
    )


def _known_repo_state(repo):
    checks = tools.doctor().get("data", {}).get("checks", [])
    kr = next(c for c in checks if c["name"] == "known_repos")
    state = next((s for s in kr["detail"] if s["repo_root"] == str(repo)), None)
    return kr, state


def test_doctor_flags_corrupt_committed_profile(tmp_path):
    repo = tmp_path / "repo"
    _register_committed_repo(repo, canonicals="{garbage")

    kr, state = _known_repo_state(repo)

    assert state is not None, "repo should appear in known_repos"
    assert state["profile_status"] == "profile_corrupt"
    assert kr["status"] == "warn"
