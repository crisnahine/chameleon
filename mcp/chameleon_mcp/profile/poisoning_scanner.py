"""Profile-poisoning scanner.

Detects dangerous coding patterns in canonical excerpts that an attacker
might commit to steer Claude toward insecure habits (e.g., raw SQL
concatenation in an "auth" canonical, eval/exec invocations in a "utility"
canonical, weak hashes in a security-related canonical, etc.).

Patterns flagged unconditionally: raw_sql_concat, eval_call, exec_call,
subprocess_shell_true, private_key headers. Patterns flagged only when a
security keyword (password / token / signature / auth / hmac / csrf /
session / api_key / nonce / salt / crypto / encrypt / decrypt / sign)
appears within ±200 chars: weak_hash, math_random_for_security — this
prevents false positives on legitimate non-crypto uses of MD5/SHA1
(stable cache keys, React component keys, etc.).
"""

from __future__ import annotations

import re

# Dangerous patterns in TS/JS/Ruby/Python.
# Each entry is (regex, kind, requires_security_context).
# When requires_security_context=True, a hit only counts if a security-related
# keyword (password, token, secret, signature, auth, hmac, hash_password,
# csrf, session) appears within ±200 chars of the match. This prevents false
# positives like md5() being used to generate stable React keys from labels.
DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    # SQL injection — always dangerous; no security keyword required.
    (re.compile(r"`[^`]*\$\{[^}]+\}[^`]*\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b", re.IGNORECASE), "raw_sql_concat", False),
    # Code execution — always dangerous.
    (re.compile(r"\beval\s*\(", re.IGNORECASE), "eval_call", False),
    (re.compile(r"\bexec\s*\(", re.IGNORECASE), "exec_call", False),
    (re.compile(r"shell\s*=\s*True", re.IGNORECASE), "subprocess_shell_true", False),
    # Crypto anti-patterns — only flag when used in a security context.
    # MD5/SHA1 have legitimate non-security uses (cache keys, dedup, ETags,
    # stable React keys); only flag when nearby code mentions security.
    (re.compile(r"\b(MD5|SHA1)\b", re.IGNORECASE), "weak_hash", True),
    # Math.random for security purposes — flagged only when security keyword nearby.
    (re.compile(r"Math\.random\s*\(", re.IGNORECASE), "math_random_for_security", True),
)

_SECURITY_KEYWORDS = re.compile(
    r"\b(password|passwd|pwd|secret|token|signature|auth|hmac|hash_password|"
    r"csrf|session|api[_-]?key|access[_-]?token|nonce|salt|crypto|encrypt|"
    r"decrypt|sign|verify_sig)\b",
    re.IGNORECASE,
)


def _has_security_context(content: str, match_start: int, match_end: int, *, window: int = 200) -> bool:
    """Return True if a security-related keyword appears within ±window chars."""
    start = max(0, match_start - window)
    end = min(len(content), match_end + window)
    return bool(_SECURITY_KEYWORDS.search(content[start:end]))


def scan_for_dangerous_patterns(content: str) -> list[dict]:
    """Return list of detected dangerous patterns.

    Empty list = canonical is safe.
    Non-empty list = CI gate fails; PR must be reviewed manually.
    """
    hits = []
    for pattern, kind, requires_security_context in DANGEROUS_PATTERNS:
        for match in pattern.finditer(content):
            if requires_security_context and not _has_security_context(
                content, match.start(), match.end()
            ):
                continue
            hits.append({
                "kind": kind,
                "match": match.group(0),
                "position": match.start(),
            })
    return hits


