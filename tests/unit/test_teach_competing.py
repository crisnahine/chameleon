"""teach_competing_import wires the wrapper-preference (competing) convention.

The competing convention + its principle were dead (competing_pairs always
None at bootstrap). This tool lets /chameleon-teach write
conventions.imports.<arch>.competing so the "use X, not Y" import rule and the
"use the project's wrapper" principle actually fire.
"""

from __future__ import annotations

import json


def _setup_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    from chameleon_mcp.conventions import empty_conventions

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "conventions.json").write_text(
        json.dumps(empty_conventions(generation=1)), encoding="utf-8"
    )
    return repo


def _data(res):
    return res.get("data", res) if isinstance(res, dict) else res


def test_teach_competing_import_writes_and_is_idempotent(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)

    res = tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    # Mutation tools report success with status "success" (matches teach_profile,
    # teach_profile_structured, apply_archetype_renames).
    assert _data(res)["status"] == "success"

    conv = json.loads((repo / ".chameleon" / "conventions.json").read_text())
    competing = conv["conventions"]["imports"]["httpclient"]["competing"]
    assert {"preferred": "@/lib/http", "over": "axios"} in competing

    # The format helper reads this entry and emits the live import rule.
    from chameleon_mcp.conventions import format_conventions_for_session

    block = format_conventions_for_session(conv)
    assert "Use @/lib/http, not axios" in block

    # Idempotent: re-teaching the same pair doesn't duplicate it.
    tools.teach_competing_import(
        str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
    )
    conv2 = json.loads((repo / ".chameleon" / "conventions.json").read_text())
    assert len(conv2["conventions"]["imports"]["httpclient"]["competing"]) == 1


def test_teach_competing_import_rejects_bad_input(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)

    # empty 'over'
    assert (
        _data(
            tools.teach_competing_import(str(repo), archetype="httpclient", preferred="x", over="")
        )["status"]
        == "failed"
    )
    # preferred == over
    assert (
        _data(
            tools.teach_competing_import(str(repo), archetype="httpclient", preferred="x", over="x")
        )["status"]
        == "failed"
    )
    # invalid archetype name
    assert (
        _data(
            tools.teach_competing_import(str(repo), archetype="Bad Name!", preferred="x", over="y")
        )["status"]
        == "failed"
    )


def _setup_repo_with_archetypes(tmp_path, monkeypatch, names):
    repo = _setup_repo(tmp_path, monkeypatch)
    (repo / ".chameleon" / "archetypes.json").write_text(
        json.dumps({"archetypes": {n: {} for n in names}}), encoding="utf-8"
    )
    return repo


def test_teach_competing_known_archetype_no_warning(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    repo = _setup_repo_with_archetypes(tmp_path, monkeypatch, ["httpclient"])
    res = _data(
        tools.teach_competing_import(
            str(repo), archetype="httpclient", preferred="@/lib/http", over="axios"
        )
    )
    assert res["status"] == "success"
    assert "warning" not in res


def test_teach_competing_unknown_archetype_warns_but_succeeds(tmp_path, monkeypatch):
    # The rule drives a lint; a typo'd archetype no file matches is a silent dead
    # rule. It is still recorded (forward-compat for renamed archetypes) but flagged.
    from chameleon_mcp import tools

    repo = _setup_repo_with_archetypes(tmp_path, monkeypatch, ["httpclient"])
    res = _data(
        tools.teach_competing_import(
            str(repo), archetype="typoclient", preferred="@/lib/http", over="axios"
        )
    )
    assert res["status"] == "success"
    assert "warning" in res
    assert "typoclient" in res["warning"]


def test_teach_competing_no_catalog_no_warning(tmp_path, monkeypatch):
    # Fail-open: with no archetypes.json the known set is undeterminable, so no warning.
    from chameleon_mcp import tools

    repo = _setup_repo(tmp_path, monkeypatch)
    res = _data(
        tools.teach_competing_import(
            str(repo), archetype="anything", preferred="@/lib/http", over="axios"
        )
    )
    assert res["status"] == "success"
    assert "warning" not in res
