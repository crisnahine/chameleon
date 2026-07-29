"""Structural contracts for the review/dig skills and their packaged agents.

Three graded-sonnet generations established one law about these skills
(CHANGELOG 4.5.4, 4.5.5, 4.5.13): a requirement shaped as a rendered slot or a
literal line is followed to the letter, and a requirement living in section
prose is dropped stochastically -- "exhortation inside a slot does not hold
where shape does". Every hardening those rounds bought was therefore a SHAPE,
and none of them was pinned: the 9-slot brief, the 9-slot report, the ladder
and experts lines, and the whole packaged-agent roster could be edited away
with the suite still green.

These tests pin the shapes. Two rules keep them from becoming the vacuous
guard the first draft of this file was:

1. **Scope every assertion to the section that must carry it.** A bare
   ``token in whole_file`` passes when the token survives anywhere -- an
   adversarial pass proved all six deep-work contracts here were jointly
   satisfiable by a 12-line prose stub that merely NAMED the slots.
2. **Pin the rendered artifact, not the vocabulary.** A template is a fenced
   block with ordered slots; a rule is its directive sentence, contiguous, so
   an inverted restatement ("universal claims may be recalled rather than
   counted") cannot satisfy it.
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


def _section(text: str, heading_startswith: str) -> str:
    """Return one `## ` section's body: the heading to the next `## `.

    Scoping is what stops a slot name surviving as a historical mention
    elsewhere in the file from satisfying a template contract.
    """
    lines = text.splitlines()
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.startswith("## ") and ln[3:].startswith(heading_startswith)
        ),
        None,
    )
    assert start is not None, f"no `## {heading_startswith}...` section found"
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
    """Every token present in this section, in this order."""
    flat = _norm(section)
    positions = []
    for token in tokens:
        idx = flat.find(_norm(token))
        assert idx >= 0, f"{what} lost its {token!r} slot (searched only its own section)"
        positions.append(idx)
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
        assert re.search(r"(?i)tool limits|you (are|have)", body), (
            f"{path.name} states no tool limits in its body"
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
        granted = front.get("tools", "")
        denied = front.get("disallowedTools", "")
        assert "Bash" not in granted, f"{name}.md GRANTS Bash via `tools:` -- it must be read-only"
        assert "Bash" in denied, f"{name}.md must deny Bash via `disallowedTools:`"


def test_agents_that_feed_the_refuter_use_its_severity_vocabulary():
    """`blocking` is not `block`; the near-miss never escalates the model."""
    body = _norm(_agent_body(AGENTS / "verifier.md")).lower()
    match = re.search(r"`severity` is one of ([^.]+)", body)
    assert match, "verifier.md no longer states its severity vocabulary"
    emitted = set(re.findall(r"`([a-z]+)`", match.group(1)))
    assert emitted & REFUTER_HIGH, (
        f"verifier emits {sorted(emitted)}, none of which is in the refuter's "
        f"escalation set {sorted(REFUTER_HIGH)} -- its top severity would never escalate"
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
    assert "render every slot below in order" in _norm(section), (
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
    assert "none - <reason>" in _norm(section), (
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
    flat = _norm(_read(DEEP_WORK))
    assert "counted, not recalled" in flat, (
        "deep-work lost the universal-claim transcription rule (v4.5.12); note an "
        "inverted phrasing that merely contains both words does NOT satisfy this"
    )
    assert "counted from the session record" in flat, (
        "deep-work honesty rules lost the universal-claim transcription rule"
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
            "| # | reviewer | file:line | class | ask | verdict | grounded by |",
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
    assert "completeness pass" in section and "all 8 slots present in order" in section, (
        "receiving report lost the terminal completeness pass that checks the slots"
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
        assert 'action="refute_finding"' in flat, f"{label} no longer calls refute_finding"
        # The payload list, wherever it sits relative to the action name.
        payload = re.search(r'"findings":\s*\[\{([^}]*)\}', flat)
        assert payload, f"{label}: refute_finding call carries no concrete findings payload"
        keys = {k.strip() for k in payload.group(1).split(",")}
        for required in ("id", "kind", "severity", "file", "line"):
            assert required in keys, (
                f"{label}: refute_finding payload omits `{required}` (has {sorted(keys)})"
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
            window = re.search(
                re.escape(ref.name) + r"`?[^.]{0,40}?\bNOW\b",
                flat,
            )
            assert window, (
                f"{refs.parent.name}/{ref.name} has no explicit read-NOW directive next to "
                "its pointer; a lazily-referenced file nobody is told to read now is unread"
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
