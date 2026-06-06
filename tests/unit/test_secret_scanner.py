"""Unit tests for chameleon_mcp.profile.secret_scanner."""

from __future__ import annotations

from chameleon_mcp.profile.secret_scanner import (
    _fallback_scan,
    _line_number_at,
    _try_detect_secrets,
    scan_for_secrets,
)


def test_detect_secrets_backend_active():
    """detect-secrets runs with registered plugins (not a bare no-op scan)."""
    hits = _try_detect_secrets('aws_key = "AKIAIOSFODNN7EXAMPLE"')
    assert hits is not None
    assert any(h["type"] == "AWS Access Key" for h in hits)


def test_clean_code_is_not_flagged():
    """Ordinary source must not trip the scanner (no high-entropy false positives)."""
    samples = [
        "const x = 1;\nexport default x;",
        "export function Button({label}) { return label; }",
        "class Listing < ApplicationRecord\n  belongs_to :user\nend",
        "import { foo } from '@/components/ui/foo';",
    ]
    for src in samples:
        assert scan_for_secrets(src) == [], src


def test_encrypted_private_key_caught():
    hits = scan_for_secrets("-----BEGIN ENCRYPTED PRIVATE KEY-----")
    assert any(h["type"] == "private_key" for h in hits)


def test_common_secret_shapes_caught():
    # Provider-token fixtures are assembled at runtime so the literal token
    # never sits in the committed file (GitHub push protection blocks it).
    gh_pat = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"
    stripe_key = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
    cases = [
        f'token = "{gh_pat}"',
        f'key = "{stripe_key}"',
        "-----BEGIN RSA PRIVATE KEY-----",
    ]
    for src in cases:
        assert scan_for_secrets(src), src


def test_empty_content_is_safe():
    assert scan_for_secrets("") == []


def test_camelcase_identifier_not_flagged_as_aws_secret():
    # A 40-char camelCase identifier on a plain line is not a credential. The
    # shape-only possible_aws_secret pattern is gated on credential context, so
    # it must not fire here (it used to match ~6% of real TS files).
    src = "const x = adminListingNotesCreateRequestDescriptorXyz;"
    assert [h for h in scan_for_secrets(src) if h["type"] == "possible_aws_secret"] == []


def test_aws_secret_assignment_is_flagged():
    blob = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
    src = f'aws_secret_key = "{blob}"'
    assert any(h["type"] == "possible_aws_secret" for h in scan_for_secrets(src))


def test_bare_git_sha_in_comment_not_flagged_as_hex():
    # A 40-char hex run with no credential token is a git SHA / sourcemap hash.
    src = "# see commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 for the fix"
    assert [h for h in scan_for_secrets(src) if h["type"] == "high_entropy_hex"] == []


def test_api_token_hex_assignment_is_flagged():
    src = 'api_token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"'
    assert any(h["type"] == "high_entropy_hex" for h in scan_for_secrets(src))


def test_line_number_at_counts_newlines():
    content = "a\nb\ncredential\n"
    pos = content.index("credential")
    assert _line_number_at(content, pos) == 3
    assert _line_number_at(content, 0) == 1


def test_fallback_hard_kinds_carry_line_number():
    # The deterministic hard kinds come only from _fallback_scan. They must carry
    # a line, not just a char offset, so the PR-review hunk gate can place them in
    # a diff hunk. Before the fix they reported `position` only and the gate was
    # unrunnable on the only secrets it gates.
    blank = "\n" * 9  # push the key onto line 10
    content = blank + 'aws = "AKIAIOSFODNN7EXAMPLE"\n'
    hits = [h for h in _fallback_scan(content) if h["type"] == "aws_access_key"]
    assert hits
    assert hits[0]["line_number"] == 10
    # position is retained for the dedup key / offset-based callers.
    assert "position" in hits[0]


def test_scan_for_secrets_hard_hit_has_line_number():
    gh_pat = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"
    content = "x = 1\ny = 2\n" + f'token = "{gh_pat}"\n'
    hard = [h for h in scan_for_secrets(content) if h["type"] == "github_token"]
    assert hard
    assert hard[0]["line_number"] == 3


def test_added_line_number_does_not_double_count():
    # One AKIA on one line yields exactly one fallback aws_access_key hit; the new
    # line_number field must not perturb the (type, position) dedup.
    content = 'aws = "AKIAIOSFODNN7EXAMPLE"\n'
    hits = [h for h in scan_for_secrets(content) if h["type"] == "aws_access_key"]
    assert len(hits) == 1


def test_gitlab_pat_caught_by_detect_secrets_backend():
    # Regression guard for the QA campaign's "glpat not caught" report: a
    # valid-length GitLab PAT (>= 20 chars after the prefix) is detected. The
    # campaign's miss reproduces only with a too-short fake token, which is
    # not a valid PAT shape.
    pat = "glpat-" + "x" * 20
    hits = _try_detect_secrets(f'token = "{pat}"')
    assert hits is not None
    assert any(h["type"] == "GitLab Token" for h in hits)


def test_gitlab_token_family_caught_by_fallback():
    # The fallback must keep parity with detect-secrets so GitLab tokens stay
    # caught if the library is ever unavailable.
    for prefix in ("glpat", "gldt", "glrt"):
        token = f"{prefix}-" + "a1B2" * 6
        hits = [h for h in _fallback_scan(f'x = "{token}"') if h["type"] == "gitlab_token"]
        assert hits, f"{prefix} token missed by fallback"
        assert hits[0]["line_number"] == 1


def test_gitlab_token_hard_kind_tagged():
    from chameleon_mcp.lint_engine import scan_secrets
    from chameleon_mcp.violation_class import tag_secret_hardness

    token = "glpat-" + "k" * 24
    violations = [v.to_dict() for v in scan_secrets(f'GITLAB_TOKEN = "{token}".freeze')]
    tag_secret_hardness(violations)
    assert any(v.get("secret_hard") for v in violations)
