# Migration-scenario A/B — chameleon vs no-guidance vs CLAUDE.md — 2026-07-11

Published per the results-published policy, WHATEVER the verdict. This started as
a search for the first positive causal result and ended as an honest, partly
unfavorable evaluation of chameleon against the one baseline that matters: a
human-written CLAUDE.md rule. Instrument: `tests/study_migration_ab.py`
(self-contained; deterministic scorer; real `claude -p`).

## Why the prior campaigns were all null

Eight session-scale A/B experiments and the repo-wide dogfood retrospective came
back null: they used **uniform** fixtures where the visible majority already
matched the convention, so a context-reading model inferred it from siblings and
chameleon had nothing to correct. The untested case was the one chameleon is
built for — a **migration state where the visible majority misleads**.

## The scenario

Five service files import the OLD internal module `./http`; one recent file uses
the NEW `./httpClient`; the team's current convention is `./httpClient` (5:1
majority lags the decision). Neutral names, no "legacy" tell. Task: add a new
service that needs the HTTP helper. Deterministic scorer: which module the new
file imports. Four arms, sonnet, N=10 each, identical fixture:

| Arm | What it is | Correct-convention rate |
|-----|------------|-------------------------|
| off | no guidance (plugin disabled) | 0/10 = 0% |
| shadow | chameleon ADVISES (counterexample injected, no deny) | 1/10 = 10% |
| on | chameleon ENFORCES (denies the old import) | 6/10 = 60% |
| static | one-line rule in CLAUDE.md, no plugin | 10/10 = 100% |

Two-sample cluster bootstrap over cells (20k resamples, fixed seed):

- on − off: **+60pp** [30, 90] — chameleon's enforcement beats no guidance.
- shadow − off: +10pp [0, 30] — advisory injection is NOT significant.
- on − shadow: +50pp [10, 80] — the DENY, not the advice, is chameleon's lift.
- **static − on: +40pp [10, 70]** — a one-line CLAUDE.md rule BEATS chameleon.

## Verdict: chameleon beats no-guidance but LOSES to a CLAUDE.md rule here

The honest, uncomfortable result. The north-star bar requires chameleon to beat
BOTH a no-plugin arm AND a static-CLAUDE.md arm. It clears the first and fails
the second — on this single-convention scenario, the sharpest comparison is
unfavorable. The earlier framing of this run as a clean "breakthrough" (measured
only against `off`) was against the wrong baseline and is corrected here.

## Why static wins — three mechanisms, each observed in the transcripts

1. **The model distrusts hook advice that contradicts the visible majority.**
   From an on-arm reasoning trace, verbatim: *"Chameleon's hook flags ./http as
   the deprecated import and wants ./httpClient instead, despite it being the
   minority pattern in existing files. That 'preferred' convention seems inverted
   from actual usage, let me check git history..."* The model treats the code's
   5:1 majority as ground truth and the hook as a possibly-wrong linter. A
   CLAUDE.md instruction is read as authoritative project policy and is not
   second-guessed. Hence shadow (advice only) barely moves off (10% vs 0%).

2. **Chameleon's own escape hatch is exploitable by the model.** The deny message
   says "add `// chameleon-ignore import-preference-violation` if intentional."
   Two on-arm cells did exactly that — added the ignore comment and kept `./http`
   — because, following the majority, the model judged its choice intentional.
   The override meant for humans to dismiss false positives became the model's
   rationalization route.

3. **The deny causes task friction.** Two on-arm cells produced no file at all:
   denied on the old import, the model went off investigating git history to
   check whether chameleon was wrong, and did not finish within the turn budget.
   CLAUDE.md has no block to route around, so the model just complies and ships.

## What this does and does not say

- It does NOT say chameleon is useless. Enforcement adds real value over both no
  guidance and advice (0% → 10% → 60%). And this tests ONE explicit convention,
  where CLAUDE.md trivially wins because there is nothing to dilute it.
- It DOES say chameleon's value proposition is not "beats a hand-written rule on
  a single clear convention." Its potential edge is elsewhere and remains
  untested here: (a) DERIVING conventions nobody wrote in CLAUDE.md; (b) SCALE —
  many conventions, where a bloated CLAUDE.md dilutes attention and chameleon
  injects only the relevant one per-edit; (c) UNESCAPABLE enforcement.
- The advisory result (10%) is the most concerning: chameleon's per-edit
  injection carries too little authority to overcome the model's trust in the
  visible majority.

## Actionable product findings this surfaced

1. **Stop advertising the escape hatch to the model in the deny reason.** A human
   who needs `// chameleon-ignore` can learn it from docs; putting it in the deny
   text hands the model a one-line rationalization. (New gap.)
2. **Advisory injection needs authority/evidence.** When chameleon's preference
   contradicts the visible majority, the advice should carry the WHY — "team
   migration in progress, N files already on the new module, decided <when>" — so
   the model trusts it instead of dismissing it as inverted. (New gap.)
3. **Reconsider deny friction for preference rules.** The no-file failures show a
   hard deny on a convention the model believes is wrong can derail the task.

## For the north-star bar

The bar (30+ tasks, judge preference, > 0.5 vs no-plugin AND vs static-CLAUDE.md,
2+ models) is NOT met, and this run shows the static-CLAUDE.md arm is the real
obstacle, not the no-plugin arm. The productive next campaign is the MULTI-
convention scenario (many rules, where per-edit relevance should beat a bloated
CLAUDE.md) plus the escape-hatch and evidence fixes above — that is where
chameleon can plausibly beat CLAUDE.md. On a single explicit rule, it does not.

## Reproduce

```bash
PYTHONPATH=. plugin/mcp/.venv/bin/python tests/study_migration_ab.py 10 sonnet off,shadow,on,static
```

## Addendum (same day): the fix — chameleon's conventions through the CLAUDE.md channel reach 100%

The unfavorable result above was diagnosed to its mechanism and fixed. Three
product changes, each verified by re-measurement (sonnet N=10 per arm unless
noted):

1. **Authority + placement fixes on the hook channel** (conventions block moved
   to the top of the SessionStart injection; explicit "existing files may be
   mid-migration, never infer the convention from sibling majority" framing;
   escape hatch re-scoped to human-approved exceptions in the deny text and the
   skill): shadow 10% -> 40%; enforce 60% -> 70% with ZERO wrong-import
   completions (every finished file was correct; remaining misses are the model
   safely stopping to ask the human, which `-p` counts as failure).
2. **Nonce-verified channel facts** (no guessing): SessionStart additionalContext
   DOES reach the model in `claude -p` (codeword test), and CLAUDE.md `@` imports
   DO resolve in `-p` (codeword test). So the hook-channel gap is authority, not
   delivery.
3. **The architecture answer — `conventions.md` via CLAUDE.md `@`-import**:
   chameleon renders its derived conventions to `.chameleon/conventions.md`; the
   repo's CLAUDE.md imports it with one line. Measured:

| Arm | sonnet | haiku |
|-----|--------|-------|
| claudemd (mirror + plugin enforcing) | **10/10** | **8/8** |
| claudemd-noplugin (mirror only) | **10/10** | not run |

   With the mirror in place the deny never fires (the model never writes the old
   import), so the friction failure mode disappears entirely.

**Shipped as a product feature**: bootstrap/refresh write `.chameleon/conventions.md`
inside the profile transaction; teach/unteach re-sync it; `/chameleon-init`
offers the one-line CLAUDE.md import (consent-gated — chameleon never edits
CLAUDE.md itself). Kill switch `CHAMELEON_CONVENTIONS_MD=0`. Unit-pinned in
`tests/unit/test_conventions_md_mirror.py`.

**Final verdict for this scenario**: chameleon-derived conventions, delivered
through the channel it now maintains, match the hand-written CLAUDE.md rule at
100% on both models — while remaining derived (nobody has to write or maintain
the rule) and enforced (the deny backstops anything the instruction misses).
The single-convention gap is closed. The multi-convention campaign (where
per-edit relevance should beat a bloated CLAUDE.md outright) is the remaining
step to the full north-star bar.
