"""Unit tests for chameleon_mcp.profile.secret_scanner."""

from __future__ import annotations

from chameleon_mcp.profile.secret_scanner import _try_detect_secrets, scan_for_secrets


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
