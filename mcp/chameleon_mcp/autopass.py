"""Auto-pass router: classify a change as auto-pass-eligible or human-mandatory.

The honest ceiling for a machine review gate is not "catch every bug" (no 2026
system does; the measured frontier is ~45-55% of human-flagged issues). It is to
correctly identify the *routine slice* that is safe to auto-pass and route
everything else to a human with a reason. This module is the calibrated router
that draws that line.

A change is auto-pass-eligible only when ALL hold:

- no grounded blocking finding fired (an active block-eligible rule on the diff),
- it typechecks (no compiler error on a changed file, when a typecheck ran),
- it touches no security-sensitive surface (auth / payment / crypto / migration /
  infra) — those always go to a human regardless of how clean they look,
- its diff removes no auth/csrf guard line and adds no chameleon-ignore
  directive (the deterministic content signals),
- it does not weaken tests (deleted test files, net test deletion, added skip
  markers, an assertion-count drop) while also changing live source — pure test
  cleanup stays eligible, the combination does not,
- it is small (within the file and line caps for a routine change),
- its blast radius is bounded AND known (few cross-file importers of the symbols
  it changed; an unreadable fan-out routes to a human rather than reading as 0),
- it stays inside profiled archetypes (a file the engine has no canonical for
  cannot be vouched for, so it is not auto-passable).

Each failing predicate adds a human-readable reason; an empty reason list means
the change cleared every gate. The router never *blocks* — it only decides
whether human review is mandatory; the residual it sends to humans is the real,
irreducible part of review the evidence says no machine removes.
"""

from __future__ import annotations

import re

from chameleon_mcp.conventions import _is_test_path
from chameleon_mcp.violation_class import _IGNORE_RE

# Path needles that mark a security-sensitive surface, by category. A change
# touching any of these always goes to a human, however clean it looks: these are
# the classes where a machine false-negative is most expensive (auth bypass, a bad
# charge, a leaked secret, an irreversible migration, a broken deploy). Matching
# is word-boundary token based for precision (an AuthorCard.tsx is not an auth
# surface); the diff-content signals below cover recall (a removed guard routes
# even when no path matches). Each category carries three needle sets: exact
# tokens, token prefixes, and structural substrings (needles spanning path
# separators or extensions, matched against the POSIX-normalized lower-cased
# path).
_SECURITY_SURFACE_PATTERNS: tuple[
    tuple[str, frozenset[str], tuple[str, ...], tuple[str, ...]], ...
] = (
    (
        "auth",
        # "auth" must stay exact-only: "author"/"authorship" defeat any prefix
        # scheme, while the real prefixed forms are covered by authentic/authoriz.
        frozenset({"auth", "jwt", "oauth", "sso", "acl", "rbac", "devise", "ability", "abilities"}),
        ("authentic", "authoriz", "session", "login", "password", "permission", "polic"),
        (),
    ),
    (
        "payment",
        frozenset(),
        ("payment", "billing", "invoice", "charge", "stripe", "subscription", "checkout"),
        (),
    ),
    (
        "crypto",
        frozenset({"secret", "secrets", "vault", "lockbox", "signing"}),
        ("crypto", "encrypt", "decrypt", "credential"),
        (),
    ),
    (
        "migration",
        frozenset(),
        (),
        ("db/migrate/", "/migrations/", "schema.rb", "structure.sql"),
    ),
    (
        "infra",
        frozenset({"dockerfile", "terraform", "kubernetes", "k8s", "helm", "nginx", "tf"}),
        ("docker", "deploy"),
        (".github/workflows/", "docker-compose"),
    ),
)


def _path_tokens(path: str) -> list[str]:
    """Split a path into lower-cased word tokens for surface matching.

    camelCase boundaries are split first so loginThrottler yields a "login"
    token; everything non-alphanumeric (separators, dots, underscores) then
    delimits tokens.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(path))
    return re.findall(r"[a-z0-9]+", spaced.lower())


def classify_security_surface(path: str) -> str | None:
    """Return the security-sensitive category a path falls in, or None.

    First matching category wins (auth before payment before crypto, etc.); the
    caller only needs to know the change touches *some* sensitive surface, the
    category is for the human-readable reason.
    """
    if not path:
        return None
    norm = str(path).replace("\\", "/").lower()
    tokens = _path_tokens(path)
    for category, exact, prefixes, structural in _SECURITY_SURFACE_PATTERNS:
        if any(t in exact for t in tokens):
            return category
        if prefixes and any(t.startswith(prefixes) for t in tokens):
            return category
        if structural and any(n in norm for n in structural):
            return category
    return None


def security_surface_categories(paths) -> set[str]:
    """The set of security-sensitive categories touched by a changeset's paths."""
    out: set[str] = set()
    for p in paths or ():
        cat = classify_security_surface(p)
        if cat is not None:
            out.add(cat)
    return out


# Guard constructs whose removal from a diff is a deterministic security signal.
# Public: the intent-capture security lens uses the same removed-invariant
# lexicon, defined once here. Matched against single diff lines. Underscores
# are word characters, so \bbefore_action\b deliberately does NOT match
# skip_before_action (that line trips the verify_ arm only when it names a
# verify_* callback). Deliberately narrow, near-zero-FP tokens: TS middleware
# guards (requireAuth wrappers, route-config flags) are out of scope here and
# belong to the stochastic review lenses.
GUARD_LEXICON: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bbefore_action\b"),
    re.compile(r"\bverify_\w+"),
    re.compile(r"\bauthoriz\w*"),
    re.compile(r"\bcsrf\w*"),
    re.compile(r"\bprotect_from_forgery\b"),
    re.compile(r"\bauthenticate_\w+"),
    # Django / DRF / Flask guard constructs: removing one is the same security
    # signal as removing a Rails before_action. Kept to the framework's
    # near-zero-FP guard names (the decorator, the DRF view attribute, the
    # permission class) so a stray identifier match does not route a clean diff.
    re.compile(r"\blogin_required\b"),
    re.compile(r"\bpermission_required\b"),
    re.compile(r"\bpermission_classes\b"),
    re.compile(r"\bIsAuthenticated\w*"),
    re.compile(r"\brequire_http_methods\b"),
)

# Test skip/disable markers, counted on added lines in test files only. The
# test-file scoping plus the underscore word boundary keeps a source-side
# skip_before_action out of the count.
_SKIP_MARKER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:it|test|describe|context)\.(?:skip|todo)\b"),
    re.compile(r"\bx(?:it|test|describe|specify|context)\b\s*[('\"]"),
    re.compile(r"\bpending\b"),
    re.compile(r"\bskip\b(?=\s*[(:'\"])"),
    # Python: pytest/unittest skip markers, parenthesized OR bare. The paren-
    # anchored arm above misses bare `@pytest.mark.skip` and `@pytest.mark.xfail`.
    re.compile(r"@(?:pytest\.mark\.(?:skipif|skip|xfail)|unittest\.(?:skip|expectedFailure))\b"),
)

# Assertion tokens for the added-minus-removed delta over test-file diff lines.
_ASSERTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bexpect\s*\("),
    re.compile(r"\bassert\w*\b"),
    re.compile(r"\.should\b"),
    re.compile(r"\bmust_\w+"),
)

# A line whose first non-whitespace run is a comment token (Python/Ruby `#`,
# TS/JS `//` `/*` `*` `*/`). Commenting out an assertion is the canonical
# test-weakening move, but the added `# assert x` line still matches
# _ASSERTION_PATTERNS, so it would cancel the removed real `assert x` in the
# delta and hide the weakening. Assertion tokens inside a comment are not live
# assertions; skip commented lines from BOTH the added and removed tallies so
# the count reflects executable assertions only (commenting one out reads as a
# drop; uncommenting reads as a restore).
_COMMENT_LINE_RE = re.compile(r"^\s*(?://|/\*|\*/|\*|#)")


def _assertion_hits(line: str) -> int:
    if _COMMENT_LINE_RE.match(line):
        return 0
    return sum(len(rx.findall(line)) for rx in _ASSERTION_PATTERNS)


def _is_test_file(path: str) -> bool:
    """True when the path is a test/spec/story file under any supported
    language's test-naming convention; scopes the test-integrity signals.

    Probes Python too (test_x.py / x_test.py / tests/ tree) so a co-located
    Python test file is recognized for the test-weakening gate, gate parity with
    the TS/Ruby paths."""
    p = str(path)
    return (
        _is_test_path(p, language="ruby")
        or _is_test_path(p, language="typescript")
        or _is_test_path(p, language="python")
    )


def _iter_diff_files(diff_text):
    """Yield ``(path, added_lines, removed_lines)`` per file block of a unified diff.

    A minimal splitter keyed on ``diff --git`` block starts and ``+++ b/<path>``
    headers (falling back to the ``--- a/<path>`` side for deletions); content
    lines are those starting with exactly one ``+``/``-``, the ``+++``/``---``
    headers excluded. Malformed input yields nothing — this feeds an advisory
    verdict that must fail open, the same discipline as parse_numstat.
    """
    blocks: list[tuple[str, list[str], list[str]]] = []
    path: str | None = None
    old_path: str | None = None
    added: list[str] = []
    removed: list[str] = []

    def _close() -> None:
        target = path or old_path
        if target is not None and (added or removed):
            blocks.append((target, added, removed))

    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            _close()
            path = old_path = None
            added, removed = [], []
        elif line.startswith("+++ "):
            target = line[4:].strip()
            if target.startswith("b/"):
                path = target[2:]
            elif target and target != "/dev/null":
                path = target
        elif line.startswith("--- "):
            target = line[4:].strip()
            if target.startswith("a/"):
                old_path = target[2:]
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    _close()
    return iter(blocks)


def _normalized(line: str) -> str:
    return " ".join(line.split())


def scan_diff_signals(
    diff_text,
    *,
    test_deletion_net_lines: int = 10,
    assertion_delta_floor: int = -3,
) -> dict:
    """Deterministic content signals over a unified diff (zero LLM, zero I/O).

    Returns ``{removed_guard_lines, ignore_directives_added, added_skip_markers,
    assertion_delta, test_weakening_markers}``. Guard and ignore-directive
    counts are netted against whitespace-normalized reappearance on the diff's
    other side: a moved ``before_action`` or relocated directive stays quiet,
    a removed or swapped one counts — raw counting would route every refactor
    that reorders callbacks. The directive scan matches added lines as-is
    (no string blanking), so a string literal or prose comment naming the
    directive counts; that false positive costs one human review, which is the
    cheap direction. ``test_weakening_markers`` is the informational rollup of
    the weakening arms detectable from diff content alone (skip markers, an
    assertion drop at/below the floor, net test-line removal past the
    threshold); the routing gate itself lives in ``classify_change``.
    """
    blocks = list(_iter_diff_files(diff_text))
    added_norm = {_normalized(line) for _, added, _ in blocks for line in added}
    removed_norm = {_normalized(line) for _, _, removed in blocks for line in removed}

    removed_guards = 0
    ignores_added = 0
    skip_markers = 0
    assertions_added = 0
    assertions_removed = 0
    test_lines_added = 0
    test_lines_removed = 0

    for path, added, removed in blocks:
        for line in removed:
            if any(rx.search(line) for rx in GUARD_LEXICON):
                if _normalized(line) not in added_norm:
                    removed_guards += 1
        for line in added:
            if _IGNORE_RE.search(line) and _normalized(line) not in removed_norm:
                ignores_added += 1
        if not _is_test_file(path):
            continue
        test_lines_added += len(added)
        test_lines_removed += len(removed)
        for line in added:
            if any(rx.search(line) for rx in _SKIP_MARKER_PATTERNS):
                skip_markers += 1
            assertions_added += _assertion_hits(line)
        for line in removed:
            assertions_removed += _assertion_hits(line)

    assertion_delta = assertions_added - assertions_removed
    weakening_markers = bool(
        skip_markers > 0
        or assertion_delta <= assertion_delta_floor
        or (test_lines_removed - test_lines_added) > test_deletion_net_lines
    )
    return {
        "removed_guard_lines": removed_guards,
        "ignore_directives_added": ignores_added,
        "added_skip_markers": skip_markers,
        "assertion_delta": assertion_delta,
        "test_weakening_markers": weakening_markers,
    }


def parse_numstat(text: str) -> list[dict]:
    """Parse ``git diff --numstat`` output into per-file line deltas.

    Each non-empty line is ``<added>\\t<removed>\\t<path>``; binary files render
    the counts as ``-`` and carry no line count (recorded as 0/0). The path is
    everything after the second tab, so spaces and rename arrows survive intact.
    Malformed lines are skipped rather than raising, since this feeds an advisory
    verdict that must fail open.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_s, removed_s, path = parts
        if not path:
            continue
        added = 0 if added_s.strip() == "-" else _safe_int(added_s)
        removed = 0 if removed_s.strip() == "-" else _safe_int(removed_s)
        if added is None or removed is None:
            continue
        rows.append({"path": path, "added": added, "removed": removed})
    return rows


def _safe_int(s: str) -> int | None:
    try:
        return int(s.strip())
    except (TypeError, ValueError):
        return None


def count_added_files(name_status_text: str) -> int:
    """Count newly-added files in ``git diff --name-status`` output.

    Lines are ``<status>\\t<path>`` (renames carry two paths); a brand-new file
    is status ``A``. New files weigh on the verdict because a freshly-added file
    has no prior canonical the engine calibrated against.
    """
    count = 0
    for line in (name_status_text or "").splitlines():
        if not line.strip():
            continue
        status = line.split("\t", 1)[0].strip()
        if status[:1] == "A":
            count += 1
    return count


def count_deleted_test_files(name_status_text: str) -> int:
    """Count deleted test files in ``git diff --name-status`` output.

    Only status ``D`` rows whose path is a test file count. Renames (``R``) are
    deliberately ignored: a moved spec still exists, and the weakening gate
    cares about coverage that vanished, not coverage that moved.
    """
    count = 0
    for line in (name_status_text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if parts[0].strip()[:1] != "D":
            continue
        path = parts[1].strip() if len(parts) > 1 else ""
        if path and _is_test_file(path):
            count += 1
    return count


def build_autopass_verdict(
    numstat_text: str,
    name_status_text: str,
    *,
    is_unarchetyped,
    importers_of,
    block_findings_for,
    type_error_files=None,
    diff_text: str | None = None,
    diff_truncated: bool = False,
    max_files: int = 10,
    max_lines: int = 150,
    max_blast_radius: int = 10,
    test_deletion_net_lines: int = 10,
    assertion_delta_floor: int = -3,
    tests_failed: bool = False,
    caller_contract_breaks: int = 0,
) -> dict:
    """End-to-end auto-pass verdict from a branch's raw git diff output.

    Composes the pure pipeline (parse -> scan -> assemble -> classify) so the
    only thing a caller supplies beyond the git outputs is the three engine
    adapters (archetype coverage, cross-file importers, active block findings).
    ``diff_text`` (the unified diff) is optional: without it the content-signal
    facts are zero and only the structural gates apply — the same fail-open
    posture as a missing typecheck. ``diff_truncated`` records that the caller
    capped the diff before handing it over, so the content scan covered a
    prefix only. Returns the classifier verdict plus the changed-file list and
    the assembled facts, so a reviewer can see *what* drove the decision.
    """
    rows = parse_numstat(numstat_text)
    changed = [r["path"] for r in rows]
    added = sum(r["added"] for r in rows)
    removed = sum(r["removed"] for r in rows)
    diff_signals = (
        scan_diff_signals(
            diff_text,
            test_deletion_net_lines=test_deletion_net_lines,
            assertion_delta_floor=assertion_delta_floor,
        )
        if diff_text is not None
        else None
    )
    net_test_line_delta = sum(r["added"] - r["removed"] for r in rows if _is_test_file(r["path"]))
    facts = assemble_facts(
        changed,
        added_lines=added,
        removed_lines=removed,
        new_files=count_added_files(name_status_text),
        is_unarchetyped=is_unarchetyped,
        importers_of=importers_of,
        block_findings_for=block_findings_for,
        type_error_files=type_error_files,
        deleted_test_files=count_deleted_test_files(name_status_text),
        net_test_line_delta=net_test_line_delta,
        diff_signals=diff_signals,
    )
    facts["diff_scan_truncated"] = bool(diff_truncated)
    # A grounded, opt-in test-run failure (CHAMELEON_ALLOW_TESTS) routes the change
    # to a human like a type error does: a runnable check the change did not pass.
    facts["tests_failed"] = 1 if tests_failed else 0
    # A deterministic caller-contract break (a narrowed positional signature with
    # committed callers) routes to a human: the auto-pass router has no other
    # per-symbol contract signal, so a narrowing in a low-importer file would
    # otherwise pass on blast radius alone.
    facts["caller_contract_breaks"] = int(caller_contract_breaks or 0)
    verdict = classify_change(
        facts,
        max_files=max_files,
        max_lines=max_lines,
        max_blast_radius=max_blast_radius,
        test_deletion_net_lines=test_deletion_net_lines,
        assertion_delta_floor=assertion_delta_floor,
    )
    return {"changed_files": changed, "facts": facts, **verdict}


def assemble_facts(
    changed_files,
    *,
    added_lines: int,
    removed_lines: int,
    new_files: int,
    is_unarchetyped,
    importers_of,
    block_findings_for,
    type_error_files=None,
    deleted_test_files: int = 0,
    net_test_line_delta: int = 0,
    diff_signals: dict | None = None,
) -> dict:
    """Turn a changeset into the fact dict ``classify_change`` consumes.

    The I/O is injected so the orchestration is testable apart from the engine
    plumbing: ``is_unarchetyped(path)`` (the engine has no canonical to vouch for
    it), ``importers_of(path)`` (cross-file fan-out from the reverse index), and
    ``block_findings_for(path)`` (active block-eligible violations on the file).
    ``importers_of`` may return ``int | None`` — None (or a raise) means the
    fan-out could not be determined, counted in ``blast_radius_unknown``. An
    unknown fan-out must never read as 0: zero is the auto-pass direction and
    exactly the wrong default for missing evidence. ``blast_radius`` is the
    worst single KNOWN fan-out, not the sum, so one widely-imported file in the
    set is what gates, not a count inflated by many leaf files.

    ``type_error_files`` is the optional set of files a typecheck reported
    errors in; only changed files in it count, so a change that does not compile
    routes to a human. ``deleted_test_files`` and ``net_test_line_delta`` are
    caller-computed from name-status/numstat; ``diff_signals`` is the
    ``scan_diff_signals`` output, or None when no diff text was available — the
    content facts then default to 0, since the absence of the scan must not
    fabricate signals (the same convention as ``type_error_files=None``).
    """
    files = list(changed_files or ())
    type_errs = set(type_error_files or ())

    known_fanouts: list[int] = []
    unknown_fanouts = 0
    for p in files:
        try:
            fanout = importers_of(p)
        except Exception:
            fanout = None
        if fanout is None:
            unknown_fanouts += 1
        else:
            known_fanouts.append(int(fanout))

    signals = diff_signals or {}
    return {
        "files_changed": len(files),
        "lines_changed": int(added_lines) + int(removed_lines),
        "new_files": int(new_files),
        "unarchetyped_files": sum(1 for p in files if is_unarchetyped(p)),
        "blast_radius": max(known_fanouts, default=0),
        "blast_radius_unknown": unknown_fanouts,
        "active_block_findings": sum(int(block_findings_for(p)) for p in files),
        "type_errors": sum(1 for p in files if p in type_errs),
        "security_surface": bool(security_surface_categories(files)),
        "source_files_changed": sum(1 for p in files if not _is_test_file(p)),
        "deleted_test_files": int(deleted_test_files),
        "net_test_line_delta": int(net_test_line_delta),
        "removed_guard_lines": int(signals.get("removed_guard_lines", 0) or 0),
        "ignore_directives_added": int(signals.get("ignore_directives_added", 0) or 0),
        "added_skip_markers": int(signals.get("added_skip_markers", 0) or 0),
        "assertion_delta": int(signals.get("assertion_delta", 0) or 0),
    }


def classify_complexity_tier(
    facts: dict,
    *,
    max_files: int = 10,
    max_lines: int = 150,
    max_blast_radius: int = 10,
) -> str:
    """Grade a change's inherent complexity from diff facts: easy → complex.

    This is STRUCTURAL (size / novelty / cross-file reach / security surface),
    distinct from ``classify_change``'s ``risk`` (which rates review-cleanliness
    confidence) and from ``auto_pass_eligible`` (whether anything is wrong). A
    clean change and a broken change of the same shape share a tier; the tier is
    what lets per-tier review-clean rates be tracked and the hard/complex residual
    routed to humans. Deterministic, zero LLM.

    - complex: a security surface, an unknown or large cross-file blast radius,
      many files outside profiled archetypes, or a change past the size caps --
      the classes a machine gate cannot vouch for review-clean.
    - hard: a new file, any unarchetyped file, or a mid-size change.
    - medium: a small multi-file or multi-line change with bounded reach.
    - easy: a tiny, in-pattern, single-spot change.

    Missing keys default to 0 / False, so a partial fact set grades DOWN to easy
    on absent data rather than inventing risk; callers that need the safe-up
    direction gate on ``auto_pass_eligible``, not the tier.
    """

    def _int(key: str) -> int:
        try:
            return int(facts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    files = _int("files_changed")
    lines = _int("lines_changed")
    blast = _int("blast_radius")

    if (
        bool(facts.get("security_surface"))
        or _int("blast_radius_unknown") > 0
        or blast > max_blast_radius
        or _int("unarchetyped_files") > 2
        or files > max_files
        or lines > max_lines
    ):
        return "complex"
    if (
        _int("new_files") > 0
        or _int("unarchetyped_files") > 0
        or files > 5
        or lines > 80
        or blast > 5
    ):
        return "hard"
    if files > 2 or lines > 30 or blast > 0:
        return "medium"
    return "easy"


def classify_change(
    facts: dict,
    *,
    max_files: int = 10,
    max_lines: int = 150,
    max_blast_radius: int = 10,
    test_deletion_net_lines: int = 10,
    assertion_delta_floor: int = -3,
) -> dict:
    """Return an auto-pass verdict for one change.

    ``facts`` carries the assembled signals (files_changed, lines_changed,
    new_files, unarchetyped_files, blast_radius, blast_radius_unknown,
    active_block_findings, security_surface, the content signals, and the
    test-integrity counts). Missing keys are treated as their safe default
    (0 / False), so a partially-assembled fact set never silently auto-passes
    on absent data.
    """
    reasons: list[str] = []

    def _int(key: str) -> int:
        try:
            return int(facts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    findings = _int("active_block_findings")
    if findings > 0:
        reasons.append(f"{findings} blocking finding(s) unresolved")

    type_errors = _int("type_errors")
    if type_errors > 0:
        reasons.append(f"{type_errors} file(s) with type errors")

    if _int("tests_failed") > 0:
        reasons.append("test suite failing")

    contract_breaks = _int("caller_contract_breaks")
    if contract_breaks > 0:
        reasons.append(
            f"{contract_breaks} caller contract break(s) "
            "(narrowed signature with committed callers)"
        )

    if bool(facts.get("security_surface")):
        reasons.append("touches a security-sensitive surface")

    removed_guards = _int("removed_guard_lines")
    if removed_guards > 0:
        reasons.append(f"{removed_guards} removed guard line(s) (auth/csrf content signal)")

    ignores_added = _int("ignore_directives_added")
    if ignores_added > 0:
        reasons.append(f"adds {ignores_added} chameleon-ignore directive(s)")

    files = _int("files_changed")
    lines = _int("lines_changed")
    if files > max_files or lines > max_lines:
        reasons.append(f"change too large ({files} files / {lines} lines)")

    blast = _int("blast_radius")
    if blast > max_blast_radius:
        reasons.append(f"high blast radius ({blast} importers)")

    blast_unknown = _int("blast_radius_unknown")
    if blast_unknown > 0:
        reasons.append(
            f"blast radius unknown for {blast_unknown} file(s) (cross-file index unavailable)"
        )

    unarchetyped = _int("unarchetyped_files")
    if unarchetyped > 0:
        reasons.append(f"{unarchetyped} file(s) outside profiled archetypes")

    # Test weakening defeats eligibility only in COMBINATION with a live-source
    # change: gutting a spec while touching source is the dangerous shape, while
    # a pure test cleanup surfaces its facts without routing.
    weakening = (
        _int("deleted_test_files") > 0
        or _int("added_skip_markers") > 0
        or _int("assertion_delta") <= assertion_delta_floor
        or -_int("net_test_line_delta") > test_deletion_net_lines
    )
    weakening_combo = weakening and _int("source_files_changed") > 0
    if weakening_combo:
        reasons.append(
            "test weakening (deleted tests / skip markers / assertion drop) "
            "alongside live-source changes"
        )

    eligible = not reasons
    if eligible:
        risk = "low"
    elif (
        findings > 0
        or type_errors > 0
        or _int("tests_failed") > 0
        or contract_breaks > 0
        or bool(facts.get("security_surface"))
        or removed_guards > 0
        or ignores_added > 0
        or weakening_combo
    ):
        # Grounded failures (a block finding, a type error, a failing test run),
        # the security surface, the deterministic content signals (a removed
        # guard, an in-diff suppression directive), and the weakening combination
        # are high-confidence reasons; size/blast/unknown-fanout/archetype are
        # softer.
        risk = "high"
    else:
        risk = "elevated"

    tier = classify_complexity_tier(
        facts,
        max_files=max_files,
        max_lines=max_lines,
        max_blast_radius=max_blast_radius,
    )
    return {
        "auto_pass_eligible": eligible,
        "risk": risk,
        "complexity_tier": tier,
        "reasons": reasons,
    }
