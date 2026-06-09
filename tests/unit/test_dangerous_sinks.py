"""Unit tests for the edit-time dangerous-sink scanner in lint_engine."""

from __future__ import annotations

import pytest

from chameleon_mcp.lint_engine import Violation, scan_dangerous_sinks


def _rules(violations: list[Violation]) -> list[str]:
    return [v.rule for v in violations]


# --- eval-call -------------------------------------------------------------


def test_eval_call_typescript_flagged_as_error():
    violations = scan_dangerous_sinks("const r = eval(req.body.code);", language="typescript")
    assert _rules(violations) == ["eval-call"]
    assert violations[0].severity == "error"
    assert "line 1" in violations[0].actual


def test_eval_call_ruby_flagged():
    violations = scan_dangerous_sinks("eval(user_supplied)", language="ruby")
    assert _rules(violations) == ["eval-call"]


def test_eval_inside_string_literal_not_flagged():
    # The literal mentions eval( but does not invoke it.
    violations = scan_dangerous_sinks('const s = "please eval(this)";', language="typescript")
    assert _rules(violations) == []


def test_eval_inside_comment_not_flagged():
    violations = scan_dangerous_sinks("# eval(this) is dangerous", language="ruby")
    assert _rules(violations) == []


def test_member_access_and_suffix_identifiers_not_flagged():
    src = "obj.evaluate(x);\nconst y = retrieval(z);\nmedieval(w);"
    violations = scan_dangerous_sinks(src, language="typescript")
    assert _rules(violations) == []


def test_eval_line_number_is_reported():
    src = "line1\nline2\nconst r = eval(x);\n"
    violations = scan_dangerous_sinks(src, language="typescript")
    assert violations[0].actual == "eval( at line 3"


def test_eval_without_language_still_detected_on_raw_content():
    violations = scan_dangerous_sinks("eval(x)", language=None)
    assert _rules(violations) == ["eval-call"]


# --- Ruby dynamic-eval variants (string-argument forms only) ----------------


def test_instance_eval_with_string_literal_flagged():
    violations = scan_dangerous_sinks('obj.instance_eval("def x; end")', language="ruby")
    assert _rules(violations) == ["eval-call"]
    assert "instance_eval" in violations[0].message
    # Advisory severity: the string-arg *_eval variants surface but never
    # hard-block (class_eval heredocs are an established Rails idiom).
    assert violations[0].severity == "warning"


def test_eval_variant_severity_split_controls_hardness():
    from chameleon_mcp.violation_class import is_hard_class

    direct = scan_dangerous_sinks("eval(params[:code])", language="ruby")[0]
    variant = scan_dangerous_sinks('k.class_eval("def x; end")', language="ruby")[0]
    send_form = scan_dangerous_sinks("obj.send(:eval, code)", language="ruby")[0]
    assert is_hard_class(direct.to_dict()) is True
    assert is_hard_class(variant.to_dict()) is False
    assert is_hard_class(send_form.to_dict()) is True


def test_class_eval_with_interpolated_string_flagged():
    src = 'klass.class_eval("def #{name}; @#{name}; end")\n'
    violations = scan_dangerous_sinks(src, language="ruby")
    assert _rules(violations) == ["eval-call"]


def test_module_eval_with_single_quoted_string_flagged():
    violations = scan_dangerous_sinks("M.module_eval('CONST = 1')", language="ruby")
    assert _rules(violations) == ["eval-call"]


def test_class_eval_heredoc_form_flagged():
    src = "klass.class_eval <<~RUBY, __FILE__, __LINE__ + 1\n  def go; end\nRUBY\n"
    violations = scan_dangerous_sinks(src, language="ruby")
    assert _rules(violations) == ["eval-call"]


def test_instance_eval_block_forms_not_flagged():
    # The block forms are the legitimate DSL pattern, not dynamic execution.
    src = "obj.instance_eval { setup }\nobj.instance_eval do\n  setup\nend\n"
    violations = scan_dangerous_sinks(src, language="ruby")
    assert _rules(violations) == []


def test_instance_eval_variable_and_block_pass_args_not_flagged():
    src = "obj.instance_eval(&block)\nobj.instance_eval(code_var)\n"
    violations = scan_dangerous_sinks(src, language="ruby")
    assert _rules(violations) == []


def test_send_eval_symbol_flagged():
    violations = scan_dangerous_sinks("obj.send(:eval, code)", language="ruby")
    assert _rules(violations) == ["eval-call"]
    assert "send" in violations[0].message


def test_public_send_eval_string_flagged():
    violations = scan_dangerous_sinks('obj.public_send("eval", code)', language="ruby")
    assert _rules(violations) == ["eval-call"]


def test_send_other_symbol_not_flagged():
    violations = scan_dangerous_sinks("obj.send(:evaluate, x)", language="ruby")
    assert _rules(violations) == []


def test_eval_variants_in_comment_or_string_not_flagged():
    src = "# obj.instance_eval(\"x\")\nmsg = 'send(:eval, y)'\n"
    violations = scan_dangerous_sinks(src, language="ruby")
    assert _rules(violations) == []


def test_eval_variant_line_numbers_truthful():
    src = 'a = 1\nb = 2\nobj.instance_eval("bad")\n'
    violations = scan_dangerous_sinks(src, language="ruby")
    assert len(violations) == 1
    assert "line 3" in violations[0].message


# --- weak-hash (advisory, security-context gated) --------------------------


def test_weak_hash_with_security_context_flagged():
    violations = scan_dangerous_sinks("digest = Digest::MD5.hexdigest(password)", language="ruby")
    assert _rules(violations) == ["weak-hash"]
    assert violations[0].severity == "warning"


def test_weak_hash_without_security_context_quiet():
    # MD5 of a cache payload is a legitimate non-crypto use.
    violations = scan_dangerous_sinks("cache_key = Digest::MD5.hexdigest(payload)", language="ruby")
    assert _rules(violations) == []


def test_weak_hash_typescript_with_context():
    violations = scan_dangerous_sinks(
        "const h = md5(secret)  // legacy SHA1 path", language="typescript"
    )
    assert "weak-hash" in _rules(violations)


def test_sha1_variant_spelling_matched():
    violations = scan_dangerous_sinks("token_hash = SHA-1(api_key)", language="ruby")
    assert "weak-hash" in _rules(violations)


# --- insecure-random (TypeScript only, advisory) ---------------------------


def test_math_random_security_context_flagged():
    violations = scan_dangerous_sinks(
        "const token = Math.random().toString(36);", language="typescript"
    )
    assert _rules(violations) == ["insecure-random"]
    assert violations[0].severity == "warning"


def test_math_random_without_context_quiet():
    violations = scan_dangerous_sinks("const jitter = Math.random() * 100;", language="typescript")
    assert _rules(violations) == []


def test_math_random_not_run_for_ruby():
    # Ruby has no Math.random; the rule is TS-scoped and must not fabricate hits.
    violations = scan_dangerous_sinks("salt = Math.random", language="ruby")
    assert "insecure-random" not in _rules(violations)


# --- sql-string-interpolation (Ruby only, advisory) ------------------------


def test_ruby_where_string_interpolation_flagged():
    violations = scan_dangerous_sinks('User.where("name = #{params[:q]}")', language="ruby")
    assert _rules(violations) == ["sql-string-interpolation"]
    assert violations[0].severity == "warning"


def test_ruby_bare_query_in_scope_flagged():
    violations = scan_dangerous_sinks(
        'scope :recent, -> { where("ts > #{cutoff}") }', language="ruby"
    )
    assert "sql-string-interpolation" in _rules(violations)


def test_ruby_parameterized_query_clean():
    violations = scan_dangerous_sinks('User.where("name = ?", name)', language="ruby")
    assert _rules(violations) == []


def test_ruby_static_string_query_clean():
    violations = scan_dangerous_sinks('User.where("active = true")', language="ruby")
    assert _rules(violations) == []


def test_ruby_sql_interpolation_in_comment_not_flagged():
    violations = scan_dangerous_sinks('# User.where("x = #{y}")', language="ruby")
    assert _rules(violations) == []


def test_ruby_sql_interpolation_with_trailing_comment_flagged():
    violations = scan_dangerous_sinks('User.where("x = #{y}")  # interpolated', language="ruby")
    assert "sql-string-interpolation" in _rules(violations)


def test_ruby_find_by_sql_interpolation_flagged():
    violations = scan_dangerous_sinks(
        'Model.find_by_sql("SELECT * FROM t WHERE id = #{id}")', language="ruby"
    )
    assert "sql-string-interpolation" in _rules(violations)


def test_ruby_connection_execute_interpolation_flagged():
    # The rawest injection vector: raw SQL through the connection, bypassing the
    # query builder. Must flag the same as where()/find_by_sql.
    for snippet in (
        'User.connection.execute("SELECT * FROM users WHERE id = #{id}")',
        'ActiveRecord::Base.connection.exec_query("SELECT #{cols} FROM t")',
        'conn.select_all("SELECT * FROM t WHERE k = #{key}")',
        'conn.select_value("SELECT count(*) FROM t WHERE id = #{id}")',
    ):
        violations = scan_dangerous_sinks(snippet, language="ruby")
        assert "sql-string-interpolation" in _rules(violations), snippet


def test_ruby_execute_parameterized_clean():
    # A non-interpolated execute is clean (no false positive).
    violations = scan_dangerous_sinks(
        'User.connection.execute("SELECT * FROM users")', language="ruby"
    )
    assert _rules(violations) == []


def test_ruby_sql_rule_not_run_for_typescript():
    # `${...}` in a TS template is handled elsewhere; this Ruby-only rule must
    # not fire on TS interpolation syntax.
    violations = scan_dangerous_sinks("db.where(`name = ${q}`)", language="typescript")
    assert "sql-string-interpolation" not in _rules(violations)


# --- robustness ------------------------------------------------------------


def test_empty_content_returns_empty():
    assert scan_dangerous_sinks("", language="typescript") == []
    assert scan_dangerous_sinks("", language="ruby") == []
    assert scan_dangerous_sinks("", language=None) == []


def test_scanner_is_pure_no_exception_on_garbage():
    # Unbalanced braces, lone interpolation markers, binary-ish bytes.
    weird = 'where("#{' + "\x00" * 10 + '}") eval( unterminated'
    out = scan_dangerous_sinks(weird, language="ruby")
    assert isinstance(out, list)
    for v in out:
        assert isinstance(v, Violation)


def test_multiple_distinct_sinks_each_emit_one_violation():
    src = (
        "class A\n"
        "  def run\n"
        "    eval(input)\n"
        '    User.where("id = #{params[:id]}")\n'
        "    h = Digest::MD5.hexdigest(password)\n"
        "  end\n"
        "end\n"
    )
    rules = _rules(scan_dangerous_sinks(src, language="ruby"))
    assert "eval-call" in rules
    assert "sql-string-interpolation" in rules
    assert "weak-hash" in rules


def test_distinct_rule_names_avoid_secret_filter_collision():
    # None of the sink rules may reuse the secret rule name, or the hook secret
    # rollup filters would misclassify them.
    src = 'eval(x); User.where("a = #{b}")'
    rules = set(_rules(scan_dangerous_sinks(src, language="ruby")))
    assert "secret-detected-in-content" not in rules


@pytest.mark.parametrize(
    "method",
    ["where", "having", "order", "group", "joins", "pluck", "find_by_sql"],
)
def test_ruby_query_methods_covered(method):
    src = f'Model.{method}("col = #{{val}}")'
    assert "sql-string-interpolation" in _rules(scan_dangerous_sinks(src, language="ruby"))
