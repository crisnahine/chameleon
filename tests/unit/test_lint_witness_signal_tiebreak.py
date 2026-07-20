"""Multi-witness canonical tie-break prefers the content_signal candidate.

lint_file recalibrates one candidate ast_query per canonicals entry and keeps
the best fit. A signal-less witness skips the directive check entirely, so it
always scored higher confidence against a probe MISSING the directive -- one
signal-less sibling witness silently disabled the frozen_string_literal check
for the whole archetype. On a structural tie the signal-carrying candidate now
wins, so the mismatch surfaces.
"""

from __future__ import annotations

import json

import pytest

from chameleon_mcp import tools
from chameleon_mcp.profile.trust import grant_trust

ARCH = "class-ledgermatch"

_WITH_DIRECTIVE = (
    "# frozen_string_literal: true\n"
    "\n"
    "module Ledgermatch\n"
    "  class Matcher\n"
    "    def match\n"
    "      :ok\n"
    "    end\n"
    "  end\n"
    "end\n"
)
_WITHOUT_DIRECTIVE = (
    "module Ledgermatch\n  class Scanner\n    def scan\n      :ok\n    end\n  end\nend\n"
)


@pytest.fixture
def two_witness_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    lib = repo / "lib" / "ledgermatch"
    lib.mkdir(parents=True)
    # The signal-less witness sorts FIRST in the entries list so the old
    # confidence tiebreak would settle on it.
    (lib / "duplicate_scanner.rb").write_text(_WITHOUT_DIRECTIVE, encoding="utf-8")
    (lib / "matcher.rb").write_text(_WITH_DIRECTIVE, encoding="utf-8")
    ast_query = {
        "top_level_node_kinds": ["ClassNode"],
        "default_export_kind": None,
        "named_export_count_bucket": "0",
        "jsx_present": False,
        "content_signal": None,
    }
    (cham / "profile.json").write_text(json.dumps({"generation": 1, "language": "ruby"}))
    (cham / "archetypes.json").write_text(
        json.dumps({"generation": 1, "archetypes": {ARCH: {"summary": "gem classes"}}})
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}))
    (cham / "canonicals.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "canonicals": {
                    ARCH: [
                        {
                            "witness": {"path": "lib/ledgermatch/duplicate_scanner.rb"},
                            "normative_shape": {"ast_query": ast_query},
                        },
                        {
                            "witness": {"path": "lib/ledgermatch/matcher.rb"},
                            "normative_shape": {"ast_query": ast_query},
                        },
                    ]
                },
            }
        )
    )
    (cham / "conventions.json").write_text(json.dumps({"generation": 1, "conventions": {}}))
    (cham / "COMMITTED").touch()
    grant_trust(tools._compute_repo_id(repo), cham)
    return repo


def _rules(res: dict) -> set[str]:
    return {v.get("rule") for v in res["data"].get("violations", [])}


def test_no_directive_probe_reports_content_signal_mismatch(two_witness_repo):
    probe = "module Ledgermatch\n  class Probe\n    def run\n      :ok\n    end\n  end\nend\n"
    res = tools.lint_file(str(two_witness_repo), ARCH, probe, file_path="lib/ledgermatch/probe.rb")
    assert "content-signal-mismatch" in _rules(res)


def test_directive_probe_stays_clean(two_witness_repo):
    probe = (
        "# frozen_string_literal: true\n"
        "\n"
        "module Ledgermatch\n"
        "  class Probe\n"
        "    def run\n"
        "      :ok\n"
        "    end\n"
        "  end\n"
        "end\n"
    )
    res = tools.lint_file(str(two_witness_repo), ARCH, probe, file_path="lib/ledgermatch/probe.rb")
    assert "content-signal-mismatch" not in _rules(res)
