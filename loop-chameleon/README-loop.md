# /loop — coverage & gap convergence for chameleon

## Install (already in place if you're reading this from the repo)
```
.claude/commands/loop.md
.claude/agents/gap-hunter.md
.claude/skills/gap-taxonomy/SKILL.md
plugin/scripts/coverage_matrix.py
```

## Usage
```
/loop                                    # full plugin, 5 rounds, S2+
/loop --lenses L0                        # coverage matrix only (fast, deterministic)
/loop --lang python --rounds 8           # one language, deeper
/loop --lenses L0,L1,L6 --min-severity S1
/loop --fix                              # S0/S1 only, test-first, gate-verified
/loop --resume loop-20260727-104500
```

## The deterministic half, standalone
```
python3 plugin/scripts/coverage_matrix.py --repo .            # table
python3 plugin/scripts/coverage_matrix.py --repo . --json     # for CI / the loop
python3 plugin/scripts/coverage_matrix.py --repo . --strict   # exit 1 on divergence
```
Exit 0 clean · 1 divergence (with --strict) · 2 could not run.

Wire it as a CI advisory next to `chameleon-gate`:
```yaml
- run: python3 plugin/scripts/coverage_matrix.py --repo .
- run: chameleon-gate --base "$BASE_SHA" --head HEAD
```
Neither should be `|| true` — that is the L10 probe about CI jobs that cannot fail.

## Output
```
.claude/loop/<run-id>/
  state.json      resumable: round, visited, matrix, convergence stats
  findings.jsonl  append-only, stable ids, dedupe across rounds
  report.md       verdict, matrix, vacuous-silence section, coverage ledger, rejected appendix
```
