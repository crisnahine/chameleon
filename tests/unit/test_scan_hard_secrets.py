"""The deterministic hard-secret fast scanner.

`scan_for_hard_secrets` / `lint_engine.scan_hard_secrets` run only the
deterministic fallback patterns whose kind may hard-block — never
detect-secrets, never the entropy/context-gated kinds — so they are safe on
the PreToolUse hot path. These tests pin kind coverage, detect-secrets
independence, concat-fold parity, equivalence of the hard-class subset with
the full `scan_secrets` pipeline, the result cap, and the invariant that no
hard kind is context-gated (which would silently weaken the fast path).
"""

from __future__ import annotations

import time

import pytest

from chameleon_mcp.lint_engine import scan_hard_secrets, scan_secrets
from chameleon_mcp.profile.secret_scanner import (
    _CONTEXT_GATED_KINDS,
    _FALLBACK_PATTERNS,
    scan_for_hard_secrets,
)
from chameleon_mcp.violation_class import (
    _DETERMINISTIC_SECRET_KINDS,
    is_hard_class,
    tag_secret_hardness,
    violation_line,
)

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

# One synthetic fixture per deterministic kind. Each token satisfies its
# pattern's fixed prefix + length shape; none is a real credential.
HARD_FIXTURES = {
    "aws_access_key": AWS_KEY,
    "github_token": "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "gitlab_token": "glpat-" + "abcdefghijklmnopqrst",
    "ai_api_key": "sk-ant-" + "abcdefghijklmnopqrstuvwx",
    "stripe_live_key": "sk_live_" + "abcdefghijklmnopqrstuvwx",
    "stripe_key": "rk_test_" + "abcdefghijklmnopqrstuvwx",
    "slack_token": "xoxb-" + "1234567890-abcdef",
    "google_api_key": "AIza" + "B" * 35,
    "azure_account_key": "AccountKey=abcd1234abcd1234abcd1234",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----",
}

# Content that only trips advisory kinds: entropy shapes (context-gated),
# keyword-adjacent quoted values, and the GCP service-account JSON marker.
ADVISORY_ONLY_CONTENT = (
    'const awsSecretKey = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0";\n'
    'const apiTokenDigest = "0123456789abcdef0123456789abcdef01234567";\n'
    'password = "supersecretvalue123"\n'
    '{"type": "service_account"}\n'
)


def test_fixture_table_covers_every_deterministic_kind():
    assert set(HARD_FIXTURES) == set(_DETERMINISTIC_SECRET_KINDS)


@pytest.mark.parametrize("kind", sorted(_DETERMINISTIC_SECRET_KINDS))
def test_each_hard_kind_is_flagged_with_parseable_actual(kind):
    content = f'const credential = "{HARD_FIXTURES[kind]}";\n'
    out = [v.to_dict() for v in scan_hard_secrets(content)]
    assert all(v["rule"] == "secret-detected-in-content" for v in out)
    tag_secret_hardness(out)
    mine = [v for v in out if v["secret_kind"] == kind]
    assert mine, f"{kind} fixture must be flagged by the fast scanner"
    assert all(violation_line(v) == 1 for v in mine)
    assert all(v["secret_hard"] for v in mine)


def test_advisory_kinds_are_not_emitted():
    assert scan_for_hard_secrets(ADVISORY_ONLY_CONTENT) == []
    assert scan_hard_secrets(ADVISORY_ONLY_CONTENT) == []


def test_never_touches_detect_secrets(monkeypatch):
    import chameleon_mcp.profile.secret_scanner as secret_scanner

    def boom(_content):
        raise AssertionError("the hard fast path must never invoke detect-secrets")

    monkeypatch.setattr(secret_scanner, "_try_detect_secrets", boom)
    out = [v.to_dict() for v in scan_hard_secrets(f'const k = "{AWS_KEY}";\n')]
    tag_secret_hardness(out)
    assert any(v["secret_kind"] == "aws_access_key" for v in out)


def test_concat_fold_parity_split_github_token():
    content = "const t = 'ghp_' + \"abcdefghijklmnopqrstuvwxyz0123456789\";\n"
    out = [v.to_dict() for v in scan_hard_secrets(content)]
    tag_secret_hardness(out)
    folded = [v for v in out if v["secret_kind"] == "github_token"]
    assert folded, "split token must be caught via the concat fold"
    assert any("[after string-concat fold]" in v["actual"] for v in folded)


def test_hard_subset_equivalence_with_scan_secrets():
    corpus = (
        f'const a = "{AWS_KEY}";\n'
        'const b = "ghp_abcdefghijklmnopqrstuvwxyz0123456789";\n'
        + ADVISORY_ONLY_CONTENT
        + 'const pem = "-----BEGIN EC PRIVATE KEY-----";\n'
        "export const benign = 'hello world';\n"
    )

    def hard_keys(violations):
        rows = [v.to_dict() for v in violations]
        tag_secret_hardness(rows)
        return {(v["rule"], v["secret_kind"], violation_line(v)) for v in rows if is_hard_class(v)}

    assert hard_keys(scan_hard_secrets(corpus)) == hard_keys(scan_secrets(corpus))


def test_100kb_token_dense_single_line_within_budget():
    content = (f"{AWS_KEY} " * 5000)[:100_000]
    start = time.perf_counter()
    out = scan_hard_secrets(content)
    elapsed = time.perf_counter() - start
    assert out
    assert elapsed < 2.0, f"hard scan took {elapsed:.2f}s on a 100KB token-dense payload"


def test_cap_summary_row_is_never_hard():
    lines = "\n".join(f'const k{i} = "AKIAIOSFODNN7EXAMPL{c}";' for i, c in enumerate("ABCDE"))
    out = [v.to_dict() for v in scan_hard_secrets(lines, max_results=3)]
    assert len(out) == 4
    cap_row = out[-1]
    assert cap_row["actual"] == "+2 more (capped at 3)"
    tag_secret_hardness(out)
    assert cap_row["secret_hard"] is False
    assert not is_hard_class(cap_row)


def test_no_hard_kind_is_context_gated():
    # A future pattern edit that context-gates a hard kind would silently drop
    # it from the fast path; the deny would weaken without any test failing.
    assert _DETERMINISTIC_SECRET_KINDS & _CONTEXT_GATED_KINDS == frozenset()


def test_every_hard_kind_originates_from_a_fallback_pattern():
    # The fast path filters _FALLBACK_PATTERNS by kind; a hard kind sourced
    # from anywhere else (e.g. a detect-secrets type string) would be missed.
    fallback_kinds = {kind for _, kind in _FALLBACK_PATTERNS}
    assert _DETERMINISTIC_SECRET_KINDS <= fallback_kinds
