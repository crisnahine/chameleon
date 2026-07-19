"""Profile-poisoning scanner.

Detects dangerous coding patterns in canonical excerpts that an attacker
might commit to steer Claude toward insecure habits (e.g., raw SQL
concatenation in an "auth" canonical, eval/exec invocations in a "utility"
canonical, weak hashes in a security-related canonical, etc.).

Patterns flagged unconditionally: raw_sql_concat, eval_call, exec_call,
subprocess_shell_true. Patterns flagged only when a
security keyword (password / token / signature / auth / hmac / csrf /
session / api_key / nonce / salt / crypto / encrypt / decrypt / sign)
appears within ±200 chars: weak_hash, math_random_for_security — this
prevents false positives on legitimate non-crypto uses of MD5/SHA1
(stable cache keys, React component keys, etc.).
"""

from __future__ import annotations

import re

# A SQL STATEMENT shape: a verb together with its mandatory clause keyword. This
# is far more specific than a lone verb -- benign English ("for update",
# "Selected") lacks the clause keyword -- so interpolation NEXT TO a real SQL
# statement is the signal, not interpolation next to any SQL-ish word.
_SQL_STMT = (
    r"(?:SELECT\b[^\n]{0,200}?\bFROM\b"
    r"|INSERT\s+INTO\b"
    r"|UPDATE\b[^\n]{0,200}?\bSET\b"
    r"|DELETE\s+FROM\b"
    r"|DROP\s+(?:TABLE|DATABASE|INDEX|SCHEMA|VIEW)\b)"
)

DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (
        re.compile(
            r"`[^`]*(?:\b(?:SELECT|INSERT|UPDATE|DELETE|DROP)\b[^`]*\$\{[^}]+\}|"
            r"\$\{[^}]+\}[^`]*\b(?:SELECT|INSERT|UPDATE|DELETE|DROP)\b)",
            re.IGNORECASE,
        ),
        "raw_sql_concat",
        False,
    ),
    # Ruby string interpolation (`"... #{x} ..."`) of a value inside a SQL
    # STATEMENT is the same injection class as the TS backtick case. The match
    # requires a full statement shape (verb + its clause keyword: SELECT..FROM,
    # INSERT INTO, UPDATE..SET, DELETE FROM, DROP TABLE/...), not a bare verb, so
    # SQL verbs occurring as ordinary English words ("for update", "Selected")
    # near an interpolation do not false-positive -- which was poisoning
    # canonical-witness selection on Rails repos.
    (
        re.compile(
            _SQL_STMT + r"[^\n]{0,200}#\{[^}]*\}|#\{[^}]*\}[^\n]{0,200}" + _SQL_STMT,
            re.IGNORECASE,
        ),
        "raw_sql_concat",
        False,
    ),
    # Python f-string (`f"... {x} ..."`) of a value inside a SQL statement.
    # Anchored on any f-string prefix -- f, rf, fr (a plain parameterized string
    # is safe) -- with the same statement-shape requirement as the Ruby case.
    (
        re.compile(
            r"(?:[fF][rR]?|[rR][fF])['\"][^\n]{0,240}(?:" + _SQL_STMT + r"[^\n]{0,200}\{[^}]+\}|"
            r"\{[^}]+\}[^\n]{0,200}" + _SQL_STMT + r")",
            re.IGNORECASE,
        ),
        "raw_sql_concat",
        False,
    ),
    (re.compile(r"\beval\s*\(", re.IGNORECASE), "eval_call", False),
    (re.compile(r"\bexec\s*\(", re.IGNORECASE), "exec_call", False),
    (re.compile(r"shell\s*=\s*True", re.IGNORECASE), "subprocess_shell_true", False),
    (re.compile(r"\b(MD5|SHA1)\b", re.IGNORECASE), "weak_hash", True),
    (re.compile(r"Math\.random\s*\(", re.IGNORECASE), "math_random_for_security", True),
)

_SECURITY_KEYWORDS = re.compile(
    r"\b(password|passwd|pwd|secret|token|signature|auth|hmac|hash_password|"
    r"csrf|session|api[_-]?key|access[_-]?token|nonce|salt|crypto|encrypt|"
    r"decrypt|sign|verify_sig)\b",
    re.IGNORECASE,
)


def _has_security_context(
    content: str, match_start: int, match_end: int, *, window: int = 200
) -> bool:
    """Return True if a security-related keyword appears within ±window chars."""
    start = max(0, match_start - window)
    end = min(len(content), match_end + window)
    return bool(_SECURITY_KEYWORDS.search(content[start:end]))


# An interpolation slot holding a SCREAMING_SNAKE_CASE identifier: the
# near-universal convention for a module constant. Digits and underscores are
# allowed after the first letter (COLUMNS, TABLE_V2, ORDER_BY_CLAUSE).
_CONSTANT_SLOT = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Every interpolation slot shape the three raw_sql_concat arms can match:
# TypeScript `${...}`, Ruby `#{...}`, Python f-string `{...}`.
_INTERPOLATION_SLOT = re.compile(r"\$\{([^}]*)\}|#\{([^}]*)\}|\{([^{}]*)\}")


def _interpolates_only_constants(matched_sql: str) -> bool:
    """True when every interpolation in the matched SQL is a module constant.

    Composing a query from a `const TABLE` / `const COLUMNS` while user values go
    through $1/$2 placeholders is the standard, safe data-access shape, not
    injection. Flagging it excluded every file in a repositories cohort from
    canonical-witness selection, which dropped the whole archetype and left edits
    there imitating a validator -- the security scanner degrading exactly the
    layer where SQL-injection mistakes happen.

    Deliberately conservative: this is a NAMING-convention judgement, not a data-
    flow analysis, so a single non-constant slot (`${userId}`, `${req.query.x}`,
    `${getId()}`, or a lower-case `${table}`) keeps the whole statement flagged --
    one safe constant never launders an unsafe sibling. A value that is both
    user-controlled AND named in SCREAMING_SNAKE_CASE would be missed; that is
    rare, against convention, and the trade for not deleting real archetypes.
    A match with no interpolation at all is not exempted (it cannot reach here --
    every arm requires a slot).
    """
    slots = [
        next(g for g in groups if g is not None)
        for groups in _INTERPOLATION_SLOT.findall(matched_sql)
    ]
    if not slots:
        return False
    return all(_CONSTANT_SLOT.match(s.strip()) for s in slots)


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
            if kind == "raw_sql_concat" and _interpolates_only_constants(match.group(0)):
                continue
            hits.append(
                {
                    "kind": kind,
                    "match": match.group(0),
                    "position": match.start(),
                }
            )
    return hits
