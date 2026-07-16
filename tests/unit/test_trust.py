"""Unit tests for chameleon_mcp.profile.trust — hashing, grant, state queries."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chameleon_mcp.profile.trust import (
    TrustRecord,
    grant_trust,
    hash_profile,
    is_material_change,
    trust_state_for,
)


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

    (profile_dir / "COMMITTED").touch()
    return profile_dir


class TestHashProfile:
    def test_deterministic_for_known_content(self, tmp_path: Path):
        profile_dir = _make_profile_dir(tmp_path)
        h1 = hash_profile(profile_dir)
        h2 = hash_profile(profile_dir)
        assert h1 == h2
        assert len(h1) == 64

    def test_changes_when_content_changes(self, tmp_path: Path):
        profile_dir = _make_profile_dir(tmp_path)
        h_before = hash_profile(profile_dir)

        (profile_dir / "rules.json").write_text(
            json.dumps({"generation": 2, "rules": [{"id": "r1"}]}),
            encoding="utf-8",
        )
        h_after = hash_profile(profile_dir)
        assert h_before != h_after

    def test_returns_empty_when_profile_json_missing(self, tmp_path: Path):
        profile_dir = tmp_path / ".chameleon"
        profile_dir.mkdir()
        assert hash_profile(profile_dir) == ""

    def test_conventions_json_changes_hash(self, tmp_path: Path):
        repo_root = tmp_path / "repo"
        profile_dir = _make_profile_dir(repo_root)
        h1 = hash_profile(profile_dir)
        (profile_dir / "conventions.json").write_text(
            '{"schema_version": 1, "conventions": {}}', encoding="utf-8"
        )
        h2 = hash_profile(profile_dir)
        assert h1 != h2

    def test_optional_idioms_md_changes_hash(self, tmp_path: Path):
        profile_dir = _make_profile_dir(tmp_path)
        h_without = hash_profile(profile_dir)

        (profile_dir / "idioms.md").write_text("# always use foo()", encoding="utf-8")
        h_with = hash_profile(profile_dir)
        assert h_without != h_with

    def test_enforcement_json_changes_hash(self, tmp_path: Path):
        # The block-rule calibration verdict is trust-hashed: a planted
        # enforcement.json that flips a known-false-positive rule to "active"
        # must de-trust the profile rather than slip past unchanged.
        profile_dir = _make_profile_dir(tmp_path)
        h_without = hash_profile(profile_dir)

        (profile_dir / "enforcement.json").write_text(
            json.dumps({"schema_version": 1, "block_rules": {}}),
            encoding="utf-8",
        )
        h_with = hash_profile(profile_dir)
        assert h_without != h_with

        (profile_dir / "enforcement.json").write_text(
            json.dumps({"schema_version": 1, "block_rules": {"no-explicit-any": "active"}}),
            encoding="utf-8",
        )
        h_tampered = hash_profile(profile_dir)
        assert h_tampered != h_with


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
        assert "repo_root_specific_hashes" not in d

    def test_from_dict_handles_missing_fields_gracefully(self):
        record = TrustRecord.from_dict({})
        assert record.granted_at == ""
        assert record.granted_by_user == ""
        assert record.profile_sha256 == ""
        assert record.repo_root_specific_hashes == {}

    def test_from_dict_drops_non_string_map_entries(self):
        record = TrustRecord.from_dict(
            {
                "granted_at": "2025-01-01T00:00:00Z",
                "granted_by_user": "u",
                "profile_sha256": "sha",
                "repo_root_specific_hashes": {
                    "/good": "hash1",
                    123: "bad_key",
                    "/also_bad": 999,
                },
            }
        )
        assert record.repo_root_specific_hashes == {"/good": "hash1"}


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

    def test_trust_persists_after_profile_change_by_default(self, tmp_path: Path, monkeypatch):
        # Trust is one-time: once granted, it stays trusted across profile changes
        # (refresh, re-bootstrap, teach) -- never goes stale -- by default.
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.delenv("CHAMELEON_TRUST_REVALIDATE", raising=False)
        repo_root = tmp_path / "repo"
        profile_dir = _make_profile_dir(repo_root)
        repo_id = "persist-001"
        grant_trust(repo_id, profile_dir)
        (profile_dir / "profile.json").write_text(
            json.dumps({"generation": 99, "language": "ruby"}), encoding="utf-8"
        )
        assert is_material_change(repo_id, profile_dir) is False

    def test_trust_revalidates_when_kill_switch_set(self, tmp_path: Path, monkeypatch):
        # CHAMELEON_TRUST_REVALIDATE=1 restores the old "stale on change" behavior.
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CHAMELEON_TRUST_REVALIDATE", "1")
        repo_root = tmp_path / "repo"
        profile_dir = _make_profile_dir(repo_root)
        repo_id = "persist-002"
        grant_trust(repo_id, profile_dir)
        (profile_dir / "profile.json").write_text(
            json.dumps({"generation": 99, "language": "ruby"}), encoding="utf-8"
        )
        assert is_material_change(repo_id, profile_dir) is True

    def test_trust_state_stale_after_profile_change(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        repo_root = tmp_path / "repo"
        profile_dir = _make_profile_dir(repo_root)
        repo_id = "test-repo-002"

        grant_trust(repo_id, profile_dir)
        hash_at_grant = hash_profile(profile_dir)

        (profile_dir / "profile.json").write_text(
            json.dumps({"generation": 99, "language": "ruby"}),
            encoding="utf-8",
        )
        hash_after = hash_profile(profile_dir)
        assert hash_at_grant != hash_after

        record = trust_state_for(repo_id)
        assert record is not None
        assert record.profile_sha256 != hash_after

    def test_multiple_workspace_roots_preserved(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        repo_id = "monorepo-001"

        root = tmp_path / "monorepo"
        root_profile = _make_profile_dir(root)
        grant_trust(repo_id, root_profile)

        ws = tmp_path / "monorepo" / "packages" / "web"
        ws_profile = _make_profile_dir(
            ws,
            extra_files={
                "profile.json": json.dumps({"generation": 1, "language": "typescript", "ws": True}),
            },
        )
        grant_trust(repo_id, ws_profile)

        record = trust_state_for(repo_id)
        assert record is not None
        assert record.repo_root == str(root.resolve())
        assert record.profile_sha256 == hash_profile(root_profile)
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
        ws_a_profile = _make_profile_dir(
            ws_a,
            extra_files={
                "profile.json": json.dumps({"generation": 2, "language": "typescript"}),
            },
        )
        grant_trust(repo_id, ws_a_profile)

        ws_b = tmp_path / "monorepo" / "packages" / "b"
        ws_b_profile = _make_profile_dir(
            ws_b,
            extra_files={
                "profile.json": json.dumps({"generation": 3, "language": "ruby"}),
            },
        )
        grant_trust(repo_id, ws_b_profile)

        assert is_material_change(repo_id, root_profile) is False
        assert is_material_change(repo_id, ws_a_profile) is False
        assert is_material_change(repo_id, ws_b_profile) is False


class TestGrantsRoot:
    """grants_root distinguishes a never-granted workspace (untrusted) from a
    granted-but-changed one (stale) under a monorepo-shared repo_id."""

    def _record(self, root: Path, *, with_map: bool = True) -> TrustRecord:
        rr = str(root.resolve())
        return TrustRecord(
            granted_at="2026-01-01T00:00:00Z",
            granted_by_user="u",
            profile_sha256="aaa",
            repo_root=rr,
            repo_root_specific_hashes={rr: "aaa"} if with_map else {},
        )

    def test_granted_root_is_granted(self, tmp_path: Path):
        root = tmp_path / "repo"
        root.mkdir()
        assert self._record(root).grants_root(root) is True

    def test_ungranted_nested_workspace_is_not_granted(self, tmp_path: Path):
        # Regression (real-app test, plane): a nested workspace with its own
        # .chameleon shares the root's git-based repo_id, but the root grant
        # must not vouch for it -- otherwise it reads "stale" and leaks an
        # unreviewed canonical instead of prompting for trust.
        root = tmp_path / "repo"
        ws = root / "packages" / "svc"
        ws.mkdir(parents=True)
        assert self._record(root).grants_root(ws) is False

    def test_legacy_record_without_map_grants_only_top_level(self, tmp_path: Path):
        root = tmp_path / "repo"
        (root / "sub").mkdir(parents=True)
        rec = self._record(root, with_map=False)
        assert rec.grants_root(root) is True
        assert rec.grants_root(root / "sub") is False

    def test_workspace_granted_after_its_own_grant(self, tmp_path: Path, monkeypatch):
        # After /chameleon-trust on the workspace, grants_root becomes True.
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        root = tmp_path / "repo"
        ws = root / "packages" / "svc"
        root_profile = _make_profile_dir(root)
        ws_profile = _make_profile_dir(
            ws,
            extra_files={"profile.json": json.dumps({"generation": 1, "language": "ruby"})},
        )
        from chameleon_mcp.tools import _compute_repo_id

        repo_id = _compute_repo_id(root.resolve())
        grant_trust(repo_id, root_profile)
        assert trust_state_for(repo_id).grants_root(ws) is False
        grant_trust(repo_id, ws_profile)
        assert trust_state_for(repo_id).grants_root(ws) is True
        # root grant survives the workspace grant
        assert trust_state_for(repo_id).grants_root(root) is True


class TestGrantTrustLockDeadline:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="same-process lock conflict is not guaranteed under msvcrt; "
        "a subprocess holder is needed to exercise this on Windows",
    )
    def test_held_trust_lock_raises_within_deadline(self, tmp_path: Path, monkeypatch):
        # A holder that never releases must not wedge grant_trust indefinitely:
        # past the deadline it raises LockHeldError, which the trust_profile
        # tool surfaces as an error envelope and refresh-time trust
        # preservation swallows.
        import time as _time

        from chameleon_mcp.locks import LockHeldError, portable_flock
        from chameleon_mcp.profile.trust import repo_data_dir

        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CHAMELEON_TRUST_LOCK_TIMEOUT_SECONDS", "0.2")
        repo_root = tmp_path / "repo"
        profile_dir = _make_profile_dir(repo_root)
        repo_id = "test-repo-lock-001"

        lock_path = repo_data_dir(repo_id) / ".trust.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            portable_flock(holder_fd, nonblocking=True)
            start = _time.monotonic()
            with pytest.raises(LockHeldError):
                grant_trust(repo_id, profile_dir)
            assert _time.monotonic() - start < 5.0
        finally:
            os.close(holder_fd)
        # Released holder: the same grant now succeeds.
        grant_trust(repo_id, profile_dir)
        assert trust_state_for(repo_id) is not None


def test_trust_state_probe_does_not_create_data_dir(tmp_path, monkeypatch):
    # qa66 rails-1: detect_repo's legacy-id probe used trust_state_for, whose
    # repo_data_dir mkdirs unconditionally — minting a permanently orphaned
    # empty directory per probed identity. The probe form must not create.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    from chameleon_mcp.profile.trust import trust_state_probe

    probed_id = "f" * 64
    assert trust_state_probe(probed_id) is None
    assert not (tmp_path / "data" / probed_id).exists()
