"""teach_profile_structured enforces the 50KB per-idiom total cap.

The cap (rationale + example + counterexample <= 50000 chars) is deterministic
server-side validation, asserted here rather than through a live editing session:
forcing a model to emit a 51KB rationale either trips the per-response output
ceiling or stalls the stream, and exercises the model, not the cap.
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


def _teach(repo, **kw):
    from chameleon_mcp import tools

    base = dict(
        slug="cap-probe",
        rationale="wrap http in src/lib/api.ts",
        example="import { api } from '@/lib/api'",
        counterexample="import axios from 'axios'",
        archetype="util",
        status="active",
    )
    base.update(kw)
    return _data(tools.teach_profile_structured(str(repo), **base))


def test_over_cap_rationale_rejected(tmp_path, monkeypatch):
    repo = _setup_repo(tmp_path, monkeypatch)
    res = _teach(repo, slug="fifty-kb-test", rationale="x" * 51000)
    assert res["status"] == "failed"
    assert "50KB cap" in res["error"]


def test_combined_fields_count_toward_cap(tmp_path, monkeypatch):
    # The cap is on rationale + example + counterexample combined, not rationale alone.
    repo = _setup_repo(tmp_path, monkeypatch)
    res = _teach(
        repo,
        slug="combined-cap",
        rationale="x" * 49990,
        example="y" * 30,
        counterexample="z" * 30,
    )
    assert res["status"] == "failed"
    assert "50KB cap" in res["error"]


def test_just_under_cap_succeeds(tmp_path, monkeypatch):
    repo = _setup_repo(tmp_path, monkeypatch)
    res = _teach(repo, slug="under-cap", rationale="x" * 49000)
    assert res["status"] == "success"
