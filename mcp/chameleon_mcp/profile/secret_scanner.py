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


# The two shape-only fallback patterns below match ordinary code with no
# credential context: any 40-char base64 run is a long camelCase identifier
# (e.g. adminListingNotesCreateRequestDescriptor) and any 40+ hex run is a git
# SHA or a sourcemap hash. Both are advisory-only, so on a real repo they flood
# the advisory tail without ever blocking. Keep one of their matches only when
# the line that holds it also names a credential, so a true secret assignment
# (api_token = "...") still flags while bare identifiers and hashes do not.
_CONTEXT_GATED_KINDS = frozenset({"possible_aws_secret", "high_entropy_hex"})

_CREDENTIAL_CONTEXT = re.compile(
    r"secret|key|token|password|passwd|credential|auth|apikey|api_key|access|private",
    re.IGNORECASE,
)


def _line_at(content: str, position: int) -> str:
    """Return the full source line that contains the byte offset `position`."""
    start = content.rfind("\n", 0, position) + 1
    end = content.find("\n", position)
    if end == -1:
        end = len(content)
    return content[start:end]


def _line_number_at(content: str, position: int) -> int:
    """1-based line number for the char offset `position`.

    The deterministic fallback patterns match on the whole-buffer offset, but
    every downstream consumer (the lint violation formatter, the PR-review hunk
    gate) reasons in lines. Counting the newlines up to the offset gives the same
    line a line-keyed diff map uses, so a hard-kind secret can be placed inside a
    changed hunk rather than reported as a bare character position.
    """
    return content.count("\n", 0, position) + 1


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
            if kind in _CONTEXT_GATED_KINDS and not _CREDENTIAL_CONTEXT.search(
                _line_at(content, match.start())
            ):
                continue
            hits.append(
                {
                    "type": kind,
                    "position": match.start(),
                    # Also carry the line so a hard-kind secret (these are the only
                    # source of the deterministic block-eligible kinds) can be
                    # mapped into a diff hunk; ``position`` is retained for the
                    # (type, position) dedup key and any offset-based caller.
                    "line_number": _line_number_at(content, match.start()),
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
