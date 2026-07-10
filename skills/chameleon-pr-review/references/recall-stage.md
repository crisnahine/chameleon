# pr-review reference — Step 3.9: RECALL stage

### Step 3.9: RECALL — decorrelated recall lenses (always)

This step is the pipeline's only add-path (the preamble states why):
independent fresh-context lenses over the same diff — the decorrelated-lens
design the engine's own turn-end synthesis is built on (~33% single-lens recall
vs ~72% combined in the literature it cites; see `lens_synthesis.py`). Run it on
every review, fan-out or not.

**The two lenses.** Dispatch read-only in-session Task agents (Read + read-only
chameleon MCP, no Bash, no Edit/Write, no nested Task) over the WHOLE diff:

- **Lens A — correctness/delta**: edge cases, removed guards and behavior the
  `-` lines took out, inverted conditions, error paths, spec/ticket compliance
  when a ticket exists.
- **Lens B — consequences**: downstream consumers of the changed values (trace
  who READS what this diff writes — an asymmetry between two consumers of the
  same changed quantity is this lens's classic catch), caller blast radius,
  deploy/rollout safety (in-flight jobs, ordering, backwards compatibility
  during a rolling deploy), concurrency, cross-file contract drift.

Each lens gets: the unified diff, the per-file hunk map, the repo id, the
ticket / acceptance criteria and PR description when they exist (requirements
are input, not anchoring risk — Lens A needs the spec, and Lens B traces
consumers better knowing what the change is FOR) — and the draft findings'
(`file:line`, defect-class) pairs ONLY (run the Step 4a hunk gate on the draft
first so dead anchors don't mask live lines; no reasoning, no messages), framed
as "these CLAIMS are covered; a DIFFERENT defect class at the same line is fair
game". Never hand a lens the draft findings' text: an anchored critic
re-derives the same list. Each
lens returns findings as JSON (`{file, line, section, rule, severity, message}`)
plus an `unrun_checks` list (below).

**Depth calibration** (from the Step 2.0/3h `get_autopass_verdict` already in
hand): `complexity_tier` easy/medium AND `risk` low or elevated → Lens B alone
suffices (the parent's own pass already covered the Lens A ground inline; B is
the orthogonal perspective). Tier hard/complex, OR risk high, OR any
security-surface reason, OR a ticket with acceptance criteria → BOTH lenses are
mandatory. When the verdict was degraded/unavailable: 3 or fewer changed files
→ one lens (B); otherwise both.

**Merge + gate.** Dedup lens candidates against the draft findings and each
other by (file, overlapping line range, defect class). Two lenses independently
agreeing on a new candidate is a strong signal — note it on the finding. Every
surviving NEW candidate then goes through the SAME gates as a draft finding:
the Step 4a hunk gate, and Step 4b refutation for model-judgment claims. Two
anchoring rules make that composable: (1) a consequence/cross-file candidate
(consumer asymmetry, blast radius, deploy safety, contract drift) must anchor
to the DIFF-SIDE line — the changed write/export/signature line inside a hunk —
with the out-of-diff consumer site cited in the message as corroborating
evidence (the 3f-i shape: source-side anchor, corroborating file cited),
because the consumer's own line is outside the diff by construction and the
hunk gate would drop it; a consequence claim with neither an in-diff anchor nor
a re-verified tool backing is dropped. (2) A lens claim that cites a tool
result (a caller list, an importer) is input, not truth: re-verify it yourself
with the tool before relaying — it then carries the Step 4b tool-grounded
exemption like any tool-backed finding.

**Loop until dry.** If a recall round contributed at least one BLOCK or FIX
that SURVIVED the Step 4a/4b gates (a surviving NIT never re-loops), run ONE
more recall round (fresh lens contexts, updated anchor pairs). Terminate when a
round contributes zero such survivors. Cap: 2 recall rounds total; if the
second round still added survivors, say so in the banner ("recall cap hit — a
further round may find more"). Refutation spent here draws from the review-wide
`refute_finding` budget (Step 4b's 4-call hard stop is shared, not per-stage),
and a recall candidate adjudicated in-loop keeps its verdict — never re-send it
at Step 4b.

**No-dispatch fallback.** When Task dispatch is unavailable (you are yourself a
subagent), do NOT skip RECALL: run it inline as an exclusion-set re-walk — for
each changed file, with the draft findings as the exclusion set, answer one
forced question: "name the worst defect in this hunk that is NOT already a
finding, or write CLEAN". Same merge/gate/loop rules; log
`recall-inline: no Task dispatch`.

**Unrun executable checks.** Each lens (and the inline fallback) also names the
checks it could NOT run because this review is static and offline: the specific
spec/test file that exercises the changed behavior, a deploy-state or data-shape
assumption a live query would settle ("does this column ever hold NULL in
production?"), a migration's real table size. Dedup and render them in the
"Unrun executable checks" output section — never as findings, never affecting
the verdict. This is the honest boundary of a static review, made visible so
the user can green-light exactly those checks instead of asking for "another
round" to discover them.
