"""Secret scanner for canonical excerpts.

Phase 4 implementation: integrates `detect-secrets` (vendored, version-pinned)
with a regex-fallback when the library is unavailable.

Round 1 + Round 4 critical security mitigation. The scanner runs on every
candidate canonical file BEFORE the file is committed to `canonicals.json`.
Files with detected secrets are excluded from the canonical pool.

Per docs/architecture.md "Security mitigations" #1.
"""

from __future__ import annotations

import re
from typing import Any

_FALLBACK_PATTERNS = (
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    (re.compile(r"\b[A-Za-z0-9/+=]{40}\b"), "possible_aws_secret"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"), "github_token"),
    (re.compile(r"\bsk-(ant-|proj-)?[A-Za-z0-9_\-]{20,}\b"), "ai_api_key"),
    (re.compile(r"\bsk_live_[A-Za-z0-9]{24,}\b"), "stripe_live_key"),
    (re.compile(r"\b(rk|sk)_(live|test)_[A-Za-z0-9]{24,}\b"), "stripe_key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    # Google API key: the AIza prefix is unique to Google credentials and the
    # 35-char body is fixed-length, so the shape is deterministic with little
    # room for a benign collision.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "google_api_key"),
    # GCP service-account key files are JSON carrying this exact type marker
    # alongside a private_key. Matching the marker catches the whole file even
    # when the embedded key itself is split or base64-wrapped.
    (
        re.compile(r"""["']type["']\s*:\s*["']service_account["']"""),
        "gcp_service_account",
    ),
    # Azure Storage / Service Bus connection strings expose the shared key via
    # an AccountKey= segment terminated by ';' or end-of-string.
    (re.compile(r"\bAccountKey=[A-Za-z0-9+/=]{16,}"), "azure_account_key"),
    (re.compile(r"\b[a-f0-9]{40,}\b"), "high_entropy_hex"),
    (
        re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
        "private_key",
    ),
    (
        re.compile(
            r"""(password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['"][^'"]{8,}['"]""",
            re.IGNORECASE,
        ),
        "password_assignment",
    ),
)


_NOISY_DETECT_SECRETS_TYPES = frozenset(
    {
        "Base64 High Entropy String",
        "Hex High Entropy String",
    }
)


def _try_detect_secrets(content: str) -> list[dict[str, Any]] | None:
    try:
        from detect_secrets.core.scan import scan_line
        from detect_secrets.settings import default_settings
    except ImportError:
        return None

    hits: list[dict[str, Any]] = []
    try:
        with default_settings():
            for line_no, line in enumerate(content.splitlines(), start=1):
                try:
                    for s in scan_line(line):
                        if s.type in _NOISY_DETECT_SECRETS_TYPES:
                            continue
                        hits.append(
                            {
                                "type": s.type,
                                "line_number": line_no,
                                "secret_value": "<redacted>",
                            }
                        )
                except Exception:
                    continue
    except Exception:
        return None
    return hits


def _fallback_scan(content: str) -> list[dict[str, Any]]:
    """Regex-based scan when detect-secrets is unavailable."""
    hits: list[dict[str, Any]] = []
    for pattern, kind in _FALLBACK_PATTERNS:
        for match in pattern.finditer(content):
            hits.append(
                {
                    "type": kind,
                    "position": match.start(),
                    "secret_value": "<redacted>",
                }
            )
    return hits


def scan_for_secrets(content: str) -> list[dict[str, Any]]:
    """Return list of detected secrets in content. Empty list = safe.

    Runs BOTH detect-secrets (when available, broad coverage) AND the
    fallback regex set (deterministic, conservative). detect-secrets relies
    on entropy + context heuristics that may miss inline test fixtures or
    short example values; the fallback patterns catch those reliably.
    Hits are deduplicated by (type, position).
    """
    hits: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    detect_secrets_hits = _try_detect_secrets(content) or []
    for hit in detect_secrets_hits:
        key = (hit.get("type"), hit.get("line_number"), hit.get("position"))
        if key not in seen:
            seen.add(key)
            hits.append(hit)

    for hit in _fallback_scan(content):
        key = (hit.get("type"), hit.get("position"))
        if key not in seen:
            seen.add(key)
            hits.append(hit)

    return hits
