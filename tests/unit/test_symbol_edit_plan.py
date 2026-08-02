"""The edit plan answers the three questions an edit to one symbol must ask.

Renaming a symbol or replacing its body needs its exact line range, everyone
who calls it, and everyone who imports it. Chameleon already held all three in
committed artifacts and made the caller assemble them from three tools with
three different response shapes. This returns them together, with line ranges
rather than prose, so a caller can drive an exact edit instead of grepping.

Read-only on purpose: chameleon's contract is that its own conclusions never
authorize a write, so the plan is the deliverable and the edit stays the
caller's. The honesty flag is the load-bearing part -- `complete` goes False the
moment any leg was unavailable or truncated, because a short reference list read
as a verified-small blast radius is exactly how a rename breaks a caller nobody
looked at.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp import tools


@pytest.fixture
def plan_env(tmp_path: Path, monkeypatch):
    """A trusted repo whose artifacts the plan reads."""
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "api.ts").write_text("export function getUser() {}\n", encoding="utf-8")
    return repo


def _grant(repo: Path):
    rec = MagicMock()
    rec.grants_root.return_value = True
    return patch("chameleon_mcp.profile.trust.trust_state_for", return_value=rec)


def test_a_bad_symbol_argument_fails_open(plan_env: Path):
    for bad in (None, 123, "", "   "):
        out = tools.get_symbol_edit_plan(str(plan_env), "src/api.ts", bad)
        assert out["data"]["found"] is False
        assert out["data"]["complete"] is False


def test_a_path_outside_any_repo_fails_open_with_a_reason(plan_env: Path):
    out = tools.get_symbol_edit_plan(str(plan_env), "/etc/passwd", "x")
    assert out["data"]["found"] is False
    assert out["data"].get("reason") in {"path-unresolved", "file-outside-repo"}


def test_an_untrusted_repo_yields_nothing(plan_env: Path):
    """Artifact-derived paths must not reach the model surface untrusted."""
    rec = MagicMock()
    rec.grants_root.return_value = False
    with patch("chameleon_mcp.profile.trust.trust_state_for", return_value=rec):
        out = tools.get_symbol_edit_plan(str(plan_env), str(plan_env / "src/api.ts"), "getUser")
    assert out["data"]["found"] is False
    assert out["data"].get("status") == "untrusted"


def test_a_missing_definition_reports_incomplete_not_found(plan_env: Path):
    """No signature row means the plan cannot anchor an edit; say so."""
    with _grant(plan_env):
        out = tools.get_symbol_edit_plan(
            str(plan_env), str(plan_env / "src/api.ts"), "neverDefined"
        )
    assert out["data"]["found"] is False
    assert out["data"]["complete"] is False


def test_the_envelope_shape_is_stable(plan_env: Path):
    """Every documented key is present even on the fail-open path, so a caller
    never has to branch on whether the lookup succeeded."""
    with _grant(plan_env):
        data = tools.get_symbol_edit_plan(str(plan_env), str(plan_env / "src/api.ts"), "x")["data"]
    for key in ("found", "definition", "references", "references_total", "importers", "complete"):
        assert key in data, key


def test_references_total_is_the_index_total_not_the_shown_count():
    """A capped symbol must not read as a small edit.

    The index caps caller rows at build time, so `len(references)` is a sample.
    Reporting only the sample would let a caller see "3 references" on a symbol
    with 112 and conclude the rename is contained.
    """
    entry = {
        "callers": [{"path": "a.ts", "caller": "f", "line": 1, "grade": "import"}],
        "total": 112,
        "truncated": True,
    }
    index = MagicMock()
    index.callers_of.return_value = entry
    sigs = MagicMock()
    sigs.lookup.return_value = {"start_line": 10, "end_line": 20}

    with (
        patch("chameleon_mcp.calls_index.load_calls_index", return_value=index),
        patch("chameleon_mcp.symbol_signatures.load_symbol_signatures", return_value=sigs),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.symbol_index.module_key_for_path", return_value="src/api.ts"),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch("chameleon_mcp.tools.query_symbol_importers", return_value={"data": {}}),
        patch("chameleon_mcp.profile.trust.trust_state_for") as trust,
    ):
        trust.return_value.grants_root.return_value = True
        data = tools.get_symbol_edit_plan("rid", "/repo/src/api.ts", "getUser")["data"]

    assert data["references_total"] == 112
    assert len(data["references"]) == 1
    assert data["complete"] is False, "a truncated index must never report complete"


def test_importers_are_filtered_by_name_from_the_row_list():
    """`query_symbol_importers` returns a LIST of {name, count, sites}, not a
    name-keyed dict, so the filter belongs on the row."""
    importers_payload = {
        "data": {
            "importers": [
                {"name": "other", "count": 1, "sites": [{"path": "z.ts", "line": 1}]},
                {"name": "getUser", "count": 2, "sites": [{"path": "b.ts", "line": 7}]},
            ]
        }
    }
    sigs = MagicMock()
    sigs.lookup.return_value = {"start_line": 3, "end_line": 4}
    index = MagicMock()
    index.callers_of.return_value = None

    with (
        patch("chameleon_mcp.calls_index.load_calls_index", return_value=index),
        patch("chameleon_mcp.symbol_signatures.load_symbol_signatures", return_value=sigs),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.symbol_index.module_key_for_path", return_value="src/api.ts"),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch("chameleon_mcp.tools.query_symbol_importers", return_value=importers_payload),
        patch("chameleon_mcp.profile.trust.trust_state_for") as trust,
    ):
        trust.return_value.grants_root.return_value = True
        data = tools.get_symbol_edit_plan("rid", "/repo/src/api.ts", "getUser")["data"]

    assert data["importers"] == [{"path": "b.ts", "line": 7}], data["importers"]


def test_the_tool_is_registered_on_the_mcp_server():
    """A tool nothing exposes is a tool nobody can call."""
    from chameleon_mcp import server

    assert hasattr(server, "get_symbol_edit_plan")
