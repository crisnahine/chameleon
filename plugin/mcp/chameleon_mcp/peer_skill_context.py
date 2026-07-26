"""Per-skill peer context: the mechanical half of composing with superpowers.

v4.5.15 routed the two plugins with one prose paragraph in the SessionStart
digest. Prose read once at session start does not survive twenty turns. The
paragraph tells the model to put the archetype and canonical into a plan, but
by the time it writes one the sentence is far behind it, and the per-edit
advisory that carries those facts does not arrive until an implementer has
already chosen a shape.

This module fires at the moment of the decision instead. A superpowers skill is
invoked through the ``Skill`` tool, so a PreToolUse hook matching ``Skill`` sees
the invocation and returns exactly what THAT skill needs, as
``hookSpecificOutput.additionalContext``.

Detection costs nothing here and reads no registry. ``tool_input.skill`` carries
the fully-qualified ``superpowers:<name>``, and Claude Code offers a plugin's
skills only when that plugin is loaded -- the invocation IS the proof of
installation. None of ``peer_plugins``' rungs, and none of their documented
blind spots (a project-level ``enabledPlugins``, a relocated install path), can
reach this path.

Two kinds of entry live in the table below, and the difference is what they
cost. A DIRECTIVE is a constant: an ordering rule, a named contradiction and its
resolution, or a pointer to the tool that answers a question the peer skill asks
without supplying one. A FACT-BEARING entry additionally renders this repo's
archetype-to-canonical map, which costs a trust check and one cached profile
read. Only the skills that author a plan, a dispatch brief, or a test carry
facts; everything else stays constant-time.

Nothing here reads a byte of the peer plugin's content. The skill NAME arrives
as model input and is never echoed back -- a matched entry is looked up by exact
table key, so what reaches the model is chameleon's own constant.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Plugin skills arrive fully qualified. Matching the prefix exactly is what
# keeps a user's own local skill named `brainstorming` from being treated as
# the superpowers one -- and what makes the invocation itself proof that the
# peer plugin is loaded.
_PREFIX = "superpowers:"

# Kill switch shared with the SessionStart routing block: both surfaces are the
# same feature (chameleon telling the model how the two plugins compose), so an
# operator who turns one off means both.
_KILL_SWITCH = "CHAMELEON_PEER_ROUTING"

_HEADER = "[🦎 chameleon: composing with superpowers:{name}]"

# Skills whose brief also carries the repo's archetype -> canonical map. They
# author something a later writer treats as authoritative -- a plan's file
# table, a dispatch brief, a first failing test -- so the shape decision is made
# HERE, before any Edit fires and before the per-edit advisory can reach it.
_FACT_BEARING = frozenset(
    {
        "writing-plans",
        "subagent-driven-development",
        "brainstorming",
        "test-driven-development",
    }
)

# Each entry answers one question: what would this skill do differently if it
# knew what chameleon knows? A skill that would do nothing differently is absent
# -- executing-plans reads whatever the plan already carries (fix writing-plans
# instead), verification-before-completion needs command output rather than repo
# knowledge, and writing-skills authors markdown outside any archetype.
_BRIEFS: dict[str, str] = {
    "writing-plans": (
        "This plan is written for an engineer assumed to have zero context on the repo, "
        "and it says to follow established patterns without any way to know what they "
        "are. The map below is what they are.\n"
        "- Name the archetype and its canonical witness beside every file in the plan's "
        "File Structure block. A path alone does not carry a shape.\n"
        "- Any task that changes an exported signature: run get_blast_radius or "
        "query_symbol_importers now and list the call sites IN the task, so the "
        "implementer updates them in the same commit instead of leaving cross-file "
        "staleness for review to catch."
    ),
    "subagent-driven-development": (
        "Two dispatches need chameleon facts, and neither subagent can go and find them.\n"
        "- The IMPLEMENTER brief is the single source of requirements by contract -- the "
        "subagent is told never to read the plan file. So the archetype and its canonical "
        "witness must be IN the brief. Chameleon's per-edit advisory reaches that subagent "
        "only after it has already picked a shape.\n"
        "- The TASK REVIEWER is deliberately context-starved (it is told not to crawl the "
        "broader codebase) while still being asked to check call sites on a contract "
        "change. Paste get_contract_breaks / query_symbol_importers / get_callers output "
        "into the review package; it is the only cheap substitute for the crawl it is "
        "forbidden to do.\n"
        "Per-edit guardrails still fire inside every subagent. The turn-end review job "
        "does not run per subagent, so a run pays for it once."
    ),
    "dispatching-parallel-agents": (
        "A brief must be self-contained, and a subagent inherits none of this session's "
        "convention context. Put the archetype, the canonical witness, and any "
        "get_blast_radius output into each brief rather than telling the agent to "
        "discover them.\n"
        "For read-only investigation chameleon ships typed agents that cannot write: "
        "chameleon:code-scout (codebase questions with file:line evidence), "
        "chameleon:web-researcher (one external unknown against the pinned version). "
        "Prefer them over general-purpose when the task is a lookup."
    ),
    "systematic-debugging": (
        "This skill owns the sequence; chameleon serves two specific steps inside it, not "
        "the whole of Phase 1.\n"
        "- Phase 1 step 5, tracing a bad value back to where it came from: that walk is "
        "BACKWARD, so it is get_callers (and get_blast_radius). get_callees is the "
        "forward counterpart -- reach for it to see what a suspect function calls, not "
        "to trace an origin. Steps 1-4 (read the error, reproduce, check recent changes, "
        "instrument boundaries) are yours; chameleon knows nothing about them.\n"
        "- Phase 2, finding a working example to compare against: search_codebase and "
        "get_canonical_excerpt locate the sibling that already does this correctly.\n"
        "Only found:true is an answer. An index-unavailable or no-calls-index result "
        "means run /chameleon-refresh; it never means 'no callers'. Dynamic dispatch is "
        "invisible to the snapshot, so an empty caller list is not proof of dead code."
    ),
    "test-driven-development": (
        "Iron Law vs a chameleon principle -- resolve it this way, do not average them.\n"
        "- WHETHER to write the test is this skill's call. It wins. Chameleon derives "
        "'skip tests where siblings have none' from what the repo currently does, which "
        "is a description of the past, not a licence to skip a test you were asked to "
        "write first. An untested area is a reason to add coverage, not to match its "
        "absence.\n"
        "- SHAPE is chameleon's. Write the failing test in the repo's own test idiom -- "
        "the canonical witness below is the file to imitate. A correct test in a shape "
        "the repo does not use still reads as noise in review."
    ),
    "brainstorming": (
        "This is the step where an architectural assumption gets locked into a committed "
        "spec, and 'explore the current structure' has a precise answer here.\n"
        "- describe_codebase gives the archetype map; the inventory below is its summary.\n"
        "- Before proposing any new module, service, or component: check the archetype's "
        "existing exports and run get_duplication_candidates. Designing a parallel "
        "abstraction beside one that already exists is the single most common thing "
        "chameleon flags after the fact, and a spec is the expensive place to lock it in."
    ),
    "requesting-code-review": (
        "/chameleon-pr-review covers most of this and adds grounded cross-file facts "
        "(contract breaks, duplication candidates, cross-file staleness) plus an "
        "adversarial refuter pass. BLOCK/FIX/NIT map to Critical/Important/Minor.\n"
        "It is not a strict superset, so choose deliberately:\n"
        "- This skill's point is dispatching a reviewer SUBAGENT so the diff never enters "
        "your own window. /chameleon-pr-review runs its convention and logic passes in "
        "YOUR context unless the engine recommends fan-out -- inside a long coordination "
        "run that context cost is real.\n"
        "- This skill's reviewer asks 'are all tests passing?'. /chameleon-pr-review is "
        "static: it runs nothing, so that verdict input is dropped, not added.\n"
        "- For the final whole-branch review of a subagent-driven run, dispatched with a "
        "review-package path and a parked-findings list, keep this skill. "
        "/chameleon-pr-review takes a PR URL or a branch, not that pair."
    ),
    "receiving-code-review": (
        "/chameleon-receiving-code-review supersedes this one and is a genuine superset: "
        "it inlines this discipline, then verifies each comment against the code and the "
        "repo's conventions before apply-or-push-back, applies fixes one at a time after "
        "per-item approval, and drafts replies without ever posting them.\n"
        "Staying here instead? The four verification questions have tools: "
        "get_pattern_context and get_canonical_excerpt answer 'is this the repo's "
        "convention or against it', get_callers and get_blast_radius answer 'does this "
        "break existing functionality'."
    ),
    "using-git-worktrees": (
        "A linked worktree keeps chameleon live: it reaches the main checkout's profile "
        "through the .git file pointer, so per-edit injection, lint, and deny gates all "
        "still fire inside it, under the same trust grant. No re-trust, no re-init.\n"
        "One ordering note: this skill asks consent before creating a worktree. "
        "/chameleon-deep-work creates one by contract and asks nothing -- invoking it IS "
        "the consent. The prompt applies when a worktree is your idea, not when the user "
        "already asked for the command that mandates one."
    ),
    "finishing-a-development-branch": (
        "Run /chameleon-pr-review before integrating. Unresolved blocking violations and "
        "pending review findings are evidence for the merge decision, not a separate "
        "cleanup pass.\n"
        "On a red baseline this skill stops, and that is right when the failure is yours. "
        "Inside a /chameleon-deep-work run an inherited pre-existing failure is reported "
        "rather than fixed, so scope holds -- do not silently adopt someone else's red "
        "suite as a blocker, and do not silently fix it either."
    ),
}


def _threshold(name: str, fallback: int) -> int:
    """A tuning threshold, floored at zero, falling back when unreadable."""
    try:
        from chameleon_mcp._thresholds import threshold_int

        return max(0, threshold_int(name))
    except Exception:
        return fallback


def _skill_key(skill: object) -> str | None:
    """The bare superpowers skill name, or None for anything else.

    Model input, so it is validated rather than trusted: a non-string, a name
    without the plugin prefix, or a name absent from the table all read as "not
    ours" and the hook stays silent.
    """
    if not isinstance(skill, str):
        return None
    name = skill.strip()
    if not name.startswith(_PREFIX):
        return None
    key = name[len(_PREFIX) :].strip()
    return key if key in _BRIEFS else None


def _pattern_facts(cwd: object) -> str:
    """The repo's archetype -> canonical-witness map, or "" when unavailable.

    Trust-gated exactly like every other profile-derived injection: an
    untrusted, ungranted, or suppressed repo contributes no facts, and the
    directive half of the brief goes out alone rather than the whole block being
    dropped -- an ordering rule is still worth stating without a witness table.

    Every seam fails open to "". A skill invocation must never be the thing that
    surfaces a profile error.
    """
    if not isinstance(cwd, str) or not cwd:
        return ""
    try:
        from chameleon_mcp.optouts import is_chameleon_suppressed
        from chameleon_mcp.profile.loader import find_repo_root, load_profile_dir
        from chameleon_mcp.profile.trust import trust_state_for
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context
        from chameleon_mcp.tools import _compute_repo_id
        from chameleon_mcp.worktree import resolve_profile_root

        root = find_repo_root(Path(cwd).expanduser())
        if root is None:
            return ""
        repo_id = _compute_repo_id(root)
        if is_chameleon_suppressed(root, repo_id, None) is not None:
            return ""
        grant = trust_state_for(repo_id)
        if grant is None or not grant.grants_root(root):
            return ""
        profile = load_profile_dir(resolve_profile_root(root) / ".chameleon")
    except Exception:
        return ""

    try:
        # `LoadedProfile.canonicals` is the whole artifact document; the
        # archetype map lives one level in, under its own key.
        doc = profile.canonicals if isinstance(profile.canonicals, dict) else {}
        canonicals = doc.get("canonicals") or {}
        if not isinstance(canonicals, dict):
            return ""
        rows: list[tuple[str, str]] = []
        for archetype in sorted(canonicals):
            entry = canonicals[archetype]
            # Schema stores a list of witnesses per archetype; tolerate the bare
            # dict a hand-edited or older profile can still hold.
            first = entry[0] if isinstance(entry, list) and entry else entry
            witness = first.get("witness") if isinstance(first, dict) else None
            path = witness.get("path") if isinstance(witness, dict) else None
            if isinstance(path, str) and path:
                rows.append((archetype, path))
        if not rows:
            return ""
        cap = _threshold("PEER_SKILL_MAX_ARCHETYPES", 12)
        shown, dropped = rows[:cap], max(0, len(rows) - cap)
        width = max(len(a) for a, _ in shown)
        lines = [f"  {a.ljust(width)}  {p}" for a, p in shown]
        if dropped:
            lines.append(f"  (+{dropped} more archetypes)")
        body = "Archetype -> canonical witness in this repo:\n" + "\n".join(lines)
        return sanitize_for_chameleon_context(body)
    except Exception:
        return ""


def skill_context(skill: object, cwd: object) -> str:
    """The context block for a superpowers skill invocation, or "".

    Returns "" for every non-superpowers tool call before touching the
    filesystem, so the common case -- a chameleon skill, a user's own skill --
    costs one string comparison.
    """
    if os.environ.get(_KILL_SWITCH) == "0":
        return ""
    key = _skill_key(skill)
    if key is None:
        return ""
    parts = [_HEADER.format(name=key), _BRIEFS[key]]
    if key in _FACT_BEARING:
        facts = _pattern_facts(cwd)
        if facts:
            parts.append(facts)
    block = "\n".join(parts)
    cap = _threshold("PEER_SKILL_CONTEXT_MAX_CHARS", 2400)
    if cap and len(block) > cap:
        # Shed the facts, never the directive: the ordering rules and named
        # contradictions are the part that has no other delivery channel, while
        # the witness map is re-derivable from the per-edit advisory later.
        block = "\n".join(parts[:2])[:cap]
    return f"<chameleon-context>\n{block}\n</chameleon-context>"


# A typed slash command is expanded into the prompt inline -- no tool call is
# ever emitted, so the Skill hook cannot see it. Anchored at the start of the
# prompt: a skill NAMED in passing is a mention, not an invocation.
_SLASH_INVOCATION = re.compile(r"^\s*/(superpowers:[A-Za-z0-9._-]+)")


def slash_skill_context(prompt: object, cwd: object) -> str:
    """The same context block for a superpowers skill typed as a slash command.

    Two invocation paths reach the same skill and only one of them is a tool
    call. When the model decides to use a skill it calls the ``Skill`` tool and
    PreToolUse sees it; when a human types ``/superpowers:brainstorming`` the
    body is expanded straight into the prompt and no hook but UserPromptSubmit
    ever runs. Covering only the first would make the composition hold for the
    model's own choices and quietly lapse for the user's.
    """
    if not isinstance(prompt, str) or _PREFIX not in prompt:
        return ""
    match = _SLASH_INVOCATION.match(prompt)
    return skill_context(match.group(1), cwd) if match else ""
