# pr-review reference — Step 4: hunk gate, refutation, output template, severity, verdict rules, ledger (Steps 5, 5b)

#### 4a. Hunk gate (apply before formatting any logic finding)

Every per-line finding must anchor to a specific line in a changed file. This is every section, not just the logic passes: the logic findings (Steps 3b-3f), the security findings (taint/SSRF/traversal Step 2.6c and secret Step 2.6a), the deterministic lint-sink findings (Step 2.6d), and the line-anchored lint/naming/inheritance violations (Steps 2b/2d). Look the anchor line up in the per-file hunk map from Step 1a. If it is NOT inside an added or changed range for that file, drop the finding. No exceptions and no judgment call: a per-line finding on a line this change did not touch is pre-existing by construction, and the integrity rule forbids flagging pre-existing issues.

This gate is the mechanical replacement for "decide by hand whether this is PR-introduced." It does not apply to convention findings whose anchor is the file as a whole (duplication NITs, missing-test NITs), the whole-diff cross-file passes (co-change FIX in Step 2.8, and the layering / duplication / existence-break / contract-break findings in Step 2.9), nor to missing-requirement BLOCKs in Step 3b (those flag the ABSENCE of code, so they have no anchor line). The cross-file passes are exempt because they are gated on their tool/artifact backing instead, NOT because they are line-free: a co-change or layering finding anchors to a file or an artifact entry, but an existence-break (Step 2.9c) and a contract-break (Step 2.9e) each anchor to a real importer/caller `file:line` that by construction lives in a NON-diff file — applying the hunk gate to that caller line would wrongly drop every valid cross-file finding, so these are gated on `high_confidence` / a returned caller list, never on the hunk map. (The 2.9c existence break additionally carries its own diff-scope gate on the MODULE side — the exporting file must be in this diff's changed set, else the break is pre-existing and goes to the hygiene note; see Step 2.9c.) The gate applies to every per-line claim: removed guards, inverted conditions, dropped awaits, null-guard gaps, placeholder names, the stale-comment NIT (Step 3f-ii) and the stale-test removed-export anchor (Step 3f-i), the taint/SSRF/traversal findings from Step 2.6c, the deterministic lint-sink findings from Step 2.6d, AND the secret findings from Step 2.6a. The secret scanner reads the full file content, not the diff, so a hit is not in the change by construction: an out-of-hunk hard-kind secret goes to the "Pre-existing repo hygiene" note (Step 2.6a) instead of the verdict.

It ALSO applies to any line-anchored convention/style finding from `lint_file` (Step 2b/2d). `lint_file` reads the whole file, not the diff, so a `style-rule-violation` (e.g. "line 19 is 103 cols"), a `naming-convention-violation`, or an `inheritance-convention-violation` can sit on a line this change never touched. Parse the line number out of the violation `message` (the `line N` / `:N` it carries) and run it through the hunk map the same way as a logic finding: if the line is outside an added or changed range, drop it from the verdict. A line-anchored style/convention nit that pre-dates the change is pre-existing by construction and the integrity rule forbids reporting it; if it is worth mentioning at all, it goes to the "Pre-existing repo hygiene" note, never the Convention-findings section. Only convention findings with NO parseable line (duplication, missing-test, key-export overlap, which anchor to the file) stay exempt.

#### 4b. Round 3 — independent refutation (model-judgment findings only)

After rounds 1-2 (Step 4a + the verification bullet), collect every surviving
BLOCK and FIX whose evidence is MODEL JUDGMENT — your reading of the code, not a
tool flag. Defined by principle, not a hand-list (which drifts as the finding
taxonomy grows): a finding is model-judgment when it is NOT in the tool-grounded
exempt set below. Typical members: change-delta logic (removed guard, dropped
await, inverted condition), canonical divergence, taint/SSRF/path-traversal,
callable-signature drift, spec-compliance / missing-requirement. Send them in
severity order (BLOCKs first) in batches of at most 8 findings per call — the
refuter's per-invocation spawn cap is 8, so a single over-cap call silently
returns "unverified / refuter cap reached" for finding 9 onward, leaving exactly
the long-tail findings of a big review unadjudicated. Call `refute_finding` once
per batch until every model-judgment BLOCK/FIX has a real verdict; hard stop
after 4 calls (32 findings) ACROSS THE WHOLE REVIEW — the Step 3.9 recall
rounds draw from this same budget, and a recall candidate already adjudicated
in-loop keeps its verdict and is never re-sent here. Label any remainder
cap-reached:

`chameleon_review(action="refute_finding", params={"repo": <repo_id>, "findings": [{id, kind, severity, file, line, claim, evidence}, ...], "base_ref": <base>})`

Three exclusions from the send set:
- **Runtime-state findings are never sent — convert them instead.** A finding
  whose truth depends on runtime, production, or deploy state (a data-shape
  assumption — "this rule never fires if the column is NULL in production" —
  deploy order, live config, real table size) cannot be adjudicated by a static
  refuter that is commanded to refute on cannot-tell; sending it is shredding
  it. Convert it to an "Unrun executable checks" line carrying the exact
  query/command that settles it, and note the conversion in the grounding
  banner.
- **TOOL-GROUNDED findings are EXEMPT** — never send them; verify inline by
  re-confirming the tool flag still holds (existence-break with `high_confidence`,
  contract-break with a returned caller list (Step 2.9e), duplication with a
  returned candidate, co-change `rule_id`, layering, a secret `lint_file` hit
  (Step 2.6a), a deterministic lint-sink hit (Step 2.6d), a lint/naming/inheritance
  violation with a parsed line). The refuter sees one excerpt and cannot re-derive
  cross-file evidence, so sending these would wrongly drop the strongest findings.
- **NITs are never sent** — they are verified inline only. The always-NIT
  model-judgment findings (placeholder-name in 3f, stale-comment in 3f-ii) are
  therefore NOT sent even though they are model-judgment; only a surviving BLOCK or
  FIX goes to the refuter.

Each finding MUST carry a unique `id` (verdicts map back by `id`) and `file`/`line`
(the refuter prefetches that excerpt; omit them and it silently degrades to the
whole-branch diff).

Read the envelope `refuter` field FIRST, not only the per-finding verdicts — the
two disagree by design:
- `refuter == "disabled"` (CHAMELEON_REVIEW_REFUTER=0): the call returns an EMPTY
  `verdicts` list — no per-finding entries at all. Do NOT expect one `unverified`
  per finding here.
- `refuter ∈ {"unavailable", "untrusted"}` (the refuter model could not spawn, or
  the profile is untrusted): one `unverified` verdict per finding.
- `refuter == "enabled"` (the success state — the engine returns `enabled`, never
  `ok`): per-finding `refuted` / `confirmed` / `unverified` mapped by `id`. A
  finding beyond the per-invocation spawn cap comes back `unverified` with `reason`
  "refuter cap reached" while the envelope stays `enabled` — treat a cap-reached
  `unverified` like any other `unverified` (KEEP, round 3 unavailable for it).

Then apply:
- `refuted` → DROP the finding (the refuter rebutted the cited evidence).
- `confirmed` → KEEP it (this never authorizes an edit or a post).
- `unverified`, OR `refuter ∈ {disabled, unavailable, untrusted}`, OR any finding
  with no matching verdict `id` → KEEP it on rounds 1-2, labeled "self-verified,
  round 3 unavailable", with downgraded confidence. Never drop and never silently
  confirm.

Banner: report `<b>` refuted-dropped, `<c>` inline-exempt, `<d>` self-verified,
and `<e>` converted to unrun checks (runtime-state) — omit `<e>` when zero.
NEVER print "3/3" when round 3 did not adjudicate — that is the `disabled`,
`unavailable`, or `untrusted` envelope, AND any individual finding the refuter
returned `unverified` (including a cap-reached tail on an otherwise-`enabled` call).

Format the review as follows:

```
## Verdict: [APPROVE / APPROVE WITH NITS / NEEDS CHANGES / BLOCK]

Reviewed N files against chameleon conventions + [ticket KEY / branch diff].

Reasoning: <one or two sentences naming the decisive finding(s) behind the verdict — e.g. "Blocks on a removed nil guard in order.rb:47; otherwise in-pattern." For APPROVE, name what made it clean. This is the superpowers "Ready to merge + reasoning" assessment.>

Grounding: rounds 1-2 self-verified; round 3 independently refuted <b> dropped, <c> inline-exempt, <d> self-verified (round 3 unavailable).
Recall: <2 lenses x R round(s) | 1 lens (<calibration reason>) | inline (no Task dispatch)> — <K> candidates, <J> survived VERIFY<, <e> converted to unrun checks (runtime-state) when e > 0><; "recall cap hit — a further round may find more" when capped>.
Review fan-out: <inline | M parallel agents over N files>.

### Strengths / verified clean
- <specific: e.g. "src/api/user.ts follows the `api` canonical; signal param present; tests paired">

### Convention findings (X issues)

**BLOCK:**
- `path/to/file:14` — [violation message from lint_file or canonical comparison]

**FIX:**
- `path/to/file:22` — [convention violation: what's wrong and what the codebase convention is]

**NIT:**
- `path/to/file` — Similar utility already exists in key_exports list
- `path/to/file:31` — Placeholder name `data2`; siblings name this `parsedRows`
- `path/to/file:18` — Comment says "returns null on miss" but the changed line now raises; comment not updated (stale-comment, Step 3f-ii)

### Logic findings (Y issues)

The change-delta pass (Step 3e) always runs; the spec-compliance findings (Step 3b/3d) only appear with a ticket.

**BLOCK:**
- `path/to/file:47` — Removed the nil guard on `user` that the deleted line had; the new code dereferences it
- Acceptance criterion "X" has no implementation in this diff

**FIX:**
- `path/to/file:22` — Dropped `await` on `fetchTotals()`; the result is still used on the next line
- `path/to/file:18` — Condition inverted vs the removed line (`if active` became `if !active`)
- `app/controllers/orders_controller.rb` — No `before_action :authorize!`; `required_guards` for archetype `controller` lists it (cleared the 60% floor across `sample_size` controllers). Cannot confirm the new action is covered; may be inherited from a base controller (advisory)
- `app/services/refund.rb:12` — Error path does not match the archetype's `error_handling` shape `render_error` (88% of `service` files); conventions.json
- `src/api/user.ts:30` — `fetchUser` drops the required `signal` param the `callable_signatures` consensus for archetype `api` carries (advisory)
- `spec/models/user_spec.rb:14` — Stale test: source removed export `getUserById` (renamed) but the paired spec still references `getUserById(` (test_pairing)
- Endpoint shape diverges from spec (spec says X, code does Y)

### Dependency / supply-chain findings (Z issues)

Only present when the diff touched a manifest or lockfile (Step 2.5). Omit the section otherwise. New direct dependencies do NOT appear here — they go to the "Acknowledge before merge" section below and never drive the verdict.

**FIX:**
- `package-lock.json:204` — Resolved host `evil.example.com` is not `registry.npmjs.org`
- `package.json:12` — New `scripts.postinstall`: `node ./setup.js` runs automatically on install
- `package.json:9` — Dependency `acme-utils` pulled from `git+ssh://git@github.com/acme/utils.git`, not the registry
- `requirements.txt:1` — `--index-url https://pypi.attacker.example/simple` redirects installs off PyPI (uncovered-manifest hand-parse, Step 2.5; same tier as an npm non-registry host)
- `requirements.txt:47` — Dependency `flask-hardening @ git+https://github.com/evil/…` pulled from a git source, not PyPI (uncovered-manifest hand-parse)

### Acknowledge before merge (ACK — does not affect the verdict)

Only present when the diff adds a new direct dependency (Step 2.5a) or touches a dependency manifest the scanner cannot parse (Step 2.5 `uncovered_manifests`). Each line is a human provenance gate, not a finding: it never changes the verdict and is never recorded as a BLOCK in the ledger.

- ACK `package.json:31` — New direct dependency `left-pad@^1.3.0`. Confirm it is the intended package (not a typosquat) and that adding it is wanted.
- ACK `requirements.txt` — Dependency manifest not covered by the automated scan (Python). Coverage-gap disclosure; the added lines were hand-reviewed (any red flags are raised as FIX in the Dependency section).
- ACK `requirements.txt:46` — New direct dependency `left-pad-py==1.0.0` (routine add, name-only). Confirm it is the intended package (not a typosquat).

### Security findings (W issues)

Always present (the security pass runs on every changed source file, ticket or not). Secret BLOCKs and the deterministic 2.6d sinks are witnessed facts; the authz and taint findings (2.6b/2.6c) are labeled advisory judgments — keep the labels.

**BLOCK:**
- `config/initializers/stripe.rb:4` — Secret detected: Stripe Secret Key. Rotate it and move to an env var. Verify this is not a live credential; if it is a test fixture, it is safe to keep.
- `app/jobs/import_job.rb:22` — `eval-call` sink on an added line (Step 2.6d, witnessed): request-reachable code execution; carry the lint message and rewrite without `eval`.

**FIX:**
- `app/controllers/orders_controller.rb` — Presence-only authz check: the witness controller declares before_action callbacks; this changed controller declares none and adds a new action. Cannot confirm the new action is covered; authorization may be inherited from a base controller.
- `app/controllers/reports_controller.rb:31` — Advisory, single-hunk scope: `params[:cmd]` flows into `system(...)` on this line with no sanitization in the hunk. May be a false positive if sanitized elsewhere.
- `app/lib/token.rb:14` — `weak-hash` sink on an added line (Step 2.6d, witnessed): MD5/SHA1 in a security context; use SHA-256+.

**NIT:**
- `src/api/poll.ts` — `then-without-catch` (Step 2.6d, whole-file): a `.then` with no `.catch` (unhandled promise rejection).

### Migration-safety findings (V issues)

Only present when the diff touched a file under `db/migrate/` (Step 2.7). Omit the section otherwise. The irreversible-`change` BLOCK is a witnessed structural fact; the null:false and concurrently FIXes are advisory "verify table size" reminders — keep the labels.

**BLOCK:**
- `db/migrate/20240101000000_drop_orders.rb:5` — Irreversible `change` block: `drop_table :orders` cannot be auto-reversed. Move the body into `def up` / `def down`, or wrap it in `reversible do |dir|`.

**FIX:**
- `db/migrate/20240101000001_add_status.rb:4` — Advisory, verify table size: `add_column ... null: false` with no `default:` fails on a populated table. Add a `default:` or backfill first.
- `db/migrate/20240101000002_index_trades.rb:4` — Advisory, verify table size: `add_index` without `algorithm: :concurrently` locks the table against writes while the index builds. Fine on a small table.

### Cross-file findings (U issues)

Present when a cross-file pass fired (co-change Step 2.8, or the layering/duplication/existence-break passes in Step 2.9). Omit the section when none fired. The existence-break FIX is the tool's witnessed fact; co-change, layering, and duplication are advisory and cite their backing artifact or candidate.

**FIX:**
- `app/models/order.rb` — New model added without a db/migrate migration in the same change (co-change `cochange-model-migration`); confirm the migration isn't needed
- `src/api/client.ts:8` — Cross-file existence break: `editPrice` is no longer exported from `./pricing`, but `src/checkout.ts:42` still imports it (get_crossfile_context, high_confidence)
- `src/pricing/calc.ts:12` — Caller-contract signature break: `applyDiscount` narrowed from 2 to 3 required positional args; 5 recorded callers now mis-match, e.g. `src/cart.ts:88`, `src/quote.ts:21` (get_contract_breaks, Step 2.9e)
- `src/domain/order.ts:3` — Upward-edge violation: `domain` imports `transport`, inverting the observed `transport -> domain` direction (layering)

**NIT:**
- `src/utils/dates.ts:20` — New function `toDisplayDate` duplicates the intent of existing `formatDate` (`src/format.ts`); call it instead (get_duplication_candidates)

### Coverage-delta (advisory)

Always present when the repo has test archetypes (Step 3g). One heads-up line plus an optional list. Never a BLOCK or FIX; it does not affect the verdict.

```
N source files changed, M test files changed.
Source files in test-paired layers with no matching test added in this diff (heads-up, not a verified gap — chameleon has no source-to-test path map and the diff only lists changed files):
- `app/services/refund_service.rb` (archetype `service`; repo tests this layer at `spec/services`)
- `app/queries/active_listings_query.rb` (archetype `query`; repo tests this layer at `spec/queries`)
```

When every changed source file in a test-paired layer has a matching test in the diff, say so in one line ("All changed source in test-paired layers has a test in this diff.") and omit the list. When the repo has no test archetypes, omit this section.

### Auto-pass routing (advisory)

One line from `get_autopass_verdict` (Step 3h), a tier line, plus an optional line for the typecheck state. Never a BLOCK/FIX/NIT; it does not affect the verdict.

```
Tier: easy — 1 file / 12 lines, in-pattern, bounded reach.
Auto-pass: ELIGIBLE — routine change, no security surface, within size/blast-radius bounds. With the APPROVE verdict above, this is a candidate to skip human review.
Typecheck: unavailable (opt-in not set)
```

or, when routed to a human:

```
Tier: complex — touches a security-sensitive surface; change too large (36 files / 694 lines).
Auto-pass: NEEDS HUMAN (risk: high) — touches a security-sensitive surface; change too large (36 files / 694 lines). Not a skip candidate regardless of the findings verdict.
Typecheck: clean
```

or, on a test-integrity routing:

```
Tier: hard — multi-file change with a new file.
Auto-pass: NEEDS HUMAN (risk: high) — test weakening (deleted tests / skip markers / assertion drop) alongside live-source changes.
Typecheck: 3 type error(s) across 2 changed file(s)
```

First check `status`: a `status == "degraded"` envelope (e.g. an unresolvable `base_ref`) carries only `{auto_pass_eligible, risk, complexity_tier, reasons, reason, fan_out, status}` — it OMITS `typecheck`, `facts`, and `changed_files`. On degraded, render `Auto-pass routing: degraded (<reason>)` plus the fields that ARE present (`Tier`, NEEDS HUMAN, `risk`, `reasons`) and do NOT reference the absent `typecheck`/`facts`/`changed_files`. Otherwise (a non-degraded envelope — the success path sets NO `status` field, so `status` is simply absent, never `"ok"`): render the `complexity_tier` field as `Tier: <easy|medium|hard|complex>` with a short reason drawn from the facts, then `auto_pass_eligible` as ELIGIBLE / NEEDS HUMAN, the `risk`, and the `reasons` list verbatim; render the `typecheck` DICT by its `typecheck.status`: `Typecheck: unavailable (<typecheck.reason>)` when `"unavailable"`, `Typecheck: clean` when `"clean"`, `Typecheck: <typecheck.diagnostics> type error(s) across <len(typecheck.files)> changed file(s)` when `"errors"`. If the tool was entirely unavailable (no envelope), write one line saying the auto-pass routing was skipped. Omit nothing: an ELIGIBLE verdict is only a skip candidate when the findings verdict is APPROVE — state that pairing explicitly. The tier is the change's inherent complexity (structural), independent of whether it is clean: an `easy`/`medium` change that is APPROVE + ELIGIBLE is the review-clean routine slice; `hard`/`complex` changes carry an irreducible human-judgment residual even when the findings verdict is clean.

### Unrun executable checks (advisory)

Present when the RECALL lenses named any (Step 3.9). Each line is a specific
executable check this static review could not run, so the user can green-light
it instead of discovering it via "another round". Never a finding; never affects
the verdict.

```
- Run `spec/services/prorate_metrics_spec.rb` — the changed proration path has a paired spec this review only read.
- Query production/staging: does `orders.client_ip` ever hold NULL? The new red-flag rule assumes it is populated.
- Check deploy state: is the consumer of the renamed field already deployed, or does rollout order matter?
```

### Pass execution manifest (always rendered)

One row per pass, no omissions — this is the generalization of the lint ledger,
and it exists because a skipped pass and a clean pass are otherwise
indistinguishable in the sections above (they render identically as "section
omitted"). Status is one of: **ran** (with its evidence: N files / K findings),
**skipped** (ONLY with a sanctioned reason: `no manifest in diff` for 2.5, `no
db/migrate file` for 2.7, `no added files` for 2.8, `no ticket` for 3a/3b/3d,
`no test archetypes` for 3g, `tool unavailable: <name>` / `degraded: <reason>`
for a tool-backed pass, `profile untrusted` for a trust-gated pass, `artifact
section absent: <layering | test_pairing | callable_signatures |
error_handling>` for an artifact-keyed pass, `not a source file
(manifest/lockfile — 2b lint + 2.5 only)` / `file deleted` / `binary` per file
— or, for any pass, the skip condition that pass's own step text defines,
named), or **n/a** (the pass ran but its input set was empty this review —
zero model-judgment findings for 4b, zero lens candidates to gate — with that
empty set named). A row you cannot fill with evidence or a sanctioned reason
is a self-evident gap to close before rendering the verdict — the same rule the
lint ledger already enforces.

```
| Pass | Status |
|------|--------|
| 1b prior-review | ran — no record pins this HEAD |
| 2a-2f convention (incl. 2b lint N/N) | ran — 6/6 files, 3 findings |
| 2.5 dependency | skipped — no manifest in diff |
| 2.6 security (a-d) | ran — 6 files, 1 finding |
| 2.7 migration | skipped — no db/migrate file |
| 2.8 co-change | ran — 2 added files, 0 findings |
| 2.9a-e cross-file | ran — existence/contract/dup/callers/layering, 1 finding |
| 3a/3b/3d ticket | skipped — no ticket |
| 3c edge cases + 3c-i signatures | ran — 6/6 files (per-file lines below) |
| 3e change-delta | ran — 6/6 files (per-file lines below) |
| 3f/3f-i/3f-ii naming/stale-test/stale-comment | ran — 0 findings |
| 3g coverage-delta | ran — advisory above |
| 3h auto-pass | ran — advisory above |
| 3.9 RECALL | ran — 2 lenses x 1 round, 3 candidates, 1 survived |
| 4a hunk gate / 4b refuter | ran — 2 dropped / 1 refuted-dropped |
```

### Recommendations (advisory)

Optional. The superpowers reviewer ends with improvement suggestions for code quality, architecture, or process. Include this section ONLY when you have a concrete, grounded suggestion that is not already a finding above (e.g. "the new util duplicates the date-format helper the repo already wraps; consolidating would remove the off-pattern import", or "this archetype has no test-pairing convention; consider adding one"). Each recommendation must cite the chameleon data or diff fact it rests on, the same integrity bar as a finding; it never carries a severity and never changes the verdict. Omit the section entirely when you have nothing grounded to add — do not pad it with generic best-practice advice.

### Per-file details

Coverage: lint_file run on N/N changed files. [If under N/N, name the skipped files and why — a gap to close before the verdict.]

#### `path/to/changed_file`
- Archetype: `name` (confidence: band, match: quality)
- Canonical witness: `path/to/witness`
- Violations: N (breakdown by severity)
- 3c: [what was checked for this file — e.g. "empty result set from the new query handled at :48; params[:id] nil-guarded" — or "no new inputs/queries in this hunk"]
- 3e: [N hunks read; removed guards / early returns / awaits / inverted conditions / error branches checked; K findings or CLEAN; removed-line quote: `- if user.nil? return`  (or "no removed lines in this file")]
- [details or "Follows conventions correctly."]
```

### Severity classification

| Severity | Meaning | Convention examples | Logic examples | Dependency examples | Security examples | Migration examples | Cross-file examples |
|----------|---------|-------------------|----------------|---------------------|-------------------|--------------------|---------------------|
| **BLOCK** | Must fix before merge | Missing base class/mixin the archetype requires | Missing requirement, race condition, removed guard/error branch | — (new dependency is an ACK, not a BLOCK) | Secret in the diff; error-severity `eval-call` sink in the diff (Step 2.6d) | Irreversible op in a `change` block | — |
| **FIX** | Should fix | Wrong response pattern, missing naming convention | Missing null guard, spec divergence, dropped await, inverted condition, error-handling/required-guard divergence (advisory), callable-signature drop (advisory), stale paired test | Non-registry resolved host, new install script, git+ssh:/file: source | Presence-only authz gap (advisory), taint/SSRF/traversal in hunk (advisory), deterministic sink `command-injection`/`sql-string-interpolation`/`insecure-deserialization`/`weak-hash`/`insecure-random` (Step 2.6d, witnessed) | null:false without default (advisory), add_index without concurrently (advisory) | High-confidence existence break (get_crossfile_context); caller-contract signature break (get_contract_breaks, Step 2.9e); missing companion (co-change, advisory); upward-edge layering violation (advisory) |
| **NIT** | Optional improvement | Potential duplication with existing utility | Minor inconsistency, placeholder name vs descriptive siblings, stale comment | — | Test-quality / `then-without-catch` / `unfrozen-clock` / `unstubbed-network` (Step 2.6d, whole-file) | — | Semantic duplication of a new function vs a returned candidate (get_duplication_candidates); borderline layering edge |

For reviewers used to the superpowers vocabulary: BLOCK ≈ Critical, FIX ≈
Important, NIT ≈ Minor. Chameleon keeps BLOCK/FIX/NIT because the review ledger
(`record_review_verdict`) is keyed on them.

Authz and taint/SSRF/traversal findings (2.6b/2.6c) are capped at FIX. They are advisory judgments and never escalate to BLOCK. Two witnessed facts in the security pass DO block on an added/changed line: a hard-kind secret (Step 2.6a kind gate + hunk gate) and a deterministic error-severity `eval-call` sink (Step 2.6d, hunk-gated; a `warning`-severity `eval-call`, the Rails `class_eval` idiom, caps at FIX). The other deterministic sinks (Step 2.6d `command-injection` / `sql-string-interpolation` / `insecure-deserialization` / `weak-hash` / `insecure-random`) cap at FIX; the 2.6d test-quality rules cap at NIT. Low-precision secret heuristics cap at NIT; out-of-hunk hard secrets and out-of-hunk sinks go to the repo-hygiene note.

The migration null:false and add_index advisories are capped at FIX. They are "verify table size" reminders the author resolves by checking the row count; only the irreversible-`change` check blocks from the migration-safety pass.

The cross-file findings (Step 2.8 co-change, Step 2.9 layering/duplication/existence-break/contract-break) cap at FIX. The high-confidence existence break from `get_crossfile_context` (Step 2.9c) and the caller-contract signature break from `get_contract_breaks` (Step 2.9e) are witnessed FIXes; co-change, layering, and duplication are advisory and never reach BLOCK. The error-handling, required-guard, callable-signature, and stale-comment findings are advisory too: required-guard and callable-signature drops cap at FIX, the stale comment caps at NIT.

### Verdict rules

- **BLOCK**: any BLOCK finding → verdict is BLOCK. A hard-kind secret on an added/changed line (Step 2.6a, both gates passed) is a BLOCK and drives a BLOCK verdict; a deterministic error-severity `eval-call` sink on an added/changed line (Step 2.6d, hunk-gated) is a BLOCK and drives a BLOCK verdict (a `warning`-severity `eval-call` caps at FIX, and `command-injection` — emitted at `warning` only, never block-eligible — caps at FIX); an irreversible op in a `change` block (Step 2.7a) is a BLOCK and drives a BLOCK verdict; the advisory authz/taint findings, the other 2.6d sinks (FIX) and 2.6d test-quality rules (NIT), and the migration table-size advisories (Step 2.7b/2.7c) are capped below BLOCK and never force a BLOCK verdict on their own. A new-dependency ACK (Step 2.5a) is not a finding and never affects the verdict. Pre-existing-hygiene secret/sink notes never affect the verdict.
- **NEEDS CHANGES**: any FIX finding but no BLOCKs → NEEDS CHANGES
- **APPROVE WITH NITS**: only NIT findings → APPROVE WITH NITS
- **APPROVE**: zero findings → APPROVE
- The coverage-delta view (Step 3g) is advisory and carries no severity. It never adds a BLOCK, FIX, or NIT and never changes the verdict; an untested-source heads-up alone still leaves an otherwise clean PR at APPROVE.
- The auto-pass routing (Step 3h) is advisory and carries no severity. It never adds a finding and never changes the verdict. It is a separate signal: a change is a "no human review needed" candidate only when the verdict is APPROVE AND auto-pass is ELIGIBLE; a NEEDS-HUMAN routing on an otherwise-APPROVE change means a human should still look, and an ELIGIBLE routing never upgrades a NEEDS CHANGES/BLOCK verdict.
- The cross-file findings (Step 2.8 co-change, Step 2.9 layering/duplication/existence-break/contract-break) cap at FIX, so they can drive NEEDS CHANGES but never BLOCK. A high-confidence existence break (Step 2.9c) and a caller-contract signature break (Step 2.9e) are witnessed FIXes; co-change, layering, and duplication are advisory FIX/NIT that the reviewer can confirm away.

**Dependency demotion sweep (mechanical, run after assembling the findings and
BEFORE rendering the verdict — the counterpart of the Step 4a hunk gate).** Scan
every assembled BLOCK and FIX line: if a finding's ONLY evidence is that a new
direct dependency was added (any name, however infamous — `leftpad`,
`event-stream` — and any manifest, `Gemfile` or `package.json` alike), delete it
from the findings and emit it as an ACK line in "Acknowledge before merge"
instead. No judgment call: a name is never a red flag by itself; only a witnessed
2.5b/c/d signal on the added line (non-registry host, install script,
non-registry source) keeps a dependency line in the findings. Then compute the
verdict from what remains. This sweep is mechanical precisely because the
name-bait escalation survives the prose rule under long-context pressure.

### Step 5: Record the verdict in the review ledger

After the verdict is rendered and shown to the user, append it to the review ledger by calling the `record_review_verdict` action:
```
chameleon_review(action="record_review_verdict", params={"repo": <repo_id>, "verdict": <the verdict string>, "findings_count": <total BLOCK+FIX+NIT count>, "commit_sha": <reviewed HEAD sha>, "complexity_tier": <the complexity_tier from get_autopass_verdict in Step 3h, or omit if that step was skipped>})
```
Pass the verdict exactly as rendered (`APPROVE`, `APPROVE WITH NITS`, `NEEDS CHANGES`, or `BLOCK`), the total finding count across all severities, the commit SHA the review covered (the branch HEAD for the no-args case, or the PR head commit), and the `complexity_tier` from Step 3h's auto-pass routing (so a lead can later read the review-clean rate per tier — the routine easy/medium slice versus the hard/complex residual). The ledger stamps the rest of the provenance itself (the profile that reviewed it, the trust state, the engine version, the reviewer, a UTC timestamp).

Once review is optional, the skill is the system of record for "this change was checked", but the chat output disappears. The ledger is the durable trail: past verdicts are queryable with `get_review_history`, and a lead can see which BLOCK verdicts shipped anyway. State the scope honestly when the ledger comes up: it is tamper-evident (a third local user editing a line makes it fail verification), NOT forgery-proof. The reviewed developer holds the signing key and CI cannot verify the records, so the ledger is an honest self-attested audit log, not a merge authority.

This is a best-effort final step. If the tool call fails (no ledger, no signing key), the verdict still stands in chat; do not retry or block the review on it.

### Step 5b: Record per-finding fates in the fate ledger

After the verdict, record the verdict-time disposition of each finding so per-lens precision becomes computable over time (`chameleon_telemetry(action="get_finding_fate_stats", ...)`). For every surfaced BLOCK/FIX finding, and every finding converted to an "Unrun executable checks" line (Step 4b), call `record_finding_fate` once:
```
chameleon_review(action="record_finding_fate", params={"repo": <repo_id>, "fate": <accepted | converted>, "message": <the finding's one-line message>, "file": <file>, "line": <line>, "lens": <the finding's lens / defect-class, e.g. correctness, consequences, duplication, security>, "confidence_at_emit": <the finding's confidence 0..1 if known>, "surface": "pr-review"})
```
A finding that survived RECALL + the refuter and is surfaced in the verdict is `accepted` (the review stands by it); a finding routed to an unrun check is `converted`. Only a 16-hex digest of the message+file+line is stored, never the prose. This is best-effort and never blocks the review: on any failure, skip it and move on. It is a distinct ledger from `record_review_verdict` (per-finding disposition vs the aggregate verdict), and like it: tamper-evident, not forgery-proof, not CI-verifiable.
