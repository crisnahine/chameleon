"""Profile-poisoning scanner — runs as CI gate on PRs that touch .chameleon/.

Detects dangerous coding patterns in canonical excerpts that an attacker
might commit to steer Claude toward insecure habits (e.g., raw SQL
concatenation in an "auth" canonical, missing CSRF middleware, etc.).

Round 4 Security Architect (red team) recommendation #6:
> "CI gate: chameleon-status --diff runs detect-secrets + dangerous-pattern
> checks (eval, exec, shell=True, raw SQL concat tokens, missing csrf
> middleware on auth-shaped functions) over canonical excerpts on every
> PR that touches .chameleon/."

Phase 1C: stub returning "no hits". Phase 4: full pattern set.
"""

from __future__ import annotations

import re

# Dangerous patterns in TS/JS/Ruby/Python.
# Phase 4 will expand based on EF code review history.
DANGEROUS_PATTERNS = (
    # SQL injection
    (re.compile(r"`[^`]*\$\{[^}]+\}[^`]*\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b", re.IGNORECASE), "raw_sql_concat"),
    # Code execution
    (re.compile(r"\beval\s*\(", re.IGNORECASE), "eval_call"),
    (re.compile(r"\bexec\s*\(", re.IGNORECASE), "exec_call"),
    (re.compile(r"shell\s*=\s*True", re.IGNORECASE), "subprocess_shell_true"),
    # Crypto anti-patterns
    (re.compile(r"\b(MD5|SHA1)\b", re.IGNORECASE), "weak_hash"),
    (re.compile(r"Math\.random\s*\(", re.IGNORECASE), "math_random_for_security"),
)


def scan_for_dangerous_patterns(content: str) -> list[dict]:
    """Return list of detected dangerous patterns.

    Empty list = canonical is safe.
    Non-empty list = CI gate fails; PR must be reviewed manually.
    """
    hits = []
    for pattern, kind in DANGEROUS_PATTERNS:
        for match in pattern.finditer(content):
            hits.append({
                "kind": kind,
                "match": match.group(0),
                "position": match.start(),
            })
    return hits


def is_safe_canonical(content: str) -> bool:
    """Convenience: True iff scan_for_dangerous_patterns(content) is empty."""
    return not scan_for_dangerous_patterns(content)
