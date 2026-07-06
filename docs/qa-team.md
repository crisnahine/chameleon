# The chameleon QA team

Five standing QA engineers, defined as dispatchable agents under `.claude/agents/` (local-only: that directory is gitignored, so the definitions live on the dev machine, not in a fresh clone). They focus on real-world testing, not unit tests or happy-path journeys: real repos, real hooks, real upgrade paths, real failure injection. Dispatch any of them by agent type from a session in this repo, individually or as a full campaign wave.

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
- Do no harm: destructive work happens on `qa<NN>-*` copies with isolated `CHAMELEON_PLUGIN_DATA`; the ~20 real Testing Apps repos and the real `~/.local/share/chameleon` are sacred.

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
4. Remediate confirmed findings; the fix wave ends with the full testing matrix from `CLAUDE.md` (that file's "/qa" shorthand — a convention the model executes from the instructions, not an installed slash command) plus two adversarial review lenses.
5. Record the campaign. Since qa49, findings, fixes, and verified-clean credit land in the remediating release's CHANGELOG entry (with working notes in session memory), not a standalone report file. When a standalone report is wanted, it goes to `~/Documents/Projects/Testing Apps/chameleon-qa-report-<scope>-<date>.md`. Reusable fixtures stay as `qa<NN>-*` dirs either way.

## Track record

- **2026-06-06, wave 1** (pre-team, 29 ad-hoc agents, gitlabhq): 20 findings, 19 confirmed and fixed in v2.4.0. Report: `chameleon-qa-report-gitlabhq-2026-06-06.md`.
- **2026-06-06, wave 2** (the founding five-lens campaign): 19/19 prior fixes held under retest; upgrade path proven safe for existing users with zero required action; first end-to-end enforcement coverage; 5 P2 + 4 P3 new findings - including a string-literal bypass of the secret block and a 27s lint stall - all fixed in v2.5.0. Report: `chameleon-qa-report-v2.4-campaign-2026-06-06.md`.
- **2026-06-06 to 2026-06-26, later report-file waves**: qa25 (the five-lens team dispatched twice, TS + Ruby squads; remediated in v2.6.0), the gitlabhq regression retest of every 2.4.0-2.6.0 fix, ULTRACODE (v2.9.x), qa26 (fixed in-release with v2.12.0), the real-human E2E bug hunt, and the `/loop` bug-hunt campaign. Reports at the same path convention: `chameleon-qa-report-{qa25-campaign-2026-06-06, gitlabhq-2.6.0-2026-06-07, ultracode-2026-06-08, qa26-campaign-2026-06-11, realhuman-2026-06-25, bughunt-loop-2026-06-26}.md`, plus per-lens files under `qa25-wave-reports/`.
- **qa49 and later** (v2.50.0-v2.54.0, 2026-07): campaigns stopped writing Testing Apps report files. Results land in the remediating release's CHANGELOG entry (headline credit, itemized fixes, verified-clean sections - the incentive structure above, unchanged) and in session memory notes.
