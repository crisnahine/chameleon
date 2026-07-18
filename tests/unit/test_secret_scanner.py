"""Unit tests for chameleon_mcp.profile.secret_scanner."""

# chameleon-ignore-file secret-detected-in-content
# Every credential-shaped string in this file is a deliberately fake fixture
# that exercises the scanner itself; nothing here is a real secret.

from __future__ import annotations

from chameleon_mcp.profile.secret_scanner import (
    _fallback_scan,
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


def test_file_path_beside_ordinary_word_not_flagged_as_aws_secret():
    # The credential-context gate must key on whole words. "authored" is prose,
    # not a credential token, and a 40-char filesystem path is not a secret --
    # the pattern's character class includes '/', so any path of that length
    # matches its shape. Together they used to report a leaked AWS credential
    # for an ordinary sentence naming a file.
    src = "| Dev tree (where fixes are authored) | `/Users/crisn/Documents/Projects/chameleon` |"
    assert [h for h in scan_for_secrets(src) if h["type"] == "possible_aws_secret"] == []


def test_substring_credential_words_do_not_open_the_context_gate():
    # Each line carries a 40-char base64-shaped run whose only "credential"
    # context is a substring buried in an ordinary English word.
    blob = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
    for word in ("authored", "monkey", "keyboard", "accessible", "privately", "secretary"):
        src = f"The {word} value {blob} is ordinary prose"
        hits = [h for h in scan_for_secrets(src) if h["type"] == "possible_aws_secret"]
        assert hits == [], f"{word!r} must not open the credential-context gate"


def test_whole_word_credential_context_still_flags():
    # The gate must keep firing on real credential words, including possessive
    # and punctuated forms -- word-boundary matching must not narrow coverage.
    blob = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0"
    for line in (
        f'secret = "{blob}"',
        f'api_key: "{blob}"',
        f"# the token below is live: {blob}",
        f'{{"access": "{blob}"}}',
        f"PRIVATE={blob}",
    ):
        hits = [h for h in scan_for_secrets(line) if h["type"] == "possible_aws_secret"]
        assert hits, f"credential context must still flag: {line!r}"


def test_bare_git_sha_in_comment_not_flagged_as_hex():
    # A 40-char hex run with no credential token is a git SHA / sourcemap hash.
    src = "# see commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 for the fix"
    assert [h for h in scan_for_secrets(src) if h["type"] == "high_entropy_hex"] == []


def test_api_token_hex_assignment_is_flagged():
    src = 'api_token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"'
    assert any(h["type"] == "high_entropy_hex" for h in scan_for_secrets(src))


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


def test_long_line_skipped_by_detect_secrets_pass():
    # A token-dense single line (minified bundle, generated const map) makes
    # detect-secrets re-scan the whole line per candidate — O(candidates x
    # length), tens of seconds at 100KB. Lines past the cap are skipped by the
    # per-line pass entirely.
    import time

    line = "".join(f"const v{i}={i};" for i in range(5000))
    start = time.monotonic()
    hits = _try_detect_secrets(line)
    elapsed = time.monotonic() - start
    assert hits == []
    assert elapsed < 2.0, f"long-line scan took {elapsed:.1f}s"


def test_hard_secret_on_long_line_still_caught():
    # Skipping a long line in the detect-secrets pass must not lose the
    # deterministic kinds: the fallback patterns scan the full content.
    long_line = "const pad=1;" * 500 + 'const k = "AKIAIOSFODNN7EXAMPLE";' + "const pad=2;" * 500
    assert "\n" not in long_line
    hits = scan_for_secrets(long_line)
    assert any(h["type"] == "aws_access_key" for h in hits)


def test_line_at_cap_still_scanned_by_detect_secrets():
    # The cap excludes only pathological lines; a line exactly at the cap (and
    # any realistic hand-written line) still goes through detect-secrets.
    from chameleon_mcp._thresholds import threshold_int

    cap = threshold_int("SECRET_SCAN_MAX_LINE_LEN")
    line = ('aws_key = "AKIAIOSFODNN7EXAMPLE"' + " " * cap)[:cap]
    hits = _try_detect_secrets(line)
    assert hits is not None
    assert any(h["type"] == "AWS Access Key" for h in hits)


# --------------------------------------------------------------------------
# qa25 P3 — the fallback scan resolved each hit's line by re-scanning the
# whole buffer (O(hits x length)); a token-dense single line stalled for
# hundreds of ms. The offset table + per-line context cache must keep the
# pathological shape linear without changing any verdict.


def test_fallback_scan_token_dense_single_line_is_bounded():
    import time

    # ~110KB single line: 2000 exactly-40-char aws-secret-shaped tokens with
    # credential context on the line.
    tokens = " ".join(
        f"x{i} = 'aBcDeFgH1jKlMnOpQrStUvWxYz0123456789abc{i % 10:01d}'" for i in range(2000)
    )
    content = "api_secret_map = " + tokens
    start = time.monotonic()
    hits = _fallback_scan(content)
    elapsed = time.monotonic() - start
    assert hits, "credential-context line must still flag"
    assert elapsed < 0.5, f"fallback scan took {elapsed:.3f}s on a token-dense line"


def test_fallback_scan_line_numbers_match_old_resolution():
    content = "first = 1\nsecond = 2\napi_key = 'aBcDeFgH1jKlMnOpQrStUvWxYz0123456789'\n"
    hits = _fallback_scan(content)
    assert hits, "shaped token on a credential line must flag"
    for h in hits:
        assert h["line_number"] == content.count("\n", 0, h["position"]) + 1


def test_fallback_scan_context_gate_still_blocks_bare_identifiers():
    # A 40-char camelCase identifier with no credential context must stay
    # un-flagged (the context gate the cache now answers per line).
    content = "const adminListingNotesCreateRequestDescriptor = factory()\n"
    assert _fallback_scan(content) == []
