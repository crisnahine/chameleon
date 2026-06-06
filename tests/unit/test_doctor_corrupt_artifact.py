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


# --------------------------------------------------------------------------
# qa25 P2 — merge-marker scan: markers inside the markdown artifacts are live
# profile state the runtime reads, so doctor must name the affected files.


class TestConflictMarkedArtifacts:
    def test_marker_laden_markdown_artifacts_are_named(self, tmp_path):
        pd = tmp_path / ".chameleon"
        pd.mkdir()
        (pd / "principles.md").write_text(
            "# Principles\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        (pd / "idioms.md").write_text("# Team idioms\n\n## active\n", encoding="utf-8")
        assert tools._conflict_marked_artifacts(pd) == ["principles.md"]

    def test_clean_artifacts_report_nothing(self, tmp_path):
        pd = tmp_path / ".chameleon"
        pd.mkdir()
        (pd / "COMMITTED").write_text("committed-at=1.0\npid=1\n", encoding="utf-8")
        (pd / "profile.summary.md").write_text("# Summary\n", encoding="utf-8")
        assert tools._conflict_marked_artifacts(pd) == []

    def test_marker_like_text_without_close_is_not_flagged(self, tmp_path):
        # A lone <<<<<<< (e.g. inside a quoted example) is not an unresolved
        # merge; both the opening and closing markers must be present.
        pd = tmp_path / ".chameleon"
        pd.mkdir()
        (pd / "idioms.md").write_text(
            "# Team idioms\n\n## active\n\n### x\n\nbody with\n<<<<<<< sample\n",
            encoding="utf-8",
        )
        assert tools._conflict_marked_artifacts(pd) == []


# --------------------------------------------------------------------------
# qa25 P3 — a garbage index.db was fail-open but never self-healed and was
# invisible to doctor; init must rebuild the derived cache instead.


class TestIndexDbSelfHeal:
    def test_corrupt_header_rebuilds_on_init(self, tmp_path):
        db = tmp_path / "index.db"
        db.write_bytes(b"this is not a sqlite database, just garbage bytes" * 10)
        conn = index_db.init_index_db(db)
        try:
            row = conn.execute("SELECT v FROM schema_meta WHERE k='schema_version'").fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_healthy_db_is_not_rebuilt(self, tmp_path):
        db = tmp_path / "index.db"
        conn = index_db.init_index_db(db)
        conn.execute(
            "INSERT OR REPLACE INTO repos (repo_id, repo_root, last_seen_at) VALUES (?, ?, ?)",
            ("r1", "/tmp/r1", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()
        conn2 = index_db.init_index_db(db)
        try:
            row = conn2.execute("SELECT repo_id FROM repos WHERE repo_id='r1'").fetchone()
            assert row is not None, "healthy db must keep its rows on re-init"
        finally:
            conn2.close()

    def test_doctor_reports_index_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        result = tools.doctor()["data"]
        by_name = {c["name"]: c for c in result["checks"]}
        assert "index_db" in by_name
        assert by_name["index_db"]["status"] == "ok"


# --------------------------------------------------------------------------
# qa25 P3 — a profile written by a NEWER engine is intact, just unreadable
# here. Display paths must say so instead of "corrupted" (detect_repo) or
# rendering the newer profile's enforcement panel (get_status).


def _too_new_profile(tmp_path):
    import json as _json

    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(
        _json.dumps({"generation": 1, "language": "ruby", "engine_min_version": "99.0.0"})
    )
    (cham / "COMMITTED").write_text("committed-at=1\npid=1\n")
    (cham / "enforcement.json").write_text(
        _json.dumps({"block_rules": {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 10}}})
    )
    return repo


class TestTooNewProfileDisplay:
    def test_detect_repo_reports_profile_too_new(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
        repo = _too_new_profile(tmp_path)
        data = tools.detect_repo(str(repo))["data"]
        assert data["profile_status"] == "profile_too_new"
        assert data["trust_state"] == "n/a"

    def test_get_status_refuses_newer_enforcement_panel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
        repo = _too_new_profile(tmp_path)
        data = tools.get_status(str(repo))["data"]
        assert data["status"] == "profile_too_new"
        assert "active" not in data, "must not render the newer profile's rules"
