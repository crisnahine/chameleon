"""Three superpowers skills that need a contract and had none.

Each is a distinct hole, not one omission repeated:

- ``dispatching-parallel-agents`` had a DIRECTIVE telling the reader to put the
  archetype and canonical witness into every brief, and was not fact-bearing --
  so it demanded a map it was never given. Its upstream skill requires briefs be
  self-contained and its agents to inherit nothing, and it dispatches
  code-writing agents, the same constraint the subagent-driven-development brief
  already cites as the reason facts must live IN the brief.
- ``using-superpowers`` fires unconditionally at the start of a conversation and
  had no entry at all. The routing contract's other delivery surface, the
  SessionStart digest paragraph, is added only onto a digest that survived the
  budget fit whole -- and it is squeezed hardest inside a linked worktree, which
  is exactly where /chameleon-deep-work runs. The skill path has no such budget.
- ``executing-plans`` was deliberately absent because it "reads whatever the
  plan already carries". That holds for a plan carrying a right fact and says
  nothing about one carrying a WRONG fact: the executing session is by design a
  different session, so a plan written before a refresh, by a teammate, or
  against another checkout can name an archetype the profile no longer has, and
  nothing said which source wins.

Membership in the fact-bearing set is deliberately NOT derived from a rule here.
``brainstorming`` is fact-bearing because it commits a spec, which is neither a
plan nor a dispatch brief nor a test, so any test that re-derives the set from
its own criterion contradicts shipped behavior.
"""

from __future__ import annotations

from chameleon_mcp.peer_skill_context import _BRIEFS, _FACT_BEARING, skill_context


def _block(name: str) -> str:
    return skill_context(f"superpowers:{name}", "/nonexistent-repo-root")


def test_dispatching_parallel_agents_is_fact_bearing() -> None:
    assert "dispatching-parallel-agents" in _FACT_BEARING


def test_using_superpowers_has_a_directive() -> None:
    assert "using-superpowers" in _BRIEFS
    assert "using-superpowers" not in _FACT_BEARING


def test_executing_plans_has_a_directive() -> None:
    assert "executing-plans" in _BRIEFS
    # The per-edit hook already delivers the facts on every Edit; what the plan
    # cannot get anywhere else is the precedence rule.
    assert "executing-plans" not in _FACT_BEARING


def test_new_entries_render_a_block_with_the_composing_header() -> None:
    for name in ("using-superpowers", "executing-plans", "dispatching-parallel-agents"):
        block = _block(name)
        assert block.startswith("<chameleon-context>")
        assert f"composing with superpowers:{name}" in block


def test_executing_plans_directive_states_which_source_wins() -> None:
    # The whole point of the entry: the live block beats the plan's text.
    brief = _BRIEFS["executing-plans"]
    assert "chameleon-context" in brief
    assert "wins" in brief


def test_every_fact_bearing_skill_has_a_brief() -> None:
    assert _FACT_BEARING <= set(_BRIEFS)
