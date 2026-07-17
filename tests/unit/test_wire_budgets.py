"""Wire-size regression guard for the MCP surface (the token-audit loop).

The v4.3/v4.4 overhaul bought its response-size wins from specific mechanics:
compact serialization (no indent), null-dropping, grouped rows, and bounded
help output. Each ceiling here is ~1.5x the measured size of the SAME call on
the deterministic fixture, so an accidental re-introduction of pretty-printing
(~+30%), payload duplication, or an unbounded new field fails THIS suite
instead of silently re-inflating every session's token bill. If a ceiling
trips on a deliberate feature, re-measure and move the pin in the same commit
that grows the payload -- never delete the guard.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import server, tools
from chameleon_mcp.profile.trust import grant_trust

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


def _wire_len(result: dict) -> int:
    return len(server._wire(result))


def test_fixture_response_wire_budgets(trusted_repo):
    # Measured on this fixture at v4.4.0: detect 382B, pattern_context 666B,
    # describe 215B. Ceilings are ~1.5x so a pretty-print or duplication
    # regression (+30%+) trips while honest drift does not.
    assert _wire_len(tools.detect_repo(str(trusted_repo / WITNESS))) <= 575
    assert _wire_len(tools.get_pattern_context(str(trusted_repo / WITNESS))) <= 1_000
    assert _wire_len(tools.describe_codebase(str(trusted_repo))) <= 325


def test_help_wire_budgets():
    # help renders one signature + <=400-char summary per action from live
    # tools.py; measured 2106/1643/1895B at v4.4.0. The ceilings bound BOTH
    # accidental serialization bloat and unbounded growth of an action's
    # first docstring paragraph.
    assert _wire_len(server.chameleon_lifecycle(action="help")) <= 3_200
    assert _wire_len(server.chameleon_review(action="help")) <= 2_500
    assert _wire_len(server.chameleon_telemetry(action="help")) <= 2_900


def test_tool_schema_stays_under_truncation_and_total_budget():
    # Every description must fit Claude Code's 2KB truncation ceiling, and the
    # whole tools/list schema stays bounded (~5.3k tokens measured at v4.4.0;
    # chars/4 approximation with 25% headroom).
    total_chars = 0
    for t in server.mcp._tool_manager.list_tools():
        blob = json.dumps(
            {"name": t.name, "description": t.description, "inputSchema": t.parameters},
            ensure_ascii=False,
            default=str,
        )
        total_chars += len(blob)
        assert len(t.description or "") <= 2048, f"{t.name} over the 2KB description ceiling"
    assert total_chars <= 28_000, f"tools/list schema grew to {total_chars} chars"


def test_wire_never_pretty_prints(trusted_repo):
    text = server._wire(tools.detect_repo(str(trusted_repo / WITNESS)))
    assert "\n" not in text and '": ' not in text and '", ' not in text
