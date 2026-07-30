"""doctor must surface a degraded linter config recorded in rules.json.

A malformed pyproject.toml/ruff.toml bootstraps to zero format rules with a
parse_warning persisted in rules.json (loud in the artifact and via
get_rules), but doctor never looked inside rules.json, so the same degraded
config passed doctor clean -- the "no doctor check" half of the gap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chameleon_mcp import doctor as doctor_mod
from chameleon_mcp import index_db, tools


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    index_db.close_index_connections()
    tools._clear_repo_id_cache()
    yield
    index_db.close_index_connections()
    tools._clear_repo_id_cache()


def _repo_with_rules(tmp_path: Path, rules: dict) -> Path:
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / "COMMITTED").write_text("committed-at=1.0\npid=1\n", encoding="utf-8")
    (cham / "profile.json").write_text(
        json.dumps({"schema_version": 8, "repo_id": "x", "language": "python", "generation": 1}),
        encoding="utf-8",
    )
    (cham / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    return repo


def _linter_check(repo: Path) -> dict | None:
    checks = doctor_mod.doctor(repo=str(repo)).get("data", {}).get("checks", [])
    return next((c for c in checks if c["name"] == "linter_config"), None)


def test_doctor_warns_on_linter_parse_warning(tmp_path):
    repo = _repo_with_rules(
        tmp_path,
        {
            "rules": {
                "python_format": {
                    "source": "pyproject.toml",
                    "parse_warning": "malformed TOML in pyproject.toml: Expected ']'",
                }
            }
        },
    )
    lc = _linter_check(repo)
    assert lc is not None, "doctor has no linter_config check"
    assert lc["status"] == "warn"
    assert "python_format" in lc["detail"]
    assert "malformed TOML" in lc["detail"]


def test_doctor_ok_when_no_linter_parse_warning(tmp_path):
    repo = _repo_with_rules(
        tmp_path,
        {"rules": {"python_format": {"source": "pyproject.toml", "rules": {"line_length": 100}}}},
    )
    lc = _linter_check(repo)
    assert lc is not None, "doctor has no linter_config check"
    assert lc["status"] == "ok"
