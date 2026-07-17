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


def test_import_preference_skips_docstring_embedded_import():
    # A competing import quoted inside a docstring is documentation, not a real
    # import; it must not flag (the string-embedded-import false-positive guard).
    content = '"""Usage example:\n\nimport requests\nr = requests.get(url)\n"""\n\nx = 1\n'
    v = lint_conventions(content, _CONV, language="python")
    assert not any(x.rule == "import-preference-violation" for x in v)


def test_import_preference_still_flags_real_import_with_docstring():
    # A real top-level import still flags even when a docstring also mentions it.
    content = '"""See import requests below."""\nimport requests\n'
    v = lint_conventions(content, _CONV, language="python")
    assert any(x.rule == "import-preference-violation" for x in v)


# --------------------------------------------------------------------------- #
# PKG-4 security sinks (advisory): weak-hash, insecure-random,
# command-injection, insecure-deserialization
# --------------------------------------------------------------------------- #


def test_weak_hash_python_crypto_context():
    v = scan_dangerous_sinks(
        "import hashlib\nsig = hashlib.md5(password).hexdigest()\n", language="python"
    )
    assert any(x.rule == "weak-hash" for x in v)


def test_weak_hash_quiet_without_crypto_context():
    # A bare md5 with no security keyword nearby stays quiet (cache-key use).
    v = scan_dangerous_sinks("key = hashlib.md5(blob).hexdigest()\n", language="python")
    assert not any(x.rule == "weak-hash" for x in v)


def test_insecure_random_python():
    v = scan_dangerous_sinks(
        "token = random.randint(0, 999999)  # session token\n", language="python"
    )
    assert any(x.rule == "insecure-random" for x in v)


def test_command_injection_os_system():
    v = scan_dangerous_sinks("import os\nos.system(user_cmd)\n", language="python")
    assert any(x.rule == "command-injection" for x in v)


def test_command_injection_subprocess_shell_true():
    v = scan_dangerous_sinks("subprocess.run(cmd, shell=True)\n", language="python")
    assert any(x.rule == "command-injection" for x in v)


def test_command_injection_quiet_without_shell():
    v = scan_dangerous_sinks('subprocess.run(["ls", "-l"])\n', language="python")
    assert not any(x.rule == "command-injection" for x in v)


def test_insecure_deserialization_pickle():
    v = scan_dangerous_sinks("import pickle\nobj = pickle.loads(blob)\n", language="python")
    assert any(x.rule == "insecure-deserialization" for x in v)


def test_insecure_deserialization_yaml_load():
    v = scan_dangerous_sinks("data = yaml.load(text)\n", language="python")
    assert any(x.rule == "insecure-deserialization" for x in v)


def test_yaml_safe_load_is_clean():
    v = scan_dangerous_sinks("data = yaml.safe_load(text)\n", language="python")
    assert not any(x.rule == "insecure-deserialization" for x in v)


def test_sinks_in_string_not_flagged():
    v = scan_dangerous_sinks(
        'doc = "use pickle.loads and os.system carefully"\n', language="python"
    )
    assert not any(x.rule in ("command-injection", "insecure-deserialization") for x in v)


def test_framework_methods_exempt_from_snake_case():
    # unittest / Django TestCase hooks (setUp/tearDown/...) are framework-mandated
    # camelCase names a class MUST match exactly, so the snake_case method rule
    # exempts them instead of flagging an unfixable "violation".
    from chameleon_mcp.lint_engine import _python_naming_violations

    naming = {"method_casing": {"pattern": "snake_case", "consistency": 0.99}}
    content = (
        "class MyTest:\n"
        "    def setUp(self):\n        pass\n"
        "    def tearDown(self):\n        pass\n"
        "    def badCamelHelper(self):\n        pass\n"
    )
    flagged = {x.actual for x in _python_naming_violations(content, naming)}
    assert "setUp" not in flagged
    assert "tearDown" not in flagged
    assert "badCamelHelper" in flagged
