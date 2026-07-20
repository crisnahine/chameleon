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
from dataclasses import dataclass, field

# Matches a `// chameleon-ignore <rule>` (TypeScript) or `# chameleon-ignore
# <rule>` (Ruby) directive. A TypeScript `/* chameleon-ignore <rule> */` block
# comment is also accepted via the optional trailing `*/`. The optional `-file`
# suffix widens the directive to the whole file; the plain form is scoped to
# its own line (or the line below, when the directive sits on a line of its
# own). The directive must end its line — nothing but whitespace (and the
# block-comment closer) may follow the rule name — so prose that merely
# MENTIONS a directive ("add // chameleon-ignore x if intentional") never
# activates it. The rule name must sit on the same line as the directive: the
# inter-token whitespace excludes newlines so a bare directive on its own line
# does not capture the first word of the following line as a rule.
_IGNORE_RE = re.compile(
    r"(?:#|//|/\*)[^\S\n]*chameleon-ignore(-file)?(?:[^\S\n]+([\w-]+))?"
    r"[^\S\n]*(?:\*/[^\S\n]*)?$",
    re.MULTILINE,
)

# Recovers the 1-based line number a violation reports inside its
# human-readable ``actual`` field ("<thing> at line N"). The line-bearing
# rules (deterministic secrets, eval-call) all use this shape; violations
# without it are file-level facts that have no line to scope a directive to.
_AT_LINE_RE = re.compile(r" at line (\d+)\b")


@dataclass(frozen=True)
class IgnoreIndex:
    """Parsed inline-ignore directives, scoped for violation filtering.

    ``file_rules`` holds the ``chameleon-ignore-file`` directives (whole-file
    scope). ``line_rules`` maps each covered line to the plain directives that
    apply there: a trailing directive covers its own line; a directive on a
    line of its own covers the line below it too. ``named_anywhere`` is the
    union of every plain directive in the file — the fallback scope for
    violations that carry no line number, which a line-scoped directive could
    otherwise never suppress. The empty-string rule means "every rule" — except
    the deterministic-kind hard class (hard-kind secrets, error-severity
    eval-call), which only a rule-named directive suppresses (see
    ``is_violation_ignored``).
    """

    file_rules: frozenset[str] = frozenset()
    line_rules: dict[int, frozenset[str]] = field(default_factory=dict)
    named_anywhere: frozenset[str] = frozenset()

    def all_rules(self) -> frozenset[str]:
        return self.file_rules | self.named_anywhere


def _blank_string_literals(content: str, file_path: str | None, language: str | None) -> str:
    """Blank string-literal bodies so embedded text cannot activate directives.

    A directive only counts when it lives in a real comment. Text inside a
    string constant ("to silence this add // chameleon-ignore ...") is
    attacker-controllable content, not author intent — left unblanked it could
    switch off the secret block from a help string. Replacement preserves
    newlines so directive line numbers stay truthful. Best-effort: a blanking
    miss fails closed (the directive deactivates and the violation still
    fires), never open.
    """
    try:
        from chameleon_mcp.lint_engine import (
            _RUBY_STRING_DQ,
            _RUBY_STRING_SQ,
            _TS_STRING,
            _blank_match_to_spaces,
            _blank_python_strings,
            _blank_ruby_heredocs,
            _blank_ruby_percent_literals,
            detect_language,
        )

        if language is None and file_path:
            language = detect_language(file_path)
        if language == "ruby":
            out = _blank_ruby_heredocs(content)
            out = _RUBY_STRING_DQ.sub(_blank_match_to_spaces, out)
            out = _RUBY_STRING_SQ.sub(_blank_match_to_spaces, out)
            # Percent-literals too: a directive inside `%q{...}` is content, not
            # author intent, and must not suppress a real violation.
            return _blank_ruby_percent_literals(out)
        if language == "python":
            # Triple-quoted / f/r/b-prefixed strings the TS regex can't span; a
            # directive inside a docstring must not suppress a real violation.
            return _blank_python_strings(content)
        # TypeScript and unknown languages share the quote/backtick shapes.
        return _TS_STRING.sub(_blank_match_to_spaces, content)
    except Exception:
        return content


def build_ignore_index(
    content: str, *, file_path: str | None = None, language: str | None = None
) -> IgnoreIndex | None:
    """Parse inline-ignore directives into their file/line scopes.

    Returns None when the content carries no directive, so callers can skip
    filtering entirely on the common path.
    """
    blanked = _blank_string_literals(content, file_path, language)
    file_rules: set[str] = set()
    named: set[str] = set()
    line_rules: dict[int, set[str]] = {}
    for m in _IGNORE_RE.finditer(blanked):
        rule = m.group(2) or ""
        if m.group(1):
            file_rules.add(rule)
            continue
        named.add(rule)
        line_no = blanked.count("\n", 0, m.start()) + 1
        line_start = blanked.rfind("\n", 0, m.start()) + 1
        standalone = not blanked[line_start : m.start()].strip()
        covered = (line_no, line_no + 1) if standalone else (line_no,)
        for ln in covered:
            line_rules.setdefault(ln, set()).add(rule)
    if not file_rules and not named:
        return None
    return IgnoreIndex(
        file_rules=frozenset(file_rules),
        line_rules={ln: frozenset(rules) for ln, rules in line_rules.items()},
        named_anywhere=frozenset(named),
    )


def violation_line(violation: dict) -> int | None:
    """1-based line a violation reports, or None for file-level violations."""
    actual = violation.get("actual")
    if not isinstance(actual, str):
        return None
    m = _AT_LINE_RE.search(actual)
    return int(m.group(1)) if m else None


def is_violation_ignored(violation: dict, idx: IgnoreIndex | None) -> bool:
    """True when an inline directive covers this violation.

    A ``-file`` directive covers everything. A plain directive covers only the
    line it annotates — except for violations that report no line, which any
    same-file plain directive may suppress (there is no line to target).

    A bare (blanket) directive — no rule named — covers every rule except the
    deterministic-kind hard class (hard-kind secrets, error-severity
    eval-call). Those are runnable security facts, so silencing one must name
    the rule explicitly; the named form stays available as the auditable
    escape for legitimate fixtures.
    """
    if idx is None:
        return False
    keys = {violation.get("rule") or ""}
    if not is_blanket_immune(violation):
        keys.add("")
    if keys & idx.file_rules:
        return True
    line = violation_line(violation)
    if line is None:
        return bool(keys & idx.named_anywhere)
    rules_at = idx.line_rules.get(line)
    return bool(rules_at and keys & rules_at)


def ignored_rules(
    content: str, *, file_path: str | None = None, language: str | None = None
) -> set[str] | None:
    """Return the set of explicitly-ignored rule names, or None if there are none.

    The flat, file-scope view of the directives: the turn-end opt-out checks
    (idioms review, stale-test, cochange, removed-export), the
    import-preference deny, and lint_conventions' naming / inheritance /
    file-naming checks all read this, since those violations report no line
    (a repo-wide or whole-file fact, not a single flagged line) and their gates
    have no per-line granularity to filter against. A plain directive naming
    one of these rules therefore has the same file-wide reach as
    ``chameleon-ignore-file`` — see docs/architecture.md's "Escape hatch"
    section. Violation filters use ``build_ignore_index`` +
    ``is_violation_ignored`` for line scoping where a violation does carry a
    line (secrets, ``eval-call``). A bare ``chameleon-ignore`` (no rule)
    contributes the empty string, which callers read as "ignore everything".
    """
    idx = build_ignore_index(content, file_path=file_path, language=language)
    if idx is None:
        return None
    return set(idx.all_rules()) or None


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

# Languages each block-eligible rule has a signal source for. Calibration
# certifies a rule "active" when it flags ~none of the repo's own files — but a
# rule with no signal source for the profile's language flags nothing
# VACUOUSLY, and listing it active misreads that silence as safety (a Ruby
# profile shipped with jsx-presence-mismatch "active" at fp_rate 0.0 this way).
# None means language-independent.
BLOCK_RULE_LANGUAGES: dict[str, frozenset[str] | None] = {
    # phantom-import is implemented for typescript + ruby + python (each resolves
    # its own relative-import form to a file on disk). Scoped so calibration never
    # certifies it active for a language whose phantom-import is not implemented.
    "phantom-import": frozenset({"typescript", "ruby", "python"}),
    "import-preference-violation": None,
    "jsx-presence-mismatch": frozenset({"typescript"}),
    "naming-convention-violation": frozenset({"typescript", "ruby", "python"}),
    # Ruby + Python each derive a dominant-base convention and lint a class that
    # inherits outside it (the Ruby check runs inline in lint_conventions, the
    # Python check in _python_inheritance_violations). Both are listed so
    # calibration can certify the rule per-language; the same noisy-rule
    # demotion that protects file-naming applies here, so calibration, not this
    # set, decides whether it actually blocks.
    "inheritance-convention-violation": frozenset({"ruby", "python"}),
    "file-naming-convention-violation": None,
    "secret-detected-in-content": None,
    "eval-call": None,
}


# Archetype-independent rules are true/false regardless of which archetype the
# file matched, so they need no confidence/match-quality gate. A hardcoded AWS
# key is a credential no matter which archetype the file resolved to, so the
# secret rule joins phantom-import here. An eval()/exec() call is a deterministic
# dangerous sink for the same reason -- an RCE is an RCE regardless of archetype,
# and a brand-new or unarchetyped file (where no archetype resolves) is exactly
# where it must still block. is_hard_class severity-gates eval-call to the
# error-severity forms (the direct call, send-dispatch, and a *_eval whose
# argument carries request input), so the warning-severity string-argument
# *_eval metaprogramming variants stay advisory and never hard-block here.
_ARCHETYPE_INDEPENDENT: frozenset[str] = frozenset(
    {"phantom-import", "secret-detected-in-content", "eval-call"}
)

# Archetype-independent rules whose block decision is deliberately deferred from
# the per-edit PostToolUse gate to the turn-end Stop backstop. A phantom import
# is a filesystem fact that a later edit in the same turn can resolve (the import
# target gets created), so blocking it mid-turn would refuse an edit the model is
# about to make valid; the Stop backstop re-lints once the turn's edits settle.
# A leaked credential is NOT deferrable: nothing a later edit does makes a
# hardcoded AKIA key safe, so it stays in the inline block set rather than being
# listed here. In enforce mode the PreToolUse gate additionally denies a
# hard-kind secret found in the proposed Edit/Write content before it reaches
# disk, so the per-edit and turn-end gates are the second and third lines of
# defense for that class. Note the inline gate itself still requires the file to have
# escalated to L2 — on a lower-escalation file a deterministic secret is
# recorded as blockable_unresolved and, in enforce mode, the Stop backstop
# refuses the turn instead, so under enforce the credential cannot leave the
# turn either way; only the block's timing differs. In shadow mode nothing
# blocks: the backstop records a would_block preview and the turn ends, so this
# no-escape guarantee holds only under enforce (the default). There is
# also a per-session stop_block_cap: after that many backstop blocks the hook
# goes advisory to avoid a stuck turn, so the no-escape guarantee is bounded by
# that cap even under enforce.
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
        # Fixed glpat-/gldt-/glrt- prefixes — same precision class as the
        # GitHub ghp_ family.
        "gitlab_token",
        "ai_api_key",
        "stripe_live_key",
        "stripe_key",
        "slack_token",
        "google_api_key",
        "azure_account_key",
        "private_key",
    }
)

# Rules whose deterministic (hard-class) variants a bare ``chameleon-ignore``
# can never suppress. A hard-kind secret or an error-severity eval call is a
# runnable security fact, not a style judgment; silencing one must name the
# rule explicitly so the bypass is deliberate and auditable. The advisory
# variants of the same rules (entropy/broad-fallback secret kinds, the
# warning-severity string-arg ``*_eval`` forms) stay blanket-suppressible —
# they are FP-prone by design and the blanket directive legitimately quiets
# them in fixtures. This set is the shared vocabulary for "deterministic-kind"
# across the enforcement surfaces; derive from it rather than redefining.
BLANKET_IMMUNE_RULES: frozenset[str] = frozenset({"secret-detected-in-content", "eval-call"})


def is_blanket_immune(violation: dict) -> bool:
    """True when only a rule-NAMED ignore directive may suppress this violation.

    Gated on ``is_hard_class`` so only the deterministic variants are immune:
    a hard-kind secret (``secret_hard`` stamped by ``tag_secret_hardness``) or
    an error-severity eval call. Untagged or advisory rows keep the blanket
    semantics.
    """
    return violation.get("rule") in BLANKET_IMMUNE_RULES and is_hard_class(violation)


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
    # eval-call is severity-gated like jsx: the direct `eval(` form,
    # send-dispatch to eval, and a `*_eval` whose argument carries request
    # input (`instance_eval(params[:x])`) are emitted at ``error`` and may
    # block, while the string-argument `*_eval` variants are emitted at
    # ``warning`` and stay advisory — `class_eval <<~RUBY` is an established
    # (if sharp) Rails metaprogramming idiom that calibration never measures
    # (content scans are not calibrated), so blocking it would FP on
    # legitimate committed code.
    if rule == "eval-call":
        return violation.get("severity") == "error"
    if rule == "secret-detected-in-content":
        return bool(violation.get("secret_hard"))
    return True


def hard_class_violations(violations: list[dict], active_rules: set[str]) -> list[dict]:
    """Hard-class violations whose rule is also in the repo's active block set."""
    return [v for v in violations if is_hard_class(v) and v.get("rule") in active_rules]


def block_eligible_on_file(hard: list[dict], *, language: str | None) -> list[dict]:
    """Drop archetype-independent hard rules on a non-code file.

    ``eval-call`` and ``secret-detected-in-content`` run on raw content, so the
    literal text ``eval(`` or a credential-shaped token in markdown / plain-text /
    config PROSE (an unrecognized extension, ``detect_language`` is None) would
    otherwise hard-block under enforce -- and such a file cannot carry an inline
    ``chameleon-ignore`` directive, so the block has no escape. They stay in the
    advisory violation list; only the BLOCK set drops them here. On a recognized
    code language the set is returned unchanged. Pass the file's
    ``detect_language()`` result; a code string keeps the rules, None drops the
    archetype-independent ones. Archetype-dependent rules are untouched (only the
    archetype-independent ones are dropped). A non-code file CAN still resolve to
    an archetype via a legacy extension-blind ``paths_pattern``, so this gate is
    applied at the with-archetype block sites too, not only the no-archetype
    ones."""
    if language is not None:
        return hard
    return [v for v in hard if not is_archetype_independent(v.get("rule"))]
