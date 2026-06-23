"""Unit tests for per-archetype doc-coverage convention derivation."""

from __future__ import annotations

from chameleon_mcp.conventions import (
    compute_doc_coverage_from_content,
    extract_doc_coverage_conventions,
    format_conventions_for_session,
)


class TestComputeDocCoverageTypeScript:
    def test_jsdoc_block_counts_as_documented(self):
        content = "/** does a thing */\nexport function foo() {}\n"
        assert compute_doc_coverage_from_content(content, language="typescript") == (1, 1)

    def test_line_comment_run_counts_as_documented(self):
        content = "// returns the answer\nexport const answer = 42\n"
        assert compute_doc_coverage_from_content(content, language="typescript") == (1, 1)

    def test_undocumented_export_counts_public_only(self):
        content = "export function bare() {}\n"
        assert compute_doc_coverage_from_content(content, language="typescript") == (0, 1)

    def test_decorator_is_transparent(self):
        content = "/** documented */\n@Injectable()\nexport class Svc {}\n"
        assert compute_doc_coverage_from_content(content, language="typescript") == (1, 1)

    def test_non_exported_declarations_ignored(self):
        content = "function helper() {}\nconst x = 1\nexport function pub() {}\n"
        # Only the exported pub counts toward the public surface.
        assert compute_doc_coverage_from_content(content, language="typescript") == (0, 1)

    def test_mixed_documented_and_not(self):
        content = (
            "/** a */\nexport function a() {}\nexport function b() {}\n// c\nexport const c = 1\n"
        )
        assert compute_doc_coverage_from_content(content, language="typescript") == (2, 3)


class TestComputeDocCoverageRuby:
    def test_documented_public_def(self):
        content = "class Foo\n  # does work\n  def work; end\nend\n"
        assert compute_doc_coverage_from_content(content, language="ruby") == (1, 1)

    def test_private_section_defs_excluded(self):
        content = (
            "class Foo\n"
            "  # pub\n"
            "  def pub; end\n"
            "  private\n"
            "  # priv comment\n"
            "  def priv; end\n"
            "end\n"
        )
        # Only the public def is part of the documented public surface.
        assert compute_doc_coverage_from_content(content, language="ruby") == (1, 1)

    def test_visibility_resets_per_class(self):
        content = (
            "class A\n  private\n  def a; end\nend\nclass B\n  # documented\n  def b; end\nend\n"
        )
        # A's def is private (0 public); B opens a new body that resets to public.
        assert compute_doc_coverage_from_content(content, language="ruby") == (1, 1)

    def test_inline_private_def_does_not_open_section(self):
        # `private def x` is one private method; the following bare def stays
        # public (the inline form must NOT flip the section to private).
        content = "class Foo\n  private def secret; end\n  # pub\n  def pub; end\nend\n"
        documented, public = compute_doc_coverage_from_content(content, language="ruby")
        # The inline `private def` is not a bare def, so only the public def is
        # counted — and it is documented.
        assert public == 1
        assert documented == 1


class TestExtractDocCoverageConventions:
    def test_below_decl_floor_returns_empty(self):
        # 4 public, all documented, but under the min-decl floor.
        assert extract_doc_coverage_conventions([(4, 4)]) == {}

    def test_below_fraction_floor_returns_empty(self):
        # 20 public, only 5 documented = 25% < 60%.
        assert extract_doc_coverage_conventions([(5, 20)]) == {}

    def test_dominant_coverage_recorded(self):
        # 20 public, 16 documented = 80%.
        result = extract_doc_coverage_conventions([(8, 10), (8, 10)])
        assert result["fraction"] == 0.8
        assert result["documented"] == 16
        assert result["public"] == 20

    def test_unsupported_language_yields_zero(self):
        assert compute_doc_coverage_from_content("func main() {}", language="go") == (0, 0)

    def test_python_doc_coverage_supported(self):
        # Python is a supported language: one public undocumented function.
        assert compute_doc_coverage_from_content("def f():\n    pass\n", language="python") == (
            0,
            1,
        )


class TestDocCoverageRendering:
    def test_advisory_line(self):
        conv = {
            "conventions": {
                "doc_coverage": {"service": {"fraction": 0.8, "documented": 16, "public": 20}}
            }
        }
        out = format_conventions_for_session(conv)
        assert "DOC COVERAGE (advisory):" in out
        assert "80% of public declarations carry a doc comment" in out

    def test_malformed_entry_skipped(self):
        conv = {"conventions": {"doc_coverage": {"x": {"fraction": "nope"}}}}
        assert format_conventions_for_session(conv) == ""
