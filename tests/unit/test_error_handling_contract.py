"""Unit tests for the async/error-handling contract.

Covers the bootstrap-derived error-handling convention (TS try/catch fraction,
Ruby controller-base rescue_from fraction + dominant error shape), its
SessionStart rendering, and the narrow single-line `.then`-without-`.catch`
advisory lint.
"""

from __future__ import annotations

from chameleon_mcp.conventions import (
    extract_all_conventions,
    extract_error_handling_conventions,
    format_conventions_for_session,
)
from chameleon_mcp.extractors._base import ParsedFile
from chameleon_mcp.lint_engine import lint_conventions
from chameleon_mcp.principles import generate_principles


def _make_file(tmp_path, name: str, content: str) -> ParsedFile:
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


class TestErrorHandlingExtractorRuby:
    def test_detects_dominant_rescue_from_and_shape(self, tmp_path):
        files = []
        for i in range(12):
            files.append(
                _make_file(
                    tmp_path,
                    f"c{i}.rb",
                    "class C{i}Controller < ApplicationController\n"
                    "  rescue_from StandardError do |e|\n"
                    "    render json: { errors: [e.message] }\n"
                    "  end\nend\n".replace("{i}", str(i)),
                )
            )
        result = extract_error_handling_conventions(files, language="ruby")
        assert result["rescues"] >= 0.9
        assert result["sample_size"] == 12
        assert result["error_shape"] == "render json: { error"

    def test_does_not_count_per_action_inline_rescue(self, tmp_path):
        # Idiomatic Rails centralizes in rescue_from; per-action inline rescue is
        # not the signal and must not derive a contract.
        files = []
        for i in range(12):
            files.append(
                _make_file(
                    tmp_path,
                    f"c{i}.rb",
                    "class C{i}Controller < ApplicationController\n"
                    "  def show\n    do_work\n  rescue => e\n    head 500\n  end\nend\n".replace(
                        "{i}", str(i)
                    ),
                )
            )
        result = extract_error_handling_conventions(files, language="ruby")
        assert result == {}

    def test_below_frequency_floor_returns_empty(self, tmp_path):
        files = []
        for i in range(12):
            body = "  rescue_from StandardError\n" if i < 5 else ""
            files.append(
                _make_file(
                    tmp_path,
                    f"c{i}.rb",
                    f"class C{i}Controller < ApplicationController\n{body}end\n",
                )
            )
        result = extract_error_handling_conventions(files, language="ruby")
        assert result == {}

    def test_below_sample_size_returns_empty(self, tmp_path):
        files = [
            _make_file(tmp_path, "c.rb", "class CController\n  rescue_from StandardError\nend\n")
        ]
        assert extract_error_handling_conventions(files, language="ruby") == {}

    def test_rescue_without_shape_still_records_rate(self, tmp_path):
        files = []
        for i in range(12):
            files.append(
                _make_file(
                    tmp_path,
                    f"c{i}.rb",
                    f"class C{i}Controller < ApplicationController\n"
                    f"  rescue_from StandardError, with: :handle\nend\n",
                )
            )
        result = extract_error_handling_conventions(files, language="ruby")
        assert result["rescues"] >= 0.9
        assert "error_shape" not in result

    def test_bare_standard_error_new_is_not_the_project_shape(self, tmp_path):
        # `raise StandardError.new(...)` is raising a stdlib exception, not handing
        # the error to a custom render target. It must not be counted as the
        # archetype's project error shape.
        files = []
        for i in range(12):
            files.append(
                _make_file(
                    tmp_path,
                    f"c{i}.rb",
                    f"class C{i}Controller < ApplicationController\n"
                    f"  rescue_from StandardError do |e|\n"
                    f"    raise StandardError.new(e.message)\n"
                    f"  end\nend\n",
                )
            )
        result = extract_error_handling_conventions(files, language="ruby")
        assert result["rescues"] >= 0.9
        assert "error_shape" not in result

    def test_custom_error_serializer_still_counts(self, tmp_path):
        # A real custom *Error/*Serializer render target is still recognized as the
        # project shape, even when a built-in raise precedes it in the file.
        files = []
        for i in range(12):
            files.append(
                _make_file(
                    tmp_path,
                    f"c{i}.rb",
                    f"class C{i}Controller < ApplicationController\n"
                    f"  rescue_from StandardError do |e|\n"
                    f"    raise StandardError.new('x') unless e\n"
                    f"    render json: ApiErrorSerializer.new(e)\n"
                    f"  end\nend\n",
                )
            )
        result = extract_error_handling_conventions(files, language="ruby")
        assert result["rescues"] >= 0.9
        assert result["error_shape"] == "ErrorSerializer"


class TestErrorHandlingExtractorTypeScript:
    def test_detects_try_catch_fraction(self, tmp_path):
        files = []
        for i in range(12):
            files.append(
                _make_file(
                    tmp_path,
                    f"s{i}.ts",
                    f"export async function load{i}() {{\n"
                    f"  try {{\n    await fetchIt();\n  }} catch (e) {{\n    log(e);\n  }}\n}}\n",
                )
            )
        result = extract_error_handling_conventions(files, language="typescript")
        assert result["try_catch"] >= 0.9
        assert "rescues" not in result

    def test_method_call_then_is_not_a_try(self, tmp_path):
        # `.retry()` / a `try` substring inside an identifier must not count.
        files = []
        for i in range(12):
            files.append(
                _make_file(
                    tmp_path,
                    f"s{i}.ts",
                    f"export const run{i} = () => registry.lookup();\n",
                )
            )
        result = extract_error_handling_conventions(files, language="typescript")
        assert result == {}


class TestErrorHandlingWiring:
    def test_extract_all_records_ruby_error_handling(self, tmp_path):
        files = [
            _make_file(
                tmp_path,
                f"c{i}.rb",
                f"class C{i}Controller < ApplicationController\n"
                f"  rescue_from StandardError do |e|\n"
                f"    render json: {{ errors: [e] }}\n  end\nend\n",
            )
            for i in range(12)
        ]
        conv = extract_all_conventions(
            files_by_archetype={"controllers": files},
            declarations_by_archetype={},
            generation=1,
            language="ruby",
        )
        eh = conv["conventions"]["error_handling"]["controllers"]
        assert eh["rescues"] >= 0.9
        assert eh["error_shape"] == "render json: { error"

    def test_extract_all_ts_uses_try_catch_not_rescue(self, tmp_path):
        files = [
            _make_file(
                tmp_path,
                f"s{i}.ts",
                f"export function f{i}() {{\n  try {{ go(); }} catch (e) {{ handle(e); }}\n}}\n",
            )
            for i in range(12)
        ]
        conv = extract_all_conventions(
            files_by_archetype={"services": files},
            declarations_by_archetype={},
            generation=1,
            language="typescript",
        )
        eh = conv["conventions"]["error_handling"]["services"]
        assert "try_catch" in eh
        assert "rescues" not in eh


class TestErrorHandlingSessionRender:
    def test_ruby_line_names_shape(self):
        conv = {
            "conventions": {
                "error_handling": {
                    "controllers": {
                        "rescues": 0.88,
                        "sample_size": 16,
                        "error_shape": "render_error",
                    }
                }
            }
        }
        out = format_conventions_for_session(conv)
        assert "ERROR HANDLING (advisory):" in out
        assert "88%" in out
        assert "render_error" in out

    def test_ts_line_renders(self):
        conv = {
            "conventions": {"error_handling": {"services": {"try_catch": 0.75, "sample_size": 20}}}
        }
        out = format_conventions_for_session(conv)
        assert "ERROR HANDLING (advisory):" in out
        assert "try/catch" in out

    def test_malformed_entry_skipped(self):
        conv = {"conventions": {"error_handling": {"a": "nope", "b": {"sample_size": 9}}}}
        out = format_conventions_for_session(conv)
        # No usable frequency on either entry -> no error-handling section.
        assert "ERROR HANDLING" not in out


class TestThenWithoutCatchLint:
    _CONV = {"imports": {}, "naming": {}}

    def test_flags_single_line_then_no_catch(self):
        content = "doThing().then((r) => use(r));\n"
        viols = lint_conventions(content, self._CONV, language="typescript")
        rules = [v.rule for v in viols]
        assert "then-without-catch" in rules
        v = next(v for v in viols if v.rule == "then-without-catch")
        assert v.severity == "info"

    def test_clean_when_catch_on_same_line(self):
        content = "doThing().then(use).catch(log);\n"
        viols = lint_conventions(content, self._CONV, language="typescript")
        assert "then-without-catch" not in [v.rule for v in viols]

    def test_honors_chameleon_ignore(self):
        content = "doThing().then(use); // chameleon-ignore then-without-catch\n"
        viols = lint_conventions(content, self._CONV, language="typescript")
        assert "then-without-catch" not in [v.rule for v in viols]

    def test_not_run_for_ruby(self):
        content = "foo.then { |r| use(r) }\n"
        viols = lint_conventions(content, self._CONV, language="ruby")
        assert "then-without-catch" not in [v.rule for v in viols]

    def test_then_inside_string_literal_ignored(self):
        # The scan runs on strings/comments-stripped content, so a `.then(` in a
        # quoted snippet is not a real call.
        content = 'const doc = "promise.then(x)";\n'
        viols = lint_conventions(content, self._CONV, language="typescript")
        assert "then-without-catch" not in [v.rule for v in viols]


class TestErrorHandlingPrinciple:
    def test_ruby_principle_names_shape(self):
        conv = {
            "conventions": {
                "error_handling": {"c": {"rescues": 0.88, "error_shape": "render_error"}}
            }
        }
        out = generate_principles(language="ruby", conventions=conv, archetypes={})
        assert "render the project error shape (render_error)" in out

    def test_ts_principle_renders_try_catch(self):
        conv = {"conventions": {"error_handling": {"s": {"try_catch": 0.75}}}}
        out = generate_principles(language="typescript", conventions=conv, archetypes={})
        assert "try/catch" in out

    def test_no_error_handling_no_principle(self):
        out = generate_principles(conventions={"conventions": {}}, archetypes={})
        assert "error shape" not in out
        assert "try/catch" not in out

    def test_malformed_entry_emits_no_principle(self):
        conv = {"conventions": {"error_handling": {"a": "nope", "b": {"sample_size": 9}}}}
        out = generate_principles(conventions=conv, archetypes={})
        assert "error shape" not in out
        assert "try/catch" not in out
