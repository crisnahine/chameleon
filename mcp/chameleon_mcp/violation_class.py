"""Classify lint violations for enforcement.

Only objective or explicitly-taught rules are block-eligible. Learned
structural/naming heuristics stay advisory regardless of escalation level,
because a wrong archetype match would make them spurious. The
archetype-independent rules are deterministic facts that hold no matter which
archetype a file resolved to: ``phantom-import`` (a filesystem fact) and a
deterministic-kind ``secret-detected-in-content`` (a leaked credential).
"""

from __future__ import annotations

import re

# Matches a `// chameleon-ignore <rule>` (TypeScript) or `# chameleon-ignore
# <rule>` (Ruby) directive. A TypeScript `/* chameleon-ignore <rule> */` block
# comment is also accepted: the trailing `*/` is not in the `[\w-]` rule class so
# the rule name stops before it. The optional `-file` suffix and the bare form
# (no rule) both parse; a bare directive means "ignore every block-eligible
# rule". The rule name must sit on the same line as the directive: the
# inter-token whitespace excludes newlines so a bare directive on its own line
# does not capture the first word of the following line as a rule.
_IGNORE_RE = re.compile(r"(?:#|//|/\*)[^\S\n]*chameleon-ignore(?:-file)?(?:[^\S\n]+([\w-]+))?")


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
# naming-convention-violation, inheritance-convention-violation, and
# file-naming-convention-violation are archetype-dependent: a wrong archetype
# match would make them spurious, so the block path gates them on confidence=high
# + match_quality=ast and per-repo calibration, same as the other dependent
# rules (mixed-casing repos calibrate file-naming back down to advisory).
# secret-detected-in-content is the exception: it is archetype-independent and
# block-eligible, but ONLY for the deterministic high-precision secret kinds
# (see _DETERMINISTIC_SECRET_KINDS). Entropy-based and broad-fallback hits stay
# advisory regardless, because their precision cannot be measured against a
# clean repo's own files and they false-positive on benign committed content.
BLOCK_ELIGIBLE_RULES: frozenset[str] = frozenset(
    {
        "phantom-import",
        "import-preference-violation",
        "jsx-presence-mismatch",
        "naming-convention-violation",
        "inheritance-convention-violation",
        "file-naming-convention-violation",
        "secret-detected-in-content",
        # An eval() / exec() invocation is a deterministic dangerous sink, not an
        # archetype heuristic, so it is block-eligible like the secret rule.
        # Calibration does not exercise content scans (it never runs
        # scan_dangerous_sinks), so this rule stays active by default rather
        # than by measurement. The rule name matches what scan_dangerous_sinks
        # emits.
        "eval-call",
    }
)

# Archetype-independent rules are true/false regardless of which archetype the
# file matched, so they need no confidence/match-quality gate. A hardcoded AWS
# key is a credential no matter which archetype the file resolved to, so the
# secret rule joins phantom-import here.
_ARCHETYPE_INDEPENDENT: frozenset[str] = frozenset({"phantom-import", "secret-detected-in-content"})

# Archetype-independent rules whose block decision is deliberately deferred from
# the per-edit PostToolUse gate to the turn-end Stop backstop. A phantom import
# is a filesystem fact that a later edit in the same turn can resolve (the import
# target gets created), so blocking it mid-turn would refuse an edit the model is
# about to make valid; the Stop backstop re-lints once the turn's edits settle.
# A leaked credential is NOT deferrable: nothing a later edit does makes a
# hardcoded AKIA key safe, and it is the documented "only security BLOCK", so it
# blocks inline at PostToolUse and is intentionally absent here.
_DEFERRED_TO_TURN_END: frozenset[str] = frozenset({"phantom-import"})

# The secret kinds precise enough to hard-block on. Each is a fixed-prefix or
# fixed-shape credential token with negligible benign-collision rate, so a match
# is a real leaked credential rather than a coincidence in committed content.
# Deliberately excluded: detect-secrets entropy types, possible_aws_secret (any
# 40-char base64 run), high_entropy_hex (any 40+ hex run), password_assignment
# (a quoted value next to a keyword, common in fixtures), and the FP-prone
# JWT/userinfo-URL shapes. Those stay advisory because their precision cannot be
# verified against a clean repo and they trip on benign committed files.
# Also excluded: gcp_service_account, which matches the bare JSON marker
# '"type": "service_account"' - that field appears in benign IAM bindings,
# terraform output, and k8s manifests that carry no credential. A real GCP
# service-account key file always contains a PEM block, which hard-blocks via
# private_key, so demoting the marker to advisory loses no protection.
_DETERMINISTIC_SECRET_KINDS: frozenset[str] = frozenset(
    {
        "aws_access_key",
        "github_token",
        "ai_api_key",
        "stripe_live_key",
        "stripe_key",
        "slack_token",
        "google_api_key",
        "azure_account_key",
        "private_key",
    }
)


def is_archetype_independent(rule: str) -> bool:
    return rule in _ARCHETYPE_INDEPENDENT


def is_deferred_to_turn_end(rule: str) -> bool:
    """True if this archetype-independent rule blocks at the Stop backstop, not inline.

    Only ``phantom-import`` defers: a later same-turn edit can create the import
    target. A deterministic secret never defers; it blocks at the per-edit gate.
    """
    return rule in _DEFERRED_TO_TURN_END


def tag_secret_hardness(violations: list[dict]) -> None:
    """Mark each secret violation with whether its kind may hard-block.

    scan_secrets emits every hit under the single ``secret-detected-in-content``
    rule, encoding the secret kind only in the human-readable ``actual`` string
    (``"<kind> at line N"``). Block-eligibility needs the kind as structured
    data, so we parse it back out once at the wiring boundary and stamp a
    ``secret_hard`` flag the enforcement gate reads. Mutates the dicts in place;
    non-secret violations are left untouched. The cap-summary row (``actual``
    like ``"+17 more (capped...)"``) has no kind, so it never hard-blocks.
    """
    for v in violations:
        if v.get("rule") != "secret-detected-in-content":
            continue
        kind = _secret_kind(v)
        v["secret_kind"] = kind
        v["secret_hard"] = kind in _DETERMINISTIC_SECRET_KINDS


def _secret_kind(violation: dict) -> str | None:
    """Recover the secret kind token from a secret violation's ``actual`` field."""
    actual = violation.get("actual")
    if not isinstance(actual, str):
        return None
    # Format is "<kind> at <location>[ suffix]"; the kind never contains a space.
    head = actual.split(" at ", 1)[0].strip()
    return head or None


def is_hard_class(violation: dict) -> bool:
    """True if this violation is block-eligible on its own merits.

    jsx-presence-mismatch is the only severity-gated rule: it qualifies only at
    severity ``error`` (file HAS JSX in a non-JSX archetype); the ``warning`` form
    (missing JSX, may be a stub) does not.

    secret-detected-in-content is kind-gated: only a deterministic high-precision
    secret kind hard-blocks. The ``secret_hard`` flag is stamped upstream by
    ``tag_secret_hardness``; an untagged secret hit (entropy/broad-fallback, or a
    hit that never passed through the tagger) defaults to advisory.

    Every other block-eligible rule qualifies regardless of severity, including
    naming/inheritance/file-naming convention violations, which are always
    emitted at ``warning``.
    """
    rule = violation.get("rule")
    if rule not in BLOCK_ELIGIBLE_RULES:
        return False
    if rule == "jsx-presence-mismatch":
        return violation.get("severity") == "error"
    if rule == "secret-detected-in-content":
        return bool(violation.get("secret_hard"))
    return True


def hard_class_violations(violations: list[dict], active_rules: set[str]) -> list[dict]:
    """Hard-class violations whose rule is also in the repo's active block set."""
    return [v for v in violations if is_hard_class(v) and v.get("rule") in active_rules]
