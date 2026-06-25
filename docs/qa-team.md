# The chameleon QA team

Five standing QA engineers, defined as dispatchable agents under `.claude/agents/`. They focus on real-world testing, not unit tests or happy-path journeys: real repos, real hooks, real upgrade paths, real failure injection. Dispatch any of them by agent type from a session in this repo, individually or as a full campaign wave.

## Roster

| # | Agent | Lens | Dispatch when |
|---|-------|------|---------------|
| 1 | `qa-upgrade-migration` | What happens to existing users when this update ships - old state under new code, trust/data survival, downgrade, mid-workflow upgrade | Any release; any schema/engine/artifact change |
| 2 | `qa-regression` | Re-run every original failing scenario from the prior campaign; attack each fix with adjacent inputs | After any remediation release |
| 3 | `qa-enforcement` | Blocking paths in anger: mode matrix, escape hatches, Stop backstop, escalation, calibration honesty | Any enforcement/blocking/calibration change |
| 4 | `qa-language-depth` | Full hostile treatment of the TS/Ruby/Python pipelines on real repos: rule precision, archetype quality, boundary inputs, monorepos | Any AST/lint/bootstrap/cross-file change |
| 5 | `qa-failure-recovery` | SIGKILL mid-write, races, corrupt state, stale sockets, closed pipes - and whether it recovers | Any hook/daemon/transport/locking/atomic-write change |

## The mindset (all five)

- Relentless: "it works on my machine" is the start of testing, not the end. Keep going until every feature, workflow, tool, and edge case in the charter has been exercised against real state.
- Real-world first: existing users, production scenarios, integrations, deployments, performance, reliability, and recovery from failures - not just happy paths.
- Evidence or it didn't happen: every finding carries a repro command, expected vs actual, and the root-cause file:line, reproduced twice. Bug vs designed-behavior is settled by reading `docs/architecture.md` before reporting.
- Do no harm: destructive work happens on `qa<NN>-*` copies with isolated `CHAMELEON_PLUGIN_DATA`; the twelve real Testing Apps repos and the real `~/.local/share/chameleon` are sacred.

## Incentive structure

Findings are graded and credited in the campaign report and the CHANGELOG of the release that remediates them:

- **P1** (data loss, crash, silent breakage, security bypass): headline credit in the release CHANGELOG entry; the finding's repro becomes a permanent regression test named for the scenario.
- **P2** (enforcement-correctness gaps, performance cliffs, cross-session leaks): itemized credit in the CHANGELOG `Fixed` section; repro lands in the unit suite.
- **P3** (paper cuts, doc drift, contract inconsistencies): batched credit; fixed in the same wave.
- **Verified clean** reports earn credit too: proving a path safe under adversarial pressure is signal, and it is listed in the campaign report's "verified clean" section.

A rejected finding (test-harness artifact, designed behavior) costs nothing - but the same false finding reported twice without new evidence does.

## Engagement protocol

1. Scope the wave: which engineers does this change need? (A release gets all five.)
2. Dispatch in parallel; each works an independent domain with no shared state.
3. The orchestrator independently verifies every load-bearing claim (file:line, repro) before accepting it.
4. Remediate confirmed findings; the fix wave ends with the full testing matrix from `CLAUDE.md` plus two adversarial review lenses.
5. Write the campaign report to `~/Documents/Projects/Testing Apps/chameleon-qa-report-<scope>-<date>.md`; reusable fixtures stay as `qa<NN>-*` dirs.

## Track record

- **2026-06-06, wave 1** (pre-team, 29 ad-hoc agents, gitlabhq): 20 findings, 19 confirmed and fixed in v2.4.0. Report: `chameleon-qa-report-gitlabhq-2026-06-06.md`.
- **2026-06-06, wave 2** (the founding five-lens campaign): 19/19 prior fixes held under retest; upgrade path proven safe for existing users with zero required action; first end-to-end enforcement coverage; 5 P2 + 4 P3 new findings - including a string-literal bypass of the secret block and a 27s lint stall - all fixed in v2.5.0. Report: `chameleon-qa-report-v2.4-campaign-2026-06-06.md`.
