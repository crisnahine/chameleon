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
    # GitLab token family (PAT, deploy, feed, SOAT, runner). detect-secrets
    # 1.5.0 covers these too; the fallback keeps parity with the GitHub entry
    # above so GitLab tokens stay caught if detect-secrets is ever unavailable.
    (re.compile(r"\b(glpat|gldt|glft|glsoat|glrt)-[A-Za-z0-9_\-]{20,50}\b"), "gitlab_token"),
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

    from chameleon_mcp._thresholds import threshold_int

    # detect-secrets re-scans the whole line through its allowlist regexes for
    # every candidate it yields, so a token-dense single line (minified bundle,
    # generated const map) costs O(candidates x length) — tens of seconds at
    # 100KB. Lines past the cap are left to the deterministic fallback patterns,
    # which scan linearly and carry every block-eligible secret kind.
    max_line_len = threshold_int("SECRET_SCAN_MAX_LINE_LEN")

    hits: list[dict[str, Any]] = []
    try:
        with default_settings():
            for line_no, line in enumerate(content.splitlines(), start=1):
                if len(line) > max_line_len:
                    continue
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


def _scan_patterns(
    content: str, patterns: list[tuple[re.Pattern[str], str]] | tuple
) -> list[dict[str, Any]]:
    """Linear regex scan over ``patterns``, one hit dict per match.

    Shared machinery for the full fallback set and the hard-kind fast path.
    """
    import bisect

    hits: list[dict[str, Any]] = []
    # Resolving each hit's line with rfind/count re-scanned the whole buffer
    # per hit — O(hits x content length), a multi-hundred-ms stall on a
    # token-dense single line. One offset table + a per-line context-verdict
    # cache makes each hit O(log lines).
    line_starts = [0]
    nl = content.find("\n")
    while nl != -1:
        line_starts.append(nl + 1)
        nl = content.find("\n", nl + 1)
    context_ok_by_line: dict[int, bool] = {}

    def _line_index(position: int) -> int:
        return bisect.bisect_right(line_starts, position) - 1

    def _credential_context_at(idx: int) -> bool:
        cached = context_ok_by_line.get(idx)
        if cached is None:
            start = line_starts[idx]
            end = line_starts[idx + 1] - 1 if idx + 1 < len(line_starts) else len(content)
            cached = bool(_CREDENTIAL_CONTEXT.search(content, start, end))
            context_ok_by_line[idx] = cached
        return cached

    for pattern, kind in patterns:
        for match in pattern.finditer(content):
            idx = _line_index(match.start())
            if kind in _CONTEXT_GATED_KINDS and not _credential_context_at(idx):
                continue
            hits.append(
                {
                    "type": kind,
                    "position": match.start(),
                    # Also carry the line so a hard-kind secret (these are the only
                    # source of the deterministic block-eligible kinds) can be
                    # mapped into a diff hunk; ``position`` is retained for the
                    # (type, position) dedup key and any offset-based caller.
                    "line_number": idx + 1,
                    "secret_value": "<redacted>",
                }
            )
    return hits


def _fallback_scan(content: str) -> list[dict[str, Any]]:
    """Regex-based scan when detect-secrets is unavailable."""
    return _scan_patterns(content, _FALLBACK_PATTERNS)


# Lazily-filtered subset of _FALLBACK_PATTERNS whose kinds may hard-block.
# Resolved on first use because the kind set lives in violation_class, which
# must stay importable without pulling this module (and vice versa).
_HARD_PATTERNS: list[tuple[re.Pattern[str], str]] | None = None


def scan_for_hard_secrets(content: str) -> list[dict[str, Any]]:
    """Scan only for the deterministic hard-block secret kinds. Regex-only.

    The fast path for latency-sensitive callers (the pre-write deny, the
    corrections-exhausted block gate): never imports or runs detect-secrets,
    whose per-line allowlist re-scan is quadratic on token-dense lines. This
    loses nothing for the block decision — hard-blockable kinds only ever
    originate from the fallback patterns (detect-secrets type strings never
    parse into the deterministic kind set) — and no hard kind is context-gated,
    so the credential-keyword gate in the shared scan never filters here.
    Emits the same hit dict shape as ``_fallback_scan``.
    """
    global _HARD_PATTERNS
    if _HARD_PATTERNS is None:
        from chameleon_mcp.violation_class import _DETERMINISTIC_SECRET_KINDS

        _HARD_PATTERNS = [
            (pattern, kind)
            for pattern, kind in _FALLBACK_PATTERNS
            if kind in _DETERMINISTIC_SECRET_KINDS
        ]
    return _scan_patterns(content, _HARD_PATTERNS)


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
