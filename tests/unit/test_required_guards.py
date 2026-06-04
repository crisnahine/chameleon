"""Unit tests for the required-guard authorization convention.

Covers the symbol-capturing extractor in conventions.py, its wiring into
extract_all_conventions, the SessionStart rendering, and the advisory lint rule
in lint_engine. The rule is advisory only -- these tests assert it never reaches
block severity and respects inheritance, skip_before_action, and scoping.
"""

from __future__ import annotations

from chameleon_mcp.conventions import (
    empty_conventions,
    extract_all_conventions,
    extract_required_guards_conventions,
    format_conventions_for_session,
)
from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.lint_engine import lint_conventions
from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES


def _make_ruby_file(tmp_path, name: str, content: str) -> ParsedFile:
    fp = tmp_path / name
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return ParsedFile(
        path=fp,
        content_first_200_bytes=content[:200],
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


def _controller(i: int, body: str) -> str:
    return f"class Foo{i}Controller < ApplicationController\n{body}end\n"


class TestExtractRequiredGuards:
    def test_derives_dominant_guard_symbol(self, tmp_path):
        files = [
            _make_ruby_file(tmp_path, f"c{i}.rb", _controller(i, "  before_action :authorize!\n"))
            for i in range(12)
        ]
        result = extract_required_guards_conventions(files)
        assert result["required_guards"] == ["authorize!"]
        assert "authorize!" in result["known_guards"]

    def test_below_threshold_not_required(self, tmp_path):
        # authorize! only in 5/12 -> below the 60% floor, so not required.
        files = []
        for i in range(12):
            body = "  before_action :authorize!\n" if i < 5 else "  before_action :set_thing\n"
            files.append(_make_ruby_file(tmp_path, f"c{i}.rb", _controller(i, body)))
        result = extract_required_guards_conventions(files)
        assert result.get("required_guards") in (None, [])

    def test_scoped_guard_does_not_count_as_blanket(self, tmp_path):
        files = [
            _make_ruby_file(
                tmp_path,
                f"c{i}.rb",
                _controller(i, "  before_action :authorize!, only: %i[show update]\n"),
            )
            for i in range(12)
        ]
        result = extract_required_guards_conventions(files)
        # A scoped guard is not a blanket requirement.
        assert result == {}

    def test_skip_before_action_neutralizes_witness(self, tmp_path):
        # 8 controllers install authorize! blanket, 4 skip it. The skip files are
        # neutral evidence (they neither install nor prove the requirement), so
        # the rate is 8/12 = 0.667 against the full sample -> required.
        files = []
        for i in range(8):
            files.append(
                _make_ruby_file(
                    tmp_path, f"c{i}.rb", _controller(i, "  before_action :authorize!\n")
                )
            )
        for i in range(8, 12):
            files.append(
                _make_ruby_file(
                    tmp_path, f"c{i}.rb", _controller(i, "  skip_before_action :authorize!\n")
                )
            )
        result = extract_required_guards_conventions(files)
        assert result["required_guards"] == ["authorize!"]

    def test_known_guards_includes_low_frequency_variant(self, tmp_path):
        files = []
        for i in range(12):
            body = "  before_action :authorize!\n"
            if i < 3:
                body += "  before_action :require_admin\n"
            files.append(_make_ruby_file(tmp_path, f"c{i}.rb", _controller(i, body)))
        result = extract_required_guards_conventions(files)
        assert "authorize!" in result["required_guards"]
        assert "require_admin" in result["known_guards"]
        assert "require_admin" not in result["required_guards"]

    def test_below_sample_size(self, tmp_path):
        files = [_make_ruby_file(tmp_path, "c.rb", _controller(0, "  before_action :authorize!\n"))]
        assert extract_required_guards_conventions(files) == {}


class TestExtractAllWiresGuards:
    def test_required_guards_section_populated_and_carries_bases(self, tmp_path):
        files = [
            _make_ruby_file(tmp_path, f"c{i}.rb", _controller(i, "  before_action :authorize!\n"))
            for i in range(12)
        ]
        out = extract_all_conventions(
            files_by_archetype={"controller": files},
            declarations_by_archetype={},
            generation=1,
            language="ruby",
        )
        guards = out["conventions"]["required_guards"]["controller"]
        assert guards["required_guards"] == ["authorize!"]
        # Inheritance is dominant here, so the base controller is carried so the
        # lint check can suppress on inheritance.
        assert "ApplicationController" in guards.get("known_bases", [])

    def test_typescript_gets_no_guard_section(self, tmp_path):
        files = [
            _make_ruby_file(tmp_path, f"c{i}.rb", _controller(i, "  before_action :authorize!\n"))
            for i in range(12)
        ]
        out = extract_all_conventions(
            files_by_archetype={"controller": files},
            declarations_by_archetype={},
            generation=1,
            language="typescript",
        )
        assert out["conventions"]["required_guards"] == {}


class TestSessionRendering:
    def test_guard_line_in_session_block(self):
        conv = empty_conventions(generation=1)
        conv["conventions"]["required_guards"]["controller"] = {
            "required_guards": ["authorize!"],
            "known_guards": ["authorize!"],
            "sample_size": 20,
        }
        text = format_conventions_for_session(conv)
        assert "AUTHZ (advisory):" in text
        assert "before_action :authorize!" in text


class TestRequiredGuardLint:
    _CONV = {"required_guards": {"required_guards": ["authorize!"], "known_guards": ["authorize!"]}}

    def test_flags_missing_guard_advisory_only(self):
        code = "class NewController < BaseController\n  before_action :set_thing\nend\n"
        viols = lint_conventions(code, self._CONV, language="ruby")
        guard = [v for v in viols if v.rule == "required-guard-convention"]
        assert len(guard) == 1
        assert guard[0].severity == "info"
        assert "authorize!" in guard[0].message
        # Advisory means it is never block-eligible.
        assert "required-guard-convention" not in BLOCK_ELIGIBLE_RULES

    def test_present_guard_not_flagged(self):
        code = "class NewController < BaseController\n  before_action :authorize!\nend\n"
        viols = lint_conventions(code, self._CONV, language="ruby")
        assert not [v for v in viols if v.rule == "required-guard-convention"]

    def test_scoped_guard_does_not_satisfy(self):
        code = (
            "class NewController < BaseController\n"
            "  before_action :authorize!, only: %i[update]\n"
            "end\n"
        )
        viols = lint_conventions(code, self._CONV, language="ruby")
        assert [v for v in viols if v.rule == "required-guard-convention"]

    def test_skip_before_action_suppresses(self):
        code = "class NewController < BaseController\n  skip_before_action :authorize!\nend\n"
        viols = lint_conventions(code, self._CONV, language="ruby")
        assert not [v for v in viols if v.rule == "required-guard-convention"]

    def test_known_base_suppresses(self):
        conv = {
            "required_guards": {
                "required_guards": ["authorize!"],
                "known_bases": ["AuthenticatedController"],
            }
        }
        code = "class NewController < AuthenticatedController\nend\n"
        viols = lint_conventions(code, conv, language="ruby")
        assert not [v for v in viols if v.rule == "required-guard-convention"]

    def test_inline_ignore_directive_clears(self):
        code = (
            "# chameleon-ignore required-guard-convention\n"
            "class NewController < BaseController\nend\n"
        )
        viols = lint_conventions(code, self._CONV, language="ruby")
        assert not [v for v in viols if v.rule == "required-guard-convention"]

    def test_no_guard_data_no_violation(self):
        code = "class NewController < BaseController\nend\n"
        viols = lint_conventions(code, {"required_guards": {}}, language="ruby")
        assert not [v for v in viols if v.rule == "required-guard-convention"]

    def test_typescript_skips_guard_check(self):
        code = "export const handler = () => {};\n"
        viols = lint_conventions(code, self._CONV, language="typescript")
        assert not [v for v in viols if v.rule == "required-guard-convention"]

    def test_guard_in_comment_does_not_satisfy(self):
        # The scan runs on strings/comments-stripped content, so a guard mentioned
        # only in a comment must not count as present.
        code = (
            "class NewController < BaseController\n"
            "  # before_action :authorize!\n"
            "  before_action :set_thing\n"
            "end\n"
        )
        viols = lint_conventions(code, self._CONV, language="ruby")
        assert [v for v in viols if v.rule == "required-guard-convention"]
