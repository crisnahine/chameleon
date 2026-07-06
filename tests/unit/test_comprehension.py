"""Codebase comprehension over the committed profile: search, overview, callees.

chameleon's conformance profile doubles as a comprehension surface. These tests
exercise the pure functions (search_symbols / god_symbols / describe_codebase /
callees_of) and the trust-gated MCP tools (search_codebase / describe_codebase /
get_callees), mirroring the trusted-profile fixture of test_get_callers_tool.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import comprehension as C
from chameleon_mcp import server, tools
from chameleon_mcp.calls_index import SCHEMA_VERSION as _CALLS_SCHEMA
from chameleon_mcp.profile.trust import grant_trust, repo_data_dir

ARCH = "service"


def _sig(start: int) -> dict:
    return {"params": [], "start_line": start, "end_line": start + 2}


@pytest.fixture
def profiled_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text(
        json.dumps({"generation": 1, "language": "python", "schema_version": 8})
    )
    (cham / "archetypes.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "archetypes": {
                    ARCH: {
                        "summary": "service objects",
                        "cluster_size": 2,
                        "paths_pattern_display": "svc/*.py",
                    }
                },
            }
        )
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "canonicals.json").write_text(
        json.dumps({"generation": 1, "canonicals": {ARCH: [{"witness": {"path": "svc.py"}}]}})
    )
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    # symbol index: two production symbols + one test symbol
    (cham / "symbol_signatures.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": {
                    "svc.py": {"make_service": _sig(10), "service_helper": _sig(20)},
                    "consumer.py": {"setup": _sig(5)},
                    "tests/test_svc.py": {"test_make_service": _sig(3)},
                },
                "classes": {
                    "svc.py": {"ServiceMaker": {"start_line": 30, "extends": "Base"}},
                },
            }
        )
    )
    # reverse calls: setup (consumer.py) calls make_service; a test calls service_helper
    (cham / "calls_index.json").write_text(
        json.dumps(
            {
                "schema_version": _CALLS_SCHEMA,
                "callees": {
                    "svc.py": {
                        "make_service": {
                            "callers": [
                                {
                                    "path": "consumer.py",
                                    "caller": "setup",
                                    "line": 5,
                                    "grade": "import",
                                }
                            ],
                            "total": 1,
                            "truncated": False,
                        },
                        "service_helper": {
                            "callers": [
                                {
                                    "path": "tests/test_svc.py",
                                    "caller": "test_make_service",
                                    "line": 3,
                                    "grade": "import",
                                }
                            ],
                            "total": 1,
                            "truncated": False,
                        },
                    }
                },
            }
        )
    )
    (cham / "COMMITTED").touch()
    grant_trust(tools._compute_repo_id(repo), cham)
    return repo


def _data(res: dict) -> dict:
    assert res.get("api_version") == "1"
    return res["data"]


# --- pure functions --------------------------------------------------------


def test_search_finds_symbol_exact_first(profiled_repo):
    results = C.search_symbols(profiled_repo, "make_service", limit=10)
    assert results, "expected a match"
    assert results[0]["name"] == "make_service"
    assert results[0]["file"] == "svc.py"
    assert results[0]["line"] == 10
    assert results[0]["callers"] == 1


def test_search_substring_matches_multiple(profiled_repo):
    names = {r["name"] for r in C.search_symbols(profiled_repo, "service", limit=10)}
    assert {"make_service", "service_helper"} <= names


def test_search_prefix_beats_substring(profiled_repo):
    # "service" is a prefix of service_helper but only a substring of
    # make_service, so the higher tier (prefix) ranks first.
    order = [r["name"] for r in C.search_symbols(profiled_repo, "service", limit=10)]
    assert order.index("service_helper") < order.index("make_service")


def test_search_blank_query_empty(profiled_repo):
    assert C.search_symbols(profiled_repo, "   ", limit=10) == []


def test_search_finds_class_definition(profiled_repo):
    # "find class X" resolves from the additive class section with a real def
    # line and a `class X(Base)` signature, not only callables.
    results = C.search_symbols(profiled_repo, "ServiceMaker", limit=10)
    assert results, "class definition should be searchable"
    top = results[0]
    assert top["name"] == "ServiceMaker"
    assert top["file"] == "svc.py"
    assert top["line"] == 30
    assert "class ServiceMaker(Base)" in top["signature"]


def test_god_symbols_exclude_tests(profiled_repo):
    gods = C.describe_codebase(profiled_repo)["god_symbols"]
    files = {g["file"] for g in gods}
    assert not any(f.startswith("tests/") for f in files), gods
    # make_service (called by production consumer.py) is a god symbol
    assert any(g["name"] == "make_service" for g in gods)


def test_describe_overview(profiled_repo):
    d = C.describe_codebase(profiled_repo)
    assert d["language"] == "python"
    assert d["file_count"] == 3
    assert d["symbol_count"] == 4
    arch = {a["name"]: a for a in d["archetypes"]}
    assert ARCH in arch
    assert arch[ARCH]["witness"] == "svc.py"
    assert arch[ARCH]["size"] == 2


def test_callees_forward_inversion(profiled_repo):
    # setup (consumer.py) calls make_service per the reverse index
    callees = C.callees_of(profiled_repo, "consumer.py", "setup")
    assert [c["callee"] for c in callees] == ["make_service"]
    assert callees[0]["file"] == "svc.py"
    assert callees[0]["grade"] == "import"


def test_callees_none_when_not_a_caller(profiled_repo):
    assert C.callees_of(profiled_repo, "svc.py", "make_service") == []


# --- tools (trust-gated) ---------------------------------------------------


def test_tools_registered():
    for name in ("search_codebase", "describe_codebase", "get_callees"):
        assert hasattr(server, name) and callable(getattr(server, name))


def test_search_codebase_tool(profiled_repo):
    data = _data(tools.search_codebase(str(profiled_repo), "make_service"))
    assert data["found"] is True
    assert any(r["name"] == "make_service" for r in data["results"])


def test_search_codebase_blank_query_found_false(profiled_repo):
    # Regression: the docstring contracts found:False on an empty/blank query, so
    # a caller can branch on `found`. It used to return found:True with results:[].
    for q in ("", "   "):
        data = _data(tools.search_codebase(str(profiled_repo), q))
        assert data["found"] is False
        assert data["results"] == []


def test_search_codebase_limit_clamped(profiled_repo):
    data = _data(tools.search_codebase(str(profiled_repo), "service", limit=1))
    assert len(data["results"]) == 1


def test_describe_codebase_tool(profiled_repo):
    data = _data(tools.describe_codebase(str(profiled_repo)))
    assert data["found"] is True
    assert data["language"] == "python"
    assert any(a["name"] == ARCH for a in data["archetypes"])


def test_get_callees_tool(profiled_repo):
    data = _data(tools.get_callees(str(profiled_repo), str(profiled_repo / "consumer.py"), "setup"))
    assert data["found"] is True
    assert [c["callee"] for c in data["callees"]] == ["make_service"]


def test_comprehension_tools_untrusted(profiled_repo):
    tp = repo_data_dir(tools._compute_repo_id(profiled_repo)) / ".trust"
    if tp.is_file():
        tp.unlink()
    assert (
        _data(tools.search_codebase(str(profiled_repo), "make_service")).get("status")
        == "untrusted"
    )
    assert _data(tools.describe_codebase(str(profiled_repo))).get("status") == "untrusted"
    assert (
        _data(
            tools.get_callees(str(profiled_repo), str(profiled_repo / "consumer.py"), "setup")
        ).get("status")
        == "untrusted"
    )


def test_bootstrap_repo_server_wrapper_forwards_production_ref(monkeypatch):
    # Regression: the MCP wrapper must EXPOSE and FORWARD production_ref, else the
    # init/refresh skills' explicit production-branch answer is silently dropped
    # (FastMCP ignores an extra kwarg the wrapper doesn't declare).
    import inspect

    from chameleon_mcp import server

    assert "production_ref" in inspect.signature(server.bootstrap_repo).parameters
    captured = {}

    def fake(path, paths_glob=None, force=False, production_ref=None, now=None):
        captured["production_ref"] = production_ref
        return {"data": {"status": "ok"}}

    monkeypatch.setattr(tools, "bootstrap_repo", fake)
    server.bootstrap_repo(path="/x", production_ref="release-1")
    assert captured["production_ref"] == "release-1"
