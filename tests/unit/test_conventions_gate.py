"""A headless, diff-scoped conventions gate for edits PreToolUse never saw.

Everything chameleon enforces runs inside an interactive session. That leaves a
real hole: human-authored commits, sessions with chameleon disabled, paused or
untrusted, and other agents entirely. architecture.md names CI as a rollout
stage and ships no mechanism for it.

Two constraints the design has to honor, both from the repo's own evidence:

- DIFF-SCOPED OR NOT AT ALL. Whole-blob counting measures accumulated load, not
  what a change did; the re-run measured pre-existing load at 5.2-5.7x the
  introduced count. A file-level gate would fail a PR for violations it
  inherited, which is how a gate gets switched off in a week.
- SOFT BY DEFAULT. lint_file is heuristic regex extraction, not a parser, and
  architecture.md deliberately keeps CI a build/lint pipeline rather than a
  correctness gate. The exit code is advisory unless a caller opts in.

The gate is a backstop, not a better arrival time: guidance that lands before
the write is strictly better, and this only covers writes that never got it.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.gate import GateResult, decide


def _f(rule="naming-convention-violation", severity="error", path="src/a.ts"):
    return {"rule": rule, "severity": severity, "file": path, "message": "m"}


class TestDecide:
    def test_clean_diff_passes(self):
        r = decide([], strict=False)
        assert isinstance(r, GateResult)
        assert r.exit_code == 0
        assert r.introduced == 0

    def test_findings_are_advisory_by_default(self):
        # Reports them, does not fail the build: the honest posture for a
        # heuristic extractor.
        r = decide([_f(), _f()], strict=False)
        assert r.introduced == 2
        assert r.exit_code == 0

    def test_strict_mode_fails_on_introduced_findings(self):
        r = decide([_f()], strict=True)
        assert r.exit_code == 1

    def test_strict_mode_still_passes_a_clean_diff(self):
        assert decide([], strict=True).exit_code == 0

    def test_per_edit_suppressed_rules_are_not_counted(self):
        # A rule the per-edit path strips before the model reads it cannot be
        # one this gate holds an author to.
        r = decide([_f(rule="cross-file-importers")], strict=True)
        assert r.introduced == 0
        assert r.exit_code == 0

    def test_findings_are_grouped_by_rule_for_reporting(self):
        r = decide([_f(rule="a"), _f(rule="a"), _f(rule="b")], strict=False)
        assert r.by_rule == {"a": 2, "b": 1}


class TestRender:
    def test_clean_render_says_so(self):
        from chameleon_mcp.gate import render_text

        out = render_text(decide([], strict=False))
        assert "no new convention violations" in out.lower()

    def test_render_names_rule_and_file(self):
        from chameleon_mcp.gate import render_text

        out = render_text(decide([_f(rule="naming-convention-violation")], strict=False))
        assert "naming-convention-violation" in out
        assert "src/a.ts" in out

    def test_render_states_the_scope_it_measured(self):
        # Anyone reading CI output must know this is introduced-only, or they
        # will read a low number as "the file is clean".
        from chameleon_mcp.gate import render_text

        out = render_text(decide([_f()], strict=False))
        assert "introduced" in out.lower()


class TestUntrusted:
    def test_untrusted_profile_is_reported_not_silently_clean(self):
        # lint_file returns zero convention findings without a trust grant, so a
        # gate that ignored trust would print a green build for a repo it never
        # actually linted -- the worst possible failure for a CI check.
        r = decide([], strict=True, trusted=False)
        assert r.exit_code == 2
        assert r.untrusted is True

    def test_trusted_is_the_default(self):
        assert decide([], strict=True).untrusted is False


@pytest.mark.parametrize("strict", [True, False])
def test_untrusted_never_reports_success(strict):
    assert decide([], strict=strict, trusted=False).exit_code != 0


class TestUnusable:
    def test_a_git_failure_never_renders_as_a_clean_build(self, tmp_path):
        """Regression: a bad base returned [] changed files and exited 0.

        A shallow CI clone missing origin/main, a typo'd --base, or no git at
        all made `git diff` exit non-zero; the loop never ran, and the gate
        printed "no new convention violations" at exit 0 for a diff nothing
        looked at. That is the silent pass the module exists to avoid.
        """
        from chameleon_mcp.gate import main

        (tmp_path / "f.ts").write_text("export const a = 1;\n", encoding="utf-8")
        code = main(["--repo", str(tmp_path), "--base", "no-such-ref", "--strict"])
        assert code == 2
