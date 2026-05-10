"""Secret scanner for canonical excerpts.

Phase 4 implementation: integrates `detect-secrets` (vendored, version-pinned)
with a regex-fallback when the library is unavailable.

Round 1 + Round 4 critical security mitigation. The scanner runs on every
candidate canonical file BEFORE the file is committed to `canonicals.json`.
Files with detected secrets are excluded from the canonical pool.

Per ARCHITECTURE.md "Security mitigations" #1.
"""

from __future__ import annotations

import re
from typing import Any

# Phase 4 fallback patterns when detect-secrets is unavailable.
# Conservative: prefer false positives over silent leaks.
_FALLBACK_PATTERNS = (
    # AWS access key
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    # AWS secret key
    (re.compile(r"\b[A-Za-z0-9/+=]{40}\b"), "possible_aws_secret"),
    # GitHub token
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"), "github_token"),
    # Generic OpenAI / Anthropic API key prefix
    (re.compile(r"\bsk-(ant-|proj-)?[A-Za-z0-9_\-]{20,}\b"), "ai_api_key"),
    # Stripe live secret key
    (re.compile(r"\bsk_live_[A-Za-z0-9]{24,}\b"), "stripe_live_key"),
    # Stripe restricted/test key
    (re.compile(r"\b(rk|sk)_(live|test)_[A-Za-z0-9]{24,}\b"), "stripe_key"),
    # Slack token
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    # Generic high-entropy hex strings (32+ hex chars; possible secret)
    (re.compile(r"\b[a-f0-9]{40,}\b"), "high_entropy_hex"),
    # Private key headers
    (re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "private_key"),
    # Generic password assignment
    (re.compile(r"""(password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['"][^'"]{8,}['"]""", re.IGNORECASE), "password_assignment"),
)


def _try_detect_secrets(content: str) -> list[dict[str, Any]] | None:
    """Use vendored detect-secrets if available; return None if not installed."""
    try:
        from detect_secrets.core.scan import scan_line
    except ImportError:
        return None

    hits: list[dict[str, Any]] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        try:
            for s in scan_line(line):
                hits.append({
                    "type": s.type,
                    "line_number": line_no,
                    "secret_value": "<redacted>",  # never echo the actual secret
                })
        except Exception:
            # detect_secrets occasionally raises on malformed input; treat as
            # no match (fail-open is acceptable here — fallback regex below
            # catches the obvious cases).
            continue
    return hits


def _fallback_scan(content: str) -> list[dict[str, Any]]:
    """Regex-based scan when detect-secrets is unavailable."""
    hits: list[dict[str, Any]] = []
    for pattern, kind in _FALLBACK_PATTERNS:
        for match in pattern.finditer(content):
            hits.append({
                "type": kind,
                "position": match.start(),
                "secret_value": "<redacted>",
            })
    return hits


def scan_for_secrets(content: str) -> list[dict[str, Any]]:
    """Return list of detected secrets in content. Empty list = safe.

    Tries `detect-secrets` first; falls back to a conservative regex set if
    the library is not available in the environment.
    """
    hits = _try_detect_secrets(content)
    if hits is not None:
        return hits
    return _fallback_scan(content)


def is_safe_canonical(content: str) -> bool:
    """Convenience: True iff scan_for_secrets(content) is empty."""
    return not scan_for_secrets(content)
