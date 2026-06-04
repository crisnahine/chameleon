"""Unit tests for the archetype-independent style baseline in lint_engine.

The style baseline reads only the declared formatter-config values bootstrap
lifts into rules.json (prettier / rubocop / .editorconfig) and checks edited
content against them. It fires regardless of archetype, so a sparse repo with no
resolvable archetype still gets indent / quote / line-length feedback. It is
advisory only and never block-eligible.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.lint_engine import Violation, scan_style_rules


def _rules(violations: list[Violation]) -> list[str]:
    return [v.rule for v in violations]


def _actuals(violations: list[Violation]) -> list[str]:
    return [v.actual for v in violations]


def _prettier(**kwargs) -> dict:
    return {"rules": {"formatting": {"source": ".prettierrc", "rules": dict(kwargs)}}}


def _rubocop(cops: dict) -> dict:
    return {"rules": {"rubocop": {"source": ".rubocop.yml", "rules": cops}}}


def _editorconfig(**kwargs) -> dict:
    return {"rules": {"editorconfig": {"source": ".editorconfig", "rules": {"*": dict(kwargs)}}}}


# --- silence when nothing is declared --------------------------------------


def test_no_config_declared_is_silent():
    out = scan_style_rules('const a = "x";\n', language="typescript", rules={"rules": {}})
    assert out == []


def test_empty_content_is_silent():
    assert scan_style_rules("", language="typescript", rules=_prettier(singleQuote=True)) == []


def test_unsupported_language_is_silent():
    out = scan_style_rules("x = 1\n", language=None, rules=_prettier(singleQuote=True))
    assert out == []
    out = scan_style_rules("x = 1\n", language="python", rules=_prettier(singleQuote=True))
    assert out == []


def test_malformed_rules_does_not_raise():
    for bad in (None, [], "nope", {"rules": "x"}, {"rules": {"formatting": 1}}):
        assert scan_style_rules('const a = "x";\n', language="typescript", rules=bad) == []


# --- quote style -----------------------------------------------------------


def test_prettier_single_quote_flags_double():
    out = scan_style_rules(
        'const a = "x";\n', language="typescript", rules=_prettier(singleQuote=True)
    )
    assert _rules(out) == ["style-rule-violation"]
    assert "double-quoted" in out[0].actual
    assert out[0].severity == "warning"


def test_prettier_single_quote_passes_single():
    out = scan_style_rules(
        "const a = 'x';\n", language="typescript", rules=_prettier(singleQuote=True)
    )
    assert out == []


def test_prettier_double_quote_flags_single():
    out = scan_style_rules(
        "const a = 'x';\n", language="typescript", rules=_prettier(singleQuote=False)
    )
    assert "single-quoted" in out[0].actual


def test_rubocop_double_quotes_flags_single():
    rules = _rubocop({"Style/StringLiterals": {"EnforcedStyle": "double_quotes"}})
    out = scan_style_rules("x = 'single'\n", language="ruby", rules=rules)
    assert "single-quoted" in out[0].actual


def test_rubocop_single_quotes_flags_double():
    rules = _rubocop({"Style/StringLiterals": {"EnforcedStyle": "single_quotes"}})
    out = scan_style_rules('x = "double"\n', language="ruby", rules=rules)
    assert "double-quoted" in out[0].actual


def test_quote_literal_containing_preferred_char_is_exempt():
    # Switching "it's" to single quotes would force an escape; both prettier and
    # rubocop allow that exception, so it must not flag.
    out = scan_style_rules(
        'const a = "it\'s fine";\n', language="typescript", rules=_prettier(singleQuote=True)
    )
    assert out == []


def test_quote_inside_line_comment_not_flagged():
    src = "// a \"double quoted\" word\nconst a = 'ok';\n"
    out = scan_style_rules(src, language="typescript", rules=_prettier(singleQuote=True))
    assert out == []


def test_quote_inside_block_comment_not_flagged():
    src = "/* a \"double\" thing */\nconst a = 'ok';\n"
    out = scan_style_rules(src, language="typescript", rules=_prettier(singleQuote=True))
    assert out == []


def test_quote_inside_ruby_comment_not_flagged():
    rules = _rubocop({"Style/StringLiterals": {"EnforcedStyle": "double_quotes"}})
    out = scan_style_rules("# a 'single' word\nx = \"ok\"\n", language="ruby", rules=rules)
    assert out == []


def test_template_literal_not_flagged_for_quotes():
    # A backtick template literal is neither single nor double; never a quote
    # violation regardless of the declared preference.
    out = scan_style_rules(
        "const a = `x`;\n", language="typescript", rules=_prettier(singleQuote=True)
    )
    assert out == []


# --- indentation -----------------------------------------------------------


def test_prettier_spaces_flags_tab_indent():
    rules = _prettier(useTabs=False, tabWidth=2)
    out = scan_style_rules("function f() {\n\treturn 1;\n}\n", language="typescript", rules=rules)
    assert any("tab indentation" in a for a in _actuals(out))


def test_prettier_tabs_flags_space_indent():
    rules = _prettier(useTabs=True)
    out = scan_style_rules("function f() {\n  return 1;\n}\n", language="typescript", rules=rules)
    assert any("space indentation" in a for a in _actuals(out))


def test_prettier_spaces_passes_space_indent():
    rules = _prettier(useTabs=False, tabWidth=2)
    out = scan_style_rules("function f() {\n  return 1;\n}\n", language="typescript", rules=rules)
    assert out == []


def test_rubocop_indentation_style_tabs_flags_spaces():
    rules = _rubocop({"Layout/IndentationStyle": {"EnforcedStyle": "tabs"}})
    out = scan_style_rules("def f\n  1\nend\n", language="ruby", rules=rules)
    assert any("space indentation" in a for a in _actuals(out))


def test_rubocop_indentation_width_implies_spaces():
    rules = _rubocop({"Layout/IndentationWidth": {"Width": 2}})
    out = scan_style_rules("def f\n\t1\nend\n", language="ruby", rules=rules)
    assert any("tab indentation" in a for a in _actuals(out))


def test_editorconfig_indent_style_tab_flags_spaces():
    rules = _editorconfig(indent_style="tab")
    out = scan_style_rules("function f() {\n  return 1;\n}\n", language="typescript", rules=rules)
    assert any("space indentation" in a for a in _actuals(out))


def test_indent_inside_multiline_string_not_read_as_code():
    # A tab on a continuation line inside a template literal is string content,
    # not code indentation; the stripper blanks the literal so it does not flag.
    rules = _prettier(useTabs=False, tabWidth=2)
    src = "const a = `line1\n\tstill string`;\n"
    out = scan_style_rules(src, language="typescript", rules=rules)
    assert out == []


# --- max line length -------------------------------------------------------


def test_prettier_print_width_flags_long_line():
    rules = _prettier(printWidth=20)
    out = scan_style_rules("const a = 123456789012345;\n", language="typescript", rules=rules)
    assert any("cols (max 20)" in a for a in _actuals(out))


def test_prettier_print_width_passes_short_line():
    rules = _prettier(printWidth=80)
    out = scan_style_rules("const a = 1;\n", language="typescript", rules=rules)
    assert out == []


def test_rubocop_line_length_max_flags_long_line():
    rules = _rubocop({"Layout/LineLength": {"Max": 10}})
    out = scan_style_rules("x = 1234567890123\n", language="ruby", rules=rules)
    assert any("max 10" in a for a in _actuals(out))


def test_rubocop_line_length_without_max_is_silent():
    # LineLength present but no explicit Max: we never assume rubocop's default,
    # so no line-length finding fires.
    rules = _rubocop({"Layout/LineLength": {"AutoCorrect": True}})
    out = scan_style_rules("x = " + "1" * 500 + "\n", language="ruby", rules=rules)
    assert out == []


def test_editorconfig_max_line_length_off_is_silent():
    rules = _editorconfig(max_line_length="off")
    out = scan_style_rules("x = " + "1" * 500 + "\n", language="ruby", rules=rules)
    assert out == []


def test_editorconfig_max_line_length_numeric_flags():
    rules = _editorconfig(max_line_length="10")
    out = scan_style_rules("x = 1234567890123\n", language="ruby", rules=rules)
    assert any("max 10" in a for a in _actuals(out))


# --- cap -------------------------------------------------------------------


def test_emissions_capped_with_summary_row(monkeypatch):
    monkeypatch.setenv("CHAMELEON_STYLE_RULE_VIOLATIONS_PER_FILE", "5")
    src = "\n".join(f'const x = "v{i}";' for i in range(20)) + "\n"
    out = scan_style_rules(src, language="typescript", rules=_prettier(singleQuote=True))
    # 5 detailed rows + 1 summary row.
    assert len(out) == 6
    assert "+15 more" in out[-1].actual
    assert out[-1].severity == "warning"


# --- never block-eligible --------------------------------------------------


def test_style_rule_never_block_eligible():
    from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES, is_hard_class

    assert "style-rule-violation" not in BLOCK_ELIGIBLE_RULES
    for sev in ("info", "warning", "error"):
        v = {"rule": "style-rule-violation", "severity": sev, "actual": "", "message": "m"}
        assert is_hard_class(v) is False


def test_style_rule_not_archetype_independent_block_class():
    # It fires archetype-independently, but it is not in the independent BLOCK set
    # used by the Stop backstop (which only re-lints blockable independent rules),
    # so it can never reach the turn-stop path.
    from chameleon_mcp.violation_class import is_archetype_independent

    assert is_archetype_independent("style-rule-violation") is False


# --- wiring into the no-archetype scan helper ------------------------------


class TestArchetypeIndependentWiring:
    def test_style_runs_when_rules_passed(self):
        from chameleon_mcp import hook_helper

        rules = _prettier(singleQuote=True)
        out = hook_helper._scan_archetype_independent('const a = "x";\n', "src/x.ts", rules)
        assert any(v.get("rule") == "style-rule-violation" for v in out)

    def test_style_skipped_without_rules(self):
        from chameleon_mcp import hook_helper

        out = hook_helper._scan_archetype_independent('const a = "x";\n', "src/x.ts")
        assert not any(v.get("rule") == "style-rule-violation" for v in out)

    def test_style_failure_is_contained(self, monkeypatch):
        import chameleon_mcp.lint_engine as le
        from chameleon_mcp import hook_helper

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(le, "scan_style_rules", boom)
        # A raising style scan must not abort the helper; a clean file still
        # yields no crash.
        out = hook_helper._scan_archetype_independent(
            "export const x = 1;\n", "src/x.ts", _prettier(singleQuote=True)
        )
        assert isinstance(out, list)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
