"""CI-gated, env-var-free coverage for the MCP tool surface (tools.py + server.py).

These were previously exercised only by tests/qa_*.py, which require
CHAMELEON_TEST_*_REPO env vars and are not run in CI, so tools.py (the ~20
model-callable tools) and server.py had ZERO CI-gated coverage. A regression in
any tool's response envelope, error handling, or logic passed CI green.

This builds a small trusted fixture profile in tmp (no subprocess, no network,
no env dependency) and asserts every read-path tool returns the standard
envelope and survives. It is the safety net the tools.py / extraction refactors
depend on.
"""
from __future__ import annotations

import json

import pytest

from chameleon_mcp import server, tools
from chameleon_mcp.profile.trust import grant_trust

ARCH = "service"
WITNESS = "service.ts"

# Every tool name registered in server.py, asserted present so a dropped/renamed
# registration is caught.
REGISTERED_TOOLS = [
    "detect_repo", "get_archetype", "get_pattern_context", "get_canonical_excerpt",
    "get_rules", "lint_file", "get_drift_status", "refresh_repo", "bootstrap_repo",
    "list_profiles", "merge_profiles", "teach_profile", "trust_profile",
    "disable_session", "pause_session", "propose_archetype_renames",
    "apply_archetype_renames", "teach_profile_structured", "daemon_status", "doctor",
]


@pytest.fixture
def trusted_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "typescript"}))
    (cham / "archetypes.json").write_text(
        json.dumps({"generation": 1, "archetypes": {ARCH: {"summary": "service objects"}}})
    )
    (cham / "rules.json").write_text(
        json.dumps({"generation": 1, "rules": {"no-default-export": {"severity": "warn"}}})
    )
    (cham / "canonicals.json").write_text(
        json.dumps({
            "generation": 1,
            "canonicals": {ARCH: [{"witness": {"path": WITNESS, "sha_hint": "deadbeef"}}]},
        })
    )
    (cham / "idioms.md").write_text("Always use the apiClient helper.\n")
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    (cham / "COMMITTED").touch()
    (repo / WITNESS).write_text("export function makeService() {\n  return 1;\n}\n")
    grant_trust(tools._compute_repo_id(repo), cham)
    return repo


def _assert_envelope(result: dict):
    assert isinstance(result, dict)
    assert result.get("api_version") == "1"
    assert "data" in result and isinstance(result["data"], dict)


def test_server_imports_and_registers_every_tool():
    for name in REGISTERED_TOOLS:
        assert hasattr(server, name), f"server.py no longer defines tool {name!r}"
        assert callable(getattr(server, name))


def test_detect_repo(trusted_repo):
    _assert_envelope(tools.detect_repo(str(trusted_repo / WITNESS)))


def test_get_pattern_context(trusted_repo):
    res = tools.get_pattern_context(str(trusted_repo / WITNESS))
    _assert_envelope(res)
    assert "trust_state" in res["data"]["repo"]


def test_get_archetype(trusted_repo):
    _assert_envelope(tools.get_archetype(str(trusted_repo), str(trusted_repo / WITNESS)))


def test_get_canonical_excerpt(trusted_repo):
    res = tools.get_canonical_excerpt(str(trusted_repo), ARCH)
    _assert_envelope(res)
    assert "makeService" in (res["data"].get("content") or "")


def test_get_rules(trusted_repo):
    _assert_envelope(tools.get_rules(str(trusted_repo)))


def test_lint_file(trusted_repo):
    res = tools.lint_file(str(trusted_repo), ARCH, "export const x = 1;\n", file_path="x.ts")
    _assert_envelope(res)


def test_get_drift_status(trusted_repo):
    _assert_envelope(tools.get_drift_status(str(trusted_repo)))


def test_list_profiles(trusted_repo):
    _assert_envelope(tools.list_profiles())


def test_detect_repo_no_repo(tmp_path, monkeypatch):
    """A path with no repo/profile still returns a clean envelope, not a crash."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _assert_envelope(tools.detect_repo(str(tmp_path / "loose.ts")))


def test_doctor(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _assert_envelope(tools.doctor())


def test_daemon_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    _assert_envelope(tools.daemon_status())
