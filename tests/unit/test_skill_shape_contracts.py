"""Structural contracts for the review/dig skills and their packaged agents.

Three graded-sonnet generations established one law about these skills
(CHANGELOG 4.5.4, 4.5.5, 4.5.13): a requirement shaped as a rendered slot or a
literal line is followed to the letter, and a requirement living in section
prose is dropped stochastically -- "exhortation inside a slot does not hold
where shape does". Every hardening those rounds bought was therefore a SHAPE,
and none of them was pinned: the 9-slot brief, the 9-slot report, the ladder
and experts lines, and the whole packaged-agent roster could be edited away
with the suite still green.

These tests pin the shapes. Two rules keep them honest:

1. **Scope every assertion to the section that must carry it.** A bare
   ``token in whole_file`` passes when the token survives anywhere -- an
   adversarial pass proved an early draft's six deep-work contracts were
   jointly satisfiable by a 12-line prose stub that merely NAMED the slots.
2. **Pin the artifact, not the vocabulary.** A template is a block with
   slots that each appear once, in order; a rule is its directive sentence.

**What this file does and does not guarantee.** These are static text pins
over prose instruction files, so they cannot be proof against a deliberate
rewrite: a second adversarial pass reduced all three skills, all seven
references and all five agent bodies to ~91 lines of filler carrying exactly
the pinned tokens, and every contract here still passed. That is the honest
ceiling of the technique and it is not worth chasing -- the pins that would
close it are the pins that fire on legitimate rewording, and a contract that
cries wolf is deleted by the next author, which costs more than the hole.

The threat model is therefore ACCIDENTAL REGRESSION: a slot dropped while
editing a nearby paragraph, a rule inverted in a reword, a template quietly
collapsed into prose, an agent's tool policy loosened. Against that these bite,
and each failure message says what to restore. Behavioural conformance -- does
a model driven by these files actually RENDER the templates -- is not testable
here and is the journey harness's job; no act covers deep-work today.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "plugin" / "skills"
AGENTS = ROOT / "plugin" / "agents"

DEEP_WORK = SKILLS / "chameleon-deep-work" / "SKILL.md"
PR_REVIEW = SKILLS / "chameleon-pr-review" / "SKILL.md"
RECEIVING = SKILLS / "chameleon-receiving-code-review" / "SKILL.md"

# Every agent the skills may dispatch. Adding one means adding its definition;
# the roster test is what keeps the two in step.
EXPECTED_AGENTS = {
    "code-scout",
    "pattern-reviewer",
    "recall-lens",
    "verifier",
    "web-researcher",
}

# The refuter escalates its adjudicating model only for these, lowercased
# (refuter.py `_REFUTER_HIGH_SEVERITIES`). An agent or skill that emits a
# near-miss like "blocking" silently drops its highest-stakes finding to the
# base model, which is the defect this file's own first round shipped.
REFUTER_HIGH = {"block", "high", "critical"}

# Capability-denial phrasings. Namespaced MCP tools are not harness-deniable,
# so any of these is a false claim; commit d282204 replaced the first with a
# directive and this catches the paraphrases too.
FALSE_DENIAL = (
    "not granted",
    "not among the tools",
    "were not given",
    "you are denied",
    "the harness denies you",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """Collapse whitespace: line WRAPPING is not part of any contract here.

    Punctuation survives, so a pinned phrase still has to appear as one
    contiguous character run -- reflow tolerance, not a looser match.
    """
    return re.sub(r"\s+", " ", text)


def _frontmatter(path: Path) -> dict[str, str]:
    """Parse the agent frontmatter into real key -> value pairs.

    Substring tests on the raw block cannot tell `disallowedTools: ... Bash`
    (a denial) from `tools: ... Bash` (a GRANT), and the allowlist form is
    live in this repo, so the inverted case would ship green.
    """
    text = _read(path)
    assert text.startswith("---"), f"{path.name} has no frontmatter"
    block = text.split("---", 2)[1]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def _agent_body(path: Path) -> str:
    return _read(path).split("---", 2)[2]


def _section(text: str, heading: str) -> str:
    """Return one `## ` section's body: the heading to the next `## `.

    Scoping is what stops a slot name surviving as a historical mention
    elsewhere in the file from satisfying a template contract.

    The heading must match UNIQUELY. A prefix match taking the first hit let a
    decoy `## Step 7a: quick recap` above the real `## Step 7` redirect every
    assertion to the decoy -- and lettered step headings are already live in
    this repo (`Step 1b`, `Step 4a`, `Step 7b`), so this is a live hazard
    rather than a hypothetical one.
    """
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if ln.startswith(f"## {heading}")]
    assert hits, f"no `## {heading}...` section found"
    assert len(hits) == 1, (
        f"`## {heading}` matches {len(hits)} headings ({[lines[i] for i in hits]}); "
        "scope is ambiguous -- rename one or pass a longer heading"
    )
    start = hits[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _fences(text: str) -> list[str]:
    return re.findall(r"```.*?```", text, re.DOTALL)


def _agent_files() -> list[Path]:
    """Every agent definition, asserted non-empty.

    A `for p in glob(...)` body that never executes passes vacuously -- the
    same "nothing to check" reading as "everything checked" that this repo's
    own gap taxonomy calls vacuous silence.
    """
    files = sorted(AGENTS.glob("*.md"))
    assert files, f"no agent definitions found under {AGENTS}"
    return files


def _reference_dirs() -> list[Path]:
    dirs = sorted(SKILLS.glob("*/references"))
    assert dirs, "no skill carries a references/ directory -- progressive disclosure regressed"
    return dirs


def _assert_ordered(section: str, tokens: tuple[str, ...], what: str) -> None:
    """Every token present EXACTLY ONCE in this section, in this order.

    Exactly-once is what gives the order check teeth: with duplicates allowed,
    a single recap sentence listing the slots in canonical order pins every
    position, and the real template underneath can then be in any order at all
    (proven -- a fully reversed template passed).
    """
    flat = _norm(section)
    positions = []
    for token in tokens:
        needle = _norm(token)
        count = flat.count(needle)
        assert count, f"{what} lost its {token!r} slot (searched only its own section)"
        assert count == 1, (
            f"{what} names {token!r} {count} times in one section; a duplicate listing "
            "defeats the order check, so keep each slot label to its own slot"
        )
        positions.append(flat.find(needle))
    assert positions == sorted(positions), (
        f"{what} slots are out of order: {tokens}; a template's order IS its contract"
    )


# --- packaged agent roster -------------------------------------------------


def test_agent_roster_matches_definitions():
    on_disk = {p.stem for p in AGENTS.glob("*.md")}
    assert on_disk == EXPECTED_AGENTS, (
        f"agent roster drifted: on disk {sorted(on_disk)}, expected {sorted(EXPECTED_AGENTS)}"
    )


def test_every_agent_definition_carries_a_real_role():
    """A file that exists but holds only frontmatter is not a packaged agent.

    The roster test pins existence; without this one, truncating a definition
    to its 5-line header ships green and every dispatch falls back to whatever
    the caller improvises.
    """
    for path in _agent_files():
        body = _agent_body(path)
        assert len(body.split()) >= 200, (
            f"{path.name} body is {len(body.split())} words -- too thin to carry a role"
        )
        # A word floor alone is satisfied by filler, so require the two
        # structural headings every packaged agent here carries: what it may
        # use, and what it must return.
        assert re.search(r"(?im)^#+ .*tool limits", body), (
            f"{path.name} has no `## Tool limits` section -- its grants are implicit"
        )
        assert re.search(r"(?im)^#+ .*(output|answer contract)", body), (
            f"{path.name} has no output/answer-contract section -- its caller cannot "
            "know what shape to expect back"
        )


def test_every_agent_names_itself_and_describes_itself():
    for path in _agent_files():
        front = _frontmatter(path)
        assert front.get("name") == path.stem, (
            f"{path.name}: frontmatter name {front.get('name')!r} must EQUAL the filename stem "
            "-- the dispatcher routes on the stem"
        )
        assert len(front.get("description", "")) > 40, (
            f"{path.name}: description is empty or too short to route on"
        )


def test_every_agent_declares_a_non_empty_tool_policy():
    for path in _agent_files():
        front = _frontmatter(path)
        policy = front.get("disallowedTools") or front.get("tools") or ""
        assert policy.strip(), (
            f"{path.name} declares no tool allowlist or denylist (a bare key does not count)"
        )


def test_no_agent_claims_a_capability_it_merely_must_not_use():
    """Namespaced MCP tools are not harness-deniable, so a denial claim is false.

    Checked across skills too: the same false claim in a skill's dispatch
    prose misleads exactly as much as in the agent definition.
    """
    for path in _agent_files() + sorted(SKILLS.rglob("*.md")):
        flat = _norm(_read(path)).lower()
        for phrase in FALSE_DENIAL:
            assert phrase not in flat, (
                f"{path.name} claims a capability denial the harness cannot enforce "
                f"({phrase!r}); state it as a directive instead"
            )


def test_read_only_agents_deny_the_shell_rather_than_granting_it():
    """The verifier's missing shell is load-bearing, not an oversight.

    A review subagent once wrote two rows outside a transaction into a shared
    test database and handed back a RED suite (CHANGELOG 4.5.14). Removing the
    shell makes that class impossible rather than merely forbidden -- so this
    reads the KEY, since `tools: ... Bash` GRANTS what the denylist withholds.
    """
    for name in ("code-scout", "pattern-reviewer", "recall-lens", "verifier"):
        front = _frontmatter(AGENTS / f"{name}.md")
        granted = front.get("tools")
        denied = front.get("disallowedTools", "")
        if granted is not None:
            # An allowlist that simply omits Bash is a STRICTLY STRONGER policy
            # than a denylist; requiring the denylist form would reject the
            # safer shape (and web-researcher.md already ships an allowlist).
            assert "Bash" not in granted, (
                f"{name}.md GRANTS Bash via `tools:` -- it must be read-only"
            )
        else:
            assert "Bash" in denied, (
                f"{name}.md neither denies Bash via `disallowedTools:` nor withholds it "
                "via a `tools:` allowlist"
            )


def test_agents_that_feed_the_refuter_use_its_severity_vocabulary():
    """`blocking` is not `block`; the near-miss never escalates the model."""
    body = _norm(_agent_body(AGENTS / "verifier.md")).lower()
    # Capture ONLY the vocabulary clause. An earlier version ran to the first
    # period, which swallowed the following sentence's citation of the engine's
    # own high set -- so the assertion passed no matter what the agent emitted,
    # which is the very defect it exists to catch.
    match = re.search(r"`severity` is one of ((?:\s*`[a-z]+`\s*/?)+)", body)
    assert match, "verifier.md no longer states its severity vocabulary as a `/`-joined list"
    emitted = re.findall(r"`([a-z]+)`", match.group(1))
    assert emitted, "verifier.md's severity vocabulary is empty"
    assert emitted[0] in REFUTER_HIGH, (
        f"verifier's TOP severity is {emitted[0]!r}, which is not in the refuter's escalation "
        f"set {sorted(REFUTER_HIGH)} -- its highest-stakes findings would never escalate the "
        "adjudicating model (refuter.py `_refuter_model_for` does an exact lowercased match)"
    )


def test_dispatched_agent_types_have_definitions():
    """A skill may not dispatch a subagent_type with no definition behind it."""
    referenced: set[str] = set()
    for path in SKILLS.rglob("*.md"):
        for match in re.finditer(r"""subagent_type:\s*["']chameleon:([\w-]+)["']""", _read(path)):
            referenced.add(match.group(1))
    assert referenced, "no skill dispatches a packaged agent -- the wiring regressed"
    missing = referenced - EXPECTED_AGENTS
    assert not missing, f"skills dispatch undefined agent type(s): {sorted(missing)}"


# --- deep-work: the shapes the graded rounds bought ------------------------


def test_deep_work_brief_is_a_fixed_ordered_slot_template():
    section = _section(_read(DEEP_WORK), "Step 4")
    _assert_ordered(
        section,
        (
            "**Goal & criteria**",
            "**Files**",
            "**Contracts**",
            "**Unknowns**",
            "**Re-audit line**",
            "**Plan**",
            "**Risks & rollback**",
            "**Ladder line**",
            "**Experts line**",
        ),
        "deep-work brief",
    )
    assert "render every slot below in order" in _norm(section).lower(), (
        "deep-work brief lost the rule that makes it a template rather than a list of topics"
    )


def test_deep_work_report_is_a_fixed_ordered_slot_template():
    section = _section(_read(DEEP_WORK), "Step 7")
    _assert_ordered(
        section,
        (
            "**Built**",
            "**Evidence table**",
            "**Guard checks**",
            "**Review convergence**",
            "**Finding fates recorded**",
            "**Defaults taken**",
            "**Not verified**",
            "**Worktree**",
            "**Proactive follow-ups**",
        ),
        "deep-work report",
    )
    assert re.search(r"none [-\u2013\u2014] <reason>", _norm(section)), (
        "deep-work report lost its empty-slot rule; a dropped slot and an empty one "
        "become indistinguishable again"
    )


def test_deep_work_plan_steps_carry_a_per_step_verify_clause():
    """Sonnet collapses the plan into the files list unless the shape forbids it."""
    section = _norm(_section(_read(DEEP_WORK), "Step 4"))
    assert "<n>. <action> -> verify:" in section, (
        "deep-work brief slot 6 lost its literal per-step shape"
    )
    assert "a line without its `-> verify:` clause is not a step" in section, (
        "deep-work brief lost the rule that a verify-less line is not a step"
    )


def test_deep_work_ladder_and_experts_lines_are_mandatory_and_carry_counts():
    """'I read every app/ file' over 15 of 19 is why these carry numbers."""
    section = _norm(_section(_read(DEEP_WORK), "Step 4"))
    assert "read N of M source files" in section, "deep-work ladder line lost its counts"
    assert "Both numbers or the line is unfinished" in section, (
        "deep-work ladder line lost the rule that makes the counts mandatory"
    )
    assert "Experts: <N> dispatched" in section, "deep-work experts line lost its count"


def test_deep_work_step_5_renders_a_build_log_as_an_artifact():
    """Step 5 was the last free-prose region, so the honesty defect relocated there.

    Pinned as a FENCED block: a sentence merely mentioning the words leaves
    nothing for a skipped plan step to be missing from.
    """
    section = _section(_read(DEEP_WORK), "Step 5")
    fenced = [f for f in _fences(section) if "Baseline:" in f and "Step <n>/<N>:" in f]
    assert fenced, (
        "deep-work Step 5 has no fenced build-log artifact carrying both a `Baseline:` "
        "line and a `Step <n>/<N>:` line"
    )
    assert "-> verify:" in _norm(fenced[0]), (
        "the build-log line dropped its per-step verify clause, so an unrun check "
        "becomes fillable without observing anything"
    )
    report = _norm(_section(_read(DEEP_WORK), "Step 7"))
    assert "Derive this slot from the Step 5 build log" in report, (
        "the report's not-verified slot no longer transcribes the build log"
    )


def test_deep_work_universal_claims_are_transcriptions_not_recollections():
    """Pinned as the contiguous directive so an inverted restatement cannot satisfy it."""
    brief = _norm(_section(_read(DEEP_WORK), "Step 4"))
    assert "counted, not recalled" in brief, (
        "deep-work's brief lost the universal-claim transcription rule (v4.5.12)"
    )
    flat = _norm(_read(DEEP_WORK))
    assert "counted from the session record" in flat, (
        "deep-work honesty rules lost the universal-claim transcription rule"
    )
    # The rule has been inverted-in-place once in an adversarial pass; a
    # substring pin cannot detect every rewording, but it can detect the
    # obvious inversion of its own vocabulary.
    for inversion in (
        "recalled rather than counted",
        "may be recalled",
        "recalled, not counted",
    ):
        assert inversion not in flat, (
            f"deep-work states the universal-claim rule inverted ({inversion!r}); "
            "claims are counted from the session record, never recalled"
        )


# --- receiving-code-review: the template it never had ----------------------


def test_receiving_renders_a_fixed_ordered_adjudication_report():
    """Scoped to Step 7b: two of these slot labels also exist in Steps 1-3.

    A file-wide substring check let slots 1 and 2 be deleted from the template
    while staying green, because Step 2 and Step 3 carry the same sentences.
    """
    section = _section(_read(RECEIVING), "Step 7b")
    _assert_ordered(
        section,
        (
            "Comments: N fetched",
            "Verification records: M/N complete",
            # The table header, not its exact column list: adding a column is a
            # strengthening edit and must not fail.
            "| # | reviewer |",
            "Grounding: R round(s)",
            "Finding fates recorded:",
            "Drafted replies:",
            "Not verified:",
            "Implementation queue:",
        ),
        "receiving adjudication report",
    )


def test_receiving_report_declares_empty_slots_rather_than_dropping_them():
    section = _norm(_section(_read(RECEIVING), "Step 7b"))
    assert "never silently dropped" in section, (
        "receiving report lost its empty-slot rule; a dropped slot and an empty one "
        "become indistinguishable"
    )
    assert "completeness pass" in section, (
        "receiving report lost the terminal completeness pass that checks the slots"
    )
    assert re.search(r"all \d+ slots present in order", section), (
        "receiving's completeness pass no longer checks that every slot is present in order"
    )


# --- refuter call shape (the E1/E2 defect class) ---------------------------


def test_every_review_skill_sends_the_refuter_a_complete_finding():
    """`kind` is rendered verbatim into the refuter prompt; `severity` picks its model.

    Omitting either is silent: the prompt reads a literal `kind: None`, and the
    model ladder never escalates, so the highest-stakes findings are adjudicated
    by the base model (refuter.py `_refuter_model_for`).
    """
    callers = {
        "chameleon-deep-work/SKILL.md": DEEP_WORK,
        "chameleon-receiving-code-review/SKILL.md": RECEIVING,
        "chameleon-pr-review/references/output-format.md": (
            SKILLS / "chameleon-pr-review" / "references" / "output-format.md"
        ),
    }
    for label, path in callers.items():
        flat = _norm(_read(path))
        call = re.search(r'action="refute_finding".{0,600}', flat, re.DOTALL)
        assert call, f"{label} no longer calls refute_finding"
        # Anchored INSIDE the call: an unanchored search over the whole file is
        # satisfied by a prose note elsewhere naming the keys, which is exactly
        # the shape a regressed call site would leave behind.
        payload = re.search(r'"findings":\s*\[\s*\{(.*?)\}', call.group(0))
        assert payload, (
            f"{label}: the refute_finding call carries no concrete findings payload "
            "(a prose description of the keys is not a payload)"
        )
        # Tolerate both the shorthand `{id, kind, ...}` and real JSON
        # `{"id": ..., "kind": ...}`; the contract is the key set, not a notation.
        keys = {
            k.strip().strip('"').split(":")[0].strip().strip('"')
            for k in payload.group(1).split(",")
        }
        for required in ("id", "kind", "severity", "file", "line"):
            assert required in keys, (
                f"{label}: refute_finding payload omits `{required}` (has {sorted(keys)}); "
                "`kind` is rendered verbatim into the refuter prompt and `severity` picks "
                "its model, so an omission is silent"
            )


# --- reference wiring ------------------------------------------------------


def test_every_reference_file_is_referenced_by_its_skill():
    """Content moved to a lazy reference nothing points at is content nobody reads."""
    for refs in _reference_dirs():
        skill_dir = refs.parent
        bodies = "\n".join(_read(p) for p in skill_dir.glob("*.md"))
        for ref in sorted(refs.rglob("*.md")):
            assert ref.name in bodies, (
                f"{skill_dir.name}/references/{ref.name} is referenced by no skill file"
            )


def test_every_referenced_reference_file_exists():
    pattern = re.compile(r"skills/(chameleon-[\w-]+)/references/([\w.-]+\.md)")
    for path in SKILLS.rglob("*.md"):
        for skill, ref in pattern.findall(_read(path)):
            target = SKILLS / skill / "references" / ref
            assert target.is_file(), f"{path.name} points at missing reference {skill}/{ref}"


def test_each_reference_is_loaded_with_its_own_explicit_read_now():
    """pr-review's working pattern: EACH reference is read AT the step that needs it.

    Per-reference, not per-skill: a single NOW anywhere let five of pr-review's
    six read-at-the-step directives be deleted. Word-boundary matched, because
    "NOW" is a substring of UNKNOWN.
    """
    for refs in _reference_dirs():
        bodies = "\n".join(_read(p) for p in refs.parent.glob("*.md"))
        flat = _norm(bodies)
        for ref in sorted(refs.rglob("*.md")):
            # A bare "NOW" near the name is satisfied by a NEGATION ("nothing
            # needs it NOW"), so require the read VERB as well. Case-insensitive
            # and period-tolerant, because "Read it NOW." and "read it right
            # now" weaken nothing and an over-tight window just gets deleted.
            window = re.search(
                r"(?i)\bread\b[^\n]{0,120}?" + re.escape(ref.name) + r"[^\n]{0,120}?\bnow\b",
                flat,
            ) or re.search(
                r"(?i)" + re.escape(ref.name) + r"[^\n]{0,120}?\bnow\b[^\n]{0,60}?\bread\b",
                flat,
            )
            assert window, (
                f"{refs.parent.name}/{ref.name} has no explicit 'read it NOW' directive next "
                "to its pointer; a lazily-referenced file nobody is told to read at the step "
                "that needs it is a file nobody reads"
            )


# --- the engine-signature fixes this change made ---------------------------


def test_pr_review_reads_the_review_history_envelope_correctly():
    """`findings` is a {severity: count} dict and `total` is the RECORD count."""
    flat = _norm(_read(PR_REVIEW))
    assert "`findings` is a `{severity: count}` DICT" in flat, (
        "pr-review Step 1b no longer says the per-record findings field is a dict; "
        "printing it renders `{'total': 5}` where a count was meant"
    )
    assert "`verified` is false" in flat, (
        "pr-review Step 1b no longer skips tamper-unverified ledger records"
    )


def test_pr_review_dedupes_contract_breaks_against_step_2_9e():
    """The same narrowing is reachable from two calls; cite it once."""
    flat = _norm(_read(PR_REVIEW))
    assert "contract_breaks" in flat, (
        "pr-review no longer documents `contract_breaks` in the auto-pass return, so the "
        "same caller-contract narrowing can be cited twice"
    )
    assert "One citation per break" in flat, (
        "pr-review lost the contract-break dedupe rule between Step 3h and Step 2.9e"
    )


def test_fate_stats_readers_treat_an_absent_surface_as_an_empty_ledger():
    """`surfaces` holds only surfaces with rows, so a first run has no key at all."""
    readers = {
        "deep-work": DEEP_WORK,
        "receiving": RECEIVING,
        "pr-review": SKILLS / "chameleon-pr-review" / "references" / "output-format.md",
    }
    for surface, path in readers.items():
        flat = _norm(_read(path))
        assert f'surfaces["{surface}"]' in flat, f"{surface} reader no longer reads its bucket"
        assert "holds ONLY surfaces that have rows" in flat, (
            f"{surface} reader no longer states that an absent key is the empty-ledger "
            "case rather than an error"
        )
