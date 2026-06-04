"""Wiring of scan_secrets into the in-process hook lint path.

scan_secrets used to run only inside the lint_file MCP tool, so a committed
credential reached neither the PostToolUse advisory nor the Stop backstop.
These tests assert the hook path now produces tagged secret violations and that
only the deterministic high-precision kinds are block-eligible, with the rest
(entropy/broad-fallback) staying advisory and an inline chameleon-ignore
reaching the secret rule.
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
