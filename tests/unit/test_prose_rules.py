"""Offline prose-rule miner: extract doc-stated "use X not Y", corroborate vs code.

The miner reads a bounded allowlist of convention-bearing docs (CONTRIBUTING /
STYLE / AGENTS.md / docs), extracts high-precision "use X not Y" / "prefer X over
Y" rules with provenance, and corroborates each against the repo's own imports.
Only a rule the code already backs (preferred used, over absent) is teachable;
everything else is surfaced advisory. The tool is propose-only: it never writes
the profile.
"""

from __future__ import annotations

import pytest

from chameleon_mcp import server, tools
from chameleon_mcp.profile.trust import grant_trust, repo_data_dir
from chameleon_mcp.prose_rules import extract_prose_rules, mine_prose_rule_candidates

# --- extraction ------------------------------------------------------------


def test_extract_use_not():
    assert ("@/lib/http", "axios") in extract_prose_rules(
        "Always use @/lib/http, not axios, for requests."
    )


def test_extract_prefer_over():
    assert ("date-fns", "moment") in extract_prose_rules("Prefer `date-fns` over moment.")


def test_extract_instead_of():
    assert ("pathlib", "os.path") in extract_prose_rules("Use pathlib instead of os.path here.")


def test_extract_strips_trailing_punctuation():
    # the "over" token must not absorb the sentence-ending period
    rules = extract_prose_rules("Use @/lib/http, not axios.")
    assert ("@/lib/http", "axios") in rules


def test_extract_ignores_prose_without_pattern():
    assert extract_prose_rules("This module handles HTTP requests for the app.") == []


def test_extract_drops_single_char_placeholder_tokens():
    # "use X not Y" doc placeholders are not real import rules
    assert extract_prose_rules("Ship a feature, e.g. use X not Y in the example.") == []


# --- corroboration + the tool ----------------------------------------------


@pytest.fixture
def trusted_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "COMMITTED").touch()
    grant_trust(tools._compute_repo_id(repo), repo / ".chameleon")
    return repo


def test_mine_corroborated_when_preferred_used_and_over_absent(trusted_repo):
    (trusted_repo / "CONTRIBUTING.md").write_text("Use @/lib/http, not axios.\n")
    (trusted_repo / "service.ts").write_text(
        "import { http } from '@/lib/http';\nexport const x = 1;\n"
    )
    cands = mine_prose_rule_candidates(trusted_repo)
    match = [c for c in cands if c["preferred"] == "@/lib/http" and c["over"] == "axios"]
    assert match, cands
    assert match[0]["status"] == "corroborated"
    assert match[0]["teachable"] is True
    assert "CONTRIBUTING.md" in match[0]["source"]


def test_mine_contested_when_over_still_imported(trusted_repo):
    (trusted_repo / "CONTRIBUTING.md").write_text("Use @/lib/http, not axios.\n")
    (trusted_repo / "service.ts").write_text("import axios from 'axios';\nexport const x = 1;\n")
    cands = mine_prose_rule_candidates(trusted_repo)
    match = [c for c in cands if c["over"] == "axios"]
    assert match and match[0]["status"] == "contested"
    assert match[0]["teachable"] is False


def test_mine_unsupported_when_neither_imported(trusted_repo):
    (trusted_repo / "CONTRIBUTING.md").write_text("Use @/lib/http, not axios.\n")
    (trusted_repo / "service.ts").write_text("export const x = 1;\n")
    cands = mine_prose_rule_candidates(trusted_repo)
    match = [c for c in cands if c["over"] == "axios"]
    assert match and match[0]["status"] == "unsupported"
    assert match[0]["teachable"] is False


def test_tool_get_prose_rule_candidates(trusted_repo):
    (trusted_repo / "CONTRIBUTING.md").write_text("Use @/lib/http, not axios.\n")
    (trusted_repo / "service.ts").write_text("import { http } from '@/lib/http';\n")
    res = tools.get_prose_rule_candidates(str(trusted_repo))
    assert res.get("api_version") == "1"
    data = res["data"]
    assert data["found"] is True
    assert any(c["preferred"] == "@/lib/http" for c in data["candidates"])


def test_tool_untrusted(trusted_repo):
    (trusted_repo / "CONTRIBUTING.md").write_text("Use @/lib/http, not axios.\n")
    tp = repo_data_dir(tools._compute_repo_id(trusted_repo)) / ".trust"
    if tp.is_file():
        tp.unlink()
    res = tools.get_prose_rule_candidates(str(trusted_repo))
    assert res["data"]["found"] is False
    assert res["data"].get("status") == "untrusted"


def test_tool_registered_in_server():
    assert hasattr(server, "get_prose_rule_candidates")
    assert callable(server.get_prose_rule_candidates)
