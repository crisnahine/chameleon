"""Hook-time lint rules for Python.

Covers the highest-value, lowest-false-positive Python checks: the eval/exec
security sink (block-eligible) and import-preference (the rule the
teach-competing-import + counterexample features depend on).
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import lint_conventions, scan_dangerous_sinks

# --------------------------------------------------------------------------- #
# eval / exec security sink
# --------------------------------------------------------------------------- #


def test_eval_call_flagged():
    v = scan_dangerous_sinks("result = eval(user_input)\n", language="python")
    assert any(x.rule == "eval-call" for x in v)


def test_exec_call_flagged():
    v = scan_dangerous_sinks("exec(compiled_code)\n", language="python")
    assert any(x.rule == "eval-call" for x in v)


def test_eval_in_comment_not_flagged():
    v = scan_dangerous_sinks("# never call eval(x) here\nx = 1\n", language="python")
    assert not any(r.rule == "eval-call" for r in v)


def test_eval_in_string_not_flagged():
    v = scan_dangerous_sinks('doc = "use eval(x) carefully"\n', language="python")
    assert not any(r.rule == "eval-call" for r in v)


def test_eval_in_triple_quoted_string_not_flagged():
    src = 'HELP = """\nDo not use eval(x) or exec(y).\n"""\n'
    v = scan_dangerous_sinks(src, language="python")
    assert not any(r.rule == "eval-call" for r in v)


def test_method_eval_not_flagged():
    # obj.eval(...) is a member call, not the builtin -- the (?<![.\w]) guard.
    v = scan_dangerous_sinks("self.evaluator.eval(node)\n", language="python")
    assert not any(r.rule == "eval-call" for r in v)


# --------------------------------------------------------------------------- #
# import-preference (competing import) — enables teach + counterexample for py
# --------------------------------------------------------------------------- #

_CONV = {
    "imports": {
        "competing": [{"over": "requests", "preferred": "httpx"}],
    }
}


def test_import_preference_flags_discouraged_module():
    v = lint_conventions("import requests\n\nr = requests.get(url)\n", _CONV, language="python")
    assert any(x.rule == "import-preference-violation" for x in v)


def test_import_preference_from_form_flagged():
    v = lint_conventions("from requests import get\n", _CONV, language="python")
    assert any(x.rule == "import-preference-violation" for x in v)


def test_import_preference_silent_when_preferred_present():
    v = lint_conventions(
        "import httpx\nimport requests  # transitional\n", _CONV, language="python"
    )
    assert not any(x.rule == "import-preference-violation" for x in v)


def test_import_preference_silent_when_neither_present():
    v = lint_conventions("import os\n", _CONV, language="python")
    assert not any(x.rule == "import-preference-violation" for x in v)


def test_import_preference_not_fooled_by_substring():
    # "requests" must not match an unrelated module that merely contains it.
    conv = {"imports": {"competing": [{"over": "requests", "preferred": "httpx"}]}}
    v = lint_conventions("import requests_oauthlib\n", conv, language="python")
    assert not any(x.rule == "import-preference-violation" for x in v)
