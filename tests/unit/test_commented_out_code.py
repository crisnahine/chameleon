"""Unit tests for commented-out-code detection (bootstrap / pr-review only)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chameleon_mcp.bootstrap.comment_scan import (
    _span_is_code,
    detect_commented_out_code,
    detect_commented_out_code_by_group,
)
from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.lint_engine import extract_comment_spans


def _fake_pf(kinds: tuple[str, ...], *, diagnostics: int = 0, imports=()) -> ParsedFile:
    return ParsedFile(
        path=Path("span_0.ts"),
        content_first_200_bytes="",
        top_level_node_kinds=kinds,
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=tuple(imports),
        has_jsx=False,
        parse_diagnostics_count=diagnostics,
    )


class TestExtractCommentSpans:
    def test_unsupported_language(self):
        assert extract_comment_spans("# x", language="go") == []

    def test_stitches_consecutive_line_comments(self):
        spans = extract_comment_spans("// a\n// b\ncode()\n", language="typescript")
        assert " a\n b" in spans

    def test_block_comment_fence_stripped(self):
        spans = extract_comment_spans("/* const x = 1 */", language="typescript")
        assert any("const x = 1" in s for s in spans)

    def test_jsdoc_star_decoration_stripped(self):
        spans = extract_comment_spans("/**\n * const y = 2\n */", language="typescript")
        assert any("const y = 2" in s for s in spans)

    def test_marker_only_spans_dropped(self):
        # A divider ruler has no word characters and must not become a candidate.
        assert extract_comment_spans("// -------\n// =======", language="typescript") == []

    def test_ruby_line_comments(self):
        spans = extract_comment_spans("# def old\n#   1\n# end\nreal", language="ruby")
        assert any("def old" in s for s in spans)


class TestSpanIsCode:
    def test_parse_errors_reject(self):
        assert _span_is_code(_fake_pf(("ImportDeclaration",), diagnostics=2), "typescript") is False

    def test_ts_declaration_accepted(self):
        assert _span_is_code(_fake_pf(("FirstStatement",)), "typescript") is True
        assert _span_is_code(_fake_pf(("ImportDeclaration",)), "typescript") is True

    def test_ts_expression_rejected(self):
        assert _span_is_code(_fake_pf(("ExpressionStatement",)), "typescript") is False

    def test_ruby_def_accepted(self):
        assert _span_is_code(_fake_pf(("DefNode",)), "ruby") is True

    def test_ruby_bare_call_rejected(self):
        # Prose parses to CallNode with no import_specifiers — must be rejected.
        assert _span_is_code(_fake_pf(("CallNode",)), "ruby") is False

    def test_ruby_require_call_accepted(self):
        pf = _fake_pf(("CallNode",), imports=[("legacy", "default")])
        assert _span_is_code(pf, "ruby") is True


_NODE = shutil.which("node")
_RUBY = shutil.which("ruby")


@pytest.mark.skipif(not _NODE, reason="node not on PATH")
class TestDetectTypeScript:
    def _extractor(self):
        from chameleon_mcp.extractors.typescript import TypeScriptExtractor

        return TypeScriptExtractor()

    def test_unsupported_language(self):
        assert detect_commented_out_code(["// x"], language="go", extractor=None) == 0

    def test_flags_commented_out_import(self):
        content = "// import { Foo } from './foo';\nfunction live() {}\n"
        assert (
            detect_commented_out_code([content], language="typescript", extractor=self._extractor())
            == 1
        )

    def test_does_not_flag_prose(self):
        content = (
            "// returns the user's display name\n"
            "// keep stable, other modules depend on it\n"
            "export function name() {}\n"
        )
        assert (
            detect_commented_out_code([content], language="typescript", extractor=self._extractor())
            == 0
        )

    def test_by_group_attribution(self):
        groups = {
            "with_dead": ["// const totalCount = 0;\nlive()\n"],
            "clean": ["// a normal explanatory note about the code\nlive()\n"],
        }
        counts = detect_commented_out_code_by_group(
            groups, language="typescript", extractor=self._extractor()
        )
        assert counts.get("with_dead") == 1
        assert "clean" not in counts


@pytest.mark.skipif(not _RUBY, reason="ruby not on PATH")
class TestDetectRuby:
    def _extractor(self):
        from chameleon_mcp.extractors.ruby import RubyExtractor

        return RubyExtractor()

    def test_flags_commented_out_def(self):
        content = "class Foo\n  # def old\n  #   work\n  # end\n  def cur; end\nend\n"
        assert (
            detect_commented_out_code([content], language="ruby", extractor=self._extractor()) == 1
        )

    def test_does_not_flag_prose(self):
        content = "class Foo\n  # handles the nil case by returning early\n  def handle; end\nend\n"
        assert (
            detect_commented_out_code([content], language="ruby", extractor=self._extractor()) == 0
        )
