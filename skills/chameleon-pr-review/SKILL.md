---
name: chameleon-pr-review
description: "Use when the user explicitly invokes /chameleon-pr-review to review a PR or branch diff against the repo's chameleon conventions, principles, and task requirements. Reports convention violations + logic gaps."
---

# PR Review with Chameleon Context

Review code changes against this codebase's actual conventions, principles, and (optionally) the task spec. Combines convention compliance with logic review.

## Reviewer discipline

This review follows the same discipline a senior reviewer applies (superpowers
`code-reviewer`): be specific (always `file:line`); explain WHY each finding
matters, not just what; never say "looks good" without checking; don't mark a
nitpick as BLOCK; never give feedback on code you didn't actually read; lead with
what's done well, then the issues; and end with a clear verdict. Every finding is
grounded in chameleon data or a removed hunk line — see the grounding loop below.

## Input formats

```
/chameleon-pr-review                      → convention-only review of current branch vs main
/chameleon-pr-review PROJ-1234            → full review (conventions + Jira logic check)
/chameleon-pr-review <PR-URL>             → full review (conventions + linked Jira)
/chameleon-pr-review <PR-URL> PROJ-1234   → full review (explicit PR + ticket)
```

## Execution

Follow these steps in order. Do not skip steps.

### Step 1: Parse input

Determine what to review:
- **No args**: review current branch. The diff base is the locked production branch when one exists — read `production_ref` from `.chameleon/config.json`; otherwise use `main` (or `production` if main doesn't exist). Run `git diff <base>...HEAD --name-only` to get changed files, then `git diff <base>...HEAD` (same base) to get the full unified diff.
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

For very large diffs, cap the removed-line text you feed forward per file (keep the hunk ranges in full; truncate only the removed-line bodies) so the review stays within context. Note in the output when a file's delta was truncated.

### Step 2: Convention review

This is the core chameleon review. For EACH changed file:

**Skip these files** (false positives):
- Auto-generated files: `schema.rb`, `*.generated.*`, vendored/third-party files
- Config/data files: `.yml`, `.json`, `.toml`, `*.lock` unless the archetype specifically covers them
- Binary files, images, fonts

**Do NOT skip the package manifests and lockfiles.** `package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile`, and `Gemfile.lock` are exempt from the skip rules above even though they match `.json`/`*.lock`/`.yml`. They get no archetype/lint/canonical review (Steps 2a-2f are for source files), but their diffs go through Step 2.5 below. A dependency change is the one place a config-file diff carries supply-chain risk, so it is reviewed, not skipped.

**Rails migrations (`db/migrate/*.rb`) get one extra pass.** A migration file is still an ordinary Ruby source file for the convention review (Steps 2a-2f run on it like any other), but its archetype is matched on top-level shape, so a risky migration looks structurally identical to its safe siblings and passes clean. After the convention review, run the migration-safety pass (Step 2.7) on every changed file whose path is under `db/migrate/`. Only `schema.rb` stays fully skipped; it is generated.

#### 2a. Get chameleon context

Call the `get_pattern_context` MCP tool with the file's absolute path:
```
get_pattern_context(file_path="/absolute/path/to/changed_file")
```

From the response, extract:
- `archetype.archetype` — which archetype this file matches
- `archetype.confidence_band` — how confident the match is
- `archetype.match_quality` — exact, ast, fallback, or none
- `canonical_excerpt.content` — the canonical witness code
- `repo.trust_state` — must be "trusted" for conventions to apply

If `trust_state` is not "trusted", warn and suggest `/chameleon-trust`.
If `match_quality` is "none" or "fallback", note it — the file may be in an uncovered area.

#### 2b. Run lint

Call the `lint_file` MCP tool:
```
lint_file(repo=<repo_id>, archetype=<archetype_name>, content=<file_content>, file_path=<abs_path>)
```

Collect ALL violations from the response. Each violation has `rule`, `severity`, `message`, `expected`, `actual`.

**Run this on every changed source file, even when no archetype matches.** `lint_file` scans for secrets before it looks at the archetype, so it returns `secret-detected-in-content` violations regardless of whether the file matches a known shape or the profile is trusted. Step 2.6 reads those secret violations, so the lint call cannot be skipped just because `match_quality` is "none". For a file with no archetype, pass the archetype name `get_pattern_context` returned (or the fallback it suggests) and ignore the structural violations; the secret scan still runs.

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

Run this whenever the diff touches a package manifest or lockfile: `package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile`, or `Gemfile.lock`. These are the supply-chain entry points a human reviewer reads line by line and the convention review above does not cover. This pass is a pure parse of the diff text and the manifest/lockfile JSON or YAML. It makes NO network calls and does not install or run anything: only the added (`+`) lines matter, and the existing repo content gives the "previously present" baseline.

Each finding cites the exact lockfile line or manifest key. The four checks are independent; run every one that applies even if an earlier check fired.

#### 2.5a. New direct dependency → verify provenance (BLOCK until acknowledged)

Parse the manifest diff (`package.json` `dependencies`/`devDependencies`/`optionalDependencies`/`peerDependencies`; `Gemfile` `gem` lines) for a dependency name that was NOT present before this change. A bump of an already-present dependency is NOT this finding (it may be a different finding under 2.5b/2.5d); only a name that did not exist in the manifest before counts as new.

For each new direct dependency, raise a **BLOCK** labeled "verify provenance". This is a deliberate human gate, not a precision claim: the reviewer must confirm the package is the intended one (not a typosquat of a popular name, e.g. `lodahs` for `lodash`, `cross-env.js` for `cross-env`) and that adding it is intended. State the dependency name, the version range added, and the manifest file. The BLOCK clears only when a human acknowledges the dependency is wanted; the review cannot clear it on its own.

When several new dependencies land in one change, list each as its own BLOCK line so each gets its own acknowledgement.

#### 2.5b. Lockfile resolved host is not the expected registry (FIX)

In the lockfile diff, every added entry that resolves a package records the URL it was fetched from. Flag any added entry whose resolved host is NOT the package manager's public registry:
- npm (`package-lock.json` `resolved`, `npm-shrinkwrap.json` `resolved`, `yarn.lock` `resolved`, `pnpm-lock.yaml` `resolution.tarball`/`resolved`): expected host is `registry.npmjs.org`.
- Bundler (`Gemfile.lock` `remote:` under a `GEM` section): expected host is `rubygems.org`.

A resolved URL pointing at any other host (a private mirror the repo does not already use, a raw GitHub tarball, an arbitrary domain) is a **FIX**: the dependency is being pulled from somewhere other than the registry, which is how a tampered or planted package enters. Cite the exact lockfile line and the host. If the repo's other lockfile entries consistently use a private registry (the diff shows the SAME non-`registry.npmjs.org` host on pre-existing entries), that host is this repo's normal registry; treat it as expected and do not flag added entries that use it. Flag only hosts that differ from what the rest of the lockfile already uses.

#### 2.5c. New install lifecycle script (FIX)

In the `package.json` diff, flag a newly added `scripts.preinstall`, `scripts.install`, or `scripts.postinstall` as a **FIX**. An install-lifecycle script runs automatically on `npm install` with no further prompt, which is the classic vector for code that executes the moment a dependency tree is materialized. Cite the script key and its command. (A script that already existed and is merely edited is still worth a look, but the BLOCK-worthy signal is a NEW install hook on a diff that also adds or bumps dependencies.)

#### 2.5d. Non-registry dependency source (FIX)

In the manifest diff, flag any added or changed dependency whose version specifier is a source other than a registry version range. These pull code straight from a remote without going through the registry's publish path:
- `git+ssh:`, `git+https:`, `git:`, `github:`, or a bare `user/repo#ref` git shorthand.
- `file:` (a local path dependency) and `link:`.
- `http:` or `https:` pointing at a tarball.

Cite the dependency name and the source string. For a `Gemfile`, the equivalent is a `gem ... git:`/`github:`/`path:` option on a `gem` line. A `git+ssh:` or `file:` source is a **FIX** because the resolved code is not the registry artifact and is not covered by the registry's integrity guarantees.

### Step 2.6: Security pass (always, every changed source file)

Run this on every changed source file regardless of whether a Jira ticket was supplied. The convention review above checks shape; this pass checks for the security shapes a human reviewer watches for. It has three parts with three different confidence levels, and they are NOT equal. Keep them separate in the output and never collapse the weaker two into the secret part's confidence.

#### 2.6a. Secret escalation (BLOCK, deterministic kinds inside the diff only)

Read the `secret-detected-in-content` violations from each file's Step 2b `lint_file` response. These come from a secret scan that runs before the archetype match and before the trust gate, so they are present for every file you linted, trusted or not.

Two gates decide what a secret violation may do, and both are mandatory:

1. **Kind gate.** Escalate to **BLOCK** only violations whose `secret_hard` field is true (the deterministic, fixed-shape credential kinds: AKIA, ghp_, sk-ant-, sk_live, PEM, AIza, AccountKey). Violations without `secret_hard` (40-char base64 runs, high-entropy hex, password assignments, JWT-shaped strings, entropy hits) match ordinary identifiers, git SHAs, and data blobs in real code at a rate that makes them verdict-poison; report them at most as a **NIT** labeled "low-precision secret heuristic, verify by eye", never as FIX or BLOCK, and never let them influence the verdict.
2. **Hunk gate.** `lint_file` scans the FULL file content, not the diff, so a hit is NOT in the change by construction. The reported line is the `at line N` token in the violation's `actual`/`message` string (e.g. `aws_access_key at line 40` -> line 40); every hard-kind secret carries one. A hard-kind secret whose reported line falls inside an added/changed hunk of this diff is a **BLOCK**. A hard-kind secret on a line the diff did not touch is pre-existing: report it in a separate "Pre-existing repo hygiene" note at the end of the review (it deserves rotation, but this PR did not introduce it), and do not let it affect the verdict.

For each secret BLOCK, cite the file and line, carry the violation's own message (it names the kind and tells the author to rotate it), and label it "verify this is not a live credential; if it is a test fixture, it is safe to keep" - a fixture key is overridden by the author, not silently dropped by this review.

#### 2.6b. Ruby controller authorization (advisory FIX, presence-only)

For Ruby controllers ONLY, compare the authorization-callback presence of the changed file against its canonical witness. The only signal the profile carries here is presence or absence of `before_action`-style callbacks; it does NOT map a callback to the action methods it guards, so this check cannot tell whether a specific new action is actually covered.

Raise a **FIX** (never BLOCK) when the canonical witness for this controller archetype declares `before_action` (or `prepend_before_action`) authorization callbacks and the changed controller declares none, AND the change adds a new public action method. Label it exactly as a heuristic: "presence-only check, cannot confirm the new action is covered. The witness controller declares before_action callbacks; this changed controller declares none. Authorization may still be inherited from a base controller." Do not claim a structured divergence; do not name which action is unguarded; do not cite a "witness authz divergence" as if the profile mapped callbacks to actions. It does not.

When the file's archetype carries a `required_guards` entry in `.chameleon/conventions.json` (`conventions.required_guards[<archetype>]`), cite the specific expected guard symbol rather than the generic "declares before_action callbacks" phrasing: name the guard (`before_action :authorize!`), the frequency band it was derived at, and the archetype. Check the archetype's `known_guards` list first; a guard the changed controller uses that is listed there is a legitimate variant and not a miss. The honesty label is unchanged: it is still presence-only and still "cannot confirm the new action is covered". The `required_guards` data names the expected guard; it does not map that guard to the action it covers, so it never reaches BLOCK and never claims a structured callback-to-action divergence.

Skip this check entirely for TypeScript and any non-Ruby file. There is no route/middleware/controller extraction for those languages, so there is no presence signal to compare and nothing honest to say.

#### 2.6c. Tainted input, SSRF, path traversal (advisory FIX, single-hunk scope)

Read each file's added (`+`) lines from the hunk map (Step 1a). Within a single file's hunk, look for these flows where request-controlled input reaches a dangerous sink:

- **Taint to sink**: a value read from request data (params, query string, request body, headers, an inbound argument) flows on an added line into `eval`/`constantize`/`send`/`system`/backticks/`%x`/a raw SQL string (Ruby), or `eval`/`Function`/a shell exec/a raw query (TS), with no sanitization between source and sink inside the hunk.
- **SSRF**: an added outbound HTTP call (`Net::HTTP`, `Faraday`, `HTTParty`, `open-uri`, `fetch`, `axios`, `http.get`) whose URL is built from request data rather than a constant or an allow-listed host.
- **Path traversal**: an added filesystem read/write (`File.read`/`File.open`/`Dir`/`fs.readFile`/`fs.createReadStream`/`require`) whose path is built from request data without a basename/allow-list check inside the hunk.

These are judgment calls, not witnessed facts. Cap every one at **FIX** (never BLOCK) and label each: "advisory, single-hunk scope; may miss a flow whose source and sink are in different files, and may be a false positive if the value was sanitized outside this hunk."

The cited tainted line MUST be inside the diff. If the source or the sink is not on an added/changed line in the hunk map, do not raise the finding: a flow you cannot point at inside the change is exactly the cross-file case this single-hunk pass cannot see, and reporting it would be a guess. These findings go through the Step 4 hunk gate like every other per-line finding.

Never let any 2.6b or 2.6c finding reach BLOCK, and do not claim they honor the integrity/calibration guarantee the same way a lint violation or a removed-guard hunk finding does. They are judgments; the secret finding (2.6a) is the only witnessed fact in this pass.

### Step 2.7: Migration-safety pass (Rails `db/migrate/*.rb` only)

Run this on every changed file whose path is under `db/migrate/` and which is a Ruby file (`.rb`). Skip every other file. This pass is a pure parse of the migration's text and the diff: it makes NO network calls, runs nothing, and reads no profile data. The convention review (Step 2) cannot help here because a dangerous migration matches its safe siblings on top-level shape; this pass reads the migration DSL inside the change directly.

The DSL calls live inside a `change`, `up`, or `down` method at deeper indentation than the top-level archetype shape the profile matches on. Read the call name and its keyword arguments (`null:`, `default:`, `algorithm:`) across the whole call, including a call that wraps onto a second line. The three checks below are independent; run every one that applies.

This pass has exactly one BLOCK-eligible check and two advisory reminders. Keep the tiers separate. The reminders are NOT findings about this migration being wrong: the dangerous condition (a populated or large table) is a runtime fact this static read cannot see, and the repo's own clean migrations share the same shapes. They are "go verify the table size" prompts for the author, capped at FIX, never BLOCK.

#### 2.7a. Irreversible `change` block (BLOCK)

A `def change` method lets Rails auto-generate the rollback. That only works when every operation in the block is reversible. An irreversible operation inside `change` with no `up`/`down` pair gives a migration that cannot be rolled back: `rails db:rollback` raises `ActiveRecord::IrreversibleMigration` at the worst possible time.

Raise a **BLOCK** when a `change` method contains an operation Rails cannot auto-reverse and the migration does NOT instead define a `def up` / `def down` pair (which makes the rollback explicit and is the correct fix). The irreversible operations are: a bare `remove_column` without the column type and options Rails needs to recreate it, `change_column`, `execute` with raw SQL, `remove_index` without the full index definition, `drop_table` without a block describing the table, and `change_column_default`/`change_column_null` given only the new value with no `from:`/`to:`. A `reversible do |dir| ... end` block or a `change` that calls only auto-reversible operations (`create_table`, `add_column`, `add_index`, `add_reference`) is correct; do not flag it.

This is the one clean static win in this pass: an irreversible op inside `change` is a witnessed structural fact in the diff, not a guess about table size, so it earns a BLOCK. Cite the file, the line of the irreversible call, and name the operation. The fix to state: move the body into `def up` / `def down`, or wrap the irreversible part in `reversible do |dir|`.

#### 2.7b. `null:false` added without a default (advisory FIX — verify table size)

Flag an `add_column` (or `add_reference`) carrying `null: false` with no `default:` keyword, and a `change_column_null ..., false` with no backfill in the same migration. On a populated table this fails: existing rows have NULL in the new column and the NOT NULL constraint rejects them mid-migration.

Raise a **FIX**, never BLOCK, and label it exactly: "advisory, verify table size: `null: false` with no `default:` fails on a populated table because existing rows violate the constraint. Safe on an empty table; this static read cannot see the row count." The fix to suggest: add a `default:`, or backfill the column in a prior step before adding the constraint. Do not present this as a confirmed defect; the repo's safe migrations use this same shape on tables that happen to be empty.

#### 2.7c. `add_index` without `algorithm: :concurrently` (advisory FIX — verify table size)

Flag an `add_index` (or `add_reference ..., index: true`) call that does NOT pass `algorithm: :concurrently`. A plain `add_index` takes a lock that blocks writes for the duration of the build; on a large table in production that is a write outage.

Raise a **FIX**, never BLOCK, and label it exactly: "advisory, verify table size: `add_index` without `algorithm: :concurrently` locks the table against writes while the index builds. Fine on a small table; this static read cannot see the row count." The fix to suggest: add `algorithm: :concurrently` (and `disable_ddl_transaction!` at the top of the migration, which the concurrent build requires). Do not present this as a confirmed defect; most index migrations in a typical repo omit `concurrently` and are fine because the table was small.

Never let 2.7b or 2.7c reach BLOCK. They are table-size reminders the author resolves by checking the row count, not findings backed by anything this pass can see. Only the irreversible-`change` check (2.7a) is a witnessed structural fact and the only BLOCK this pass can raise.

### Step 2.8: Co-change advisory (when the diff ADDS new files)

Run this once over the whole changed-file set, not per file. A new file of a kind that structurally cannot stand alone (a Rails model needs a migration, a new controller needs a route wired up, a Prisma schema change needs a migration, a Redux slice needs to be registered in the store) is a missing-companion gap a human reviewer catches by reading the whole change at once. The convention review above checks each file in isolation and cannot see it.

This pass uses chameleon's curated co-change pairs, not a learned statistic. The pairs are a small directional table in the engine (`cochange.py`): each rule has a `trigger` (the new file that demands a companion), a `companion` (the file that satisfies it), and a `rule_id`. The shipped rules are `cochange-model-migration`, `cochange-controller-route`, `cochange-prisma-migration`, and `cochange-slice-store`. Co-presence is never derived from this repo; only these curated rules apply, and each is silenced for a repo whose own committed files break the pairing too often to trust it.

Restrict this to files the diff ADDS (status `A` in the diff, a brand-new path). A modified existing file does NOT trigger: editing a method on an existing model must not demand a fresh migration. For each added file:
- Match its repo-relative path against each rule's trigger (a Rails model is `app/models/*.rb` excluding `concerns/` and `application_record.rb`; a controller is `app/controllers/*_controller.rb` excluding `concerns/` and `application_controller.rb`; a Prisma schema is `*.prisma`; a slice is a `*slice.ts`/`*slice.tsx` file).
- If a rule's trigger matches, check whether ANY file in the whole change-set (added or modified) satisfies that rule's companion. The companion may be an edit to an existing file (a route added to an existing `config/routes.rb`), so check the full set, not just the new files.
- If no file satisfies the companion, raise a **FIX** advisory naming the new file and the rule's expectation (e.g. "new model added without a db/migrate migration in the same change"). Cite the `rule_id` so the author can suppress a deliberate split with `chameleon-ignore <rule_id>`.

Cap this at FIX, never BLOCK. A partial change may legitimately defer its companion to a follow-up commit, so this is a "confirm the companion isn't needed" prompt, not a confirmed gap. The trigger path is the witnessed fact each finding cites: the added file is in the diff and matches a curated rule, and no companion is present in the change-set.

### Step 2.9: Cross-file passes (layering, duplication, existence breaks, caller blast radius)

These four passes see across files, which the per-file convention loop cannot. Each is grounded in a concrete chameleon artifact or tool result; a finding with no backing entry is dropped by the integrity rule like any other.

The three tool-backed passes below (2.9b duplication, 2.9c existence breaks, 2.9d caller blast radius) depend on an MCP tool. If a tool is not available in this session, skip that pass and note it in one line ("cross-file existence-break pass skipped: `get_crossfile_context` unavailable") rather than failing the review. A missing cross-file tool removes a signal; it never blocks the rest of the review or forces a verdict.

#### 2.9a. Layering / cycle violations (NIT or FIX, advisory)

Load the `layering` section of `.chameleon/conventions.json` (`conventions.layering`). When present it carries `forbidden_upward_edges` (each a `{from, to, observed_direction}` pair: the engine saw archetype `to` import archetype `from` in N files and never the reverse, so a new `from -> to` import inverts the established direction) and `import_cycles` (the bootstrap static cluster-level cycle report). When the section is empty or absent, skip this pass.

For each file the diff ADDS or changes an import in, resolve the new import's target to its archetype (the same archetype the file would match) and check whether the resulting edge matches a `forbidden_upward_edges` entry. Surface a diff-introduced upward-edge violation as a **FIX** advisory, naming the two archetypes and the `observed_direction` the edge inverts. Reference the bootstrap cycle report: if the new edge appears in or extends an `import_cycles` entry, note it. Keep this advisory (NIT for a borderline edge, FIX for a clear inversion), never BLOCK: the layering data is statistical and a deliberate exception is indistinguishable from a mistake here. Cite the `layering` entry the finding rests on.

#### 2.9b. Semantic duplication of NEW functions (FIX or NIT, advisory)

For each NEW function or method the diff adds, call the `get_duplication_candidates` MCP tool:
```
get_duplication_candidates(repo=<repo_id>, file_path=<abs_path_of_changed_file>)
```
The tool returns candidate existing functions that are semantically similar to the functions defined in the file, prefiltered from the bootstrap function catalog by signature shape and name-token overlap. The tool only PREFILTERS; it does not decide duplication. You are the semantic-equivalence judge: read the new function body against each returned candidate's name, signature, and excerpt, and decide whether the new function re-implements the intent of one of them.

Raise a **FIX** (or NIT for a weak match) only when the new function duplicates the intent of a candidate the tool returned, citing that candidate's symbol and path. Never claim duplication without a candidate: if the tool returns no candidates for a function, there is no duplication finding for it, full stop. A re-implemented helper that the catalog did not surface is invisible to this pass, and inventing a "this probably already exists somewhere" finding is exactly the ungrounded claim the integrity rule forbids. Advisory only, never BLOCK.

#### 2.9c. Cross-file existence breaks (FIX)

Call the `get_crossfile_context` MCP tool once for the whole review:
```
get_crossfile_context(repo=<repo_id>)
```
It returns ONLY existence-break findings: an export that the indexed importer set still references by name is now gone from the module that used to export it, so the importer's call site is broken. Each finding carries a `high_confidence` flag.

Relay a finding as a **FIX** ONLY when `high_confidence` is true, citing the removed/renamed symbol, the module that no longer exports it, and the importer file:line the tool reported. Drop every finding without `high_confidence=true`: a leaky resolver can produce a finding that cites a real-looking entry but resolved wrong (a barrel re-export, a same-name collision, a dynamic import), and relaying it would launder a wrong inference past the integrity rule. The tool is the witnessed fact here; do not add your own cross-file existence claims on top of what it returns.

#### 2.9d. Caller blast radius for MODIFIED functions (context, not a finding)

For each function the diff modifies, call the `get_callers` MCP tool:
```
get_callers(repo=<repo_id>, file_path=<abs_path_of_changed_file>, function_name=<function>)
```
List the returned caller sites with their grades as blast-radius context for the finding pass: a signature, contract, or behavior change to a function with recorded callers is judged against those call sites, not in isolation. Grades are deterministic (`same_file` / `import` / `constant_receiver`), read from the committed calls snapshot at profile derivation, so each cited site is a real recorded call, not an inference.

Absence of callers is NOT evidence of dead code: dynamic and unsupported call paths (reflection, metaprogramming, superclass chains) are invisible to the index, as is anything added after the last refresh. Never raise an "unused function" finding from an empty result. Name-token candidates from `get_duplication_candidates` may be listed separately alongside this context, but must be labeled non-deterministic; they never carry the deterministic grades above.

### Step 3: Logic review (only when Jira ticket provided)

#### 3a. Gather task context

- **Jira ticket**: use the Atlassian MCP `getJiraIssue` tool. Read description, acceptance criteria, attachments.
- **Slack threads**: if the Jira ticket references Slack, or if a linked Slack thread exists, read it via Slack MCP `slack_read_thread`.
- **Attached docs**: if the ticket has attachments (screenshots, design docs), fetch what you can. List what you can't fetch and ask the user to paste them.

#### 3b. Check implementation completeness

For each requirement or acceptance criterion in the ticket:
- Is there corresponding code in the diff that implements it?
- If not, flag as BLOCK: "Requirement X has no implementation in this diff."

#### 3c. Check edge cases

For each changed file, consider:
- **Null/nil/undefined guards**: can any input be empty or missing? Is it handled?
- **Empty collections**: what happens when a query returns no results?
- **Authorization**: does the endpoint check permissions? Does it match the ticket's permission requirements? For a Ruby controller, look up the file's archetype in the `required_guards` section of `.chameleon/conventions.json` (`conventions.required_guards[<archetype>]`). When it carries `required_guards` (the authorization guards present in at least 60% of that archetype's controllers, e.g. `["authorize!"]`) and the changed controller declares none of them, raise a **FIX** naming the specific expected guard (`before_action :authorize!`) and the archetype it was derived from. Keep the same honesty label as Step 2.6b: "cannot confirm the new action is covered; authorization may be inherited from a base controller." Check the archetype's `known_guards` first, a guard listed there is a legitimate variant, not a miss. When the archetype has no `required_guards` entry, fall back to the presence-only check (Step 2.6b). Never reach BLOCK; this is advisory.
- **Error handling**: look up the file's archetype in the `error_handling` section of `.chameleon/conventions.json` (`conventions.error_handling[<archetype>]`). When present, it carries the archetype's dominant shape: `try_catch` or `rescues` frequency, `sample_size`, and an optional `error_shape` naming the project error target (e.g. `render json: { error`, `render_error`, an `*Error`/`*Serializer` call). Cite that entry: "this archetype handles errors via `<error_shape>` in `<frequency>` of its files; this change does not." Raise a **FIX** when the changed code adds an error path that does not match the recorded shape. When the archetype has no `error_handling` entry, fall back to comparing against the canonical witness for the pattern (Step 2c).
- **Race conditions**: for async or background operations, can two requests conflict?

Flag genuine risks as FIX. Don't flag hypothetical concerns.

#### 3c-i. Callable signature drift (advisory FIX at most)

When the changed file declares or overrides a function/method whose name the archetype has a consensus shape for, compare its signature against `.chameleon/conventions.json` `conventions.callable_signatures[<archetype>]`. Each entry under `signatures` records, per callable name, the consensus `params` (positional arity and which slots are optional), the `agreement` across the archetype's files, `file_count`, and an optional `overrides_base` (the in-repo base class the name is overridden from, recorded only when that base is itself defined in the repo).

Raise a **FIX** (never BLOCK) when a changed callable drops a required positional parameter the consensus shape carries, or when an override named in `overrides_base` diverges from the base's shape. The full file content lets you resolve the multiline/destructured/defaulted cases a regex cannot, so this comparison is yours to make at review time. Cite the callable name and the `callable_signatures` entry. When the name has no consensus entry, or the archetype has no `callable_signatures` section, there is nothing to compare against, skip it. Framework base contracts (`ApplicationController#render`, a Sidekiq `perform`) are not in the profile, so do not assert a divergence against them.

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

The findings sections answer "is anything wrong with this change?" This step answers the different question they cannot: "is this change *routine* enough that, with a clean review, a human can skip it?" Call the `get_autopass_verdict` MCP tool once for the whole diff:

```
get_autopass_verdict(repo=<repo_id>, base_ref=<the PR base branch, or the branch's merge base; use the locked production_ref from .chameleon/config.json when no PR base is known; default "main">)
```

It returns `{auto_pass_eligible, risk, complexity_tier, reasons, facts, changed_files}`. Report it verbatim as an advisory line (the Auto-pass routing section in Step 4). It is ADVISORY only: it never produces a BLOCK, FIX, or NIT and never changes the verdict. Its job is to mark the safe-to-skip slice, grade the change's inherent complexity (`complexity_tier`: easy / medium / hard / complex — structural, independent of cleanliness), and name why a change is NOT in the skip slice (a security-sensitive surface, too large, high cross-file blast radius, a file outside the profiled archetypes, or a grounded block finding).

The verdict also carries `typecheck` (three-state) and deterministic test-integrity/content facts inside `facts` (`deleted_test_files`, `net_test_line_delta`, added skip markers, assertion delta, removed guard lines, chameleon-ignore directives added, `blast_radius_unknown`, `diff_scan_truncated`) — all engine-computed. Relay them verbatim; never recompute them by eyeballing the diff. This does NOT loosen the Step 3g integrity rule against hand-counting assertions: the assertion delta is now engine-grounded and arrives in the tool result, and the skill still never counts by hand. The three-state typecheck rule: `typecheck: unavailable` (the default — the runner is opt-in via `CHAMELEON_ALLOW_TSC`) is reported as one fact line and is NOT a needs-human reason; `errors` appears in `reasons` and routes needs-human.

Read it together with the findings verdict, never instead of it: a change is a credible "no human review needed" candidate only when the findings verdict is APPROVE AND `auto_pass_eligible` is true. A clean findings verdict on a change the router sent to a human (an auth/payment/migration surface, say) is NOT a skip candidate — state that plainly. If the tool is unavailable this session, skip this step and note it in one line, the same as the cross-file passes.

### Step 4: Output

#### 4a. Hunk gate (apply before formatting any logic finding)

Every logic finding (from Step 3b through 3f) must anchor to a specific line in a changed file. Look that line up in the per-file hunk map from Step 1a. If the anchor line is NOT inside an added or changed range for that file, drop the finding. No exceptions and no judgment call: a logic finding on a line this change did not touch is pre-existing by construction, and the integrity rule forbids flagging pre-existing issues.

This gate is the mechanical replacement for "decide by hand whether this is PR-introduced." It does not apply to convention findings whose anchor is the file as a whole (duplication NITs, missing-test NITs), the whole-diff cross-file passes (co-change FIX in Step 2.8, layering/duplication/existence-break findings in Step 2.9, which anchor to a file or an artifact entry, not a single changed line), nor to missing-requirement BLOCKs in Step 3b (those flag the ABSENCE of code, so they have no anchor line). It applies to every per-line logic claim: removed guards, inverted conditions, dropped awaits, null-guard gaps, placeholder names, the stale-comment NIT (Step 3f-ii) and the stale-test removed-export anchor (Step 3f-i), the taint/SSRF/traversal findings from Step 2.6c, AND the secret findings from Step 2.6a. The secret scanner reads the full file content, not the diff, so a hit is not in the change by construction: an out-of-hunk hard-kind secret goes to the "Pre-existing repo hygiene" note (Step 2.6a) instead of the verdict.

It ALSO applies to any line-anchored convention/style finding from `lint_file` (Step 2b/2d). `lint_file` reads the whole file, not the diff, so a `style-rule-violation` (e.g. "line 19 is 103 cols"), a `naming-convention-violation`, or an `inheritance-convention-violation` can sit on a line this change never touched. Parse the line number out of the violation `message` (the `line N` / `:N` it carries) and run it through the hunk map the same way as a logic finding: if the line is outside an added or changed range, drop it from the verdict. A line-anchored style/convention nit that pre-dates the change is pre-existing by construction and the integrity rule forbids reporting it; if it is worth mentioning at all, it goes to the "Pre-existing repo hygiene" note, never the Convention-findings section. Only convention findings with NO parseable line (duplication, missing-test, key-export overlap, which anchor to the file) stay exempt.

#### 4b. Round 3 — independent refutation (model-judgment findings only)

After rounds 1-2 (Step 4a + the verification bullet), collect every surviving
BLOCK and FIX that is a MODEL-JUDGMENT finding — change-delta logic (removed
guard, dropped await, inverted condition), canonical-divergence, taint/SSRF/
path-traversal, callable-signature drift, spec-compliance / missing-requirement,
placeholder-name, stale-comment. Send them in ONE call:

`refute_finding(repo=<repo_id>, findings=[{id, kind, severity, file, line, claim, evidence}, ...], base_ref=<base>)`

TOOL-GROUNDED findings are EXEMPT — never send them to the refuter; verify them
inline by re-confirming the tool flag still holds (existence-break with
`high_confidence`, duplication with a returned candidate, co-change `rule_id`,
layering, a secret `lint_file` hit, a lint/naming/inheritance violation with a
parsed line). The refuter sees one excerpt and cannot re-derive cross-file
evidence, so sending these would wrongly drop the strongest findings.

Apply each returned verdict:
- `refuted` → DROP the finding (the refuter rebutted the cited evidence).
- `confirmed` → KEEP it (this never authorizes an edit or a post).
- `unverified` (refuter disabled / unavailable / timed out / cap reached) → KEEP
  it on rounds 1-2, labeled "self-verified, round 3 unavailable", with downgraded
  confidence. Never drop and never silently confirm.

Banner: report `<b>` refuted-dropped, `<c>` inline-exempt, `<d>` self-verified.
NEVER print "3/3" when round 3 did not adjudicate (disabled/unavailable/capped).
NITs are verified inline only — they are not sent to the refuter.

Format the review as follows:

```
## Verdict: [APPROVE / APPROVE WITH NITS / NEEDS CHANGES / BLOCK]

Reviewed N files against chameleon conventions + [ticket KEY / branch diff].

Grounding: rounds 1-2 self-verified; round 3 independently refuted <b> dropped, <c> inline-exempt, <d> self-verified (round 3 unavailable).
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
- `app/controllers/orders_controller.rb` — No `before_action :authorize!`; `required_guards` for archetype `controller` lists it (60% of controllers). Cannot confirm the new action is covered; may be inherited from a base controller (advisory)
- `app/services/refund.rb:12` — Error path does not match the archetype's `error_handling` shape `render_error` (88% of `service` files); conventions.json
- `src/api/user.ts:30` — `fetchUser` drops the required `signal` param the `callable_signatures` consensus for archetype `api` carries (advisory)
- `spec/models/user_spec.rb:14` — Stale test: source removed export `getUserById` (renamed) but the paired spec still references `getUserById(` (test_pairing)
- Endpoint shape diverges from spec (spec says X, code does Y)

### Dependency / supply-chain findings (Z issues)

Only present when the diff touched a manifest or lockfile (Step 2.5). Omit the section otherwise.

**BLOCK:**
- `package.json:31` — New direct dependency `left-pad@^1.3.0`; verify provenance (not a typosquat, intended addition) before merge

**FIX:**
- `package-lock.json:204` — Resolved host `evil.example.com` is not `registry.npmjs.org`
- `package.json:12` — New `scripts.postinstall`: `node ./setup.js` runs automatically on install
- `package.json:9` — Dependency `acme-utils` pulled from `git+ssh://git@github.com/acme/utils.git`, not the registry

### Security findings (W issues)

Always present (the security pass runs on every changed source file, ticket or not). Secret BLOCKs are witnessed facts; the authz and taint findings are labeled advisory judgments — keep the labels.

**BLOCK:**
- `config/initializers/stripe.rb:4` — Secret detected: Stripe Secret Key. Rotate it and move to an env var. Verify this is not a live credential; if it is a test fixture, it is safe to keep.

**FIX:**
- `app/controllers/orders_controller.rb` — Presence-only authz check: the witness controller declares before_action callbacks; this changed controller declares none and adds a new action. Cannot confirm the new action is covered; authorization may be inherited from a base controller.
- `app/controllers/reports_controller.rb:31` — Advisory, single-hunk scope: `params[:cmd]` flows into `system(...)` on this line with no sanitization in the hunk. May be a false positive if sanitized elsewhere.

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
Typecheck: 2 changed file(s) with type errors
```

Render the `complexity_tier` field as `Tier: <easy|medium|hard|complex>` with a short reason drawn from the facts, then `auto_pass_eligible` as ELIGIBLE / NEEDS HUMAN, the `risk`, and the `reasons` list verbatim; render the `typecheck` field as `Typecheck: unavailable (<reason>)` / `Typecheck: clean` / `Typecheck: N changed file(s) with type errors`. If the tool was unavailable, write one line saying the auto-pass routing was skipped. Omit nothing: an ELIGIBLE verdict is only a skip candidate when the findings verdict is APPROVE — state that pairing explicitly. The tier is the change's inherent complexity (structural), independent of whether it is clean: an `easy`/`medium` change that is APPROVE + ELIGIBLE is the review-clean routine slice; `hard`/`complex` changes carry an irreducible human-judgment residual even when the findings verdict is clean.

### Per-file details

#### `path/to/changed_file`
- Archetype: `name` (confidence: band, match: quality)
- Canonical witness: `path/to/witness`
- Violations: N (breakdown by severity)
- [details or "Follows conventions correctly."]
```

### Severity classification

| Severity | Meaning | Convention examples | Logic examples | Dependency examples | Security examples | Migration examples | Cross-file examples |
|----------|---------|-------------------|----------------|---------------------|-------------------|--------------------|---------------------|
| **BLOCK** | Must fix before merge | Missing base class/mixin the archetype requires | Missing requirement, race condition, removed guard/error branch | New direct dependency (verify provenance) | Secret detected in the diff | Irreversible op in a `change` block | — |
| **FIX** | Should fix | Wrong response pattern, missing naming convention | Missing null guard, spec divergence, dropped await, inverted condition, error-handling/required-guard divergence (advisory), callable-signature drop (advisory), stale paired test | Non-registry resolved host, new install script, git+ssh:/file: source | Presence-only authz gap (advisory), taint/SSRF/traversal in hunk (advisory) | null:false without default (advisory), add_index without concurrently (advisory) | High-confidence existence break (get_crossfile_context); missing companion (co-change, advisory); upward-edge layering violation (advisory) |
| **NIT** | Optional improvement | Potential duplication with existing utility | Minor inconsistency, placeholder name vs descriptive siblings, stale comment | — | — | — | Semantic duplication of a new function vs a returned candidate (get_duplication_candidates); borderline layering edge |

For reviewers used to the superpowers vocabulary: BLOCK ≈ Critical, FIX ≈
Important, NIT ≈ Minor. Chameleon keeps BLOCK/FIX/NIT because the review ledger
(`record_review_verdict`) is keyed on them.

Authz and taint/SSRF/traversal findings are capped at FIX. They are advisory judgments and never escalate to BLOCK; only a hard-kind secret on an added/changed line blocks from the security pass (Step 2.6a kind gate + hunk gate). Low-precision secret heuristics cap at NIT; out-of-hunk hard secrets go to the repo-hygiene note.

The migration null:false and add_index advisories are capped at FIX. They are "verify table size" reminders the author resolves by checking the row count; only the irreversible-`change` check blocks from the migration-safety pass.

The cross-file findings (Step 2.8 co-change, Step 2.9 layering/duplication/existence-break) cap at FIX. Only a high-confidence existence break from `get_crossfile_context` is a witnessed FIX; co-change, layering, and duplication are advisory and never reach BLOCK. The error-handling, required-guard, callable-signature, and stale-comment findings are advisory too: required-guard and callable-signature drops cap at FIX, the stale comment caps at NIT.

### Verdict rules

- **BLOCK**: any BLOCK finding → verdict is BLOCK. A hard-kind secret on an added/changed line (Step 2.6a, both gates passed) is a BLOCK and drives a BLOCK verdict; an irreversible op in a `change` block (Step 2.7a) is a BLOCK and drives a BLOCK verdict; the advisory authz/taint findings and the migration table-size advisories (Step 2.7b/2.7c) are capped at FIX and never force a BLOCK verdict on their own. Pre-existing-hygiene secret notes never affect the verdict.
- **NEEDS CHANGES**: any FIX finding but no BLOCKs → NEEDS CHANGES
- **APPROVE WITH NITS**: only NIT findings → APPROVE WITH NITS
- **APPROVE**: zero findings → APPROVE
- The coverage-delta view (Step 3g) is advisory and carries no severity. It never adds a BLOCK, FIX, or NIT and never changes the verdict; an untested-source heads-up alone still leaves an otherwise clean PR at APPROVE.
- The auto-pass routing (Step 3h) is advisory and carries no severity. It never adds a finding and never changes the verdict. It is a separate signal: a change is a "no human review needed" candidate only when the verdict is APPROVE AND auto-pass is ELIGIBLE; a NEEDS-HUMAN routing on an otherwise-APPROVE change means a human should still look, and an ELIGIBLE routing never upgrades a NEEDS CHANGES/BLOCK verdict.
- The cross-file findings (Step 2.8 co-change, Step 2.9 layering/duplication/existence-break) cap at FIX, so they can drive NEEDS CHANGES but never BLOCK. A high-confidence existence break is a real FIX; co-change, layering, and duplication are advisory FIX/NIT that the reviewer can confirm away.

### Step 5: Record the verdict in the review ledger

After the verdict is rendered and shown to the user, append it to the review ledger by calling the `record_review_verdict` MCP tool:
```
record_review_verdict(repo=<repo_id>, verdict=<the verdict string>, findings_count=<total BLOCK+FIX+NIT count>, commit_sha=<reviewed HEAD sha>, complexity_tier=<the complexity_tier from get_autopass_verdict in Step 3h, or omit if that step was skipped>)
```
Pass the verdict exactly as rendered (`APPROVE`, `APPROVE WITH NITS`, `NEEDS CHANGES`, or `BLOCK`), the total finding count across all severities, the commit SHA the review covered (the branch HEAD for the no-args case, or the PR head commit), and the `complexity_tier` from Step 3h's auto-pass routing (so a lead can later read the review-clean rate per tier — the routine easy/medium slice versus the hard/complex residual). The ledger stamps the rest of the provenance itself (the profile that reviewed it, the trust state, the engine version, the reviewer, a UTC timestamp).

Once review is optional, the skill is the system of record for "this change was checked", but the chat output disappears. The ledger is the durable trail: past verdicts are queryable with `get_review_history`, and a lead can see which BLOCK verdicts shipped anyway. State the scope honestly when the ledger comes up: it is tamper-evident (a third local user editing a line makes it fail verification), NOT forgery-proof. The reviewed developer holds the signing key and CI cannot verify the records, so the ledger is an honest self-attested audit log, not a merge authority.

This is a best-effort final step. If the tool call fails (no ledger, no signing key), the verdict still stands in chat; do not retry or block the review on it.

## Integrity rules

- **Be honest.** If you're unsure about a finding, say so. Don't guess whether something is a violation — verify it against the canonical witness and conventions data. If the data doesn't clearly show a violation, don't flag it.
- **Don't hallucinate findings.** Every convention/logic BLOCK and FIX must reference specific chameleon data (a lint violation, a canonical mismatch, a convention entry, a principle) or, for logic findings, the removed (`-`) lines of a hunk. If you can't point to the data, it's not a finding. Dependency findings (Step 2.5) are the one exception to the chameleon-data requirement: they are backed by the manifest/lockfile diff itself, so every one must cite the exact added line or manifest key it parsed, not the profile. The error-handling, required-guard, and callable-signature findings (Step 3c/3c-i) cite the matching `conventions.json` entry (`error_handling`/`required_guards`/`callable_signatures` for the file's archetype); without that entry, fall back to the witness and do not invent the convention. The stale-test finding (Step 3f-i) cites the removed export and the line in the paired test that still names it.
- **Cross-file findings cite their tool or artifact, not intuition.** The existence-break FIX (Step 2.9c) relays only `get_crossfile_context` findings with `high_confidence=true`, citing the symbol, the module, and the importer file:line the tool returned; a finding without that flag is dropped. The duplication finding (Step 2.9b) is allowed only when `get_duplication_candidates` returned a candidate you judged equivalent, citing that candidate's symbol and path; never claim duplication with no returned candidate. The layering finding (Step 2.9a) cites a `conventions.layering` `forbidden_upward_edges` entry. The co-change FIX (Step 2.8) cites the curated `rule_id` and the added trigger file. None of these is bare model intuition; each points at a tool result or an artifact entry, same bar as a lint violation.
- **Security findings carry their own honesty bar.** A secret BLOCK (Step 2.6a) cites the `secret-detected-in-content` violation the scanner returned — a witnessed fact, like a lint violation — but only after both 2.6a gates passed: `secret_hard` is true AND the line sits inside an added/changed hunk. A low-precision heuristic hit or an out-of-hunk hit presented as a BLOCK is a false claim, the exact kind that destroys trust in a green gate. The authz FIX (2.6b) and the taint/SSRF/traversal FIX (2.6c) are judgments, not witnessed facts: they must carry their advisory labels, must never claim a structured profile cite (no profile data maps callbacks to actions), and the taint line must be inside the diff. Do not present a judgment as if it had the same backing as a gated secret hit or a lint violation.
- **Migration findings carry their own honesty bar.** The irreversible-`change` BLOCK (Step 2.7a) cites the irreversible operation in the diff — a witnessed structural fact. The null:false and add_index FIXes (2.7b/2.7c) are table-size reminders, not confirmed defects: the dangerous condition is a row count this static read cannot see, and the repo's own safe migrations share the same shapes. They must keep their "verify table size" label and never reach BLOCK. Do not present either reminder as if it were a confirmed migration bug.
- **The coverage-delta view is advisory and grounded only in archetypes.** The Step 3g partition rests on the file's archetype name (source vs test) and the test-vs-source archetype path mirror in `archetypes.json`. It must not claim a specific missing test file ("`foo.rb` needs `foo_spec.rb`") — chameleon has no source-to-test path map, and the diff lists only changed files, so a pre-existing untouched test is invisible. Keep it as a heads-up listing changed source in a test-paired layer, never a FIX or BLOCK, and never count an assertions delta: there is no assertion counter in chameleon, so an eyeball count of diff hunks would be exactly the ungrounded finding the integrity rule forbids.
- **3-round grounding loop.** After producing the review, re-read each BLOCK, FIX, and NIT finding. For each one, verify: (1) does the canonical witness, conventions data (`error_handling`/`required_guards`/`callable_signatures`/`test_pairing`/`layering`), the removed (`-`) lines of the hunk, a parsed manifest/lockfile line, a returned secret violation, or a returned tool result (`get_duplication_candidates` candidate, `get_crossfile_context` finding with `high_confidence=true`) actually support this claim? (2) for per-line logic findings, the stale-comment NIT (3f-ii), AND the taint/SSRF/traversal findings (2.6c), is the anchor line inside an added/changed hunk range (Step 4a)? Drop any finding that fails either check. The hunk gate is the deterministic answer to "PR-introduced vs pre-existing"; do not override it by judgment. The whole-diff cross-file findings (Step 2.8/2.9) are not hunk-gated; they are gated on their tool/artifact backing instead. Round 3 is the independent engine refutation pass for surviving model-judgment findings — see Step 4b.

## Important

- Do NOT auto-fix code. Report only.
- Do NOT post comments to Bitbucket/GitHub. Show findings in chat only.
- Do NOT touch the Jira ticket (no comments, no status changes).
- When unsure if something is a violation, check the canonical witness. If the witness does the same thing, it's not a violation.
- Distinguish between violations the PR INTRODUCED vs pre-existing issues the PR didn't cause. Only flag PR-introduced issues. For per-line logic findings this is the Step 4a hunk gate, not a judgment call: a finding off the changed hunks is dropped.
- The change-delta logic pass (Step 3e) compares the hunk against its own removed (`-`) lines, not against the canonical witness. The witness is for convention/shape comparison (Step 2c) only.
- Skip auto-generated files: `schema.rb`, `*.generated.*`, vendored files. These produce false positives. Lockfiles (`*.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`) are skipped for archetype/lint/canonical review but NOT for the dependency-change review (Step 2.5).
- Dependency findings come from the diff parse only (Step 2.5). Do not run a security audit, hit a network, or install packages during the review.
- Large utility files (helpers, concerns, base classes) often have different shapes than the canonical — use judgment, not blind flagging.
- The cross-file tools (`get_duplication_candidates`, `get_crossfile_context`, `get_callers`) read prebuilt profile artifacts; they make no network call and run no repo code. Do not relay a duplication finding without a returned candidate, or an existence break without `high_confidence`, and never read an empty `get_callers` result as dead code.
- After the verdict is shown, append it to the ledger via `record_review_verdict` (Step 5). It is best-effort and never blocks the review. The ledger is tamper-evident, not forgery-proof, and CI cannot verify it; past verdicts are queryable with `get_review_history`.
