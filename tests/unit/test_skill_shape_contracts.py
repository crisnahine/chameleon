"""Structural contracts for the review/dig skills and their packaged agents.

Three graded-sonnet generations established one law about these skills
(CHANGELOG 4.5.4, 4.5.5, 4.5.13): a requirement shaped as a rendered slot or a
literal line is followed to the letter, and a requirement living in section
prose is dropped stochastically -- "exhortation inside a slot does not hold
where shape does". Every hardening those rounds bought was therefore a SHAPE,
and until now none of them was pinned: the 9-slot brief, the 9-slot report,
the ladder and experts lines, and the whole packaged-agent roster could be
edited away with the suite still green.

These tests pin the shapes, not the prose. They assert that the forcing
functions still exist and that the skill/agent wiring is internally
consistent -- a skill may not dispatch an agent type that has no definition,
and an agent may not claim a capability it does not have.
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

# Every agent the skills are allowed to dispatch. Adding one means adding its
# definition file; the roster test below is what keeps the two in step.
EXPECTED_AGENTS = {
    "code-scout",
    "pattern-reviewer",
    "recall-lens",
    "verifier",
    "web-researcher",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _norm(path: Path) -> str:
    """Read with whitespace collapsed: line WRAPPING is not part of the contract.

    A phrase these tests pin may be re-wrapped by any later edit without its
    meaning changing, so asserting against the raw text would make the suite
    fail on reflow -- noise that trains the next author to loosen the pin.
    """
    return re.sub(r"\s+", " ", _read(path))


# --- packaged agent roster -------------------------------------------------


def test_agent_roster_matches_definitions():
    on_disk = {p.stem for p in AGENTS.glob("*.md")}
    assert on_disk == EXPECTED_AGENTS, (
        f"agent roster drifted: on disk {sorted(on_disk)}, expected {sorted(EXPECTED_AGENTS)}"
    )


def test_every_agent_declares_name_and_description():
    for path in sorted(AGENTS.glob("*.md")):
        text = _read(path)
        assert text.startswith("---"), f"{path.name} has no frontmatter"
        front = text.split("---", 2)[1]
        assert f"name: {path.stem}" in front, f"{path.name} frontmatter name must match filename"
        assert "description:" in front, f"{path.name} has no description"


def test_every_agent_declares_its_tool_limits():
    """A packaged agent whose grants are implicit gets them wrong under pressure."""
    for path in sorted(AGENTS.glob("*.md")):
        front = _read(path).split("---", 2)[1]
        assert "disallowedTools:" in front or "tools:" in front, (
            f"{path.name} declares no tool allowlist or denylist"
        )


def test_no_agent_claims_a_capability_it_merely_must_not_use():
    """Namespaced MCP tools are not harness-deniable, so 'not granted' is a false claim.

    Commit d282204 replaced that phrasing with an explicit directive after the
    false-grant claim shipped; one residual survived in pattern-reviewer.md and
    is what this pins.
    """
    for path in sorted(AGENTS.glob("*.md")):
        text = _read(path)
        assert "not granted" not in text, (
            f"{path.name} claims a capability denial the harness cannot enforce; "
            "state it as a directive instead"
        )


def test_dispatched_agent_types_have_definitions():
    """A skill may not dispatch a subagent_type with no definition behind it."""
    referenced: set[str] = set()
    for path in SKILLS.rglob("*.md"):
        for match in re.finditer(r'subagent_type:\s*"chameleon:([a-z-]+)"', _read(path)):
            referenced.add(match.group(1))
    assert referenced, "no skill dispatches a packaged agent -- the wiring regressed"
    missing = referenced - EXPECTED_AGENTS
    assert not missing, f"skills dispatch undefined agent type(s): {sorted(missing)}"


def test_read_only_agents_have_no_shell():
    """The verifier's missing shell is load-bearing, not an oversight.

    A review subagent once wrote two rows outside a transaction into a shared
    test database and handed back a RED suite (CHANGELOG 4.5.14). Removing the
    shell makes that class impossible rather than merely forbidden.
    """
    for name in ("code-scout", "pattern-reviewer", "recall-lens", "verifier"):
        front = _read(AGENTS / f"{name}.md").split("---", 2)[1]
        assert "Bash" in front, f"{name}.md must deny Bash in its frontmatter"


# --- deep-work: the shapes the graded rounds bought ------------------------


def test_deep_work_brief_is_a_fixed_slot_template():
    text = _read(DEEP_WORK)
    for slot in (
        "**Goal & criteria**",
        "**Files**",
        "**Contracts**",
        "**Unknowns**",
        "**Re-audit line**",
        "**Plan**",
        "**Risks & rollback**",
        "**Ladder line**",
        "**Experts line**",
    ):
        assert slot in text, f"deep-work brief lost its {slot} slot"


def test_deep_work_report_is_a_fixed_slot_template():
    text = _read(DEEP_WORK)
    for slot in (
        "**Built**",
        "**Evidence table**",
        "**Guard checks**",
        "**Review convergence**",
        "**Finding fates recorded**",
        "**Defaults taken**",
        "**Not verified**",
        "**Worktree**",
        "**Proactive follow-ups**",
    ):
        assert slot in text, f"deep-work report lost its {slot} slot"


def test_deep_work_plan_steps_carry_a_per_step_verify_clause():
    """Sonnet collapses the plan into the files list unless the shape forbids it."""
    text = _norm(DEEP_WORK)
    assert "-> verify:" in text
    assert "a line without its `-> verify:` clause is not a step" in text


def test_deep_work_ladder_and_experts_lines_carry_counts():
    """'I read every app/ file' over 15 of 19 is why these carry numbers."""
    text = _norm(DEEP_WORK)
    assert "read N of M source files" in text
    assert "Experts: <N> dispatched" in text


def test_deep_work_step_5_renders_a_build_log():
    """Step 5 was the last free-prose region, so the honesty defect relocated there.

    The build log makes a skipped plan step visible as a missing line instead
    of leaving the Step 7 not-verified slot to reconstruct it from memory.
    """
    text = _norm(DEEP_WORK)
    assert "build log" in text.lower()
    assert "Baseline:" in text
    assert "Step <n>/<N>:" in text
    assert "Derive this slot from the Step 5 build log" in text


def test_deep_work_universal_claims_are_transcriptions():
    text = _read(DEEP_WORK)
    assert "counted" in text and "recalled" in text


# --- receiving-code-review: the template it never had ----------------------


def test_receiving_renders_a_fixed_adjudication_report():
    """The one review skill that never got the v4.5.5 slot treatment."""
    text = _read(RECEIVING)
    for marker in (
        "Comments: N fetched",
        "Verification records: M/N complete",
        "| # | reviewer | file:line | class | ask | verdict | grounded by |",
        "Grounding: R round(s)",
        "Finding fates recorded:",
        "Drafted replies:",
        "Not verified:",
        "Implementation queue:",
    ):
        assert marker in text, f"receiving report lost its {marker!r} slot"


def test_receiving_report_declares_empty_slots_rather_than_dropping_them():
    text = _read(RECEIVING)
    assert "none - <reason>" in text
    assert "completeness pass" in text


# --- refuter call shape (the E1/E2 defect class) ---------------------------


def test_every_refute_finding_call_site_carries_kind_and_severity():
    """`kind` is rendered verbatim into the refuter prompt; `severity` picks its model.

    Omitting them is silent: the prompt reads a literal `kind: None`, and the
    model ladder never escalates, so the highest-stakes findings are
    adjudicated by the base model (refuter.py `_refuter_model_for`).
    """
    call = re.compile(r'action="refute_finding".{0,400}?\]', re.DOTALL)
    seen = 0
    for path in SKILLS.rglob("*.md"):
        for match in call.finditer(_read(path)):
            body = match.group(0)
            if "findings" not in body:
                continue
            seen += 1
            assert "kind" in body, f"{path.name}: refute_finding call omits `kind`"
            assert "severity" in body, f"{path.name}: refute_finding call omits `severity`"
            assert "id" in body and "file" in body and "line" in body, (
                f"{path.name}: refute_finding call omits id/file/line"
            )
    assert seen >= 3, f"expected a refute_finding call shape in each review skill, found {seen}"


# --- reference wiring ------------------------------------------------------


def test_every_reference_file_is_referenced_by_its_skill():
    """Content moved to a lazy reference nothing points at is content nobody reads."""
    for refs in SKILLS.glob("*/references"):
        skill_dir = refs.parent
        bodies = "\n".join(_read(p) for p in skill_dir.glob("*.md"))
        for ref in sorted(refs.glob("*.md")):
            assert ref.name in bodies, (
                f"{skill_dir.name}/references/{ref.name} is referenced by no skill file"
            )


def test_every_referenced_reference_file_exists():
    pattern = re.compile(r"skills/(chameleon-[a-z-]+)/references/([a-z-]+\.md)")
    for path in SKILLS.rglob("*.md"):
        for skill, ref in pattern.findall(_read(path)):
            target = SKILLS / skill / "references" / ref
            assert target.is_file(), f"{path.name} points at missing reference {skill}/{ref}"


def test_lazy_references_are_loaded_with_an_explicit_read_now():
    """pr-review's working pattern: a reference is read AT the step that needs it."""
    for refs in SKILLS.glob("*/references"):
        bodies = "\n".join(_read(p) for p in refs.parent.glob("*.md"))
        assert "NOW" in bodies, (
            f"{refs.parent.name} has references but no explicit read-NOW instruction"
        )
