"""Secret scanner for canonical excerpts.

Wraps `detect-secrets` (vendored, version-pinned) to ensure canonical
files do not leak credentials when bootstrapped into committed
canonicals.json.

Round 1 + Round 4 critical security mitigation. detect-secrets rules
must be vendored at known version + quarterly-bumped per MAINTAINER.md.

Phase 1C: stub returning "no hits". Phase 4: full integration.

See ARCHITECTURE.md "Security mitigations" #1 + Round 4 changelog.
"""

from __future__ import annotations


def scan_for_secrets(content: str) -> list[dict]:
    """Return list of detected secrets in content. Empty list = safe.

    Phase 1C stub: always returns []. Phase 4 wires up detect_secrets.
    """
    # TODO Phase 4: from detect_secrets.core.scan import scan_line
    # TODO Phase 4: vendor detect-secrets rule files at known version
    # TODO Phase 4: extend with EF-specific patterns (AWS account IDs, internal hostnames)
    return []


def is_safe_canonical(content: str) -> bool:
    """Convenience: True iff scan_for_secrets(content) is empty."""
    return not scan_for_secrets(content)
