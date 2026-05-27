"""Unit tests for chameleon_mcp.profile.trust — hashing, grant, state queries."""
from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.profile.trust import (
    TrustRecord,
    grant_trust,
    hash_profile,
    is_material_change,
    trust_state_for,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile_dir(root: Path, *, extra_files: dict[str, str] | None = None) -> Path:
    """Create a minimal .chameleon/ with the four required artifacts + COMMITTED."""
    profile_dir = root / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)

    defaults = {
        "profile.json": json.dumps({"generation": 1, "language": "typescript"}),
        "archetypes.json": json.dumps({"generation": 1, "archetypes": {}}),
        "rules.json": json.dumps({"generation": 1, "rules": []}),
        "canonicals.json": json.dumps({"generation": 1, "canonicals": {}}),
    }
    if extra_files:
        defaults.update(extra_files)

    for name, content in defaults.items():
        (profile_dir / name).write_text(content, encoding="utf-8")

    # COMMITTED sentinel so loader accepts the profile
    (profile_dir / "COMMITTED").touch()
    return profile_dir


# ---------------------------------------------------------------------------
# hash_profile
# ---------------------------------------------------------------------------

class TestHashProfile:
    def test_deterministic_for_known_content(self, tmp_path: Path):
        profile_dir = _make_profile_dir(tmp_path)
        h1 = hash_profile(profile_dir)
        h2 = hash_profile(profile_dir)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_changes_when_content_changes(self, tmp_path: Path):
        profile_dir = _make_profile_dir(tmp_path)
        h_before = hash_profile(profile_dir)

        # mutate rules.json
        (profile_dir / "rules.json").write_text(
            json.dumps({"generation": 2, "rules": [{"id": "r1"}]}),
            encoding="utf-8",
        )
        h_after = hash_profile(profile_dir)
        assert h_before != h_after

    def test_returns_empty_when_profile_json_missing(self, tmp_path: Path):
        profile_dir = tmp_path / ".chameleon"
        profile_dir.mkdir()
        # no profile.json at all
        assert hash_profile(profile_dir) == ""

    def test_optional_idioms_md_changes_hash(self, tmp_path: Path):
        profile_dir = _make_profile_dir(tmp_path)
        h_without = hash_profile(profile_dir)

        (profile_dir / "idioms.md").write_text("# always use foo()", encoding="utf-8")
        h_with = hash_profile(profile_dir)
        assert h_without != h_with


# ---------------------------------------------------------------------------
# TrustRecord round-trip
# ---------------------------------------------------------------------------

class TestTrustRecord:
    def test_to_dict_from_dict_round_trip(self):
        record = TrustRecord(
            granted_at="2025-01-01T00:00:00Z",
            granted_by_user="testuser",
            profile_sha256="abc123",
            repo_root="/tmp/repo",
            repo_root_specific_hashes={"/tmp/repo": "abc123"},
        )
        d = record.to_dict()
        restored = TrustRecord.from_dict(d)
        assert restored.granted_at == record.granted_at
        assert restored.granted_by_user == record.granted_by_user
        assert restored.profile_sha256 == record.profile_sha256
        assert restored.repo_root == record.repo_root
        assert restored.repo_root_specific_hashes == record.repo_root_specific_hashes

    def test_from_dict_omits_repo_root_specific_hashes_when_empty(self):
        record = TrustRecord(
            granted_at="2025-01-01T00:00:00Z",
            granted_by_user="testuser",
            profile_sha256="abc123",
            repo_root="/tmp/repo",
        )
        d = record.to_dict()
        # empty map should not appear in serialized form (backward compat)
        assert "repo_root_specific_hashes" not in d

    def test_from_dict_handles_missing_fields_gracefully(self):
        record = TrustRecord.from_dict({})
        assert record.granted_at == ""
        assert record.granted_by_user == ""
        assert record.profile_sha256 == ""
        assert record.repo_root_specific_hashes == {}

    def test_from_dict_drops_non_string_map_entries(self):
        record = TrustRecord.from_dict({
            "granted_at": "2025-01-01T00:00:00Z",
            "granted_by_user": "u",
            "profile_sha256": "sha",
            "repo_root_specific_hashes": {
                "/good": "hash1",
                123: "bad_key",
                "/also_bad": 999,
            },
        })
        assert record.repo_root_specific_hashes == {"/good": "hash1"}


# ---------------------------------------------------------------------------
# grant_trust + trust_state_for round-trip
# ---------------------------------------------------------------------------

class TestGrantAndQuery:
    def test_grant_then_query_returns_trusted(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        repo_root = tmp_path / "repo"
        profile_dir = _make_profile_dir(repo_root)
        repo_id = "test-repo-001"

        grant_trust(repo_id, profile_dir)
        record = trust_state_for(repo_id)

        assert record is not None
        assert record.profile_sha256 == hash_profile(profile_dir)
        assert record.repo_root == str(repo_root.resolve())

    def test_trust_state_returns_none_when_no_trust_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        record = trust_state_for("nonexistent-repo")
        assert record is None

    def test_trust_state_stale_after_profile_change(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        repo_root = tmp_path / "repo"
        profile_dir = _make_profile_dir(repo_root)
        repo_id = "test-repo-002"

        grant_trust(repo_id, profile_dir)
        hash_at_grant = hash_profile(profile_dir)

        # mutate profile after trust was granted
        (profile_dir / "profile.json").write_text(
            json.dumps({"generation": 99, "language": "ruby"}),
            encoding="utf-8",
        )
        hash_after = hash_profile(profile_dir)
        assert hash_at_grant != hash_after

        record = trust_state_for(repo_id)
        assert record is not None
        # the stored hash no longer matches the on-disk profile
        assert record.profile_sha256 != hash_after

    def test_multiple_workspace_roots_preserved(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        repo_id = "monorepo-001"

        # Root workspace
        root = tmp_path / "monorepo"
        root_profile = _make_profile_dir(root)
        grant_trust(repo_id, root_profile)

        # Sub-workspace with different profile content
        ws = tmp_path / "monorepo" / "packages" / "web"
        ws_profile = _make_profile_dir(ws, extra_files={
            "profile.json": json.dumps({"generation": 1, "language": "typescript", "ws": True}),
        })
        grant_trust(repo_id, ws_profile)

        record = trust_state_for(repo_id)
        assert record is not None
        # original root trust preserved
        assert record.repo_root == str(root.resolve())
        assert record.profile_sha256 == hash_profile(root_profile)
        # workspace hash also recorded
        ws_key = str(ws.resolve())
        assert ws_key in record.repo_root_specific_hashes
        assert record.repo_root_specific_hashes[ws_key] == hash_profile(ws_profile)

    def test_workspace_trust_not_stale_after_cascade(self, tmp_path: Path, monkeypatch):
        """BUG-029: after trusting root + workspaces, is_material_change
        should return False for each workspace profile."""
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        repo_id = "monorepo-cascade-001"

        root = tmp_path / "monorepo"
        root_profile = _make_profile_dir(root)
        grant_trust(repo_id, root_profile)

        ws_a = tmp_path / "monorepo" / "packages" / "a"
        ws_a_profile = _make_profile_dir(ws_a, extra_files={
            "profile.json": json.dumps({"generation": 2, "language": "typescript"}),
        })
        grant_trust(repo_id, ws_a_profile)

        ws_b = tmp_path / "monorepo" / "packages" / "b"
        ws_b_profile = _make_profile_dir(ws_b, extra_files={
            "profile.json": json.dumps({"generation": 3, "language": "ruby"}),
        })
        grant_trust(repo_id, ws_b_profile)

        assert is_material_change(repo_id, root_profile) is False
        assert is_material_change(repo_id, ws_a_profile) is False
        assert is_material_change(repo_id, ws_b_profile) is False
