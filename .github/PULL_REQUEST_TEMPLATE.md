<!--
BEFORE SUBMITTING: Read every word of this template. PRs that leave
sections blank, contain multiple unrelated changes, or show no evidence
of human involvement will be sent back.

Chameleon is a private Empire Flippers plugin. Internal contributions
follow the same rigor we'd want before shipping anything to teammates.
-->

## What problem are you trying to solve?

<!-- Describe the specific problem you (or another EF engineer) encountered.
     If this was a Claude Code session issue, include:
       - What you were doing
       - What chameleon did wrong (or didn't do)
       - The exact failure mode (error, missing context, wrong archetype, etc.)
       - Ideally, a session log or transcript

     "Improving" something is not a problem statement. What broke? -->

## What does this PR change?

<!-- 1-3 sentences. What, not why — the "why" belongs above. -->

## Languages affected

<!-- Chameleon currently supports TypeScript and Ruby on Rails.
     Mark which language(s) this PR touches: -->

- [ ] TypeScript (EF client)
- [ ] Ruby on Rails (EF api)
- [ ] Both
- [ ] Neither (infrastructure / docs / tests)

## What alternatives did you consider?

<!-- What other approaches did you try or evaluate before landing on this
     one? Why were they worse? -->

## Does this PR contain multiple unrelated changes?

<!-- If yes: stop. Split it into separate PRs. -->

## Test coverage

- [ ] Unit + integration tests pass: `cd mcp && PYTHONPATH=. .venv/bin/python ../tests/run_all_orders.py`
- [ ] Real Claude Code acceptance pass on at least one EF stack: `cd mcp && PYTHONPATH=. .venv/bin/python ../tests/claude_code_acceptance_test.py`
- [ ] If this changes the slash command surface, the bash skill-triggering test passes: `bash tests/skill_triggering_test.sh`
- [ ] If this changes the MCP tool surface, the protocol test passes: `cd mcp && PYTHONPATH=. .venv/bin/python ../tests/mcp_protocol_test.py`

## Environment tested

| Claude Code version | OS | Repo (TS / Ruby) | Result |
|---|---|---|---|
|  |  |  |  |

## Rigor

- [ ] If this is a skills change: I considered adversarial cases, not just the happy path
- [ ] If this changes a hook or MCP tool, I added a regression test under `tests/`
- [ ] I did not modify carefully-tuned content (Red Flags table, rationalizations, "human partner" language) without strong reasoning

## Human review

- [ ] An EF engineer has reviewed the COMPLETE proposed diff before submission

<!--
STOP. If the checkbox above is not checked, do not submit this PR.
-->
