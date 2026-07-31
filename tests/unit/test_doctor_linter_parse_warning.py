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
    # Not just the status: an always-"ok" check would pass the assertion above
    # while reporting nothing, so pin the reason too.
    assert "no linter config parse warnings" in lc["detail"]


def test_tolerant_parse_that_salvaged_rules_is_not_a_warn(tmp_path):
    # ERB in a Rails .rubocop.yml is how that file is meant to be written, and
    # _parse_rubocop_tolerant returns the FULL rule set plus a note saying what
    # it neutralized. Warning on that note left doctor permanently yellow on a
    # healthy repo with nothing to fix. The stanza carrying "rules" is what
    # separates a salvaged read from a config that yielded nothing.
    repo = _repo_with_rules(
        tmp_path,
        {
            "rules": {
                "rubocop": {
                    "source": ".rubocop.yml",
                    "rules": {"Style/FrozenStringLiteralComment": {"Enabled": True}},
                    "parse_warning": (
                        "ERB tags neutralized (templated values appear as 'erb_omitted')"
                    ),
                }
            }
        },
    )
    lc = _linter_check(repo)
    assert lc is not None, "doctor has no linter_config check"
    assert lc["status"] == "ok", lc["detail"]
    assert "erb_omitted" in lc["detail"], "the salvaged note should still be reported"


def test_unread_config_warns_even_when_a_sibling_salvaged(tmp_path):
    # The two classes are independent: a genuinely unreadable config must still
    # warn while a salvaged sibling rides along as informational.
    repo = _repo_with_rules(
        tmp_path,
        {
            "rules": {
                "rubocop": {
                    "source": ".rubocop.yml",
                    "rules": {"Style/FrozenStringLiteralComment": {"Enabled": True}},
                    "parse_warning": "ERB tags neutralized",
                },
                "python_format": {
                    "source": "pyproject.toml",
                    "parse_warning": "malformed TOML in pyproject.toml: Expected ']'",
                },
            }
        },
    )
    lc = _linter_check(repo)
    assert lc is not None, "doctor has no linter_config check"
    assert lc["status"] == "warn"
    assert "malformed TOML" in lc["detail"]
    assert "ERB tags neutralized" in lc["detail"]
