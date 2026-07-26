"""Tier-1/Tier-2 dedup is per AGENT, not per session.

"Already shown this archetype" is a claim about a context window. A session
stops being one context the moment subagents are dispatched, which is the
normal case under superpowers' subagent-driven execution: a fresh implementer
per task, deliberately inheriting none of the coordinator's history.

Every subagent nonetheless shares the coordinator's session_id -- verified
against real transcripts, where two distinct agent transcripts carry one
sessionId -- and the enforcement state that holds ``archetypes_seen`` is keyed
by exactly that. Before the per-agent key, the second and later implementers in
a run were told "you have seen this archetype" and got the one-line Tier-1
pointer instead of the canonical excerpt, inverting the tier logic precisely
where context is scarcest.

The discriminator is ``agent_id``, which a subagent's tool-call payload carries
and the top-level agent's does not (session_id and transcript_path are byte
identical across both, so neither can substitute).
"""

from __future__ import annotations

from chameleon_mcp.hook_helper import _archetype_seen_key


def test_top_level_agent_keys_on_the_bare_archetype_name():
    """No agent_id means the top-level agent. Its keys must stay exactly what
    they were, so a session's already-persisted state keeps deduping and an
    in-flight upgrade does not re-fire every archetype's Tier-2 block."""
    assert _archetype_seen_key("component", None) == "component"


def test_a_subagent_gets_its_own_key():
    assert _archetype_seen_key("component", "a680c2d3d2aaa609f") != "component"


def test_two_subagents_do_not_share_a_sighting():
    """The whole point: implementer 2 must not inherit implementer 1's sighting."""
    first = _archetype_seen_key("component", "aaaa1111")
    second = _archetype_seen_key("component", "bbbb2222")
    assert first != second


def test_one_subagent_still_dedups_against_itself():
    """Per-agent, not per-call: the same agent editing a second file in the same
    archetype should still get the short form the second time."""
    assert _archetype_seen_key("component", "aaaa1111") == _archetype_seen_key(
        "component", "aaaa1111"
    )


def test_one_agent_still_distinguishes_two_archetypes():
    assert _archetype_seen_key("component", "aaaa1111") != _archetype_seen_key(
        "service", "aaaa1111"
    )


def test_the_separator_cannot_be_forged_from_an_archetype_name():
    """agent_id and archetype are joined with a unit separator, a character no
    archetype name derived from a path can contain -- so a crafted archetype
    name cannot collide with another agent's key."""
    key = _archetype_seen_key("component", "aaaa1111")
    assert "\x1f" in key
    assert key.split("\x1f") == ["aaaa1111", "component"]


def test_a_non_string_agent_id_reads_as_top_level():
    """Payload fields are model/harness input. A malformed agent_id must degrade
    to today's session-scoped behaviour, never crash the per-edit hook."""
    for junk in (123, {"id": "x"}, [], "", None):
        assert _archetype_seen_key("component", junk) == "component"
