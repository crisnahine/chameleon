---
name: chameleon-auto-idiom
description: Use when the user explicitly invokes /chameleon-auto-idiom to auto-derive high-quality team idioms from repo evidence, or accepts the offer after /chameleon-init or /chameleon-refresh when idioms.md has no active idioms
---

# /chameleon-auto-idiom

Derive **legit, high-value team idioms** from repo evidence and append them to
`.chameleon/idioms.md` via the structured teach path. The whole point of this
skill is the *complement*: it only proposes guidance that chameleon does NOT
already capture — not in `conventions.json`, not in `principles.md`, not in
`rules.json`, not already taught in `idioms.md`. Everything it produces is
deduplicated twice: once by you against the coverage map, once deterministically
by `check_idiom_candidates`.

## Hard rules (non-negotiable)

1. **Append-only. This skill NEVER modifies, removes, or deprecates an
   existing idiom.** Writing a new idiom with `status="active"` only appends.
   If a candidate contradicts an existing idiom, do not write it — surface the
   conflict and ASK the user whether the old idiom should be deprecated. Only
   an explicit "yes, deprecate it" authorizes a deprecation. Note the
   mechanics so you don't destroy guidance: calling
   `chameleon_lifecycle(action="teach_profile_structured", params={"slug": <existing active slug>, "status": "deprecated", ...})`
   does NOT just flip a status flag — it OVERWRITES that idiom's body with
   whatever rationale/example/counterexample you pass. To deprecate without
   losing the original guidance, re-pass the original body (read it from
   `idioms.md` first) plus a deprecation note. Never edit `idioms.md` directly.
2. **Only write candidates that are (a) verdict `novel` from
   `check_idiom_candidates` AND (b) approved by the user.** Skipped candidates
   are reported, never silently written.
3. **Evidence or it doesn't exist.** Every candidate must cite concrete repo
   evidence: ≥ 3 independent occurrences in committed source, or an explicit
   statement in the repo's own docs (README, CONTRIBUTING, docs/). Never
   propose an idiom from general best practices or from what you'd expect a
   codebase like this to do.

## When to use

- The user invokes `/chameleon-auto-idiom`.
- The user accepts the offer `/chameleon-init` or `/chameleon-refresh` makes
  when the profile has zero active idioms.
- The user wants to expand a thin `idioms.md` without dictating each idiom.

## When NOT to use

- No `.chameleon/profile.json` in the repo — run `/chameleon-init` first.
- The user states a specific known pattern — that's `/chameleon-teach`
  (faster, no mining).
- The user wants a "use X, not Y" import rule — that's
  `teach_competing_import` via `/chameleon-teach` (it drives the lint engine,
  not just prose).

## The flow

1. **Resolve + precheck.** Confirm `.chameleon/profile.json` exists in the
   repo root. If missing, suggest `/chameleon-init` and stop.
2. **Read the coverage map.** Call
   `chameleon-mcp::chameleon_telemetry(action="get_idiom_coverage", params={"repo": <abs-repo-path>})`. From `data`:
   - `existing_idioms.active` — slugs + summaries already taught. Do not
     re-derive these.
   - `covered.principles` — auto-derived principles. Do not restate them.
   - `covered.competing_imports`, `covered.import_preferences` — wrapper and
     import preferences already enforced.
   - `covered.naming`, `covered.inheritance`, `covered.error_handling`,
     `covered.convention_kinds` — structured conventions already injected at
     edit time.
   - `covered.class_contract` — per-archetype DSL macros, class decorators, and
     required methods already derived. A bare restatement is covered; an idiom
     that EXPLAINS the full contract a base/decorator implies (which macros are
     mandatory, which method every subclass defines, and why) can still be novel
     — see step 3.
   - `covered.lint_sources` — formatting/lint topics already in `rules.json`.
     Formatting is NEVER idiom-worthy.
   If `status` is `untrusted`, stop and tell the user to run `/chameleon-trust`
   (an untrusted profile withholds all content; `active_count: 0` there does
   NOT mean the profile has no idioms).
   **`checks_skipped` is a hard gate, not a footnote**, scoped per dimension:
   - `idioms.md` listed → the duplicate check itself ran blind; trust NO
     `novel` verdict until it is repaired (`/chameleon-refresh`).
   - `conventions.json` listed → the naming / inheritance / competing-import
     dedup ran blind; a `novel` verdict on a candidate touching those
     dimensions is UNVERIFIED — do not write it until repaired.
   - `principles.md` listed → the covered-by-principle check ran blind; treat a
     `novel` verdict on a candidate that restates a general principle as
     unverified.
   - `rules.json` listed → the covered-by-lint check ran blind.
   - `archetypes.json` listed → the archetype LIST you mine from is
     incomplete, but the naming/inheritance/competing dedup is unaffected (it
     reads `conventions.json`). Mine cautiously; dedup still holds.
   Report every skipped artifact.
3. **Mine the repo for Tier-2 candidates.** Read, at minimum:
   `.chameleon/profile.summary.md`, the canonical witness file of each major
   archetype, 3-5 additional representative files per major archetype, and
   the repo's own docs (README, CONTRIBUTING, docs/, code comments near the
   patterns). Hunt the dimensions AST analysis fundamentally cannot infer —
   see the candidate sources table below. **Verify the occurrence count with
   a repo-wide grep before drafting** — counting from the witness + summary
   alone undercounts and risks promoting a one-off to an idiom. Cite the
   grep numbers as evidence (e.g. "169 wrapper imports vs 4 raw").
   When `covered.class_contract[<arch>]` or
   `covered.inheritance[<arch>].dominant_base` shows a framework/gem base or
   decorator, read 3-5 of that archetype's members and capture the FULL contract
   it implies: which DSL macros are mandatory, which method(s) every subclass
   defines, and the order. The base/decorator alone is covered; the body
   contract it implies usually is not. Cite the macro/method names and the grep
   count as evidence.
3b. **Pull drift-derived anti-patterns.** Call
   `chameleon-mcp::chameleon_telemetry(action="get_drift_antipatterns", params={"repo": <abs-repo-path>})`. For each
   archetype it returns, the listed rules are conventions edits there repeatedly
   bumped against (`count` is how often), and `violation_edits` is how often edits
   drifted off-pattern. **Skip the security / enforcement rules in this list**
   (`eval-call`, `secret-detected-in-content`, and any other hard-block rule): they
   are already deterministically enforced at the deny path, and a "don't eval" /
   "don't hardcode secrets" idiom is a generic best practice the quality bar below
   rejects, not a team idiom. Mine counterexamples only from CONVENTION rules
   (`import-preference-violation`, naming, inheritance, and similar). These point at where a counterexample earns its place:
   open a flagged file for that archetype, read the actual off-pattern form the
   rule names, and let it shape that candidate's `counterexample` — the tool
   stores no wrong-way code, so you write the form from the file, never invent it.
   When you save such a candidate, set `source` to note the drift origin, e.g.
   `"drift: import-preference-violation x6"`. Skip an archetype with no flagged
   rules; do not manufacture an anti-pattern the history does not evidence.
4. **Draft at most 10 candidates.** Each candidate must have:
   - `slug` — `^[a-z][a-z0-9-]{2,63}$`, descriptive.
   - `rationale` — what to do AND why the team does it (one to three
     sentences; the why is mandatory — extract it from code comments, docs,
     or the visible consequence, never invent it; if the why is genuinely
     unknowable, write "team convention" and say so in chat).
   - `example` — real code from the repo, trimmed to the pattern.
   - `counterexample` — what NOT to write (the thing a model would plausibly
     produce without the idiom).
   - `archetype` — set when the idiom is scoped to one archetype; OMIT the
     field entirely for repo-wide idioms.
5. **Gate the batch.** Call
   `chameleon-mcp::chameleon_telemetry(action="check_idiom_candidates", params={"repo": <abs-repo-path>, "candidates": [...]})`.
   - `duplicate` / `covered` → drop, and report each with its reasons. Never
     reword a `duplicate`/`covered` candidate just to slip it past the gate —
     that reintroduces the redundancy the gate exists to prevent.
   - A reason of `slug-exists-in-deprecated`, or one ending `:deprecated`
     (e.g. `similar-to-idiom:zod-at-boundary:deprecated`), means the candidate
     re-derives a pattern the team DELIBERATELY deprecated. Don't silently
     drop it as a generic duplicate — surface it to the user as "the team
     already moved away from this; want it back?" so they can decide.
   - `invalid` → fix the field and re-check, or drop.
   - `quality_warnings` on a `novel` candidate → improve it (add the missing
     example/counterexample, fatten a thin rationale) and re-check. Prefer
     fewer, better idioms over volume.
6. **Present and ask.** Show the surviving candidates in chat — numbered, each
   with slug, rationale, archetype, and the file paths that evidence it. Ask
   the user: save all, pick numbers, or cancel. Do not write before this
   step.
7. **Write the approved ones — at most 5 per run.** Idioms inject into every
   edit's context; volume dilutes signal. When more than 5 novel candidates
   survive, drop the one with the most overlap with a stronger sibling, then
   the one with the narrowest evidence base, until 5 remain (tell the user
   what was held back — they can re-run later). For each kept candidate call
   `chameleon-mcp::chameleon_lifecycle(action="teach_profile_structured",
   params={"repo": ..., "slug": ..., "rationale": ..., "example": ...,
   "counterexample": ..., "archetype": ..., "status": "active",
   "source": ...})`. Pass `source` as the provenance string: the 2-3 evidence file
   paths you verified the occurrences in, plus the ref they were derived from
   when known (e.g. `"src/lib/api.ts, src/services/http.ts @ <production-ref-sha>"`).
   This records where an auto-derived idiom came from so a poisoned one is
   traceable and the trust gate shows its origin before `/chameleon-trust`.
   Success/failure lives at `data.status` in the envelope (`"success"` /
   `"failed"`), NOT at the top level — the call returns
   `{"api_version": "1", "data": {"status": ..., "idioms_added": ...}}`.
   Surface any `failed` envelope verbatim; do not silently mangle a slug to
   force a write.
8. **Report.** Idioms added (slugs), candidates skipped (reason each),
   explicit confirmation that existing idioms were untouched. Teaching
   changes the profile hash — if trust shows stale, suggest `/chameleon-trust`.

## Candidate sources (what AST cannot see)

| Dimension | What to look for | Where |
|---|---|---|
| Mandatory wrappers with a why | All call sites go through one helper; raw API absent or rare | service/util dirs, canonical witnesses |
| Domain vocabulary | One term used consistently where a synonym would be natural | model/class names, user copy, docs |
| Auth/security invariants | Every entry point applies the same guard | controllers, middleware, API routes |
| Money/time/precision handling | Integer cents, UTC-only, a single date lib via one helper | helpers + their call sites |
| Transaction/consistency patterns | Multi-write flows always wrapped the same way | services, jobs, models |
| Deprecated-vs-new API splits | Old and new pattern coexist; recent files use only the new | git-recent files vs older siblings |
| Cross-cutting conventions | Pagination, error envelopes, event naming, feature-flag usage | shared modules + consumers |
| Test data conventions | Factories-not-fixtures, builder helpers, network stubbing rules | spec/test dirs |
| Base-class / decorator contract | An archetype has a framework/gem `dominant_base` or class decorator (`covered.class_contract`), and its members share a body shape: typed DSL macros + a required method (ActiveInteraction `string`/`integer` + `def execute`; NestJS `@Injectable` + `execute`). Propose the full shape as one idiom even though the bare base is `covered-by-inheritance`. | canonical witness + 3-5 archetype members |

## What is NOT a candidate (the covered map decides)

- File naming/casing, import ordering, indent/quotes/semicolons — auto-derived
  (`covered.naming`, `covered.lint_sources`). The teach skill's anti-patterns
  apply here verbatim.
- "Use wrapper X, not raw Y" already present in `covered.competing_imports`.
- Bare base-class choice in `covered.inheritance` ("inherit from X" alone);
  error-handling shape in `covered.error_handling`. The CONTRACT a base implies
  (its mandatory DSL macros + required methods) is a candidate — see the
  base-class/decorator contract row above.
- Anything restating a `covered.principles` line.
- Body-shape/size guidance — `body_shape` is measured, not taught.

## Failure modes

| Failure | Action |
|---|---|
| `get_idiom_coverage` → `no profile in this repo` | Suggest `/chameleon-init`, stop. |
| `get_idiom_coverage` / `check_idiom_candidates` → `status: untrusted` | The profile isn't trusted; both tools withhold content. Tell the user to run `/chameleon-trust`, then re-run. Do NOT mine or write — `active_count: 0` here is "withheld", not "no idioms". |
| `check_idiom_candidates` → `failed` validation | Fix the batch shape (≤ 32 objects, slug + rationale each) and retry once. |
| `teach_profile_structured` → `slug already exists` | The gate should have caught it; pick a new slug, re-check, re-present that one candidate. |
| `teach_profile_structured` → `another /chameleon-teach is in progress` | Retry shortly; writes are flock-serialized. |
| Zero novel candidates survive | Honest outcome — report "profile + existing idioms already cover what the repo evidences" and suggest `/chameleon-teach` for tribal knowledge only humans hold. |

## Quality bar (apply before presenting)

For each candidate ask: **would a competent model, given chameleon's existing
injection for this repo, plausibly write the counterexample?** If no — the
idiom adds nothing; drop it. If the rationale would be true of most codebases
("write clear code", "handle errors"), it's not a team idiom; drop it. One
idiom = one actionable rule.

## Honesty Rules

- Evidence or it doesn't exist: never propose an idiom the repo's own evidence does not back. Show the occurrence count (or the corroborating files) for every candidate.
- Never manufacture a rule, a rationale, or a count. If you cannot ground it in the AST census, the import graph, or a corroborated doc, don't propose it.
- A rule a doc states but the code does not follow is documented-unverified: surface it for the human, never write it active.
- Append-only and approval-gated: never modify or remove an existing idiom, and never write `idioms.md` without explicit approval. Run candidates through `check_idiom_candidates` first.
- Record `source` provenance on every auto-derived idiom so a reviewer can trace it to its evidence at trust time.
