"""Wiring of scan_secrets into the in-process hook lint path.

scan_secrets used to run only inside the lint_file MCP tool, so a committed
credential reached neither the PostToolUse advisory nor the Stop backstop.
These tests assert the hook path now produces tagged secret violations and that
only the deterministic high-precision kinds are block-eligible, with the rest
(entropy/broad-fallback) staying advisory and an inline chameleon-ignore
reaching the secret rule. ``_content_has_hard_secret`` (the corrections-
exhausted block gate) runs the regex-only hard scanner and honors only
rule-NAMED directives: the deterministic hard class is blanket-immune.
"""

from __future__ import annotations

from types import SimpleNamespace

from chameleon_mcp import hook_helper
from chameleon_mcp.violation_class import (
    hard_class_violations,
    ignored_rules,
    is_archetype_independent,
    is_hard_class,
)

# A real AWS access key shape (deterministic, hard) and a benign 40-char base64
# run (possible_aws_secret / high-entropy — advisory only).
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _loaded():
    """A minimal profile object: no archetype data, so only the path-independent
    sub-lints (phantom-import, secret scan) can contribute."""
    return SimpleNamespace(
        canonicals={"canonicals": {}},
        conventions={"conventions": {}},
        rules={},
    )


def _lint(content, tmp_path, file_rel="src/config.ts"):
    return hook_helper._lint_file_in_process(
        tmp_path, "util", content, str(tmp_path / file_rel), loaded=_loaded()
    )


def test_deterministic_secret_reaches_hook_path_and_is_hard(tmp_path):
    out = _lint(f'const k = "{AWS_KEY}";\n', tmp_path)
    secrets = [v for v in out if v.get("rule") == "secret-detected-in-content"]
    assert secrets, "scan_secrets must run on the in-process hook path"
    assert any(v.get("secret_hard") for v in secrets)
    assert any(is_hard_class(v) for v in secrets)
    # Archetype-independent: a credential is a credential regardless of archetype.
    assert is_archetype_independent("secret-detected-in-content")


def test_secret_is_hard_only_when_in_active_set(tmp_path):
    out = _lint(f'const k = "{AWS_KEY}";\n', tmp_path)
    # Not in the active block set -> not hard-class for this repo.
    assert hard_class_violations(out, active_rules=set()) == []
    hard = hard_class_violations(out, active_rules={"secret-detected-in-content"})
    assert [v["rule"] for v in hard] == ["secret-detected-in-content"]


def test_clean_file_yields_no_secret_violation(tmp_path):
    out = _lint("export const greeting = 'hello world';\n", tmp_path)
    assert [v for v in out if v.get("rule") == "secret-detected-in-content"] == []


def test_scan_archetype_independent_skips_eval_on_non_code_file():
    # eval-call must not fire on a non-code file (detect_language None): the
    # literal `eval(` in markdown / doc prose is not a runnable sink, and such a
    # file cannot carry an inline chameleon-ignore directive. Under enforce-default
    # this would otherwise turn-trap a session that merely documents eval().
    md = "Docs: never write eval(userInput) for parsing user input.\n"
    out = hook_helper._scan_archetype_independent(md, "README.md")
    assert [v for v in out if v.get("rule") == "eval-call"] == []


def test_scan_archetype_independent_flags_eval_on_code_file():
    out = hook_helper._scan_archetype_independent("x = eval(user_input)\n", "app.py")
    evals = [v for v in out if v.get("rule") == "eval-call"]
    assert evals, "a real eval() in a .py file must still flag"
    assert any(is_hard_class(v) for v in evals)


def test_with_archetype_path_drops_eval_secret_on_non_code(tmp_path):
    # Guard the legacy-blind-archetype path: a non-code file CAN resolve to an
    # archetype via an old extension-blind paths_pattern, routing it through
    # _lint_file_in_process (which runs scan_dangerous_sinks + scan_secrets) and
    # the with-archetype block sites. Those sites now apply block_eligible_on_file,
    # so eval/secret in a doc still never hard-block. This mirrors the composition
    # at posttool_verify and _stop_file_still_blockable's with-archetype branch.
    from chameleon_mcp.lint_engine import detect_language, scan_dangerous_sinks, scan_secrets
    from chameleon_mcp.violation_class import block_eligible_on_file, tag_secret_hardness

    md_path = "apps/web/src/README.md"
    lang = detect_language(md_path)
    assert lang is None
    content = f'eval(user_input)\nKEY = "{AWS_KEY}"\n'
    viol = [v.to_dict() for v in scan_dangerous_sinks(content, language=lang)]
    secs = [v.to_dict() for v in scan_secrets(content)]
    tag_secret_hardness(secs)
    viol += secs
    active = {"eval-call", "secret-detected-in-content"}
    hard = hard_class_violations(viol, active)
    assert {v["rule"] for v in hard} == active, "raw with-archetype lint emits both on a doc"
    assert block_eligible_on_file(hard, language=lang) == [], "both must drop from the block set"


def test_secret_in_non_code_file_is_advisory_not_block_eligible():
    from chameleon_mcp.lint_engine import detect_language
    from chameleon_mcp.violation_class import block_eligible_on_file

    # A deterministic AKIA in a markdown doc still surfaces (advisory), but is
    # dropped from the BLOCK set: a non-code file can't carry a chameleon-ignore,
    # so blocking it would turn-trap with no escape.
    md_out = hook_helper._scan_archetype_independent(f"example key {AWS_KEY}\n", "NOTES.md")
    md_secrets = [v for v in md_out if v.get("rule") == "secret-detected-in-content"]
    assert md_secrets, "the credential must still surface as an advisory in a doc"
    md_hard = hard_class_violations(md_out, active_rules={"secret-detected-in-content"})
    assert block_eligible_on_file(md_hard, language=detect_language("NOTES.md")) == []

    # The same key in a .py file stays block-eligible.
    py_out = hook_helper._scan_archetype_independent(f'KEY = "{AWS_KEY}"\n', "settings.py")
    py_hard = hard_class_violations(py_out, active_rules={"secret-detected-in-content"})
    assert py_hard, "a credential in code must stay block-eligible"
    assert block_eligible_on_file(py_hard, language=detect_language("settings.py")) == py_hard


def test_benign_base64_run_on_plain_line_does_not_surface(tmp_path):
    # 40 base64 chars on a line with no credential token: a long identifier or a
    # checksum, not a secret. The broad fallback is gated on credential context,
    # so it must not surface at all (it used to flood the advisory tail).
    blob = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
    out = _lint(f'const checksum = "{blob}";\n', tmp_path)
    secrets = [v for v in out if v.get("rule") == "secret-detected-in-content"]
    assert secrets == [], "a 40-char run with no credential context must not flag"


def test_credential_context_base64_run_surfaces_advisory(tmp_path):
    # Same 40-char run, but the line names a credential: surfaced
    # (possible_aws_secret) yet never hard, so it can advise and cannot block.
    blob = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
    out = _lint(f'const awsSecretKey = "{blob}";\n', tmp_path)
    secrets = [v for v in out if v.get("rule") == "secret-detected-in-content"]
    assert secrets, "a credential-context run should still surface as advisory"
    assert all(not v.get("secret_hard") for v in secrets)
    assert all(not is_hard_class(v) for v in secrets)


def test_chameleon_ignore_reaches_secret_rule(tmp_path):
    content = f'const k = "{AWS_KEY}"; // chameleon-ignore secret-detected-in-content\n'
    out = _lint(content, tmp_path)
    hard = hard_class_violations(out, active_rules={"secret-detected-in-content"})
    ign = ignored_rules(content) or set()
    # The ignore filter the hook applies on both the posttool and Stop paths.
    surviving = [v for v in hard if not ({"", v.get("rule")} & ign)]
    assert surviving == [], "chameleon-ignore must drop the secret rule from the hard set"


def test_bare_chameleon_ignore_drops_secret(tmp_path):
    content = f'const k = "{AWS_KEY}"; // chameleon-ignore\n'
    out = _lint(content, tmp_path)
    hard = hard_class_violations(out, active_rules={"secret-detected-in-content"})
    ign = ignored_rules(content) or set()
    surviving = [v for v in hard if not ({"", v.get("rule")} & ign)]
    assert surviving == []


def test_content_has_hard_secret_true_under_bare_blanket_directive():
    # The non-suppressible tier reaches the corrections-exhausted branch: a
    # bare directive no longer hides the hard kind from the block gate.
    content = f'const k = "{AWS_KEY}"; // chameleon-ignore\n'
    assert hook_helper._content_has_hard_secret(content, "src/config.ts") is True


def test_content_has_hard_secret_false_under_named_directive():
    content = f'const k = "{AWS_KEY}"; // chameleon-ignore secret-detected-in-content\n'
    assert hook_helper._content_has_hard_secret(content, "src/config.ts") is False


def test_content_has_hard_secret_uses_fast_path(monkeypatch):
    # Regression pin: after the rewire to scan_hard_secrets, the full
    # detect-secrets pipeline must not run, and the AKIA fixture is still
    # caught by the regex-only path.
    import chameleon_mcp.lint_engine as le

    def boom(*_a, **_k):
        raise AssertionError("_content_has_hard_secret must not call scan_secrets")

    monkeypatch.setattr(le, "scan_secrets", boom)
    assert hook_helper._content_has_hard_secret(f'const k = "{AWS_KEY}";\n', "src/config.ts")


def test_secret_scan_failure_is_contained(tmp_path, monkeypatch):
    # A raising scan_secrets must not abort the whole re-lint: the secret block
    # is wrapped in its own try/except so other sub-lints still contribute.
    import chameleon_mcp.lint_engine as le

    def boom(*_a, **_k):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(le, "scan_secrets", boom)
    out = _lint(f'const k = "{AWS_KEY}";\n', tmp_path)
    assert isinstance(out, list)
    assert [v for v in out if v.get("rule") == "secret-detected-in-content"] == []
