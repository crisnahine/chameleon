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


def test_jsx_attribute_double_quotes_not_flagged_under_single_quote():
    # prettier's jsxSingleQuote defaults False, so JSX attribute values stay
    # double-quoted even under singleQuote:true -- flagging them steers the model
    # to break prettier-conforming code.
    jsx = 'function C() {\n  return <input type="email" className="box" />;\n}\n'
    out = scan_style_rules(
        jsx, language="typescript", rules=_prettier(singleQuote=True), file_path="src/C.tsx"
    )
    assert all("quoted string" not in v.actual for v in out)


def test_jsx_attribute_flagged_when_jsx_single_quote_true():
    jsx = 'function C() {\n  return <input type="email" />;\n}\n'
    out = scan_style_rules(
        jsx,
        language="typescript",
        rules=_prettier(singleQuote=True, jsxSingleQuote=True),
        file_path="src/C.tsx",
    )
    assert any("double-quoted" in v.actual for v in out)


def test_js_assignment_double_still_flagged_alongside_jsx():
    # The JSX skip must NOT suppress an ordinary JS double-quoted assignment
    # (prettier reformats it to single); only the no-space `name="v"` attribute
    # shape is exempt, not `key: "v"` or `x = "v"`.
    out = scan_style_rules(
        'const g = "hi";\nconst o = { key: "v" };\n',
        language="typescript",
        rules=_prettier(singleQuote=True),
        file_path="src/x.tsx",
    )
    assert sum("double-quoted" in v.actual for v in out) == 2


def test_compact_assignment_in_ts_still_flags():
    # The JSX skip is gated on ACTUAL JSX presence in the content, not the
    # extension: a compact (no-space) double-quoted assignment in a file with no
    # JSX (`const x="y"`, which prettier rewrites to single) is not a JSX attribute
    # and must still flag, even though its `x="y"` shape matches the signature. A
    # TS generic in the same file must not fool the JSX detector.
    out = scan_style_rules(
        'const a: Array<string> = [];\nconst x="y";\n',
        language="typescript",
        rules=_prettier(singleQuote=True),
        file_path="a.ts",
    )
    assert any("double-quoted" in v.actual for v in out)


def test_jsx_attribute_in_js_file_not_flagged():
    # `.js` is a valid JSX host (Babel/React). A JSX attribute in a .js file whose
    # content contains JSX is correctly double-quoted and must not flag; a compact
    # assignment in a plain .js file (no JSX) still flags.
    jsx_js = 'function C() {\n  return <input className="x" type="email" />;\n}\n'
    out = scan_style_rules(
        jsx_js, language="typescript", rules=_prettier(singleQuote=True), file_path="App.js"
    )
    assert all("quoted string" not in v.actual for v in out)

    plain_js = scan_style_rules(
        'const x="y";\n', language="typescript", rules=_prettier(singleQuote=True), file_path="u.js"
    )
    assert any("double-quoted" in v.actual for v in plain_js)


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


def test_ruby_interpolated_and_escaped_double_quotes_are_exempt():
    # Interpolation and escape sequences REQUIRE double quotes in Ruby;
    # rubocop's single_quotes style accepts both, so flagging them steers
    # the model to break working code (observed as FP noise on a real run).
    rules = _rubocop({"Style/StringLiterals": {"EnforcedStyle": "single_quotes"}})
    src = 'a = "value #{x}"' + chr(10) + 'b = "line' + "\\n" + 'break"' + chr(10)
    out = scan_style_rules(src, language="ruby", rules=rules)
    assert out == []
    # A plain double-quoted literal still flags under the same rules.
    out2 = scan_style_rules('c = "plain"' + chr(10), language="ruby", rules=rules)
    assert "double-quoted" in out2[0].actual


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


def test_rubocop_allowed_patterns_exempts_comment_line():
    # The repo's rubocop exempts comment lines from LineLength via
    # AllowedPatterns. A long comment line must not be flagged here either, or
    # chameleon contradicts the repo's own clean rubocop run.
    rules = _rubocop({"Layout/LineLength": {"Max": 10, "AllowedPatterns": [r"(\A|\s)#"]}})
    long_comment = "# " + "x" * 50 + "\n"
    out = scan_style_rules(long_comment, language="ruby", rules=rules)
    assert out == []


def test_rubocop_allowed_patterns_still_flags_code_line():
    rules = _rubocop({"Layout/LineLength": {"Max": 10, "AllowedPatterns": [r"(\A|\s)#"]}})
    src = "# " + "x" * 50 + "\n" + "value = " + "1" * 50 + "\n"
    out = scan_style_rules(src, language="ruby", rules=rules)
    # Only the code line (line 2) flags; the comment line is exempt.
    assert any("line 2" in a for a in _actuals(out))
    assert not any("line 1" in a for a in _actuals(out))


def test_rubocop_allowed_uri_exempts_url_line():
    rules = _rubocop({"Layout/LineLength": {"Max": 10, "AllowedURI": True}})
    out = scan_style_rules(
        "see https://example.com/" + "a" * 50 + "\n", language="ruby", rules=rules
    )
    assert out == []


def test_rubocop_bad_allowed_pattern_does_not_raise():
    # An uncompilable pattern is skipped, not raised, on the hot path.
    rules = _rubocop({"Layout/LineLength": {"Max": 10, "AllowedPatterns": ["(unclosed"]}})
    out = scan_style_rules("value = " + "1" * 50 + "\n", language="ruby", rules=rules)
    assert any("max 10" in a for a in _actuals(out))


def test_prettier_print_width_has_no_pattern_exemption():
    # prettier's printWidth applies uniformly; a long comment line still flags.
    rules = _prettier(printWidth=10)
    out = scan_style_rules("// " + "x" * 50 + "\n", language="typescript", rules=rules)
    assert any("max 10" in a for a in _actuals(out))


def test_editorconfig_max_line_length_off_is_silent():
    rules = _editorconfig(max_line_length="off")
    out = scan_style_rules("x = " + "1" * 500 + "\n", language="ruby", rules=rules)
    assert out == []


def test_editorconfig_max_line_length_numeric_flags():
    rules = _editorconfig(max_line_length="10")
    out = scan_style_rules("x = 1234567890123\n", language="ruby", rules=rules)
    assert any("max 10" in a for a in _actuals(out))


# --- rubocop AllCops.Exclude / per-cop Exclude -----------------------------


def _rubocop_with_allcops(cops: dict, exclude: list[str]) -> dict:
    cops = dict(cops)
    cops["AllCops"] = {"Exclude": exclude}
    return _rubocop(cops)


def test_allcops_exclude_skips_excluded_path():
    # The repo's CI rubocop never inspects db/migrate; the style baseline must
    # not flag a long line there either, or it nags a line CI deliberately exempts.
    rules = _rubocop_with_allcops({"Layout/LineLength": {"Max": 10}}, ["db/migrate/*"])
    out = scan_style_rules(
        "x = 1234567890123\n",
        language="ruby",
        rules=rules,
        file_path="/repo/db/migrate/20240101_add.rb",
        repo_root="/repo",
    )
    assert out == []


def test_allcops_exclude_double_star_matches_nested_path():
    rules = _rubocop_with_allcops({"Layout/LineLength": {"Max": 10}}, ["lib/**/*"])
    out = scan_style_rules(
        "x = 1234567890123\n",
        language="ruby",
        rules=rules,
        file_path="/repo/lib/foo/bar.rb",
        repo_root="/repo",
    )
    assert out == []


def test_allcops_exclude_does_not_skip_unexcluded_path():
    # A path NOT under any Exclude glob still flags, so the exclude is a scalpel
    # not a blanket suppression.
    rules = _rubocop_with_allcops({"Layout/LineLength": {"Max": 10}}, ["db/migrate/*"])
    out = scan_style_rules(
        "x = 1234567890123\n",
        language="ruby",
        rules=rules,
        file_path="/repo/app/models/foo.rb",
        repo_root="/repo",
    )
    assert any("max 10" in a for a in _actuals(out))


def test_allcops_exclude_no_file_path_keeps_flagging():
    # Backwards compatibility: a caller that supplies no path gets the old
    # behavior (the exclude check is a no-op without a path to match).
    rules = _rubocop_with_allcops({"Layout/LineLength": {"Max": 10}}, ["db/migrate/*"])
    out = scan_style_rules("x = 1234567890123\n", language="ruby", rules=rules)
    assert any("max 10" in a for a in _actuals(out))


def test_per_cop_exclude_drops_only_line_length():
    # A per-cop Exclude on Layout/LineLength drops the line-length check for that
    # path; the indent check (a different cop) still runs.
    rules = _rubocop(
        {
            "Layout/LineLength": {"Max": 10, "Exclude": ["app/views/**/*"]},
            "Layout/IndentationStyle": {"EnforcedStyle": "spaces"},
        }
    )
    src = "\tx = 1234567890123\n"  # tab indent (wrong) + long line
    out = scan_style_rules(
        src,
        language="ruby",
        rules=rules,
        file_path="/repo/app/views/foo.rb",
        repo_root="/repo",
    )
    actuals = _actuals(out)
    assert not any("max 10" in a for a in actuals)  # line-length excluded for this path
    assert any("tab indentation" in a for a in actuals)  # indent still runs


def test_allcops_exclude_relative_path_without_root_matches():
    # A relative file_path with no repo_root still matches a glob against its own
    # POSIX form, so a bash-recorded relative target is still honored.
    rules = _rubocop_with_allcops({"Layout/LineLength": {"Max": 10}}, ["db/migrate/*"])
    out = scan_style_rules(
        "x = 1234567890123\n",
        language="ruby",
        rules=rules,
        file_path="db/migrate/20240101_add.rb",
    )
    assert out == []


def test_allcops_exclude_does_not_apply_to_typescript():
    # AllCops is a rubocop concept; a TS file with prettier printWidth ignores it
    # and still flags (a db/migrate path is meaningless for TS anyway).
    rules = _prettier(printWidth=10)
    rules["rules"]["rubocop"] = {
        "source": ".rubocop.yml",
        "rules": {"AllCops": {"Exclude": ["**/*"]}},
    }
    out = scan_style_rules(
        "const a = 123456789012345;\n",
        language="typescript",
        rules=rules,
        file_path="/repo/src/x.ts",
        repo_root="/repo",
    )
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
