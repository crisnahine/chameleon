"""Unit tests for broadened secret coverage and de-obfuscation folding.

Covers the new deterministic fallback patterns (Google API key, GCP service-
account marker, Azure AccountKey) and the cross-quote / array-join additions to
`_fold_string_concat`, including the end-to-end path where a token split across
quote styles or assembled via `.join('')` is reassembled before scanning.
"""

from __future__ import annotations

# chameleon-ignore-file secret-detected-in-content
# This is the secret scanner's OWN test suite: it must embed fake credentials
# (dummy AWS/GitHub/Stripe tokens) as literals so the detector and the concat
# de-obfuscation fold can be exercised. None are real secrets. The named
# file-scope directive is the sanctioned escape for exactly such fixtures.
from chameleon_mcp.lint_engine import _fold_string_concat, scan_hard_secrets, scan_secrets
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


class TestHardSecretReportsLine:
    """A hard-kind secret's formatted violation cites a line, not a char offset.

    The deterministic kinds (the only block-eligible secrets) used to surface a
    `position N` offset, which a line-keyed diff hunk map can't place. The
    formatter must read the line the fallback scan now carries.
    """

    def test_hard_kind_actual_says_line_not_position(self):
        # AKIA on line 4 of the buffer.
        content = "a = 1\nb = 2\nc = 3\n" + 'aws = "AKIAIOSFODNN7EXAMPLE"\n'
        vs = scan_secrets(content)
        hard = [v for v in vs if "aws_access_key at " in v.actual]
        assert hard
        assert "at line 4" in hard[0].actual
        assert "at position" not in hard[0].actual

    def test_github_token_actual_says_line(self):
        pat = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"
        content = "\n" * 5 + f'token = "{pat}"\n'
        vs = scan_secrets(content)
        hard = [v for v in vs if "github_token at " in v.actual]
        assert hard
        assert "at line 6" in hard[0].actual


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

    def test_string_dense_large_content_is_fast(self):
        # Regression: an atom whose body swallowed newlines turned every quote in
        # a docstring-heavy file into a giant multi-line "adjacency", making the
        # fold multi-second on ~100KB. Newline-bounded atoms keep it near-linear.
        # A docstring/string-dense 100KB blob must fold well under a generous bound.
        import time

        blob = (
            'def f():\n    """doc "quoted" text over\n    many lines"""\n    x = "a" + "b"\n' * 1200
        )[:100_000]
        start = time.perf_counter()
        _fold_string_concat(blob)
        assert time.perf_counter() - start < 1.0  # real cost ~60ms; 1s is slack for CI


class TestSplitSecretForms:
    """Every string-splitting form must reconstruct a hardcoded AWS key so the
    hard-secret scan sees it (regression for the concat-fold evasion sweep)."""

    def _denies(self, code: str) -> bool:
        return bool(scan_hard_secrets(_fold_string_concat(code)))

    def test_bypass_forms_all_fold_to_a_hit(self):
        forms = [
            "k = `AKIA` + `ABCDEFGHIJKLMNOP`",  # backtick concat
            "k = \"AKIA\" + 'ABCDEFGHIJKLMNOP'",  # cross quote
            'k = f"AKIA" + f"ABCDEFGHIJKLMNOP"',  # f-string prefix
            'k = r"AKIA" + b"ABCDEFGHIJKLMNOP"',  # r/b prefixes
            'k = """AKIA""" + "ABCDEFGHIJKLMNOP"',  # triple + single mix
            'k = "AKIA" "ABCDEFGHIJKLMNOP"',  # implicit adjacency (Python/Ruby)
            'k = "AKIA""ABCDEFGHIJKLMNOP"',  # adjacency, no space
            'k = "".join(["AKIA", "ABCDEFGHIJKLMNOP"])',  # Python join order
            'k = ["AKIA", "ABCDEFGHIJKLMNOP"].join("")',  # JS join order
            "k = %w[AKIA ABCDEFGHIJKLMNOP].join",  # Ruby word array
            'k = `AKIA${""}ABCDEFGHIJKLMNOP`',  # template empty interpolation
            "k = `AKIA${}ABCDEFGHIJKLMNOP`",  # template bare ${}
            r'k = "\x41\x4b\x49\x41" + "ABCDEFGHIJKLMNOP"',  # hex-escaped head + concat
        ]
        for code in forms:
            assert self._denies(code), f"split form not folded to a hit: {code!r}"

    def test_known_limits_are_not_claimed_fixed(self):
        # Documented residuals of a lint-time regex heuristic (no taint analysis,
        # no runtime evaluation). These are NOT caught; the assertion pins the
        # boundary so a future change that DOES catch them updates this test
        # deliberately rather than by accident.
        data_flow = 'parts = ["AKIA", "ABCDEFGHIJKLMNOP"]\nk = "".join(parts)'
        single_all_hex = (
            r'k = "\x41\x4b\x49\x41\x41\x42\x43\x44\x45\x46'
            r'\x47\x48\x49\x4a\x4b\x4c\x4d\x4e\x4f\x50"'
        )
        assert not self._denies(data_flow)  # needs variable taint tracking
        assert not self._denies(single_all_hex)  # single lone literal, never folded

    def test_legit_code_does_not_false_positive(self):
        clean = [
            "const u = `https://api/${id}/x`;",  # real interpolation
            'msg = "hello" + "world"',
            'msg = "error: " "not found"',  # adjacent non-secret
            'xs = ["apple", "banana", "cherry"]',
            'connect("localhost", "8080")',
            'p = "/".join(["usr", "local"])',  # real separator
            "['a', 'b'].join('-')",  # non-empty separator
            "['a', x, 'b'].join('')",  # non-literal element
            'langs = %w[ruby python go].join(",")',
        ]
        for code in clean:
            assert not self._denies(code), f"false positive on: {code!r}"
