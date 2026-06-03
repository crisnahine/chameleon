"""Classify lint violations for enforcement.

Only objective or explicitly-taught rules are block-eligible. Learned
structural/naming heuristics stay advisory regardless of escalation level,
because a wrong archetype match would make them spurious. ``phantom-import`` is
the only archetype-independent rule (it is a filesystem fact).
"""

from __future__ import annotations

import re

# Matches a `// chameleon-ignore <rule>` (TypeScript) or `# chameleon-ignore
# <rule>` (Ruby) directive. The optional `-file` suffix and the bare form (no
# rule) both parse; a bare directive means "ignore every block-eligible rule".
# The rule name must sit on the same line as the directive: the inter-token
# whitespace excludes newlines so a bare directive on its own line does not
# capture the first word of the following line as a rule.
_IGNORE_RE = re.compile(r"(?:#|//)[^\S\n]*chameleon-ignore(?:-file)?(?:[^\S\n]+([\w-]+))?")


def ignored_rules(content: str) -> set[str] | None:
    """Return the set of explicitly-ignored rule names, or None if there are none.

    A bare ``chameleon-ignore`` (no rule) contributes the empty string, which
    callers read as "ignore everything": membership of ``""`` downgrades any
    block-eligible rule on this file.
    """
    found: set[str] = set()
    for m in _IGNORE_RE.finditer(content):
        found.add(m.group(1) or "")
    return found or None


# Rules that MAY block, before per-repo self-calibration narrows the set.
# naming-convention-violation and inheritance-convention-violation are
# archetype-dependent: a wrong archetype match would make them spurious, so the
# block path gates them on confidence=high + match_quality=ast and per-repo
# calibration, same as the other dependent rules.
BLOCK_ELIGIBLE_RULES: frozenset[str] = frozenset(
    {
        "phantom-import",
        "import-preference-violation",
        "jsx-presence-mismatch",
        "naming-convention-violation",
        "inheritance-convention-violation",
    }
)

# Archetype-independent rules are true/false regardless of which archetype the
# file matched, so they need no confidence/match-quality gate.
_ARCHETYPE_INDEPENDENT: frozenset[str] = frozenset({"phantom-import"})


def is_archetype_independent(rule: str) -> bool:
    return rule in _ARCHETYPE_INDEPENDENT


def is_hard_class(violation: dict) -> bool:
    """True if this violation is block-eligible on its own merits.

    jsx-presence-mismatch is the only severity-gated rule: it qualifies only at
    severity ``error`` (file HAS JSX in a non-JSX archetype); the ``warning`` form
    (missing JSX, may be a stub) does not. Every other block-eligible rule
    qualifies regardless of severity, including naming/inheritance convention
    violations, which are always emitted at ``warning``.
    """
    rule = violation.get("rule")
    if rule not in BLOCK_ELIGIBLE_RULES:
        return False
    if rule == "jsx-presence-mismatch":
        return violation.get("severity") == "error"
    return True


def hard_class_violations(violations: list[dict], active_rules: set[str]) -> list[dict]:
    """Hard-class violations whose rule is also in the repo's active block set."""
    return [v for v in violations if is_hard_class(v) and v.get("rule") in active_rules]
