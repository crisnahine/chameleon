"""Unit tests for the get_callers tool and its server registration.

Mirrors the fixture approach of test_mcp_tools.py::test_query_symbol_importers_*:
a trusted in-memory profile with a planted calls_index.json, exercising the
trust gate, missing-artifact, known-absent-callee, and round-trip paths.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import server, tools
from chameleon_mcp.calls_index import CALLS_INDEX_FILENAME, SCHEMA_VERSION
from chameleon_mcp.profile.trust import grant_trust, repo_data_dir

ARCH = "service"
WITNESS = "service.ts"


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
        json.dumps(
            {
                "generation": 1,
                "canonicals": {ARCH: [{"witness": {"path": WITNESS, "sha_hint": "deadbeef"}}]},
            }
        )
    )
    (cham / "idioms.md").write_text("Always use the apiClient helper.\n")
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    (cham / "COMMITTED").touch()
    (repo / WITNESS).write_text("export function makeService() {\n  return 1;\n}\n")
    grant_trust(tools._compute_repo_id(repo), cham)
    return repo


def _write_calls_index(cham, callees: dict) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "callees": callees}
    (cham / CALLS_INDEX_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    # Re-grant trust: calls_index.json is a hashed artifact so the profile
    # surface has changed since the fixture set up trust.
    grant_trust(tools._compute_repo_id(cham.parent), cham)


def _assert_envelope(result: dict):
    assert isinstance(result, dict)
    assert result.get("api_version") == "1"
    assert "data" in result and isinstance(result["data"], dict)


# ---------------------------------------------------------------------------
# Server registration
# ---------------------------------------------------------------------------


def test_get_callers_registered_in_server():
    assert hasattr(server, "get_callers"), "server.py did not register get_callers"
    assert callable(server.get_callers)


# ---------------------------------------------------------------------------
# Round-trip: callers present
# ---------------------------------------------------------------------------


def test_get_callers_returns_callers(trusted_repo):
    cham = trusted_repo / ".chameleon"
    _write_calls_index(
        cham,
        {
            "service.ts": {
                "makeService": {
                    "callers": [
                        {"path": "consumer.ts", "caller": "setup", "line": 5, "grade": "import"}
                    ],
                    "total": 1,
                    "truncated": False,
                }
            }
        },
    )
    res = tools.get_callers(str(trusted_repo), str(trusted_repo / "service.ts"), "makeService")
    _assert_envelope(res)
    data = res["data"]
    assert data["found"] is True
    assert data["module"] == "service.ts"
    assert data["function"] == "makeService"
    assert data["total"] == 1
    assert data["truncated"] is False
    assert len(data["callers"]) == 1
    row = data["callers"][0]
    assert row["path"] == "consumer.ts"
    assert row["caller"] == "setup"
    assert row["line"] == 5
    assert row["grade"] == "import"


# ---------------------------------------------------------------------------
# Artifact absent -> found False, reason no-calls-index
# ---------------------------------------------------------------------------


def test_get_callers_no_artifact_found_false(trusted_repo):
    res = tools.get_callers(str(trusted_repo), str(trusted_repo / "service.ts"), "makeService")
    _assert_envelope(res)
    data = res["data"]
    assert data["found"] is False
    assert data.get("reason") == "no-calls-index"


# ---------------------------------------------------------------------------
# Callee absent in index -> found True, empty callers
# ---------------------------------------------------------------------------


def test_get_callers_callee_absent_empty(trusted_repo):
    cham = trusted_repo / ".chameleon"
    # Write an index that has NO entry for makeService -- it is a known-absent callee.
    _write_calls_index(
        cham, {"other.ts": {"otherFn": {"callers": [], "total": 0, "truncated": False}}}
    )
    res = tools.get_callers(str(trusted_repo), str(trusted_repo / "service.ts"), "makeService")
    _assert_envelope(res)
    data = res["data"]
    assert data["found"] is True
    assert data["callers"] == []
    assert data["total"] == 0
    assert data["truncated"] is False


# ---------------------------------------------------------------------------
# Untrusted repo -> withheld (found False, status untrusted)
# ---------------------------------------------------------------------------


def test_get_callers_untrusted(trusted_repo):
    cham = trusted_repo / ".chameleon"
    _write_calls_index(
        cham,
        {"service.ts": {"makeService": {"callers": [], "total": 0, "truncated": False}}},
    )
    # Drop the trust grant.
    trust_path = repo_data_dir(tools._compute_repo_id(trusted_repo)) / ".trust"
    if trust_path.is_file():
        trust_path.unlink()

    res = tools.get_callers(str(trusted_repo), str(trusted_repo / "service.ts"), "makeService")
    _assert_envelope(res)
    data = res["data"]
    assert data["found"] is False
    assert data.get("status") == "untrusted"
