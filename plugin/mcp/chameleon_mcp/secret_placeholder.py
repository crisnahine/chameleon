"""Placeholder-secret classification (leaf module, stdlib-only).

The Secret Keyword / password_assignment filter needs a per-VALUE verdict on
whether a flagged token is an obvious placeholder (a test/example marker, or a
low-entropy placeholder-shaped value) rather than a real-looking credential. This
lives in a dependency-free leaf module so the secret scanner can compute the
verdict at SCAN time from the exact flagged token -- which it then discards,
never storing the raw value -- without a circular import back into lint_engine.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# Values that are clearly NOT a credential -- test/example markers only. A weak
# but PLAUSIBLY-REAL password (`admin`, `password123`, `123456`, `s3cr3t`,
# `changeme`, a bare `secret`/`password`) is deliberately absent: committing one
# is a genuine credential leak the scanner must still flag. An exact match is
# unconditional (`test` is never a real secret); the PATTERNS below add an entropy
# backstop so a real key that merely starts with "your-" is not dropped.
_PLACEHOLDER_SECRET_VALUES = frozenset(
    {
        "test",
        "testing",
        "example",
        "dummy",
        "sample",
        "placeholder",
        "redacted",
        "none",
        "null",
        "nil",
        "todo",
        "tbd",
        "xxx",
        "foo",
        "bar",
        "baz",
        "fixme",
        "fake",
        "mock",
        "your-secret-key",
        "your_secret_key",
        "your-api-key",
        "your_api_key",
        "yoursecretkey",
        "notasecret",
        "not-a-secret",
    }
)
_PLACEHOLDER_SECRET_RE = re.compile(
    r"^(?:your[-_]|my[-_]secret|put[-_]your|xxx+)"
    r"|[-_]here$|<[^>]*>|\{\{.*\}\}|\$\{[^}]*\}|%\([^)]*\)|example\.(?:com|org)"
    # A delimited "test"/"testing"/"dummy"/"fake" SEGMENT marks a fixture value
    # ("gh-test-secret", "mg_test_key", "fake-token"). Segment-bounded so a
    # random credential merely containing the letters is untouched, and the
    # entropy gate below keeps a REAL test-mode key (Stripe's sk_test_<random>:
    # high entropy) flagged -- only low-entropy human-written placeholders drop.
    r"|(?:^|[-_.])(?:test(?:ing)?|dummy|fake)(?:[-_.]|$)"
    r"|^x+$|^0+$|^\.+$|^-+$",
    re.IGNORECASE,
)


def _shannon_entropy(s: str) -> float:
    n = len(s)
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def secret_value_is_placeholder(value: str) -> bool:
    """True when ``value`` is an obvious placeholder, never a real credential.

    Exact known placeholders (and empty) are unconditional; a placeholder-SHAPED
    value counts only when its entropy is low, so a real credential that merely
    starts with "your-" (high entropy) is never dropped. A strict function of the
    single token -- a real high-entropy secret is never in the placeholder set, so
    its verdict is always False.
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or v.lower() in _PLACEHOLDER_SECRET_VALUES:
        return True
    # A credential of one to three characters does not exist; "pw"-style
    # dummies in test fixtures fired error-severity findings on a real run.
    if len(v) <= 3 and v.isalpha():
        return True
    return bool(_PLACEHOLDER_SECRET_RE.search(v)) and _shannon_entropy(v) < 3.5


def value_looks_secretish(value: str) -> bool:
    """True when a value could plausibly be a REAL credential (long, or random).

    Used as a co-located guard: detect-secrets flags a multi-assignment line only
    ONCE, so if the flagged token is a placeholder but a real secret sits under a
    different key on the same line, the placeholder verdict must NOT drop the line.
    Erring toward True is the safe direction (an advisory kept, never a real secret
    silently dropped). A short low-entropy value (a username like "eric") is not
    secretish."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if secret_value_is_placeholder(v):
        return False
    return len(v) >= 20 or _shannon_entropy(v) >= 3.5


# An assignment TARGET that names a credential (`token = "s3cr3t"`,
# `db_password: "..."`). A non-placeholder value under such a key is a real secret
# even when it is short and low-entropy -- unlike a co-located `username`/`email`/
# `host`, which is not. Mirrors detect-secrets' keyword denylist so the co-located
# guard shares the detector's own notion of a secret-bearing key.
_SECRET_KEY_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])"
    r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|apikey"
    r"|access[_-]?key|secret[_-]?key|client[_-]?secret|api[_-]?secret"
    r"|private[_-]?key|credential|auth[_-]?token)"
    r"(?:[^A-Za-z0-9]|$)",
    re.IGNORECASE,
)
# A `key = "value"` / `"key": "value"` assignment: the key may be bare or quoted
# (dict-literal form), the value is a single/double-quoted string with an optional
# str-prefix. Group 1 = key identifier, group 2 = value.
_KEYVAL_RE = re.compile(r"""(\w[\w-]*)['"]?\s*[:=]\s*(?:[rbfuRBFU]{0,2})['"]([^'"]*)['"]""")


def line_has_colocated_real_secret(line: str) -> bool:
    """True when a line carries a plausibly-REAL credential besides the flagged
    placeholder token.

    A `key = "value"` counts as a real secret when the value is NOT a placeholder
    and either the key is credential-named (`token`, `db_password`, `api_key`) or
    the value itself looks secretish (high-entropy / long). A non-placeholder value
    under a NON-secret key (`username = "eric"`) is not a secret, so the common
    `login(username="x", password="test")` false positive still drops. This lets a
    WEAK real secret (`token = "s3cr3t"`) co-located with a placeholder survive the
    per-value filter, which ``value_looks_secretish`` alone (length/entropy only)
    would miss. Non-str input is safe (returns False)."""
    if not isinstance(line, str):
        return False
    for key, val in _KEYVAL_RE.findall(line):
        if secret_value_is_placeholder(val):
            continue
        if _SECRET_KEY_RE.search(key) or value_looks_secretish(val):
            return True
    return False
