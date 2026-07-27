---
description: Adversarial gap-hunting loop for chameleon itself. Audits rule/advisory coverage across every supported language and framework, then hunts logic, workflow, edge-case and consistency gaps until convergence.
argument-hint: "[scope glob] [--rounds N] [--min-severity S0|S1|S2] [--lenses L0,L5,L9] [--lang typescript,ruby,python] [--fix] [--resume <run-id>]"
allowed-tools: Read, Grep, Glob, Bash, Edit, Write, Task, Agent, TodoWrite, TaskCreate, TaskUpdate
model: inherit
---

# /loop — coverage & gap convergence for chameleon

This repo's founding invariant, from `gate.py`:

> a gate that could not run must never be mistaken for a gate that ran and found nothing

This command turns that invariant on the repo itself. A rule that flags nothing on Python
because nobody implemented the Python branch is indistinguishable — from every consumer's side —
from a rule that ran and found the file clean. `BLOCK_RULE_LANGUAGES` exists because that bug
shipped once (a Ruby profile certified `jsx-presence-mismatch` active at fp_rate 0.0 by flagging
nothing **vacuously**). Nothing in the repo verifies that map against the implementation it
describes. **L0 is that verification.**

Raw args: `$ARGUMENTS`

## Ground truth (injected)

- HEAD: !`git branch --show-current 2>/dev/null` @ !`git rev-parse --short HEAD 2>/dev/null`
- Version parity across the six manifests: !`bash scripts/bump-version.sh --check 2>&1 | grep -E 'sync at|DRIFT|MISSING|^  [0-9]+\.'`
- Schema version: !`grep -oE 'CURRENT_SCHEMA_VERSION = [0-9]+' plugin/mcp/chameleon_mcp/profile/schema.py 2>/dev/null`
- Rules in engine: !`grep -ohE 'rule="[a-z-]+"' plugin/mcp/chameleon_mcp/*.py | sort -u | wc -l`
- Extractors: !`ls plugin/mcp/chameleon_mcp/extractors/*.py | grep -vE '_base|registry|__init__'`
- Sidecars: !`ls plugin/scripts/*_dump.* 2>/dev/null`
- Churn (90d, source only): !`git log --since=90.days --name-only --pretty=format: -- 'plugin/**/*.py' ':(exclude)*test*' 2>/dev/null | grep -v '^$' | sort | uniq -c | sort -rn | head -15`

---

## Phase 0 — Bootstrap

`run_id = loop-$(date +%Y%m%d-%H%M%S)`, state under `.claude/loop/$run_id/`.
Defaults: `--rounds 5`, `--min-severity S2`, all lenses, all three languages, no `--fix`.
`--resume` loads existing `state.json` and continues.

```jsonc
{ "run_id": "...", "round": 0, "max_rounds": 5, "min_severity": "S2",
  "languages": ["typescript","ruby","python"],
  "frameworks": [],        // filled by L0
  "matrix": {},            // rule -> {declared, observed, unproven}
  "visited": [],           // novelty pressure
  "findings": [], "rejected": [],
  "rounds": [] }           // [{round, strategy, new, rejected, lens_outcomes}]
```

Seed TodoWrite with one item per planned round.

## Phase 1 — L0: the coverage matrix (always first, always deterministic)

```bash
python3 plugin/scripts/coverage_matrix.py --repo . --json
```

Exit 2 means the matrix could not be built (a module moved, source unparseable). **Abort the
run and report that** — do not fall through to the other lenses and present a partial sweep as
a coverage audit.

The script is a **narrowing device, not an oracle.** It still over-reports a rule whose
enclosing block references several languages' constants, as `weak-hash` does. So it renders
`?` (no evidence) and never `X` (confirmed absent). **Every `?` is a read assignment, not a
finding.** Promoting a `?` to a finding without opening the construction site commits the
exact error the tool exists to catch. `A` is a separate verdict — language-agnostic by
construction, nothing on the emission path takes a `language` at all — and needs no read.

Five ownership idioms coexist in the engine, which is why no extraction is clean:

| Idiom | Example | Detectable by |
|---|---|---|
| `if language == "ruby":` guard | `scan_dangerous_sinks` | AST branch walk |
| language-prefixed constant | `_TS_THEN_RE` vs `_RUBY_SQL_INTERP_RE` | name convention |
| per-language helper dispatch | `_python_naming_violations` | function name |
| anonymous `else:` arm | `ruby / elif python / else` → TS | complement of the chain's tests |
| no language at all | `scan_secrets(content, *, max_results)` | absence of a `language` param |

**That mixing is itself finding #1 of every run.** Report it once per run, not once per rule.

Two shapes read as absent to any parent-scope walk, so check a `?` against them before
believing it: a rule named through a module constant (`rule=_RULE`, as `phantom_imports.py`
does — no `rule="..."` literal exists anywhere), and a rule emitted from a shared helper
whose language dispatch lives in the CALLER, since a call edge is not a parent edge.

Then build the second half of the matrix by hand — the frameworks. `_classify_framework` returns
exactly six (`rails`, `django`, `flask`, `fastapi`, `nextjs`, `nestjs`); `principles.py` keys the
same six. Anything referenced elsewhere but never *classified* (e.g. `vue`/`react` appear in
`conventions.py`) is a coverage claim with no detection behind it — check before believing it.

## Phase 2 — Parallel gap sweep

Read `.claude/skills/gap-taxonomy/SKILL.md` for lens definitions and chameleon-specific probes.
Dispatch one `gap-hunter` sub-agent per lens in batches of 3–4, each receiving: lens ID + probes,
the round's traversal strategy, the `visited` set, the L0 matrix, and the target languages.

Traversal rotates per round so the loop stops rediscovering the same three bugs:

| Round | Strategy | Entry |
|---|---|---|
| 1 | matrix-first | every `?` cell from L0, then every rule with asymmetric language coverage |
| 2 | channel-first | the five delivery channels — SessionStart, PreToolUse Edit, PreToolUse Skill, PostToolUse/Stop, `chameleon-gate` |
| 3 | diff-first | 90-day churn on `plugin/**/*.py`, tests excluded |
| 4 | boundary-first | `safe_open`, `sanitization`, `profile/trust`, `locks`, `worktree` — the security chokepoints |
| 5 | leaf-first | modules with no recent churn: `kind_labels`, `repo_id`, `log_rotation`, `optouts` |
| 6+ | cold-sampling | unvisited modules weighted by LOC (`tools.py` at 15k and `lint_engine.py` at 5.1k are where boundary discipline is thinnest) |

## Phase 3 — Adversarial verification

Every candidate must survive all four before entering the ledger:

1. **Anchor** — `path:line-range` + the symbol. No anchor → drop.
2. **Trace** — from an input a real caller produces to the bad outcome. For this repo the caller
   is usually a hook payload, an MCP tool call, a committed profile, or a git revision range.
3. **Falsify** — argue against your own finding and grep for the counter-argument: an upstream
   guard, a `_coerce_*` fail-open, a threshold read at call time, a `safe_open` boundary check,
   an existing test. Write the counter-argument into the finding. If it wins → `rejected`,
   recorded so round N+1 doesn't re-raise it.
4. **Classify** — severity × confidence × authority:

```
SEVERITY
S0  a silent-failure path (the §2 class), data loss, trust bypass, secret leak
S1  a rule/advisory that never fires for a supported language or framework;
    unhandled failure with no recovery on a real hook path
S2  edge case, missing state, inconsistency degrading the model's context
S3  nit

CONFIDENCE   proven (traced or reproduced) | probable (static trace) | speculative (NOT REPORTED)

AUTHORITY — what backs the claim, mirroring this repo's own ladder
absolute  language/runtime semantics, a filesystem fact, a failing test
taught    an explicit invariant this repo states in a docstring or comment
derived   observed frequency across sibling modules — NOT a decision; never render as
          "violates convention", render as "11 of 13 siblings do X; this one doesn't"
```

An `S0` claiming `derived` authority is a contradiction — re-classify one of them.

### The not-run rule

`[]` is three different outcomes and they must not collapse:

| Outcome | Meaning | Counts toward convergence |
|---|---|---|
| `clean` | ran every probe over the seed set, found nothing | **yes** |
| `partial` | ran, hit the read budget, named what it skipped | **no** |
| `not_run` | lens inapplicable, tool absent, source unparseable, agent error | **never** |

`partial`/`not_run` propagate to the report's coverage ledger **by ID**. If more than a third of
lenses return `not_run` in any round, abort — that is a run that could not look, not a run that
found nothing.

## Phase 4 — Ledger

`.claude/loop/$run_id/findings.jsonl`, one object per line, `id = sha1(path+lens+symbol)[:10]`
for stable dedupe across rounds. Fields: `id, round, lens, severity, confidence, authority, path,
lines, flow, gap, languages_affected, frameworks_affected, what, why_it_matters,
counter_argument, repro, fix_sketch, queries_run, status`.

`gap` ∈ `Vacuous-silence | Missing-language | Missing-framework | Missing-guard | Missing-step |
Unreachable | Inconsistent | Unhandled-failure | Race | Contract-drift | Untested`.

`Vacuous-silence` is this repo's signature failure and gets its own report section.

## Phase 5 — Fix (only with `--fix`)

- `S0`/`S1` + `confirmed` only. Never batch.
- **Failing test first**, in the battery that owns the surface (`tests/` mirrors the module tree).
- One finding = one atomic change.
- **Postcondition:** `python3 plugin/scripts/coverage_matrix.py --repo . --strict` must not
  introduce a new `?`, and `chameleon-gate --base <pre-fix-sha> --strict` must pass. Exit 2 from
  either is a **failed** postcondition, not a pass — say so rather than proceeding quietly.
- A fix that adds a language branch must also update `BLOCK_RULE_LANGUAGES`, or it has moved the
  gap rather than closed it.
- Two failures → revert, mark `wontfix` with the reason, move on.

## Phase 6 — Convergence

```
ABORT  when > 1/3 of lenses returned not_run this round

STOP   when two consecutive rounds produced 0 new findings ≥ min_severity
         AND every lens in both rounds returned `clean`
       or round == max_rounds
       or visited ≥ 95% of in-scope modules AND new < 2 AND no lens is `partial`

CONTINUE otherwise → increment, rotate strategy, re-dispatch Phase 2
```

Per-round line, so the loop is observable:

```
round 2 [channel-first]  new: 5 (S0:1 S1:2 S2:2)  rejected: 7  visited: 61/118 (52%)
                         lenses: 8 clean, 1 partial, 1 not_run(L7: no ruby fixture repo)   3m12s
```

## Phase 7 — Report

`.claude/loop/$run_id/report.md`:

1. **Verdict** — one paragraph, no hedging.
2. **Coverage matrix** — the L0 table, with every `?` resolved to confirmed-gap or
   confirmed-present *by a read*, and the reason.
3. **Vacuous-silence findings** first, separately. This repo's own doctrine ranks them above
   ordinary bugs and the report should too.
4. **Convergence table** — rounds × new × strategy. If it did not converge, say so loudly.
5. **Coverage ledger** — a table of every lens × outcome with reasons and file counts.
   Mandatory, never empty; if every lens ran clean, state that explicitly. A report whose
   silence about L7 is indistinguishable from a report whose L7 passed is the bug this
   whole command is about.
6. **Rejected appendix** — id, what it looked like, why it isn't real.

Print a ≤15-line digest and stop.
