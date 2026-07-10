<!--
BEFORE SUBMITTING: Read every word of this template. PRs that leave
sections blank, contain multiple unrelated changes, or show no evidence
of human involvement will be sent back.
-->

## What problem are you trying to solve?

<!-- Describe the specific problem you ran into. If this was a Claude Code
     session issue, include:
       - What you were doing
       - What chameleon did wrong (or didn't do)
       - The exact failure mode (error, missing context, wrong archetype, etc.)
       - Ideally, a session log or transcript

     "Improving" something is not a problem statement. What broke? -->

## What does this PR change?

<!-- 1-3 sentences. What, not why. The "why" belongs above. -->

## Languages affected

<!-- Chameleon supports TypeScript/JavaScript, Ruby, and Python as first-class
     languages, with deeper framework-aware guidance for Rails (Ruby) and
     Django/DRF/Flask/FastAPI (Python).
     Mark which language(s) this PR touches: -->

- [ ] TypeScript/JavaScript
- [ ] Ruby
- [ ] Python
- [ ] Multiple languages
- [ ] Neither (infrastructure / docs / tests)

## What alternatives did you consider?

<!-- What other approaches did you try or evaluate before landing on this
     one? Why were they worse? -->

## Does this PR contain multiple unrelated changes?

<!-- If yes: stop. Split it into separate PRs. -->

## Test coverage

- [ ] Unit tests pass: `PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/unit/ -v`
- [ ] Harness library self-tests pass: `PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/journey/harness/tests/ -v`
- [ ] If this changes hooks, skills, or MCP tools: journey harness dry-run passes: `PYTHONPATH=. plugin/mcp/.venv/bin/python -m tests.journey.runner --dry-run`

## Environment tested

| Claude Code version | OS | Repo language (TS / Ruby / Python) | Result |
|---|---|---|---|
|  |  |  |  |

## Rigor

- [ ] If this is a skills change: I considered adversarial cases, not just the happy path
- [ ] If this changes a hook or MCP tool, I added a regression test under `tests/`
- [ ] I did not modify carefully-tuned content (enforcement rules, trust mechanisms, skill prose) without strong reasoning

## Human review

- [ ] A human has reviewed the COMPLETE proposed diff before submission

<!--
STOP. If the checkbox above is not checked, do not submit this PR.
-->
