---
name: chameleon-pr-review
argument-hint: "[PR-URL | ticket-key]"
description: "Use when the user explicitly invokes /chameleon-pr-review to review a PR or branch diff against the repo's chameleon conventions, principles, and task requirements. Reports convention violations + logic gaps."
---

# PR Review with Chameleon Context

Review code changes against this codebase's actual conventions, principles, and (optionally) the task spec. Combines convention compliance with logic review.

## Reviewer discipline

This review follows the same discipline a senior reviewer applies (the superpowers
`requesting-code-review` skill's `code-reviewer` template): be specific (always `file:line`); explain WHY each finding
matters, not just what; never say "looks good" without checking; don't mark a
nitpick as BLOCK; never give feedback on code you didn't actually read; and always
give a clear verdict with its reasoning. The output leads with the verdict line
(Step 4 format) and the one-sentence Verdict Reasoning that carries the superpowers
"Ready to merge?" assessment, then the strengths, then the findings — a
verdict-first order for the reader who needs the decision immediately, not the
template's verdict-last order. Every finding is grounded in chameleon data or a
removed hunk line — see the grounding loop below.

## Input formats

```
/chameleon-pr-review                      → convention-only review of current branch vs main
/chameleon-pr-review PROJ-1234            → full review (conventions + Jira logic check)
/chameleon-pr-review <PR-URL>             → full review (conventions + linked Jira)
/chameleon-pr-review <PR-URL> PROJ-1234   → full review (explicit PR + ticket)
```

## The six-phase discipline

Every review runs the pipeline the chameleon engine runs at turn end, plus a
RECALL stage —
**SCOPE → EVIDENCE → ATTACK → RECALL → VERIFY → REPORT**. The steps below are that
pipeline; each phase gates the next, so a finding never ships until it has
survived VERIFY. Two of the phases pull in opposite directions and BOTH are
mandatory: VERIFY only ever REMOVES findings (hunk gate, refuter), so without
RECALL — the one stage that ADDS what the single ATTACK pass missed — the
review's recall ceiling is one context's first pass, and a manual "run it again"
would beat it. RECALL exists so a second manual round finds nothing new.

- **SCOPE** — Step 1 (parse the diff into a per-file hunk map) + Step 2.0 (fan-out
  routing). Fix exactly what changed; the hunk gate depends on precise scoping.
- **EVIDENCE** — the tool-grounding passes: Step 2a `get_pattern_context`, 2b
  `lint_file`, 2.5 `scan_dependency_changes`, 2.9 (`get_duplication_candidates`,
  `get_crossfile_context`, `get_callers`, `get_contract_breaks`), 3a task context,
  3h `get_autopass_verdict`. Gather grounded facts before judging.
- **ATTACK** — the adversarial lenses: Step 2.6 security, 2.7 migration-safety,
  2.9a layering, Step 3 logic (edge cases, perf, type safety), 3e change-delta. Hunt
  defects across independent lenses.
- **RECALL** — Step 3.9 (decorrelated recall lenses over the whole diff, fresh
  context, no anchoring on the draft findings). Candidates it adds flow through
  the same VERIFY gates as everything else; the review is not done until a
  recall round adds zero surviving findings, or the 2-round cap is hit and
  disclosed in the banner.
- **VERIFY** — Step 4a hunk gate + Step 4b round-3 `refute_finding`. Every
  model-judgment finding must survive an independent refuter or it is dropped. A
  finding that cannot survive round 3 does not ship.
- **REPORT** — Step 4 output (verdict-first, BLOCK/FIX/NIT, grounding banner) +
  Step 5 ledger. Emit only verified findings, ranked by severity.

## Execution

Follow these steps in order. Do not skip steps.

**Read-only review.** This review never mutates the repo: do not edit the working tree, stage changes, move HEAD, or switch/reset/create branches. Inspect with `git diff` / `git show` / `git log` (and `gh` / `bbcurl` for a PR) only. If you must inspect another revision, use `git worktree add --detach <tmp-dir> <sha>` into a temp dir — ALWAYS `--detach` with a resolved sha, never a branch name and never `git checkout` / `git reset` on this checkout. Detach is interrupt-safety, not style: a review session can be killed at any moment (timeout, user abort), and a worktree that bound a branch leaves that branch checked-out-elsewhere state behind for the user to untangle, while a killed detached worktree is just a disposable directory. Remove the worktree (`git worktree remove`) when done. (You also do NOT auto-fix code, per the Important section.)

**Runtime budget.** A full review runs three rounds plus an independent refuter; even a one-file diff commonly takes 8-10 minutes of wall time. When invoked non-interactively (`claude -p`), give it a timeout of at least 600 seconds — a kill mid-review loses the entire buffered report.

### Step 1: Parse input

Determine what to review:
- **No args**: review current branch. The diff base is the locked production branch when one exists — read `production_ref` from `.chameleon/config.json`; otherwise use `main` (or `production` if main doesn't exist). Run `git diff <base>...HEAD --name-status -M` to get changed files WITH their status (`A`dded / `M`odified / `D`eleted / `R`enamed — `--name-status` names the status `--name-only` hides, and `-M` surfaces a rename as `R<score>  old_path  new_path` instead of an unrelated delete+add), then `git diff <base>...HEAD` (same base) to get the full unified diff.
- **Jira key** (matches `[A-Z]+-\d+`): note it for Step 3.
- **PR URL** (contains `pullrequests` or `pull`): fetch the PR diff. For Bitbucket, use `bbcurl`. For GitHub, use `gh`. This already returns the full unified diff.
- **Both**: use the PR diff and the Jira key.

If no changed files found, stop and tell the user.

#### 1a. Parse hunks from the unified diff

You now hold the full unified diff (not just file names). For each changed file, parse its hunk headers (`@@ -old_start,old_count +new_start,new_count @@`) and record:
- **Added/changed line ranges** in the post-change file: the line numbers covered by `+` lines and context inside each hunk, derived from `new_start` and the running offset as you walk the hunk body.
- **Removed lines** (`-` lines) per hunk: the pre-change code this change deleted or replaced.

Keep this per-file hunk map. Two later steps depend on it:
- The **change-delta logic pass** (Step 3e) reads the removed lines to see what behavior the change took out.
- The **hunk gate** (Step 4, applied to every logic finding) drops any BLOCK/FIX whose anchor line is not inside an added/changed range.

**Record each file's status** (from the `--name-status -M` output) alongside its hunk map, because three shapes have no on-disk content or no hunks and the per-file loop (Step 2) must not treat them as ordinary source files:
- **Deleted (`D`)**: the file is gone. There is no content to `get_pattern_context` / `lint_file` (Step 2a/2b), so do NOT call them on it — its `get_pattern_context` returns `archetype.file_exists: false` (the flag is nested under `archetype` — see Step 2a). A deletion has one real risk: importers of its removed exports now break. That is covered by the cross-file existence pass (Step 2.9c), which the engine reports for a deleted module. In the Step 2 coverage ledger, account for a deleted file as an explicit sanctioned skip (`lint_file skipped: file deleted`), never a gap to close.
- **Renamed (`R`)**: the diff lists the NEW path (the old path is invisible to `--name-only`, and a 100%-similarity rename has NO hunks). Add the OLD path to the changed-file set the Step 2.9c diff-scope gate reads, so a rename that removes an export the old path used to provide can reach the verdict (its `module` is the old path, which is otherwise absent from the diff). Review the NEW path as an ordinary modified file.
- **Binary**: `git` shows `Binary files differ` with no hunk. It is skipped per the Step 2 binary skip rule; account for it in the ledger as a named skip (`skipped: binary`).

For very large diffs, cap the removed-line text you feed forward per file (keep the hunk ranges in full; truncate only the removed-line bodies) so the review stays within context. Note in the output when a file's delta was truncated.

#### 1b. Prior-review comparison (same HEAD only)

Call `chameleon_review(action="get_review_history", params={"repo": <repo_id>, "limit": 5})` once. If a returned record pins
the SAME commit SHA as this review's HEAD, this is a re-review: print one line
("prior review of this HEAD: verdict V, N findings") and treat it as a bar to
clear, not a cache to trust — if this review ends with FEWER findings than the
prior record without an explanation (findings fixed since, or prior findings
refuted), that is a coverage gap to close before rendering the verdict. If no
record matches HEAD (the normal case) or the tool is unavailable, add nothing
to the review body — the pass-execution manifest row still records the outcome
("ran — no record pins this HEAD" / "skipped — tool unavailable"). This step
never seeds findings and never changes the verdict on its own.

### Step 2.0: Fan-out routing (large diffs only)

Call `chameleon_review(action="get_autopass_verdict", params={"repo": <repo_id>, "base_ref": <base>})` and read
`data.fan_out`. The engine decides; you never read env yourself. If `recommended`
is false (the diff is under the threshold, or the engine saw `CHAMELEON_REVIEW_FANOUT=0`
when it computed the verdict), run the review single-pass inline exactly as today —
STOP here and continue with Step 2. If `recommended` is true, fan out:

- **If you cannot dispatch in-session Task reviewers** (no Task tool is available in this context — e.g. you are yourself running as a subagent), do NOT skip the review and do NOT rationalize a bypass: run the review single-pass inline exactly as the `recommended=false` path (every pass yourself, over the whole diff), and log `fan-out-recommended-but-unavailable`. The inline run is the correct, complete outcome; fan-out is only a parallelism optimization, never a precondition for reviewing.
- Partition the changed files (from your Step 1a hunk map) into ~4-6 slices,
  MULTIPLE files per slice — never one slice per file.
- Dispatch one in-session Task reviewer per slice as the packaged
  `chameleon:pattern-reviewer` plugin agent (Task tool
  `subagent_type: "chameleon:pattern-reviewer"`), filling the per-slice prompt
  in `reviewer.md` — that file also carries the fallback for a harness that
  does not expose the agent type. The role itself (passes, tool limits, output
  schema) lives in the agent definition (`agents/pattern-reviewer.md`); do not
  restate it in the prompt. Each reviewer runs
  ONLY the per-file passes for its files: 2a-2f (convention/lint/canonical), 2.6
  (security, including the 2.6d deterministic lint-sink routing — it reads the
  slice's own per-file `lint_file` output), 2.7 (the slice owning a migration),
  3c, 3e, 3f, 3f-ii. Reviewers are read-only (Read/Grep/Glob + read-only MCP;
  the agent definition disallows every mutating tool). Fill the
  prompt's `{SLICE_HUNKS}` with each slice file's hunk map from Step 1a (added/
  changed ranges + removed lines): the reviewer has no Bash and cannot re-derive
  the diff, so without its hunk map it cannot run 3e or hunk-gate anything — a
  slice dispatched without hunks reviews blind. Each reviewer returns
  `{manifest, findings}`; at synthesis, reject a slice whose manifest has an
  unexplained gap (a pass neither run nor covered by a sanctioned skip reason)
  and re-run that slice's missing passes yourself before merging. Step 2.5
  (dependency-change) is NOT delegated per-slice: it is a whole-diff pass and the
  slice reviewers are directed not to call the `chameleon_review` dispatcher (which
  carries `scan_dependency_changes`), so it runs once at synthesis.
- Synthesize in two parts: (a) merge + dedup the slice findings with the key
  `(file, section, rule, message-fingerprint)` — a `(file, line, rule)` key would
  mis-merge the file-anchored and missing-requirement findings that have no line;
  then (b) run the WHOLE-DIFF passes ONCE on the merged set: 2.5 (dependency-change —
  `scan_dependency_changes` parses the whole diff in one call, so it runs once here,
  never per slice), 2.8 (co-change), 2.9a
  (layering — needs the import graph), 2.9b (duplication), 2.9c (existence-break),
  2.9d (caller), 2.9e (contract-break), 3a (task context), 3b (completeness), 3c-i (callable-signature drift),
  3f-i (stale paired-test), 3g (coverage), 3h (auto-pass). Any pass not listed runs whole-diff at synthesis.
  These whole-diff passes run once during synthesis, never in a slice.
- THEN run the RECALL stage (Step 3.9) over the whole diff and the 3-round
  grounding loop (Step 4a/4b) on the merged findings — slices partition files;
  they are not a second perspective on any file, so they never replace the
  recall lenses.
- Log that fan-out fired and how files were partitioned.

Fallback: if a reviewer reports it cannot reach the chameleon MCP tools, the
parent prefetches each slice's archetype/lint/canonical payload and the reviewer
does file-reading + judgment only.

### Step 2: Convention review

This is the core chameleon review. For EACH changed file:

**Coverage ledger (forcing function).** Before the per-file loop, take the FULL list of changed files from the Step 1a hunk map and run the per-file passes on EVERY one (minus the explicit skips below) — do not sample a subset. `lint_file` (Step 2b) in particular runs on every changed FILE, source or not, because its secret scan is pre-archetype. In the output's Per-file details, account for it explicitly with an `lint_file run on N/N changed files` line (and name any file deliberately skipped per the rules below). A count under N/N is a self-evident gap to close before you render the verdict.

**Skip these files** (false positives):
- Auto-generated files: `schema.rb`, `*.generated.*`, vendored/third-party files
- Config/data files: `.yml`, `.json`, `.toml`, `*.lock` unless the archetype specifically covers them
- Binary files, images, fonts

**Do NOT skip the package manifests and lockfiles.** `package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile`, and `Gemfile.lock` are exempt from the skip rules above even though they match `.json`/`*.lock`/`.yml`. They get no archetype/canonical/structural review (Steps 2a, 2c-2f are for source files), but they are NOT exempt from the Step 2b `lint_file` call: its secret scan runs pre-archetype, so a hard credential committed into a manifest or lockfile (an `_authToken`, an embedded registry password) must still surface as `secret-detected-in-content` and reach the 2.6a BLOCK gate. Run `lint_file` on them (pass a placeholder `"none"` archetype and read only the secret violations; ignore the structural ones), and their diffs also go through Step 2.5 below. A dependency change is the one place a config-file diff carries supply-chain risk, so it is reviewed, not skipped.

**Rails migrations (`db/migrate/*.rb`) get one extra pass.** A migration file is still an ordinary Ruby source file for the convention review (Steps 2a-2f run on it like any other), but its archetype is matched on top-level shape, so a risky migration looks structurally identical to its safe siblings and passes clean. After the convention review, run the migration-safety pass (Step 2.7) on every changed file whose path is under `db/migrate/`. Only `schema.rb` stays fully skipped; it is generated.

#### 2a. Get chameleon context

Call the `get_pattern_context` MCP tool with the file's absolute path:
```
get_pattern_context(file_path="/absolute/path/to/changed_file")
```

Every chameleon MCP tool returns a `{"api_version": "1", "data": {...}}` envelope; every field path in this skill is relative to `data` (so `archetype.archetype` below means `data.archetype.archetype`, `fan_out.recommended` in Step 2.0 means `data.fan_out.recommended`, and so on). From the response `data`, extract:
- `archetype.archetype` — which archetype this file matches
- `archetype.confidence_band` — how confident the match is
- `archetype.match_quality` — exact, ast, fallback, or none
- `archetype.file_exists` — `false` for a path deleted in this diff (there is NO top-level `data.file_exists`; read it under `archetype`)
- `canonical_excerpt.content` — the canonical witness code
- `repo.trust_state` — must be "trusted" for conventions to apply

If `trust_state` is not "trusted", warn and suggest `/chameleon-trust`.
If `match_quality` is "none" or "fallback", note it — the file may be in an uncovered area.
If `archetype.file_exists` is `false`, the path was deleted in this diff (a `D` file that slipped past the Step 1a status check): do NOT review it as a normal source file or call `lint_file` on it — route it to the deleted-file handling (Step 1a), whose only real risk is the cross-file existence break (Step 2.9c).

#### 2b. Run lint

Call the `lint_file` MCP tool:
```
lint_file(repo=<repo_id>, archetype=<archetype_name>, content=<file_content>, file_path=<abs_path>)
```

Collect ALL violations from the response. Each violation has `rule`, `severity`, `message`, `expected`, `actual`.

**Run this on every changed FILE (source or not), even when no archetype matches.** `lint_file` scans for secrets before it looks at the archetype, so it returns `secret-detected-in-content` violations regardless of whether the file matches a known shape or the profile is trusted. Step 2.6 reads those secret violations, so the lint call cannot be skipped just because `match_quality` is "none" or the file is a doc/config file — the secret scan runs on all of them. When `get_pattern_context` returns `archetype` null/none (no match), STILL call `lint_file`, but pass a non-null placeholder archetype STRING — the literal `"none"` (the null-match envelope carries no suggested fallback to read). Do NOT pass `null` and do NOT omit the `archetype` argument: it is a required string, and a null/omitted value makes `lint_file` return early BEFORE the secret and sink scans run, defeating the purpose. With a non-null string the secret and sink scans run regardless of the archetype (the structural part stubs for an unknown one — expected fail-open, not an error); those violations are exactly what Steps 2.6a/2.6d read. Ignore the structural violations for an unmatched file.

#### 2c. Check against canonical witness

Read the canonical witness content from Step 2a. Compare the changed file against it:
- Does the file follow the same structure as the witness?
- Does it use the same patterns the witness uses? (base classes, method shapes, import style, response format)
- Does it inherit/include/extend the same things the witness does?

The canonical witness IS the codebase's pattern. If the changed file diverges from it without good reason, flag as FIX.

**Use judgment for utility/helper files** — large multi-purpose files often have different shapes than the canonical. Don't blindly flag structural differences on files that serve a different purpose than the witness.

#### 2d. Check conventions

Load `.chameleon/conventions.json` from the repo root. Check per-archetype:

**Imports**: does the file use preferred imports? Does it import something the conventions say to avoid?

**Naming**: does the file follow the detected naming convention for declarations in this archetype? (prefix patterns, casing conventions)

**Inheritance**: does the file inherit/include/extend the dominant base class or mixin for this archetype?

**Method calls**: does the file use the common patterns for this archetype?

**Key exports**: is the author creating something that already exists in the key_exports list? Check for duplication.

#### 2e. Check principles

Read `.chameleon/principles.md` from the repo root. For each principle listed, check if the changed file violates it. Principles are auto-generated per repo — only the ones listed apply. Common principles:

- **Conventions override best practices** — does the file follow codebase patterns or generic patterns?
- **Match directory granularity** — is the file over-extracted or under-extracted vs siblings?
- **Match sibling test shape** — if this is a test file, do siblings have tests? If not, should this test exist?
- **One action, one job** — if this is an endpoint/action, does it combine data queries with file operations?
- **Use the wrapper** — does the file import a raw library when a project wrapper exists?
- **Prefer built-in idioms** — does the file use manual patterns when the language has a built-in idiom?

#### 2f. Check directory for existing similar code

For new files (not modifications), list sibling files in the same directory. Check if any existing file already provides what the new file is trying to do. Flag as NIT if potential duplication found.

### Step 2.5: Dependency-change review (always, for manifest/lockfile diffs)

**Trigger:** the diff touches a dependency manifest or lockfile of ANY ecosystem — the npm/Bundler set the tool parses (`package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile`, `Gemfile.lock`) or one it does not (Python `requirements*.txt` / `pyproject.toml` / `Pipfile` / `setup.py`, Go, Rust, PHP). When no such file is in the diff, skip this step (`skipped — no manifest in diff` in the pass execution manifest).

This step calls `scan_dependency_changes` once for the whole diff (a pure diff parse: no network, no install), routes its deterministic findings, hand-reviews the ecosystems the scanner does not parse at the same severities, and runs the checks 2.5a (new-dependency ACK), 2.5b (non-registry resolved host), 2.5c (install lifecycle script), 2.5d (non-registry source), and 2.5e (minified manifest).

**A new direct dependency whose ONLY signal is its name is an ACK, never a BLOCK or FIX** — however infamous the name (`leftpad`, `event-stream`) or plausible the typosquat, it goes to the "Acknowledge before merge" channel and does not drive the verdict; this is the single most-baited escalation. Only a red flag readable on the added line itself (a non-registry source, an install script, a redirected host — 2.5b/c/d) raises the severity; escalating a routine add corrupts the review-clean ledger.

When the trigger fires, read `${CLAUDE_PLUGIN_ROOT}/skills/chameleon-pr-review/references/dependency-review.md` NOW before executing this step. Do not run the checks from memory; the reference carries the tool contract, the uncovered-ecosystem severity routing, and the full check definitions.

### Step 2.6: Security pass (always, every changed source file)

**Trigger:** always — every changed source file, ticket or no ticket; this pass never depends on a Jira ticket being supplied.

It has four parts at three confidence levels that must never be conflated: 2.6a secret escalation (BLOCK, deterministic `secret_hard` kinds gated to the diff), 2.6b Ruby controller authorization (advisory FIX, presence-only), 2.6c tainted input / SSRF / path traversal (advisory FIX, single-hunk scope), and 2.6d deterministic lint-sink and test-quality findings (witnessed facts from `lint_file`; only the error-severity `eval-call` sink blocks).

Read `${CLAUDE_PLUGIN_ROOT}/skills/chameleon-pr-review/references/security-pass.md` NOW before executing this step. Do not run the parts from memory; the reference carries the two mandatory secret gates (kind + hunk), the exact severity routing per sink rule, and the honesty labels every advisory finding must carry.

### Step 2.7: Migration-safety pass (Rails `db/migrate/*.rb` only)

**Trigger:** the diff changes at least one Ruby file under `db/migrate/`. Skip every other file, and skip this step entirely when none changed (`skipped — no db/migrate file` in the pass execution manifest).

This pass reads the migration DSL inside the change directly (a pure static parse: no network, runs nothing, reads no profile data). It has exactly one BLOCK-eligible check — an irreversible operation inside a `def change` block (2.7a) — plus two advisory "verify table size" reminders capped at FIX: `null: false` without a `default:` (2.7b) and `add_index` without `algorithm: :concurrently` (2.7c).

When the trigger fires, read `${CLAUDE_PLUGIN_ROOT}/skills/chameleon-pr-review/references/migration-safety.md` NOW before executing this step. Do not run the checks from memory; the reference carries the full irreversible-operation list, the `reversible` block rules, and the exact advisory labels.

### Step 2.8: Co-change advisory (when the diff ADDS new files)

Run this once over the whole changed-file set, not per file. A new file of a kind that structurally cannot stand alone (a Rails model needs a migration, a new controller needs a route wired up, a Prisma schema change needs a migration, a Redux slice needs to be registered in the store) is a missing-companion gap a human reviewer catches by reading the whole change at once. The convention review above checks each file in isolation and cannot see it.

This pass uses chameleon's curated co-change pairs, not a learned statistic. The pairs are a small directional table in the engine (`cochange.py`): each rule has a `trigger` (the new file that demands a companion), a `companion` (the file that satisfies it), and a `rule_id`. The shipped rules: `cochange-model-migration`, `cochange-controller-route`, `cochange-prisma-migration`, `cochange-slice-store`, `cochange-django-model-migration`, `cochange-nestjs-controller-module`. Co-presence is never derived from this repo; only these curated rules apply, and each is silenced for a repo whose own committed files break the pairing too often to trust it.

Restrict this to files the diff ADDS (status `A` in the diff, a brand-new path). A modified existing file does NOT trigger: editing a method on an existing model must not demand a fresh migration. For each added file:
- Match its repo-relative path against each rule's trigger (a Rails model is `app/models/*.rb` excluding `concerns/` and `application_record.rb`; a controller is `app/controllers/*_controller.rb` excluding `concerns/` and `application_controller.rb`; a Prisma schema is `*.prisma`; a slice is a `.ts`/`.tsx` file whose basename ends in `<alnum>Slice.ts`/`.tsx` — a capital-S suffix match, so `userSlice.ts` triggers while `sliceHelpers.ts`, `imageSlicer.ts`, and `pizzaSlices.ts` do not; a Django model is `models.py`, or a file under a `models/` package that the role classifier tags as a model — a co-located `managers.py`/`querysets.py`/`signals.py` under `models/` does NOT trigger; a NestJS controller is a `*.controller.ts`, and that rule additionally fires ONLY when the repo's `package.json` declares `@nestjs/core`/`@nestjs/common`, a framework-manifest gate, because `*.controller.ts` is not unique to NestJS).
- If a rule's trigger matches, check whether ANY file in the whole change-set (added or modified) satisfies that rule's companion. The companion may be an edit to an existing file (a route added to an existing `config/routes.rb`), so check the full set, not just the new files.
- If no file satisfies the companion, raise a **FIX** advisory naming the new file and the rule's expectation (e.g. "new model added without a db/migrate migration in the same change"). Cite the `rule_id` so the author can suppress a deliberate split with `chameleon-ignore <rule_id>`.

Cap this at FIX, never BLOCK. A partial change may legitimately defer its companion to a follow-up commit, so this is a "confirm the companion isn't needed" prompt, not a confirmed gap. The trigger path is the witnessed fact each finding cites: the added file is in the diff and matches a curated rule, and no companion is present in the change-set.

### Step 2.9: Cross-file passes (layering, duplication, existence breaks, caller blast radius)

**Trigger:** always — these passes see across files, which the per-file convention loop cannot. Each is grounded in a concrete chameleon artifact or tool result; when a needed MCP tool is unavailable this session, skip that one pass and note it in one line, never the whole review.

Read `${CLAUDE_PLUGIN_ROOT}/skills/chameleon-pr-review/references/crossfile-passes.md` NOW before executing this step. Do not run the passes from memory; the reference carries all five in full — 2.9a layering/cycle violations (advisory), 2.9b semantic duplication of new functions via `get_duplication_candidates` (advisory), 2.9c cross-file existence breaks via `get_crossfile_context` (FIX, double-gated), 2.9d caller blast radius via `get_callers` (context, not a finding), and 2.9e caller-contract signature breaks via `get_contract_breaks` (FIX) — with each tool's exact response shape and degraded handling.

### Step 3: Logic review

Edge cases (3c) and callable-signature drift (3c-i) run ALWAYS, ticket or not; neither is gated on a ticket. In fan-out, 3c is delegated per slice (like the change-delta pass 3e, per `reviewer.md`) while 3c-i runs once at whole-diff synthesis. Only task context (3a), implementation completeness (3b), and spec compliance (3d) require a Jira ticket; skip those three in the no-args convention-only review.

**Plan-level concern (calibration).** Review the implementation against the spec, but if an acceptance criterion or spec line is itself contradictory, infeasible, or wrong (not merely unimplemented), say so as a plan-level note rather than only flagging the code. And surface a significant, justified-looking deviation from the spec as a confirm-intent advisory ("the change does X where the spec says Y; confirm this is intended") so the author can distinguish an intentional improvement from a problematic departure, rather than reading it as a flat FIX.

#### 3a. Gather task context

- **Jira ticket**: use the Atlassian MCP `getJiraIssue` tool. Read description, acceptance criteria, attachments.
- **Slack threads**: if the Jira ticket references Slack, or if a linked Slack thread exists, read it via Slack MCP `slack_read_thread`.
- **Attached docs**: if the ticket has attachments (screenshots, design docs), fetch what you can. List what you can't fetch and ask the user to paste them.

#### 3b. Check implementation completeness

For each requirement or acceptance criterion in the ticket:
- Is there corresponding code in the diff that implements it?
- If not, flag as BLOCK: "Requirement X has no implementation in this diff."

#### 3c. Check edge cases, performance, and type safety (always)

For each changed file, consider:
- **Null/nil/undefined guards**: can any input be empty or missing? Is it handled?
- **Empty collections**: what happens when a query returns no results?
- **Authorization**: does the endpoint check permissions, and does it match the ticket's permission requirements? For a Ruby controller, look up `conventions.required_guards[<archetype>]`: when it carries `required_guards` (guards present in at least 60% of that archetype's controllers) and the changed controller declares none of them — check `known_guards` first; a listed variant is not a miss — raise a **FIX** naming the expected guard (`before_action :authorize!`) and the archetype, with the Step 2.6b honesty label: "cannot confirm the new action is covered; authorization may be inherited from a base controller." No `required_guards` entry → fall back to the presence-only check (Step 2.6b). Never reach BLOCK; this is advisory.
- **Error handling**: look up `conventions.error_handling[<archetype>]` — the archetype's dominant shape (`try_catch`/`rescues` frequency, `sample_size`, optional `error_shape` naming the project error target). Raise a **FIX**, citing that entry ("this archetype handles errors via `<error_shape>` in `<frequency>` of its files; this change does not"), when the changed code adds an error path that does not match the recorded shape. No `error_handling` entry → fall back to comparing against the canonical witness (Step 2c).
- **Race conditions**: for async or background operations, can two requests conflict?
- **Performance / scalability (advisory)**: did an added line introduce a query or network/IO call inside a loop (an N+1), an unbounded collection load, or an O(n^2) pass over request-controlled data? These are judgments, not witnessed facts: cap at **FIX**, label advisory, anchor to the added line (the Step 4a hunk gate applies), and raise one only when the cost is visible in the diff, never a hypothetical.
- **Type safety (advisory)**: did an added boundary drop to `any` / untyped where the archetype's siblings are typed? Note it as a **NIT** (advisory); otherwise type errors are covered by `lint_file` and the Step 3h typecheck fact.
- **Documentation (advisory)**: a new public export/endpoint that the archetype's siblings document but this change leaves undocumented is a **NIT**, cited against a documented sibling. Do not invent a doc convention the archetype does not show.

Flag genuine risks as FIX. Don't flag hypothetical concerns.

This pass is where a skim is invisible, so it carries a per-file output
obligation: each file's Per-file details entry must include a `3c:` line naming
what was actually checked for THAT file (which inputs can be empty/missing and
how they are handled, or "no new inputs/queries in this hunk") — either findings
or a specific clean claim, never a bare "ok".

#### 3c-i. Callable signature drift (advisory FIX at most)

When the changed file declares or overrides a function/method whose name the archetype has a consensus shape for, compare its signature against `conventions.callable_signatures[<archetype>]`. Each `signatures` entry records the consensus `params` (positional arity, optional slots), `agreement`, `file_count`, and an optional `overrides_base` (an in-repo base class only).

Raise a **FIX** (never BLOCK) when a changed callable drops a required positional parameter the consensus shape carries, or when an override named in `overrides_base` diverges from the base's shape — the full file content resolves the multiline/destructured/defaulted cases a regex cannot, so this comparison is yours to make. Cite the callable name and the `callable_signatures` entry. No consensus entry (or no `callable_signatures` section) → nothing to compare; skip. Framework base contracts (`ApplicationController#render`, a Sidekiq `perform`) are not in the profile, so do not assert a divergence against them.

#### 3d. Check spec compliance

Does the implementation match the spec exactly, or does it diverge?
- Different field names than the spec describes?
- Different endpoint shape than sibling endpoints use?
- Missing features the spec lists?
- Extra features the spec doesn't mention?

Flag divergences as FIX with the specific spec reference.

### Step 3e: Change-delta logic pass (always, ticket or not)

Run this for every reviewed file using the per-file hunk map from Step 1a. This is the pass a human does by reading the diff itself: it compares the post-change code against what the change removed, not against the canonical witness.

For each hunk, look at the removed (`-`) lines next to the added (`+`) lines and ask:
- **Removed guard or validation**: did a `-` line carry a null/nil check, a presence check, a permission check, or an input validation that the `+` side does not restore?
- **Deleted early return**: did the change drop a `return` / `raise` / `next` / `break` that short-circuited an error or edge case?
- **Dropped await / async**: in TS, was an `await` removed from a call whose result is still used? In Ruby, was a `rescue`/`ensure`/`yield` dropped?
- **Inverted condition**: did a `+` line flip the sense of a condition the `-` line had (e.g. `if x` became `if !x`, `>` became `>=`, `&&` became `||`)?
- **Weakened error handling**: did the change remove a `rescue`/`catch`/`.catch`/error branch that the `+` side does not replace?

The removed lines are your reference, NOT the canonical witness. The witness shows the archetype's shape (Step 2c); it rarely contains the exact construct this hunk touched, so do not compare the hunk to it here.

Anchor every finding to a specific post-change line inside the hunk. A removed-guard finding points at the line where the guard should be; an inverted-condition finding points at the changed condition. Classify as BLOCK (a removed guard or error branch that can crash or skip authorization) or FIX (a weakened check, a dropped await whose result is awaited elsewhere). These findings go through the Step 4 hunk gate like all logic findings.

Same per-file output obligation as 3c: each file's Per-file details entry must
include a `3e:` line — "N hunks read; removed guards / early returns / awaits /
inverted conditions / error branches checked; K findings" or the same with
"CLEAN" — AND the line must quote ONE actual removed (`-`) line from that
file's hunks (or say `no removed lines in this file`). The quote is the token a
skim cannot fake: the fixed vocabulary above is fillable by pattern, a real
removed line is not.

### Step 3f: Placeholder-name NIT

For each added or renamed identifier in the diff (variables, parameters, functions, methods), flag low-information placeholder names as NIT when sibling code uses descriptive names:
- Numbered or recycled placeholders: `data2`, `result3`, `temp`, `tmp`, `obj`, `val`, `thing`, `stuff`, `foo`, `bar`, `baz`.
- Single-letter names that are NOT loop counters or idiomatic short scopes: a single-letter `x` holding a fetched user is a NIT; `i`/`j`/`k` in a `for` loop, `e` in a `rescue`/`catch`, `_` for a discard, and a one-letter block param in a short `map`/`each` are fine.

Only raise the NIT when the surrounding archetype and sibling files use descriptive names for the same kind of thing (compare against the `key_exports` and conventions from Step 2). A repo whose own code is full of `tmp`/`d` has no such convention to cite, so skip it there. Cite the placeholder name and a sibling that names the same concept clearly. Never escalate above NIT.

### Step 3f-i: Stale paired-test check (FIX)

This catches the near-certain stale test: an exported symbol was removed or renamed in the source file, but the paired test still references the old name. A renamed `getUserById -> fetchUserById` whose spec still reads `getUserById(` is a test that no longer exercises the code it claims to.

Run this only for a changed source file whose archetype carries a `test_pairing` entry in `.chameleon/conventions.json` (`conventions.test_pairing[<archetype>]`, the archetypes the bootstrap recorded as above the pairing dominance floor). The entry's `mapping` names the source-to-test path convention the repo follows; derive the paired test path from it and read that test file.

For each exported symbol the diff REMOVES or renames (visible in the hunk's `-` lines for an `export`/`def`/`class`/`module`/named declaration), check whether the OLD name still appears as a string token in the paired test file. When it does, raise a **FIX**: the symbol the test names no longer exists under that name in the source, so the test is stale. Cite the removed symbol, the paired test path, and the line in the test that still references it.

Anchor the source side to the `-` line that removed the export (the hunk gate in Step 4 applies). The test-file reference is the corroborating fact, not a separately-anchored finding. Skip this when the archetype has no `test_pairing` entry (the repo does not pair tests for that layer) or no paired test exists on disk.

### Step 3f-ii: Stale-comment check (NIT)

For each hunk, ask one question: did this change alter code whose adjacent comment now lies? A comment that described the old behavior (a parameter that was removed, a return value that changed, a condition that was inverted, a default that moved) and was not updated alongside the code is now misleading.

Raise a **NIT** only when an added/changed (`+`) line contradicts a comment that sits adjacent to it (the line above, an inline trailing comment, or a doc comment on the same declaration) AND that comment was not itself updated in the hunk. Cite the comment text and the changed line it no longer matches. This is a judgment call on the diff, never a witnessed fact, so it caps at NIT and goes through the Step 4 hunk gate (anchor it to the changed code line, not the stale comment line). Do not flag a comment far from the change, and do not flag a comment that the change did update.

### Step 3g: PR-level test-coverage-delta view (always, advisory only)

The file-by-file loop (Step 2) reviews each changed file in isolation and cannot make the one-glance judgment a human makes across the whole diff: "this PR changed several source files and one test — are the untested ones intentional?" This step assembles that aggregate view from the per-file archetype data already gathered, and emits it as a heads-up. It is ADVISORY only. It never produces a BLOCK or a FIX, and it never forces a verdict.

The reason for the advisory cap is that chameleon has no source-to-test path map: it cannot say "`app/services/foo.rb` should have `spec/services/foo_spec.rb`." It only knows each file's archetype and the repo's archetype set. The diff also lists only changed files (`--name-only`), so a source file whose test already exists and was not touched in this PR is invisible to this step. Both limits mean the "untested source" list is a prompt for the reviewer, not a verified gap. Say so in the output.

#### 3g-i. Partition the changed set into source vs test

For every changed source file, take the `archetype.archetype` name from its Step 2a `get_pattern_context` response. Classify the file:
- **test** if the archetype name is `test` or starts with `test-` (these are the names chameleon gives clusters that sit under `spec`/`test`/`tests`/`__tests__` or whose filenames carry a test suffix).
- **source** otherwise.

A file whose `match_quality` is `none` or `fallback` has no reliable archetype; leave it out of both partitions and out of the untested list. Skipped files (Step 2 skip rules), manifests, lockfiles, and migrations are not part of this partition.

Count the two partitions. The headline is the pair: "N source files changed, M test files changed."

#### 3g-ii. Decide which source archetypes are test-paired in this repo

Load `.chameleon/archetypes.json` from the repo root (the same profile the skill already reads). Build the set of test archetypes: every archetype whose name is `test` or starts with `test-`. Read each test archetype's `paths_pattern` (e.g. `spec/services:rb`, `spec/models:rb`); the leading directory segments are the test tree's mirror of a source tree.

A source archetype is **test-paired** when a test archetype's `paths_pattern` mirrors it: the test pattern's non-leading directory segments match the source archetype's `paths_pattern` segments (e.g. source `app/services` is paired with test `spec/services`; source `app/models` with test `spec/models`). This is the repo's own norm — the members of that source archetype predominantly have sibling tests, because the repo carries a whole test cluster shadowing that source tree. A source archetype with no mirroring test archetype is NOT test-paired; the repo does not test that layer as a rule, so omit its files from the untested list.

If the repo has no test archetypes at all, skip this step entirely: there is no test norm to measure a delta against.

#### 3g-iii. Build the untested-source list

From the source partition (Step 3g-i), keep only files whose archetype is test-paired (Step 3g-ii). For each kept file, this PR changed a source file in a layer the repo normally tests, and the diff added no test file in the mirroring test archetype. List those files.

Drop a source file from the list when a changed test file in this same diff is in the test archetype that mirrors its archetype — that source change did get a test in this PR. This is a coarse pairing on archetype, not on file name; it is the most this step can ground without a path map, and it is why the list stays a heads-up rather than a finding.

Emit the result in the verdict block as an advisory summary line (the Coverage-delta section in Step 4). Do not anchor it to a line, do not call it a FIX or BLOCK, and state plainly that it is a heads-up, not a verified missing test.

### Step 3h: Auto-pass routing (always, advisory only)

The findings sections answer "is anything wrong with this change?" This step answers the different question they cannot: "is this change *routine* enough that, with a clean review, a human can skip it?" Call the `get_autopass_verdict` action once for the whole diff:

```
chameleon_review(action="get_autopass_verdict", params={"repo": <repo_id>, "base_ref": <the PR base branch, or the branch's merge base; use the locked production_ref from .chameleon/config.json when no PR base is known; default "main">})
```

It returns `{auto_pass_eligible, risk, complexity_tier, reasons, facts, changed_files}`. Report it verbatim as an advisory line (the Auto-pass routing section in Step 4). It is ADVISORY only: it never produces a BLOCK, FIX, or NIT and never changes the verdict. Its job is to mark the safe-to-skip slice, grade the change's inherent complexity (`complexity_tier`: easy / medium / hard / complex — structural, independent of cleanliness), and name why a change is NOT in the skip slice (a security-sensitive surface, too large, high cross-file blast radius, a file outside the profiled archetypes, or a grounded block finding).

The verdict also carries `typecheck` (three-state) and deterministic test-integrity/content facts inside `facts` (`deleted_test_files`, `net_test_line_delta`, added skip markers, assertion delta, removed guard lines, chameleon-ignore directives added, `blast_radius_unknown`, `diff_scan_truncated`) — all engine-computed. Relay them verbatim; never recompute them by eyeballing the diff. This does NOT loosen the Step 3g integrity rule against hand-counting assertions: the assertion delta is now engine-grounded and arrives in the tool result, and the skill still never counts by hand. `typecheck` is a DICT, not a scalar — the state is `typecheck.status`, one of `"unavailable"` / `"clean"` / `"errors"` (never compare `typecheck` itself to a string). The three-state rule: `typecheck.status == "unavailable"` (the default — the runner is opt-in via `CHAMELEON_ALLOW_TSC`; the human-readable why is `typecheck.reason`) is reported as one fact line and is NOT a needs-human reason; `typecheck.status == "errors"` (the error files are `typecheck.files`, the count is `typecheck.diagnostics`) also appears in `reasons` and routes needs-human. (`tests` mirrors this: `tests.status` is `"unavailable"` / `"clean"` / `"failures"`.)

The superpowers reviewer asks "are all tests passing?" — that is OUT OF SCOPE for this static review: it runs nothing and makes no network call. The deterministic test-integrity facts above (and the opt-in typecheck/test states) are the proxy; relay them as the heads-up and say plainly that the suite's actual pass/fail was not executed here.

Read it together with the findings verdict, never instead of it: a change is a credible "no human review needed" candidate only when the findings verdict is APPROVE AND `auto_pass_eligible` is true. A clean findings verdict on a change the router sent to a human (an auth/payment/migration surface, say) is NOT a skip candidate — state that plainly. If the tool is unavailable this session, skip this step and note it in one line, the same as the cross-file passes.

### Step 3.9: RECALL — decorrelated recall lenses (always)

**Trigger:** always — every review, fan-out or not. This is the pipeline's only add-path: independent fresh-context lenses over the whole diff, whose surviving candidates flow through the same VERIFY gates as everything else, so a second manual round finds nothing new.

Read `${CLAUDE_PLUGIN_ROOT}/skills/chameleon-pr-review/references/recall-stage.md` NOW before executing this step. Do not run it from memory; the reference carries the two lens definitions and their dispatch contract, the depth calibration off the `get_autopass_verdict` already in hand, the merge + gate anchoring rules, the loop-until-dry 2-round cap, the no-dispatch inline fallback, and the unrun-executable-checks capture.

### Step 4: Output

This is the VERIFY + REPORT stage and it runs on every review, in three parts: the 4a hunk gate (every per-line finding must anchor inside an added/changed hunk of this diff or it is dropped; out-of-hunk witnessed facts go to the "Pre-existing repo hygiene" note), the 4b round-3 independent refutation (surviving model-judgment BLOCK/FIX findings go to `refute_finding`; tool-grounded findings are verified inline and never sent), and the output rendering — verdict-first, with the grounding and recall banner, the findings sections, and the pass execution manifest.

Read `${CLAUDE_PLUGIN_ROOT}/skills/chameleon-pr-review/references/output-format.md` NOW before executing this step. Do not gate or format from memory; the reference carries the full 4a/4b gate rules (exemptions, refuter batching and budget, envelope states), the complete output template, the severity classification table, the verdict rules, and the ledger-recording steps 5 and 5b.

### Step 5: Record the verdict in the review ledger

After the verdict is rendered and shown to the user, append it to the review ledger via `record_review_verdict`, and record per-finding fates via `record_finding_fate` (Step 5b). Both call contracts, their exact arguments, and the ledger's honest scope (tamper-evident, not forgery-proof) are in the output-format reference read at Step 4. Both are best-effort: a failed ledger call never blocks or retries the review.

## Integrity rules

- **Be honest.** If you're unsure about a finding, say so. Don't guess whether something is a violation — verify it against the canonical witness and conventions data. If the data doesn't clearly show a violation, don't flag it.
- **Don't hallucinate findings.** Every convention/logic BLOCK and FIX must reference specific chameleon data (a lint violation, a canonical mismatch, a convention entry, a principle) or, for logic findings, the removed (`-`) lines of a hunk. If you can't point to the data, it's not a finding. Dependency findings (Step 2.5) are the one exception to the chameleon-data requirement: they are backed by the manifest/lockfile diff itself, so each cites the exact added line or manifest key it parsed, never the profile. A conventions-keyed finding (3c/3c-i/3f-i) cites its `conventions.json` entry; without that entry, fall back to the witness and do not invent the convention.
- **Cross-file findings cite their tool or artifact, not intuition.** Each cross-file claim points at a tool result or an artifact entry, same bar as a lint violation: the 2.9c break only at `high_confidence=true` on a module in this diff's changed set, the 2.9b duplication only with a returned candidate, the 2.9a layering edge from `conventions.layering`, the 2.8 companion gap from its curated `rule_id`, and the 2.9e narrowing (or its `removed_export_still_imported` row, deduped against 2.9c) only with returned callers — the full relay gates and citation shapes live in the crossfile reference. A high-confidence break on a module this diff never touched is pre-existing: hygiene note, never the verdict.
- **Security findings carry their own honesty bar.** A secret BLOCK needs BOTH 2.6a gates (`secret_hard` true AND in-hunk); the deterministic 2.6d sinks are witnessed facts routed by their RETURNED severity, hunk-gated, never sent to the refuter; the authz (2.6b) and taint/SSRF/traversal (2.6c) FIXes are judgments that keep their advisory labels and never claim a structured profile cite. A new-dependency ACK (Step 2.5a) is a human provenance gate, not a finding — never render it as BLOCK/FIX/NIT and never let it change the verdict or the ledger record. A low-precision heuristic hit or an out-of-hunk hit presented as a BLOCK is a false claim, the exact kind that destroys trust in a green gate; the full severity routing lives in the security reference.
- **Migration findings carry their own honesty bar.** The irreversible-`change` BLOCK (Step 2.7a) cites the irreversible operation in the diff — a witnessed structural fact. The null:false and add_index FIXes (2.7b/2.7c) are table-size reminders, not confirmed defects: the dangerous condition is a row count this static read cannot see, and the repo's own safe migrations share the same shapes. They must keep their "verify table size" label and never reach BLOCK. Do not present either reminder as if it were a confirmed migration bug.
- **The coverage-delta view is advisory and grounded only in archetypes.** It must not claim a specific missing test file ("`foo.rb` needs `foo_spec.rb`") — no source-to-test path map exists, and a pre-existing untouched test is invisible to the diff. Keep it a heads-up listing changed source in a test-paired layer, never a FIX or BLOCK, and never count an assertions delta by eyeballing hunks (the engine-grounded delta arrives via Step 3h).
- **3-round grounding loop.** After producing the review, re-read every BLOCK, FIX, and NIT and verify: (1) does its backing — the canonical witness, conventions data (`error_handling`/`required_guards`/`callable_signatures`/`test_pairing`/`layering`), the hunk's removed (`-`) lines, a parsed manifest/lockfile line, a returned secret or deterministic lint-sink violation, or a returned tool result (`get_duplication_candidates` candidate, `get_crossfile_context` finding with `high_confidence=true`, `get_contract_breaks` finding with returned callers) — actually support the claim? (2) for every per-line finding, is the anchor line inside an added/changed hunk range (Step 4a)? Drop any finding that fails either check. The hunk gate is the deterministic answer to "PR-introduced vs pre-existing"; do not override it by judgment (the whole-diff cross-file findings are gated on their tool/artifact backing instead). Round 3 is the independent engine refutation pass for surviving model-judgment findings — see Step 4b.

## Important

- Do NOT auto-fix code (report only), do NOT post comments to Bitbucket/GitHub (findings in chat only), and do NOT touch the Jira ticket (no comments, no status changes).
- When unsure if something is a violation, check the canonical witness. If the witness does the same thing, it's not a violation. Large utility files (helpers, concerns, base classes) often have different shapes than the canonical — use judgment, not blind flagging.
- Distinguish between violations the PR INTRODUCED vs pre-existing issues. For per-line logic findings this is the Step 4a hunk gate, not a judgment call: a finding off the changed hunks is dropped. The change-delta pass (Step 3e) compares the hunk against its own removed (`-`) lines; the witness is for convention/shape comparison (Step 2c) only.
- Skip auto-generated files: `schema.rb`, `*.generated.*`, vendored files. These produce false positives. Lockfiles (`*.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`) are skipped for archetype/lint/canonical review but NOT for the dependency-change review (Step 2.5).
- Dependency findings come from the diff parse only (Step 2.5). Do not run a security audit, hit a network, or install packages during the review.
- The cross-file tools read prebuilt profile artifacts — no network call, no repo code. Never relay a duplication finding without a returned candidate or an existence break without `high_confidence`, and never read an empty `get_callers`/`query_symbol_importers` result as dead code: `query_symbol_importers` sees a barrel/index-re-exported symbol `get_callers` cannot (it tracks only direct call expressions), and neither's absence is conclusive.
- After the verdict is shown, append it to the ledger via `record_review_verdict` (Step 5) — best-effort, never blocks the review; tamper-evident, not forgery-proof, CI cannot verify it; past verdicts queryable with `get_review_history`.

## Honesty Rules

- Never invent a violation. Every BLOCK/FIX/NIT cites a real `file:line` inside an added/changed hunk plus the artifact that backs it: a returned lint/secret violation, a `conventions.json` entry, a returned tool result, or a parsed manifest/lockfile line.
- Distinguish a witnessed fact (a returned secret/lint violation, an irreversible migration op in the diff) from a judgment (authz, taint, error-shape). Label judgments advisory and never let one reach BLOCK.
- The hunk gate answers "PR-introduced vs pre-existing": if the anchor line is not in an added/changed hunk, drop the finding — don't override the gate by judgment. A finding that cannot survive the round-3 refuter does not ship.
- Never read an empty `get_callers`/`query_symbol_importers`/cross-file result as dead code, and never relay a duplication or existence break without its returned candidate / `high_confidence` backing.
- State what you verified clean, too. Don't pad the review with hypothetical concerns to look thorough. This is a REPORT-phase rule, not a generation-phase one: during ATTACK and RECALL, write every candidate down (including borderline ones) and let the hunk gate and the refuter kill the weak ones. Filter at the end, not at the moment of noticing.
