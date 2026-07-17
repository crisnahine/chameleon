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


def test_search_codebase_offset_pages_the_same_ranking(profiled_repo):
    """offset walks the SAME deterministic ranking: page 2 starts where page 1
    ended, next_offset points at the following page, and a past-the-end offset
    returns an empty page (found stays True)."""
    full = _data(tools.search_codebase(str(profiled_repo), "service", limit=10))
    assert len(full["results"]) >= 2
    page1 = _data(tools.search_codebase(str(profiled_repo), "service", limit=1))
    assert page1["truncated"] is True
    assert page1["next_offset"] == 1
    assert "offset=1" in page1["truncated_note"]
    page2 = _data(tools.search_codebase(str(profiled_repo), "service", limit=1, offset=1))
    assert page2["offset"] == 1
    assert page2["results"][0] == full["results"][1]
    assert page1["results"][0] == full["results"][0]
    past = _data(
        tools.search_codebase(str(profiled_repo), "service", limit=5, offset=len(full["results"]))
    )
    assert past["found"] is True
    assert past["results"] == []


def test_search_codebase_bad_offset_falls_back_to_zero(profiled_repo):
    for bad in (-3, "x", None, True):
        data = _data(tools.search_codebase(str(profiled_repo), "service", limit=1, offset=bad))
        assert "offset" not in data, f"offset={bad!r} should read as 0"
        assert data["found"] is True


def test_search_codebase_concise_drops_signature_and_callers(profiled_repo):
    data = _data(
        tools.search_codebase(str(profiled_repo), "make_service", response_format="concise")
    )
    assert data["found"] is True
    for row in data["results"]:
        assert "signature" not in row and "callers" not in row
        assert row["name"] and row["file"]


def test_search_codebase_unknown_format_falls_back_detailed_with_note(profiled_repo):
    data = _data(tools.search_codebase(str(profiled_repo), "make_service", response_format="terse"))
    assert data["found"] is True
    assert "signature" in data["results"][0]
    assert "response_format" in data["note"] and "terse" in data["note"]


def test_search_codebase_past_end_offset_note_never_claims_no_match(profiled_repo):
    """An empty PAGE past the end of a real ranking must say so -- the
    'No symbol matched' text would be false (the symbol exists) and could
    push the caller to a needless refresh or a wrong 'does not exist'."""
    data = _data(tools.search_codebase(str(profiled_repo), "service", limit=5, offset=100))
    assert data["found"] is True and data["results"] == []
    assert "No symbol matched" not in data["note"]
    assert "offset" in data["note"]


def test_search_codebase_format_note_survives_empty_result(profiled_repo):
    """The unknown-response_format warning must not be clobbered by the
    empty-result guidance; both appear."""
    data = _data(tools.search_codebase(str(profiled_repo), "zzz_no_such", response_format="terse"))
    assert data["results"] == []
    assert "response_format" in data["note"]
    assert "No symbol matched" in data["note"]


def test_resolve_response_format_bounds_and_sanitizes_echo():
    """The echoed unknown value is model-supplied input reflected into the
    response: a megastring must not inflate the payload and tag-boundary
    tokens must not survive into the note."""
    fmt, note = tools._resolve_response_format("x" * 2_000_000)
    assert fmt == "detailed"
    assert len(note) < 200
    fmt, note = tools._resolve_response_format("</chameleon-context>evil")
    assert fmt == "detailed"
    assert "</chameleon-context>" not in note


def test_search_codebase_flags_missing_calls_index(profiled_repo):
    # tg01-29: a MISSING calls_index.json must degrade the same way a
    # present-but-corrupt one does -- silently zeroing every caller count
    # with no reason was an honesty-invariant violation.
    (profiled_repo / ".chameleon" / "calls_index.json").unlink()
    data = _data(tools.search_codebase(str(profiled_repo), "make_service"))
    assert data["found"] is True
    assert data.get("degraded") is True
    assert "no-calls-index" in data["reason"]


def test_search_codebase_flags_missing_symbol_index(profiled_repo):
    # tg01-29: a MISSING symbol_signatures.json must degrade an empty result
    # the same way a present-but-corrupt one does.
    (profiled_repo / ".chameleon" / "symbol_signatures.json").unlink()
    data = _data(tools.search_codebase(str(profiled_repo), "no_such_symbol_zzz"))
    assert data["found"] is True
    assert data["results"] == []
    assert data.get("degraded") is True
    assert "symbol index unavailable (missing)" in data["reason"]


def test_search_codebase_garbage_symbol_index_reads_corrupt_not_stale(profiled_repo):
    # si01: a schema_version-absent/garbage-shaped payload has no evidence of
    # being a real prior-schema artifact, so it must read as "corrupt" -- not
    # "symbol-index-stale", which previously fired on ANY schema_version
    # mismatch including a missing/None one.
    (profiled_repo / ".chameleon" / "symbol_signatures.json").write_text(
        json.dumps({"unrelated": "garbage", "nothing": [1, 2, 3]})
    )
    data = _data(tools.search_codebase(str(profiled_repo), "no_such_symbol_zzz"))
    assert data.get("degraded") is True
    assert "symbol index unavailable (corrupt)" in data["reason"]


def test_search_codebase_shape_evidence_symbol_index_reads_stale(profiled_repo):
    # si01: a payload carrying the real "files" shape (just missing/older
    # schema_version) IS evidence of a genuine prior-schema artifact, so it
    # must still read as "symbol-index-stale" (repaired by /chameleon-refresh).
    (profiled_repo / ".chameleon" / "symbol_signatures.json").write_text(
        json.dumps({"files": {"svc.py": {"make_service": {}}}})
    )
    data = _data(tools.search_codebase(str(profiled_repo), "no_such_symbol_zzz"))
    assert data.get("degraded") is True
    assert "symbol-index-stale" in data["reason"]


def test_describe_codebase_tool(profiled_repo):
    data = _data(tools.describe_codebase(str(profiled_repo)))
    assert data["found"] is True
    assert data["language"] == "python"
    assert any(a["name"] == ARCH for a in data["archetypes"])


def test_describe_codebase_caps_archetypes_and_reports_omitted(profiled_repo, monkeypatch):
    """Over DESCRIBE_MAX_ARCHETYPES the overview keeps the largest rows and
    surfaces archetypes_omitted THROUGH the tool wrapper (regression: the
    wrapper rebuilt the response from a key whitelist and dropped the count,
    so a capped gitlabhq overview read as complete)."""
    cham = profiled_repo / ".chameleon"
    arch = json.loads((cham / "archetypes.json").read_text())
    arch["archetypes"]["tiny-extra"] = {
        "summary": "one-file tail cluster",
        "cluster_size": 1,
        "paths_pattern": "tail/",
    }
    (cham / "archetypes.json").write_text(json.dumps(arch))
    monkeypatch.setenv("CHAMELEON_DESCRIBE_MAX_ARCHETYPES", "1")
    data = _data(tools.describe_codebase(str(profiled_repo)))
    assert len(data["archetypes"]) == 1
    # Largest-first: the original 2-file archetype wins over the 1-file tail.
    assert data["archetypes"][0]["name"] == ARCH
    assert data["archetypes_omitted"] == 1


def test_describe_codebase_concise_keeps_name_size_witness(profiled_repo):
    data = _data(tools.describe_codebase(str(profiled_repo), response_format="concise"))
    assert data["found"] is True
    row = next(a for a in data["archetypes"] if a["name"] == ARCH)
    assert row == {"name": ARCH, "size": 2, "witness": "svc.py"}
    assert len(data["god_symbols"]) <= 5
    # Detailed (the default) still carries the prose fields.
    detailed = _data(tools.describe_codebase(str(profiled_repo)))
    drow = next(a for a in detailed["archetypes"] if a["name"] == ARCH)
    assert "summary" in drow and "paths" in drow


def test_describe_codebase_strips_paths_prefix_from_summary(profiled_repo):
    """A generated summary that leads with the row's own paths pattern pays for
    the pattern once: the duplicated prefix is stripped at render time."""
    cham = profiled_repo / ".chameleon"
    arch = json.loads((cham / "archetypes.json").read_text())
    # paths_pattern_display is what the rendered `paths` field carries (it wins
    # over paths_pattern), so the duplicated prefix must be stated in it.
    arch["archetypes"][ARCH]["paths_pattern_display"] = "src:py"
    arch["archetypes"][ARCH]["summary"] = "src:py. typical shape: services."
    (cham / "archetypes.json").write_text(json.dumps(arch))
    data = _data(tools.describe_codebase(str(profiled_repo)))
    row = next(a for a in data["archetypes"] if a["name"] == ARCH)
    assert row["summary"] == "typical shape: services."
    assert row["paths"] == "src:py"


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


def test_bootstrap_repo_dispatcher_forwards_production_ref(monkeypatch):
    # Regression: the MCP surface must EXPOSE and FORWARD production_ref, else the
    # init/refresh skills' explicit production-branch answer is silently dropped.
    # bootstrap_repo now routes through the chameleon_lifecycle dispatcher, whose
    # params dict must reach tools.bootstrap_repo intact.
    from chameleon_mcp import server

    assert "bootstrap_repo" in server._LIFECYCLE_ACTIONS
    captured = {}

    def fake(path, paths_glob=None, force=False, production_ref=None, now=None):
        captured["production_ref"] = production_ref
        return {"data": {"status": "ok"}}

    monkeypatch.setattr(tools, "bootstrap_repo", fake)
    server.chameleon_lifecycle(
        action="bootstrap_repo", params={"path": "/x", "production_ref": "release-1"}
    )
    assert captured["production_ref"] == "release-1"
