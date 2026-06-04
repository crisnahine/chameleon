"""Unit tests for broadened secret coverage and de-obfuscation folding.

Covers the new deterministic fallback patterns (Google API key, GCP service-
account marker, Azure AccountKey) and the cross-quote / array-join additions to
`_fold_string_concat`, including the end-to-end path where a token split across
quote styles or assembled via `.join('')` is reassembled before scanning.
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import _fold_string_concat, scan_secrets
from chameleon_mcp.profile.secret_scanner import scan_for_secrets


def _types(content: str) -> set[str]:
    return {h.get("type") for h in scan_for_secrets(content)}


class TestNewFallbackPatterns:
    def test_google_api_key(self):
        # AIza prefix + 35-char body is the fixed Google API key shape.
        key = "AIza" + "B" * 35
        assert "google_api_key" in _types(f'const k = "{key}";')

    def test_google_api_key_too_short_ignored(self):
        # A near-miss (short body) must not trip the fixed-length pattern.
        assert "google_api_key" not in _types("AIza" + "B" * 10)

    def test_gcp_service_account_marker(self):
        blob = '{"type": "service_account", "project_id": "demo"}'
        assert "gcp_service_account" in _types(blob)

    def test_gcp_service_account_single_quote_marker(self):
        blob = "{'type': 'service_account'}"
        assert "gcp_service_account" in _types(blob)

    def test_azure_account_key(self):
        conn = "DefaultEndpointsProtocol=https;AccountKey=" + "A" * 40 + ";"
        assert "azure_account_key" in _types(conn)

    def test_clean_code_still_clean(self):
        # The new patterns must not false-positive on ordinary source.
        samples = [
            "type Foo = { id: string };",
            "type: 'submit'",
            "const account = { key: lookup() };",
        ]
        for src in samples:
            assert scan_for_secrets(src) == [], src


class TestCrossQuoteFold:
    def test_dq_then_sq(self):
        assert _fold_string_concat("\"a\" + 'b'") == '"ab"'

    def test_sq_then_dq(self):
        assert _fold_string_concat("'a' + \"b\"") == '"ab"'

    def test_inner_quote_reescaped(self):
        # A double-quote inside the joined body must be escaped so the emitted
        # literal stays well-formed and does not swallow following text.
        out = _fold_string_concat('\'say "hi"\' + "!"')
        assert out == '"say \\"hi\\"!"'

    def test_chains_with_same_quote(self):
        # Mixed and same-quote folds interleave across passes.
        assert _fold_string_concat('"a" + \'b\' + "c"') == '"abc"'


class TestArrayJoinFold:
    def test_empty_separator(self):
        assert _fold_string_concat("['a', 'b', 'c'].join('')") == '"abc"'

    def test_double_quote_empty_separator(self):
        assert _fold_string_concat('["a", "b"].join("")') == '"ab"'

    def test_bare_join(self):
        assert _fold_string_concat("['a', 'b'].join()") == '"ab"'

    def test_trailing_comma(self):
        assert _fold_string_concat("['a', 'b',].join('')") == '"ab"'

    def test_nonempty_separator_not_folded(self):
        original = "['a', 'b'].join('-')"
        assert _fold_string_concat(original) == original

    def test_nonliteral_element_not_folded(self):
        original = "['a', x, 'b'].join('')"
        assert _fold_string_concat(original) == original


class TestSplitSecretDetection:
    def test_cross_quote_github_pat(self):
        pat = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"
        src = f"token = 'ghp_' + \"{pat[4:]}\""
        vs = scan_secrets(src)
        assert any(v.rule == "secret-detected-in-content" for v in vs)
        assert any("string-concat fold" in v.actual for v in vs)

    def test_array_join_stripe_key(self):
        key = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
        src = f"k = ['sk_live_', \"{key[8:]}\"].join('')"
        vs = scan_secrets(src)
        assert any(v.rule == "secret-detected-in-content" for v in vs)

    def test_split_google_key_via_array(self):
        body = "B" * 35
        src = f"k = ['AIza', '{body}'].join('')"
        assert any(v.rule == "secret-detected-in-content" for v in scan_secrets(src))


class TestFoldRobustness:
    def test_empty_content(self):
        assert _fold_string_concat("") == ""

    def test_no_fold_tokens_passthrough(self):
        original = "const x = 1;"
        assert _fold_string_concat(original) == original

    def test_pathological_input_stays_bounded(self):
        # Linear-time guarantee: a long alternating chain must not hang.
        chain = ("'a' + \"b\" + " * 500) + '"c"'
        _fold_string_concat(chain)
