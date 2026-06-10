"""Regression pins for the qa26 campaign remediation.

Each test encodes the FIXED behavior of a bug found by the qa26 squads, so a
later change that reintroduces the bug fails here with the original repro
shape, not a vague assertion.
"""

from chameleon_mcp.bootstrap import orchestrator as o
from chameleon_mcp.duplication_review import Finding, build_duplication_prompt
from chameleon_mcp.hook_helper import _extract_bash_write_targets, _split_outside_quotes
from chameleon_mcp.idiom_coverage import merge_idioms_markdown
from chameleon_mcp.lint_engine import (
    _is_self_assignment_line,
    _namespace_local_base,
    scan_secrets,
)


class TestSedWriteTargetExtraction:
    """ENF P2-1: separators inside quoted sed scripts are script content."""

    def test_pipe_delimiter_script(self):
        cmd = "sed -i '' 's|API_KEY=.*|API_KEY=AKIAQA26ZZTESTKEY123|' /repo/app/models/secret.rb"
        assert _extract_bash_write_targets(cmd) == ["/repo/app/models/secret.rb"]

    def test_chained_semicolon_script(self):
        assert _extract_bash_write_targets("sed -i '' 's/a/b/;s/c/d/' /repo/f.ts") == ["/repo/f.ts"]

    def test_ampersand_backreference_script(self):
        assert _extract_bash_write_targets("sed -i '' 's/a/&z/' /repo/f.ts") == ["/repo/f.ts"]

    def test_real_pipe_still_scopes_to_sed_segment(self):
        assert _extract_bash_write_targets("sed -i 's/a/b/' /repo/f.ts | grep done") == [
            "/repo/f.ts"
        ]

    def test_second_command_after_semicolon(self):
        assert _extract_bash_write_targets("echo hi; sed -i 's/x/y/' /repo/g.rb") == ["/repo/g.rb"]

    def test_split_outside_quotes_keeps_quoted_separators(self):
        assert _split_outside_quotes("a 'x;y|z' b; c") == ["a 'x;y|z' b", " c"]

    def test_redirect_and_tee_unaffected(self):
        assert _extract_bash_write_targets("cat foo > /repo/out.ts 2>&1") == ["/repo/out.ts"]
        assert _extract_bash_write_targets("echo x | tee /repo/t.rb") == ["/repo/t.rb"]


class TestSecretSelfAssignment:
    """Jira P1-1: KEY: "KEY" route-name constants are never credentials."""

    def test_route_key_map_produces_no_findings(self):
        content = (
            "const K = {\n"
            '  FORGET_PASSWORD: "FORGET_PASSWORD",\n'
            '  FORGET_PASSWORD_AUTH: "FORGET_PASSWORD_AUTH",\n'
            '  RESET_PASSWORD_SUCCESS: "RESET_PASSWORD_SUCCESS",\n'
            "};\n"
        )
        assert scan_secrets(content) == []

    def test_equals_and_hashrocket_forms(self):
        assert _is_self_assignment_line('API_PASSWORD = "API_PASSWORD"')
        assert _is_self_assignment_line('"SECRET_TOKEN" => "SECRET_TOKEN"')
        assert not _is_self_assignment_line('API_PASSWORD = "hunter2"')

    def test_declaration_prefixes_do_not_defeat_suppression(self):
        # qa26 regression-squad BUG 1: the canonical redux-action shape.
        assert _is_self_assignment_line('const SECRET_KEY = "SECRET_KEY";')
        assert _is_self_assignment_line('export const RESET_PASSWORD = "RESET_PASSWORD";')
        assert _is_self_assignment_line("  let API_TOKEN = 'API_TOKEN'")
        assert not _is_self_assignment_line('export const API_KEY = "AKIAIOSFODNN7EXAMPLE";')
        assert scan_secrets('export const SECRET_KEY = "SECRET_KEY";\n') == []

    def test_real_secrets_still_caught(self):
        content = 'const aws = "AKIAIOSFODNN7EXAMPLE";\npassword = "hunter2secret"\n'
        found = scan_secrets(content)
        assert found, "real credentials must still be flagged"
        kinds = " ".join(v.actual for v in found)
        assert "aws_access_key" in kinds

    def test_overlapping_detectors_dedupe_to_one_per_line(self):
        # A single password assignment historically drew multiple detector
        # hits on the same line; one actionable finding per line is enough.
        content = 'password = "hunter2secret"\n'
        lines = [v.actual for v in scan_secrets(content) if "line 1" in v.actual]
        assert len(lines) == 1


class TestNamespaceLocalBase:
    """Jira P3-2: suggest the namespace-local dominant base, not repo-wide."""

    def test_admin_namespace_prefers_admin_base(self):
        known = {"Api::V1::BaseController", "Api::V1::Admin::BaseController"}
        assert (
            _namespace_local_base(
                "Api::V1::Admin::FlaggedListingsController", known, "Api::V1::BaseController"
            )
            == "Api::V1::Admin::BaseController"
        )

    def test_no_namespace_falls_back_to_dominant(self):
        known = {"Api::V1::BaseController", "Api::V1::Admin::BaseController"}
        assert _namespace_local_base("Widget", known, "Api::V1::BaseController") == (
            "Api::V1::BaseController"
        )

    def test_tie_keeps_dominant(self):
        known = {"Api::V1::BaseController", "Api::V1::OtherBase"}
        assert (
            _namespace_local_base("Api::V1::UsersController", known, "Api::V1::BaseController")
            == "Api::V1::BaseController"
        )

    def test_sibling_version_namespace_never_suggested(self):
        # qa26 regression-squad BUG 3: a partially-shared prefix is a sibling
        # namespace, not an enclosing one — V2 must not be steered onto V1.
        known = {
            "Api::V1::BaseController",
            "Api::V1::Admin::BaseController",
            "ApplicationController",
        }
        assert (
            _namespace_local_base("Api::V2::WidgetsController", known, "ApplicationController")
            == "ApplicationController"
        )

    def test_enclosing_parent_namespace_still_wins(self):
        known = {"Api::V1::BaseController", "ApplicationController"}
        assert (
            _namespace_local_base("Api::V1::Admin::XController", known, "ApplicationController")
            == "Api::V1::BaseController"
        )


class TestIdiomsMergeLooseContent:
    """FR P2-3: hand-written content outside ### blocks survives the merge."""

    BASE = "# Team idioms\n\n## active\n\n_(no idioms yet)_\n"

    def test_loose_bullets_from_both_sides_survive(self):
        ours = "# Team idioms\n\n## active\n\n- HAND-WRITTEN RULE A\n"
        theirs = "# Team idioms\n\n## active\n\n- HAND-WRITTEN RULE B\n"
        merged = merge_idioms_markdown(self.BASE, ours, theirs)
        assert "- HAND-WRITTEN RULE A" in merged
        assert "- HAND-WRITTEN RULE B" in merged

    def test_structured_blocks_still_union_with_fences(self):
        base = "# idioms\n\n## active\n\n### use-notify\n- always notify\n"
        ours = base + "\n### ours-slug\n- ours rule\n"
        theirs = base + "\n### theirs-slug\n```ts\nconst x = 1;\n```\n"
        merged = merge_idioms_markdown(base, ours, theirs)
        for token in ("### use-notify", "### ours-slug", "### theirs-slug"):
            assert token in merged
        assert merged.count("```") == 2

    def test_placeholders_not_treated_as_content(self):
        merged = merge_idioms_markdown(self.BASE, self.BASE, self.BASE)
        assert merged.count("_(no idioms yet)_") == 1


class TestDuplicationPrompt:
    """Jira P1-2: the judge sees BOTH bodies, signatures included."""

    def test_prompt_carries_existing_body(self):
        f = Finding(
            new_name="ensure",
            new_file="src/utils/qa26ensure.ts",
            line=1,
            excerpt="export function ensure(condition, message) {\n  if (!condition) {",
            existing_name="assert",
            existing_file="src/utils/assert.ts",
            existing_excerpt="export function assert(condition, message) {\n  if (!condition) {",
        )
        prompt = build_duplication_prompt([f])
        assert "new body:\nexport function ensure" in prompt
        assert "existing body:\nexport function assert" in prompt

    def test_missing_existing_body_is_explicit(self):
        f = Finding(
            new_name="a",
            new_file="f.ts",
            line=1,
            excerpt="function a() {}",
            existing_name="b",
            existing_file="g.ts",
        )
        assert "(source unavailable)" in build_duplication_prompt([f])


class TestTierOneSummary:
    """Jira P3-1: Tier 1 pointer text is humanized, no parser jargon."""

    def test_kinds_humanized_and_deduped(self):
        entry = {
            "paths_pattern": "src/components",
            "top_level_node_kinds": ["ImportDeclaration", "FirstStatement", "VariableStatement"],
        }
        summary = o._generate_archetype_summary(entry, None, "typescript")
        assert "typical shape: imports, declarations." in summary
        assert "ImportDeclaration" not in summary

    def test_dsl_call_kinds_named(self):
        assert o._humanize_kind("DslCall:before_action") == "before_action calls"

    def test_ruby_prism_kinds_humanized(self):
        # qa26 regression-squad BUG 2: CallNode leaked raw into every Rails
        # config/spec archetype summary.
        assert o._humanize_kind("CallNode") == "method calls"
        assert o._humanize_kind("ConstantWriteNode") == "constant assignments"
