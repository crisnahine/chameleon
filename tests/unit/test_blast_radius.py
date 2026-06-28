"""get_blast_radius tool + the shared transitive caller walk (blast_radius.py).

The walk is the judge's bounded upward caller traversal, extracted so the
get_blast_radius read tool and the turn-end judge share ONE deterministic,
conservatively-graded reach over the committed calls_index. Mirrors the trust /
missing-artifact / untrusted fixture approach of test_get_callers_tool.py.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import server, tools
from chameleon_mcp.blast_radius import compute_blast_radius, transitive_caller_chains
from chameleon_mcp.calls_index import CALLS_INDEX_FILENAME, SCHEMA_VERSION, CallsIndex
from chameleon_mcp.profile.trust import grant_trust, repo_data_dir

ARCH = "service"
WITNESS = "service.ts"

# Linear two-hop chain: makeService <- setup (consumer.ts) <- boot (main.ts).
_TWO_HOP = {
    "service.ts": {
        "makeService": {
            "callers": [{"path": "consumer.ts", "caller": "setup", "line": 5, "grade": "import"}],
            "total": 1,
            "truncated": False,
        }
    },
    "consumer.ts": {
        "setup": {
            "callers": [{"path": "main.ts", "caller": "boot", "line": 9, "grade": "import"}],
            "total": 1,
            "truncated": False,
        }
    },
}


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
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
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
    grant_trust(tools._compute_repo_id(cham.parent), cham)


def _assert_envelope(result: dict) -> None:
    assert result.get("api_version") == "1"
    assert isinstance(result.get("data"), dict)


# --- the shared walk / compute layer ---------------------------------------


def test_compute_blast_radius_two_hop_chain():
    out = compute_blast_radius(CallsIndex(_TWO_HOP), "service.ts", "makeService", depth=2)
    assert out["chains"], "expected at least one caller chain"
    chain = out["chains"][0]
    assert [hop["name"] for hop in chain] == ["makeService", "setup", "boot"]
    assert [hop["path"] for hop in chain] == ["service.ts", "consumer.ts", "main.ts"]
    assert out["reached"] == 2


def test_compute_blast_radius_depth_one_stops_at_direct_callers():
    out = compute_blast_radius(CallsIndex(_TWO_HOP), "service.ts", "makeService", depth=1)
    assert [hop["name"] for hop in out["chains"][0]] == ["makeService", "setup"]
    assert out["reached"] == 1


def test_compute_blast_radius_no_callers_is_empty():
    out = compute_blast_radius(CallsIndex(_TWO_HOP), "main.ts", "boot", depth=2)
    assert out["chains"] == []
    assert out["reached"] == 0


# --- the tool: trust / artifact / untrusted gates --------------------------


def test_get_blast_radius_registered_in_server():
    assert hasattr(server, "get_blast_radius"), "server.py did not register get_blast_radius"
    assert callable(server.get_blast_radius)


def test_get_blast_radius_returns_chain(trusted_repo):
    cham = trusted_repo / ".chameleon"
    _write_calls_index(cham, _TWO_HOP)
    res = tools.get_blast_radius(str(trusted_repo), str(trusted_repo / "service.ts"), "makeService")
    _assert_envelope(res)
    data = res["data"]
    assert data["found"] is True
    assert data["module"] == "service.ts"
    assert data["function"] == "makeService"
    assert data["chains"]
    flat = json.dumps(data["chains"])
    assert "setup" in flat and "boot" in flat
    # carries the judge's honesty posture verbatim
    assert "note" in data and "dead code" in data["note"].lower()


def test_get_blast_radius_accepts_relative_file_path(trusted_repo):
    """A repo-relative file_path must resolve against the repo arg's root (not the
    server CWD) and return the same chains as the absolute form."""
    cham = trusted_repo / ".chameleon"
    _write_calls_index(cham, _TWO_HOP)
    res = tools.get_blast_radius(str(trusted_repo), "service.ts", "makeService")  # RELATIVE
    _assert_envelope(res)
    data = res["data"]
    assert data["found"] is True, f"relative path silently failed open: {data}"
    assert data["module"] == "service.ts"
    assert data["chains"]


def test_get_blast_radius_no_artifact(trusted_repo):
    res = tools.get_blast_radius(str(trusted_repo), str(trusted_repo / "service.ts"), "makeService")
    _assert_envelope(res)
    assert res["data"]["found"] is False
    assert res["data"].get("reason") == "no-calls-index"


def test_get_blast_radius_untrusted(trusted_repo):
    cham = trusted_repo / ".chameleon"
    _write_calls_index(cham, _TWO_HOP)
    trust_path = repo_data_dir(tools._compute_repo_id(trusted_repo)) / ".trust"
    if trust_path.is_file():
        trust_path.unlink()
    res = tools.get_blast_radius(str(trusted_repo), str(trusted_repo / "service.ts"), "makeService")
    _assert_envelope(res)
    assert res["data"]["found"] is False
    assert res["data"].get("status") == "untrusted"


def test_judge_reuses_the_shared_walker():
    """Behavior-preserving extraction: the judge keeps its private name bound to
    the shared walker, so its transitive impact block is unchanged."""
    from chameleon_mcp import judge

    assert judge._transitive_caller_chains is transitive_caller_chains
