---
name: chameleon-pr-review
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

**Read-only review.** This review never mutates the repo: do not edit the working tree, stage changes, move HEAD, or switch/reset/create branches. Inspect with `git diff` / `git show` / `git log` (and `gh` / `bbcurl` for a PR) only. If you must inspect another revision, use `git worktree add` into a temp dir, never `git checkout` / `git reset` on this checkout. (You also do NOT auto-fix code, per the Important section.)

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
- **Deleted (`D`)**: the file is gone. There is no content to `get_pattern_context` / `lint_file` (Step 2a/2b), so do NOT call them on it — its `get_pattern_context` returns `archetype.file_exists: false` (there is NO top-level `data.file_exists` key; the flag is nested under `archetype`, and Step 2a reads it there). A deletion has one real risk: importers of its removed exports now break. That is covered by the cross-file existence pass (Step 2.9c), which the engine reports for a deleted module. In the Step 2 coverage ledger, account for a deleted file as an explicit sanctioned skip (`lint_file skipped: file deleted`), never a gap to close.
- **Renamed (`R`)**: the diff lists the NEW path (the old path is invisible to `--name-only`, and a 100%-similarity rename has NO hunks). Add the OLD path to the changed-file set the Step 2.9c diff-scope gate reads, so a rename that removes an export the old path used to provide can reach the verdict (its `module` is the old path, which is otherwise absent from the diff). Review the NEW path as an ordinary modified file.
- **Binary**: `git` shows `Binary files differ` with no hunk. It is skipped per the Step 2 binary skip rule; account for it in the ledger as a named skip (`skipped: binary`).

For very large diffs, cap the removed-line text you feed forward per file (keep the hunk ranges in full; truncate only the removed-line bodies) so the review stays within context. Note in the output when a file's delta was truncated.

#### 1b. Prior-review comparison (same HEAD only)

Call `get_review_history(repo=<repo_id>, limit=5)` once. If a returned record pins
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

Call `get_autopass_verdict(repo=<repo_id>, base_ref=<base>)` and read
`data.fan_out`. The engine decides; you never read env yourself. If `recommended`
is false (the diff is under the threshold, or the engine saw `CHAMELEON_REVIEW_FANOUT=0`
when it computed the verdict), run the review single-pass inline exactly as today —
STOP here and continue with Step 2. If `recommended` is true, fan out:

- **If you cannot dispatch in-session Task reviewers** (no Task tool is available in this context — e.g. you are yourself running as a subagent), do NOT skip the review and do NOT rationalize a bypass: run the review single-pass inline exactly as the `recommended=false` path (every pass yourself, over the whole diff), and log `fan-out-recommended-but-unavailable`. The inline run is the correct, complete outcome; fan-out is only a parallelism optimization, never a precondition for reviewing.
- Partition the changed files (from your Step 1a hunk map) into ~4-6 slices,
  MULTIPLE files per slice — never one slice per file.
- Dispatch one in-session Task reviewer per slice using `reviewer.md`. Each runs
  ONLY the per-file passes for its files: 2a-2f (convention/lint/canonical), 2.6
  (security, including the 2.6d deterministic lint-sink routing — it reads the
  slice's own per-file `lint_file` output), 2.7 (the slice owning a migration),
  3c, 3e, 3f, 3f-ii. Reviewers are read-only (Read + read-only MCP). Fill the
  template's `{SLICE_HUNKS}` with each slice file's hunk map from Step 1a (added/
  changed ranges + removed lines): the reviewer has no Bash and cannot re-derive
  the diff, so without its hunk map it cannot run 3e or hunk-gate anything — a
  slice dispatched without hunks reviews blind. Each reviewer returns
  `{manifest, findings}`; at synthesis, reject a slice whose manifest has an
  unexplained gap (a pass neither run nor covered by a sanctioned skip reason)
  and re-run that slice's missing passes yourself before merging. Step 2.5
  (dependency-change) is NOT delegated per-slice: it is a whole-diff tool and the
  reviewers are not granted `scan_dependency_changes`, so it runs once at synthesis.
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
If `archetype.file_exists` is `false` (the nested flag — there is no top-level `data.file_exists`), the path was deleted in this diff (a `D` file that slipped past the Step 1a status check): do NOT review it as a normal source file or call `lint_file` on it — route it to the deleted-file handling (Step 1a), whose only real risk is the cross-file existence break (Step 2.9c).

#### 2b. Run lint

Call the `lint_file` MCP tool:
```
lint_file(repo=<repo_id>, archetype=<archetype_name>, content=<file_content>, file_path=<abs_path>)
```

Collect ALL violations from the response. Each violation has `rule`, `severity`, `message`, `expected`, `actual`.

**Run this on every changed FILE (source or not), even when no archetype matches.** `lint_file` scans for secrets before it looks at the archetype, so it returns `secret-detected-in-content` violations regardless of whether the file matches a known shape or the profile is trusted. Step 2.6 reads those secret violations, so the lint call cannot be skipped just because `match_quality` is "none" or the file is a doc/config file — the secret scan runs on all of them. When `get_pattern_context` returns `archetype` null/none (no match), STILL call `lint_file`, but pass a non-null placeholder archetype STRING — the fallback `get_pattern_context` suggests, or the literal `"none"`. Do NOT pass `null` and do NOT omit the `archetype` argument: it is a required string, and a null/omitted value makes `lint_file` return early BEFORE the secret and sink scans run, defeating the purpose. With a non-null string the secret scan and the dangerous-sink scan both run regardless of the archetype, and the structural part simply stubs/noops for an unknown archetype (an expected fail-open, not an error) — those `secret-detected-in-content` and sink violations are exactly what Step 2.6a / 2.6d read. Ignore the structural violations for an unmatched file.

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

Run this whenever the diff touches a dependency manifest or lockfile of ANY ecosystem — the npm/Bundler set the tool parses (`package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `Gemfile`, `Gemfile.lock`) AND the ones it does not (Python `requirements*.txt` / `pyproject.toml` / `Pipfile` / `setup.py`, Go, Rust, PHP). Always call `scan_dependency_changes` (below) — it parses the npm/Bundler files and reports the rest in `uncovered_manifests` for the hand-review disclosure. These are the supply-chain entry points a human reviewer reads line by line and the convention review above does not cover. This pass is a pure parse of the diff text and the manifest/lockfile JSON or YAML. It makes NO network calls and does not install or run anything: only the added (`+`) lines matter, and the existing repo content gives the "previously present" baseline.

**Tool-backed (deterministic):** Call the `scan_dependency_changes` MCP tool once for the whole diff:

```
scan_dependency_changes(repo=<repo_id>, base_ref=<the PR base branch, or the branch's merge base; use the locked production_ref from .chameleon/config.json when no PR base is known; default "main">)
```

It parses the manifest/lockfile diff (no network) and returns structured `findings`. Each finding is `{check, severity, path, evidence, message, detail}` — the check TYPE is `finding.check` (NOT `rule`; that is `lint_file`'s field), the cited added line is `finding.evidence` (NOT `line`), the manifest/lockfile it sits in is `finding.path`, and `finding.severity` is the literal `"FIX"` or `"NIT"` (not `error`/`warning`). Route by `finding.check`: `install-script`, `non-registry-host`, `non-registry-source`, and `minified-manifest` come back at severity `"FIX"`; `new-dependency` comes back at `"NIT"` and is the 2.5a listing you carry into the human-judgment gate below. (The top-level envelope also carries `manifests_changed` and `summary` on a successful scan, and `uncovered_manifests` ONLY when non-empty; a degraded scan — `status` `"degraded"`/`"failed"` — omits all three, so guard for their absence rather than reading them unconditionally.) Use these findings as the deterministic source for 2.5b/2.5c/2.5d/2.5e and the 2.5a listing instead of hand-parsing the JSON/YAML — each is groundable by the round-3 refuter against the tool result (this is the Step 2.5 exception to the chameleon-data rule). The tool does NOT score typosquats; that judgment stays yours under 2.5a. If `scan_dependency_changes` is unavailable in this session, fall back to the manual parse described below and note it in one line. It is no-network and never replaces the opt-in `dep_audit` CVE scan.

**Uncovered ecosystems (Python and others) — disclose AND hand-review, split by severity exactly as the npm path does.** The scanner parses only npm and Bundler. A changed dependency manifest of an ecosystem it does not parse — Python (`requirements*.txt`, `pyproject.toml`, `Pipfile`, `setup.py`), Go, Rust, PHP — comes back in the envelope's `uncovered_manifests` list (with `findings` empty and `manifests_changed` empty, which for those files means "not parsed", NOT "reviewed clean"). When `uncovered_manifests` is non-empty, the tool could not scan it, but YOU can still read the added (`+`) lines — so hand-review them and route each signal to the SAME severity the npm/Bundler checks give the identical content. Do not lump everything into an ACK: a blatant supply-chain red flag that is plainly visible in the diff is a witnessed finding, and suppressing it because the parser was silent inverts the very asymmetry the ACK rule exists to prevent.

- **ACK (does NOT drive the verdict):** the "not covered by the automated scan" disclosure for each file (its own ACK line), AND a new direct dependency whose only signal is its NAME — a routine add, treated exactly like the npm 2.5a new-dependency ACK (confirm it is not a typosquat, e.g. `left-pad-py`). A routine Python dep add therefore stays APPROVE + ACK, symmetric with the identical npm add.
- **FIX (drives NEEDS CHANGES, like npm 2.5b/2.5d):** an added line carrying a red flag you can read directly — a non-registry or git/URL/path source (`pkg @ git+https://…`, a `-e <url>`, a `file:`/`path:` dep, a `[[tool.poetry.source]]` pointing off-PyPI), a registry redirection (a `--index-url`/`--extra-index-url` to a non-PyPI host in requirements), or an install hook (a `setup.py` that runs code, a Pipfile script). Cite the exact added line — this is the Step 2.5 diff-parse exception to the chameleon-data rule (the manifest diff is the backing fact), the same as the npm findings, and it is refuter-groundable against that line.

So a clean Python dependency add is APPROVE + ACK (no pollution), and a Python manifest carrying an index redirection or a git source is NEEDS CHANGES with a FIX — identical to how the same content reads in a `package.json`. The one thing never acceptable is a Python PR that added a non-registry source rendering a silent clean APPROVE.

Each finding cites the exact lockfile line or manifest key. The five checks are independent; run every one that applies even if an earlier check fired.

#### 2.5e. Minified manifest (FIX)

`scan_dependency_changes` returns a `minified-manifest` FIX finding when a `package.json` diff collapses a manifest object onto ONE physical added line — either the whole manifest as a single JSON line (`detail.reason` `single-line-manifest`) OR a dependency/pin/scripts container opened inline with its pairs packed onto one line (`detail.reason` `packed-container-line`, e.g. `"dependencies": { "evil": "git+ssh://…", "left-pad": "^1.0.0" }`, or a packed `overrides`/`resolutions` object that pins a transitive dependency to a git source). Both are supply-chain evasions: the per-key scanners (install-script, non-registry-host/-source, new-dependency) are line-oriented and cannot decompose a packed line, so every other 2.5 check was silently defeated for it. Surface it as a **FIX** citing the finding, and re-review the manifest by hand (expand it) before trusting any of the 2.5a-2.5d results for that file. A source file (not a manifest) is never subject to this check.

#### 2.5a. New direct dependency → acknowledge provenance (ACK, does NOT drive the verdict)

Parse the manifest diff (`package.json` `dependencies`/`devDependencies`/`optionalDependencies`/`peerDependencies`; `Gemfile` `gem` lines) for a dependency name that was NOT present before this change. A bump of an already-present dependency is NOT this finding (it may be a different finding under 2.5b/2.5d); only a name that did not exist in the manifest before counts as new. `scan_dependency_changes` reports each as a `new-dependency` finding at severity `NIT`.

For each new direct dependency, emit an **ACK** line in the dedicated "Acknowledge before merge" section. An ACK is NOT a BLOCK and does NOT drive the verdict. This is a deliberate human gate, not a defect claim: the reviewer must confirm the package is the intended one (not a typosquat of a popular name, e.g. `lodahs` for `lodash`, `cross-env.js` for `cross-env`) and that adding it is wanted. State the dependency name, the version range added, and the manifest file. A routine PR that only adds a legitimate dependency stays at its findings verdict (APPROVE if nothing else fired) with an outstanding ACK the human clears out-of-band.

(Earlier versions raised a BLOCK here. That conflated "must fix before merge" with "please confirm this is intended" and recorded every routine dependency add as a BLOCK verdict in the durable ledger, corrupting the per-`complexity_tier` review-clean metric. The provenance gate stays — it just lives in its own non-verdict ACK channel now, matching the engine's own `NIT`/advisory classification of `new-dependency`.)

When several new dependencies land in one change, list each as its own ACK line so each gets its own acknowledgement.

#### 2.5b. Lockfile resolved host is not the expected registry (FIX)

In the lockfile diff, every added entry that resolves a package records the URL it was fetched from. Flag any added entry whose resolved host is NOT the package manager's public registry:
- npm (`package-lock.json` `resolved`, `npm-shrinkwrap.json` `resolved`, `yarn.lock` `resolved`, `pnpm-lock.yaml` `resolution.tarball`/`resolved`): expected host is `registry.npmjs.org`.
- Bundler (`Gemfile.lock` `remote:` under a `GEM` section): expected host is `rubygems.org`.

A resolved URL pointing at any other host (a private mirror the repo does not already use, a raw GitHub tarball, an arbitrary domain) is a **FIX**: the dependency is being pulled from somewhere other than the registry, which is how a tampered or planted package enters. Cite the exact lockfile line and the host. If the repo's other lockfile entries consistently use a private registry (the diff shows the SAME non-`registry.npmjs.org` host on pre-existing entries), that host is this repo's normal registry; treat it as expected and do not flag added entries that use it. Flag only hosts that differ from what the rest of the lockfile already uses.

#### 2.5c. New install lifecycle script (FIX)

In the `package.json` diff, flag a newly added `scripts.preinstall`, `scripts.install`, or `scripts.postinstall` as a **FIX**. An install-lifecycle script runs automatically on `npm install` with no further prompt, which is the classic vector for code that executes the moment a dependency tree is materialized. Cite the script key and its command. (A script that already existed and is merely edited is still worth a look, but the FIX-worthy signal is a NEW install hook on a diff that also adds or bumps dependencies.)

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

1. **Kind gate.** Escalate to **BLOCK** only violations whose `secret_hard` field is true. The `secret_hard` boolean is the authority — read it off the violation, do not re-derive it from a prefix list (the recognized-kind set grows, and a hand-list drifts out of date). It marks the deterministic, fixed-shape credential kinds the engine recognizes: AWS `AKIA`, GitHub `ghp_`, GitLab `glpat-`/`gldt-`/`glft-`/`glsoat-`/`glrt-`, Anthropic `sk-ant-`, Stripe `sk_live_`/`sk_test_`/`rk_live_`/`rk_test_`, Slack `xox[baprs]-`, Google `AIza`, Azure `AccountKey=`, and PEM private keys — but trust the flag, not this parenthetical. Violations without `secret_hard` (40-char base64 runs, high-entropy hex, password assignments, JWT-shaped strings, entropy hits) match ordinary identifiers, git SHAs, and data blobs in real code at a rate that makes them verdict-poison; report them at most as a **NIT** labeled "low-precision secret heuristic, verify by eye", never as FIX or BLOCK, and never let them influence the verdict.
2. **Hunk gate.** `lint_file` scans the FULL file content, not the diff, so a hit is NOT in the change by construction. The reported line is the `at line N` token in the violation's `actual`/`message` string (e.g. `aws_access_key at line 40` -> line 40); every hard-kind secret carries one. A hard-kind secret whose reported line falls inside an added/changed hunk of this diff is a **BLOCK**. A hard-kind secret on a line the diff did not touch is pre-existing: report it in a separate "Pre-existing repo hygiene" note at the end of the review (it deserves rotation, but this PR did not introduce it), and do not let it affect the verdict.

For each secret BLOCK, cite the file and line, carry the violation's own message (it names the kind and tells the author to rotate it), and label it "verify this is not a live credential; if it is a test fixture, it is safe to keep" - a fixture key is overridden by the author, not silently dropped by this review.

#### 2.6b. Ruby controller authorization (advisory FIX, presence-only)

For Ruby controllers ONLY, compare the authorization-callback presence of the changed file against its canonical witness. The only signal the profile carries here is presence or absence of `before_action`-style callbacks; it does NOT map a callback to the action methods it guards, so this check cannot tell whether a specific new action is actually covered.

Raise a **FIX** (never BLOCK) when the canonical witness for this controller archetype declares `before_action` (or `prepend_before_action`) authorization callbacks and the changed controller declares none, AND the change adds a new public action method. Label it exactly as a heuristic: "presence-only check, cannot confirm the new action is covered. The witness controller declares before_action callbacks; this changed controller declares none. Authorization may still be inherited from a base controller." Do not claim a structured divergence; do not name which action is unguarded; do not cite a "witness authz divergence" as if the profile mapped callbacks to actions. It does not.

When the file's archetype carries a `required_guards` entry in `.chameleon/conventions.json` (`conventions.required_guards[<archetype>]`), cite the specific expected guard symbol rather than the generic "declares before_action callbacks" phrasing: name the guard (`before_action :authorize!`), the `sample_size` (how many of the archetype's controllers were measured) and the fixed 60% derivation floor the guard cleared, and the archetype. The entry carries `{required_guards, known_guards, sample_size}` and NO per-archetype frequency field, so cite the floor and the sample count, not an invented measured percentage. Check the archetype's `known_guards` list first; a guard the changed controller uses that is listed there is a legitimate variant and not a miss. The honesty label is unchanged: it is still presence-only and still "cannot confirm the new action is covered". The `required_guards` data names the expected guard; it does not map that guard to the action it covers, so it never reaches BLOCK and never claims a structured callback-to-action divergence.

Skip this check entirely for TypeScript and any non-Ruby file. There is no route/middleware/controller extraction for those languages, so there is no presence signal to compare and nothing honest to say.

#### 2.6c. Tainted input, SSRF, path traversal (advisory FIX, single-hunk scope)

Read each file's added (`+`) lines from the hunk map (Step 1a). Within a single file's hunk, look for these flows where request-controlled input reaches a dangerous sink:

- **Taint to sink**: a value read from request data (params, query string, request body, headers, an inbound argument) flows on an added line into `eval`/`constantize`/`send`/`system`/backticks/`%x`/a raw SQL string (Ruby), or `eval`/`Function`/a shell exec/a raw query (TS), with no sanitization between source and sink inside the hunk.
- **SSRF**: an added outbound HTTP call (`Net::HTTP`, `Faraday`, `HTTParty`, `open-uri`, `fetch`, `axios`, `http.get`) whose URL is built from request data rather than a constant or an allow-listed host.
- **Path traversal**: an added filesystem read/write (`File.read`/`File.open`/`Dir`/`fs.readFile`/`fs.createReadStream`/`require`) whose path is built from request data without a basename/allow-list check inside the hunk.

These are judgment calls, not witnessed facts. Cap every one at **FIX** (never BLOCK) and label each: "advisory, single-hunk scope; may miss a flow whose source and sink are in different files, and may be a false positive if the value was sanitized outside this hunk."

The cited tainted line MUST be inside the diff. If the source or the sink is not on an added/changed line in the hunk map, do not raise the finding: a flow you cannot point at inside the change is exactly the cross-file case this single-hunk pass cannot see, and reporting it would be a guess. These findings go through the Step 4 hunk gate like every other per-line finding.

Never let any 2.6b or 2.6c finding reach BLOCK, and do not claim they honor the integrity/calibration guarantee the same way a lint violation or a removed-guard hunk finding does. They are judgments; the secret finding (2.6a) and the deterministic lint sinks (2.6d below) are the witnessed facts in this pass.

#### 2.6d. Deterministic lint-sink and quality findings (witnessed facts)

Step 2b's `lint_file` already returns more than secrets and style. Beyond `secret-detected-in-content` (Step 2.6a), the `violations` list carries deterministic security-sink and test-quality rules the convention loop never routes — so route them here. Each violation is `{rule, severity, message, expected, actual}`; there is NO integer line field. The line, WHEN PRESENT, is the ` at line N` token inside `actual` (parse it with the same `at line N` rule the secret hunk gate uses in Step 2.6a). A violation also carries `ignored: true` when an inline `chameleon-ignore` directive already covers it — skip those.

These are WITNESSED facts (the engine's own deterministic rules), so they are refuter-EXEMPT (Step 4b): verify each inline by re-confirming the returned violation, never send it to `refute_finding`. Where this pass and the hand-rolled taint pass (Step 2.6c) fire on the SAME line, the deterministic hit here WINS — drop the 2.6c judgment for that line.

Two groups, with different scope and severity:

**Security sinks — pre-trust, every linted file, line-anchored → hunk-gated.** These fire before the trust gate and before the archetype match, so they are present for every file you linted (trusted or not), and each carries ` at line N` in `actual`. Run each through the Step 4a hunk gate; an out-of-hunk hit goes to the "Pre-existing repo hygiene" note like an out-of-hunk secret, never the verdict.
- `eval-call` (only the `severity: error` forms) → **BLOCK**. A code-execution sink introduced on an added line is the same tier as a secret: a witnessed structural fact in the diff. Cite the file, the parsed line, the rule, and carry the violation `message`. The error-severity `eval-call` forms are TS/Python `eval(`, Python `exec(`, and the Ruby paren-less `eval`/`send(:eval)`. RESPECT the returned `severity`: the engine DELIBERATELY emits `eval-call` at `severity: warning` for the Ruby string-argument `class_eval`/`instance_eval`/`module_eval` metaprogramming forms (an established Rails idiom it refuses to hard-block), so route a `warning`-severity `eval-call` at **FIX**, never BLOCK — do not escalate by rule name alone. `command-injection` is NOT block-eligible: the engine emits it at `severity: warning` only and keeps it out of `BLOCK_ELIGIBLE_RULES` (it is a `#{…}`-in-a-shell-string heuristic, not taint analysis, so a constant interpolation like `system "echo #{VERSION}"` would false-BLOCK), so it caps at **FIX** below — the same tier the receiving skill gives it. Route by the returned `severity`, not the rule name: `eval-call` at `error` is the ONLY deterministic sink that blocks.
- `command-injection`, `sql-string-interpolation` (Ruby only), `insecure-deserialization`, `weak-hash`, `insecure-random`, and any `warning`-severity `eval-call` → **FIX**. A witnessed dangerous pattern on an added line; cite the rule and parsed line.

**Test-quality / discipline — trusted path, whole-file, no line → NIT.** These run only when the profile is trusted (the test-discipline rules additionally require a `test`/`spec` archetype; `then-without-catch` fires for any TypeScript file). They carry NO line — they are whole-file advisories — so they are NOT hunk-gated; report each at **NIT** anchored to the file (they only ever run on a changed file you are already reviewing).
- `then-without-catch` (a `.then` with no `.catch`, i.e. an unhandled promise rejection), `unfrozen-clock`, `unstubbed-network`, `skipped-test`, `tautological-assertion`, `assertion-free-test`, `real-sleep-in-test`, `random-in-test` → **NIT**, citing the rule.

A clean file emits none of these; only route what `lint_file` actually returned, never a sink you reasoned about yourself (that is Step 2.6c's job, capped at FIX). Render them in the Security / quality findings output section.

### Step 2.7: Migration-safety pass (Rails `db/migrate/*.rb` only)

Run this on every changed file whose path is under `db/migrate/` and which is a Ruby file (`.rb`). Skip every other file. This pass is a pure parse of the migration's text and the diff: it makes NO network calls, runs nothing, and reads no profile data. The convention review (Step 2) cannot help here because a dangerous migration matches its safe siblings on top-level shape; this pass reads the migration DSL inside the change directly.

The DSL calls live inside a `change`, `up`, or `down` method at deeper indentation than the top-level archetype shape the profile matches on. Read the call name and its keyword arguments (`null:`, `default:`, `algorithm:`) across the whole call, including a call that wraps onto a second line. The three checks below are independent; run every one that applies.

This pass has exactly one BLOCK-eligible check and two advisory reminders. Keep the tiers separate. The reminders are NOT findings about this migration being wrong: the dangerous condition (a populated or large table) is a runtime fact this static read cannot see, and the repo's own clean migrations share the same shapes. They are "go verify the table size" prompts for the author, capped at FIX, never BLOCK.

#### 2.7a. Irreversible `change` block (BLOCK)

A `def change` method lets Rails auto-generate the rollback. That only works when every operation in the block is reversible. An irreversible operation inside `change` with no `up`/`down` pair gives a migration that cannot be rolled back: `rails db:rollback` raises `ActiveRecord::IrreversibleMigration` at the worst possible time.

Raise a **BLOCK** when a `change` method contains an operation Rails cannot auto-reverse and the migration does NOT instead define a `def up` / `def down` pair (which makes the rollback explicit and is the correct fix). The irreversible operations are: a bare `remove_column` without the column type and options Rails needs to recreate it, `change_column` (a column TYPE change — always irreversible, Rails cannot know the prior type), `execute` with raw SQL, `remove_index` without the full index definition, `drop_table` without a block describing the table, and `change_column_default` given only the new value with no `from:`/`to:` pair. Note `change_column_null` is NOT in this list: Rails inverts it (it flips the null flag back), so it is auto-reversible and belongs only to the 2.7b table-size check, not here — do not BLOCK on it. A `change` that calls only auto-reversible operations (`create_table`, `add_column`, `add_index`, `add_reference`, `change_column_null`) is correct; do not flag it. A `reversible do |dir| ... end` block clears the BLOCK ONLY when it defines BOTH directions for the irreversible op (`dir.up` AND `dir.down`); a one-directional `reversible` block (`dir.up { execute … }` with no matching `dir.down`) still cannot roll back and does NOT clear the BLOCK — treat the wrapped irreversible op as unhandled.

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

This pass uses chameleon's curated co-change pairs, not a learned statistic. The pairs are a small directional table in the engine (`cochange.py`): each rule has a `trigger` (the new file that demands a companion), a `companion` (the file that satisfies it), and a `rule_id`. The shipped rules are `cochange-model-migration` (a Rails model needs a migration), `cochange-controller-route` (a Rails controller needs a route), `cochange-prisma-migration` (a Prisma schema change needs a migration), `cochange-slice-store` (a Redux slice needs to be registered in the store), `cochange-django-model-migration` (a new Django `models.py` needs a `migrations/*.py`), and `cochange-nestjs-controller-module` (a new NestJS `*.controller.ts` needs a `*.module.ts` that wires it up). Co-presence is never derived from this repo; only these curated rules apply, and each is silenced for a repo whose own committed files break the pairing too often to trust it.

Restrict this to files the diff ADDS (status `A` in the diff, a brand-new path). A modified existing file does NOT trigger: editing a method on an existing model must not demand a fresh migration. For each added file:
- Match its repo-relative path against each rule's trigger (a Rails model is `app/models/*.rb` excluding `concerns/` and `application_record.rb`; a controller is `app/controllers/*_controller.rb` excluding `concerns/` and `application_controller.rb`; a Prisma schema is `*.prisma`; a slice is any `.ts`/`.tsx` file whose lowercased basename CONTAINS `slice` — a substring match, so `userSlice.ts` and also `sliceHelpers.ts` trigger, not only a `*slice.ts` suffix; a Django model is `models.py`, or a file under a `models/` package that the role classifier tags as a model — a co-located `managers.py`/`querysets.py`/`signals.py` under `models/` does NOT trigger; a NestJS controller is a `*.controller.ts`, and that rule additionally fires ONLY when the repo's `package.json` declares `@nestjs/core`/`@nestjs/common`, a framework-manifest gate, because `*.controller.ts` is not unique to NestJS).
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
The tool returns `{found, file, matches}` — the similar-function pairs are under `data.matches`, NOT a top-level `candidates` key (reading `data.candidates` finds nothing). Each entry in `matches` is `{function, candidates}`: `function` is the NEW function the diff defined (`{name, kind, arity, required}`) and `candidates` is the list of existing catalog functions prefiltered as semantically similar by signature shape and name-token overlap. Each candidate is `{name, file, kind, arity, required, shared_tokens, body_excerpt, body_match}` — the identifier is `name` and the location is `file` (there is NO `symbol`/`path` field), and the snippet is `body_excerpt` (not `excerpt`). The tool only PREFILTERS; it does not decide duplication. You are the semantic-equivalence judge: for each `matches` entry, read the new `function` body against each of its `candidates`' `name`, shape (`kind`/`arity`/`required`), and `body_excerpt`, and decide whether the new function re-implements the intent of one of them.

Raise a **FIX** (or NIT for a weak match) only when the new function duplicates the intent of a candidate the tool returned, citing that candidate's `name` and `file`. Never claim duplication without a candidate: if the tool returns no candidates for a function, there is no duplication finding for it, full stop. A re-implemented helper that the catalog did not surface is invisible to this pass, and inventing a "this probably already exists somewhere" finding is exactly the ungrounded claim the integrity rule forbids. Advisory only, never BLOCK.

#### 2.9c. Cross-file existence breaks (FIX)

Call the `get_crossfile_context` MCP tool once for the whole review:
```
get_crossfile_context(repo=<repo_id>)
```
It returns `{found, findings, low_confidence_dropped}` (with `status`/`reason` on the degraded/untrusted paths below; a trusted success sets NO `status`, so its absence reads as OK) and reports ONLY existence-break findings: an export that the indexed importer set still references by name is now gone from the module that used to export it, so the importer's call site is broken. Each finding is `{symbol, module, count, high_confidence, sites}` — the removed export is `symbol`, the module that no longer exports it is `module`, and the importer file:line list is `finding.sites`, each entry `{path, line}` (there is NO flat importer `file`/`line` on the finding). Each finding carries a `high_confidence` flag.

**Degraded handling (mirror Step 2.9e's contract-break rule).** When the envelope carries `status: "degraded"` (or `found: false` with a `reason` such as `index-unavailable` — the reverse/constant index is corrupt, missing, or an unsupported layout), the scan could NOT run: do NOT read the empty `findings` as a verified "no existence breaks". Skip the pass, note it in one line ("cross-file existence-break pass skipped: `get_crossfile_context` degraded (`<reason>`)"), and rely on the change-delta and per-file review — exactly as you would on an unresolvable `get_contract_breaks`. A `status: "untrusted"` envelope means the profile isn't trusted; suggest `/chameleon-trust` and skip the pass.

The tool scans the WHOLE repo, not the diff, so a returned break is not in this change by construction. Two gates decide relay, and both are mandatory: (1) `high_confidence` is true; (2) diff scope — the finding's `module` (the file that no longer exports the symbol) is in this diff's changed-file set, or the symbol's removal/rename is visible in a changed hunk's `-` lines. Relay a finding as a **FIX** ONLY when both gates pass, citing the removed/renamed symbol, the module that no longer exports it, and the importer file:line the tool reported. A high-confidence break whose module this diff did NOT touch is PRE-EXISTING (the repo was already broken before this change): report it in the "Pre-existing repo hygiene" note exactly like an out-of-hunk secret (Step 2.6a), never in the verdict — on a repo carrying an old break, every unrelated PR would otherwise be verdict-poisoned into NEEDS CHANGES for code it never touched. Drop every finding without `high_confidence=true`: a leaky resolver can produce a finding that cites a real-looking entry but resolved wrong (a barrel re-export, a same-name collision, a dynamic import), and relaying it would launder a wrong inference past the integrity rule. The tool is the witnessed fact here; do not add your own cross-file existence claims on top of what it returns.

#### 2.9d. Caller blast radius for MODIFIED functions (context, not a finding)

For each function the diff modifies, call the `get_callers` MCP tool:
```
get_callers(repo=<repo_id>, file_path=<abs_path_of_changed_file>, function_name=<function>)
```
List the returned caller sites with their grades as blast-radius context for the finding pass: a signature, contract, or behavior change to a function with recorded callers is judged against those call sites, not in isolation. Each site is `{path, caller, line, grade}` (the calling symbol is `caller`, not `name`; a barrel-chased site also carries a `via` list). Grades are deterministic (`same_file` / `import` / `constant_receiver`, plus `typed_property` for TypeScript dependency-injection edges and `module_attribute` for Python module-attribute calls), read from the committed calls snapshot at profile derivation, so each cited site is a real recorded call, not an inference — treat the grade set as open (a new deterministic edge kind reads as deterministic, never as a non-deterministic name-token guess).

Absence of callers is NOT evidence of dead code: dynamic and unsupported call paths (reflection, metaprogramming, superclass chains) are invisible to the index, as is anything added after the last refresh. Never raise an "unused function" finding from an empty result. Name-token candidates from `get_duplication_candidates` may be listed separately alongside this context, but must be labeled non-deterministic; they never carry the deterministic grades above.

#### 2.9e. Caller-contract signature breaks (FIX)

Call the `get_contract_breaks` MCP tool once for the whole diff:
```
get_contract_breaks(repo=<repo_id>, base_ref=<the PR base branch, or the branch's merge base; the locked production_ref when no PR base is known; default "main">)
```
It compares each changed TS/Ruby/Python callable's POSITIONAL parameter contract at the merge-base of `base_ref` and HEAD vs HEAD (three-dot semantics, matching the rest of the diff so a divergent base does not read as this branch's change) and returns `findings` only for a callable that NARROWED (a new required positional argument, or an optional positional flipped required) AND has committed callers. Each is a **FIX**: the narrowed callable now mis-matches `caller_total` recorded call sites. Each finding is `{file, name, old_required_positional, new_required_positional, caller_total, callers}` — cite the symbol (`name`), the `old_required_positional`->`new_required_positional` count, and the affected `callers` file:line list the tool returned (each caller `{path, line}`, plus a `via` barrel-chain list when present; a deterministic fact, same bar as 2.9c). This is the deterministic complement to the LLM contract check: a required-positional narrowing in a low-importer file that the size/blast gates miss. The tool flags ONLY positional narrowing — never a removed/reordered param, a new optional/keyword arg, or a return-type change; for those, fall back to the logic review. If `get_contract_breaks` returns `status: degraded` — an unresolvable `base_ref`, a missing/corrupt calls index, or `reason: diff_too_large` (the deterministic pass is capped at `AUTOPASS_MAX_FILES` = 10 changed files, so a larger diff cannot run it) — or is otherwise unavailable, do NOT read the empty `findings` as a verified clean: skip the deterministic pass, note it in one line, and rely on the LLM contract/logic review (Step 3c) to cover narrowings on that diff.

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
- **Authorization**: does the endpoint check permissions? Does it match the ticket's permission requirements? For a Ruby controller, look up the file's archetype in the `required_guards` section of `.chameleon/conventions.json` (`conventions.required_guards[<archetype>]`). When it carries `required_guards` (the authorization guards present in at least 60% of that archetype's controllers, e.g. `["authorize!"]`) and the changed controller declares none of them, raise a **FIX** naming the specific expected guard (`before_action :authorize!`) and the archetype it was derived from. Keep the same honesty label as Step 2.6b: "cannot confirm the new action is covered; authorization may be inherited from a base controller." Check the archetype's `known_guards` first, a guard listed there is a legitimate variant, not a miss. When the archetype has no `required_guards` entry, fall back to the presence-only check (Step 2.6b). Never reach BLOCK; this is advisory.
- **Error handling**: look up the file's archetype in the `error_handling` section of `.chameleon/conventions.json` (`conventions.error_handling[<archetype>]`). When present, it carries the archetype's dominant shape: `try_catch` or `rescues` frequency, `sample_size`, and an optional `error_shape` naming the project error target (e.g. `render json: { error`, `render_error`, an `*Error`/`*Serializer` call). Cite that entry: "this archetype handles errors via `<error_shape>` in `<frequency>` of its files; this change does not." Raise a **FIX** when the changed code adds an error path that does not match the recorded shape. When the archetype has no `error_handling` entry, fall back to comparing against the canonical witness for the pattern (Step 2c).
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

The findings sections answer "is anything wrong with this change?" This step answers the different question they cannot: "is this change *routine* enough that, with a clean review, a human can skip it?" Call the `get_autopass_verdict` MCP tool once for the whole diff:

```
get_autopass_verdict(repo=<repo_id>, base_ref=<the PR base branch, or the branch's merge base; use the locked production_ref from .chameleon/config.json when no PR base is known; default "main">)
```

It returns `{auto_pass_eligible, risk, complexity_tier, reasons, facts, changed_files}`. Report it verbatim as an advisory line (the Auto-pass routing section in Step 4). It is ADVISORY only: it never produces a BLOCK, FIX, or NIT and never changes the verdict. Its job is to mark the safe-to-skip slice, grade the change's inherent complexity (`complexity_tier`: easy / medium / hard / complex — structural, independent of cleanliness), and name why a change is NOT in the skip slice (a security-sensitive surface, too large, high cross-file blast radius, a file outside the profiled archetypes, or a grounded block finding).

The verdict also carries `typecheck` (three-state) and deterministic test-integrity/content facts inside `facts` (`deleted_test_files`, `net_test_line_delta`, added skip markers, assertion delta, removed guard lines, chameleon-ignore directives added, `blast_radius_unknown`, `diff_scan_truncated`) — all engine-computed. Relay them verbatim; never recompute them by eyeballing the diff. This does NOT loosen the Step 3g integrity rule against hand-counting assertions: the assertion delta is now engine-grounded and arrives in the tool result, and the skill still never counts by hand. `typecheck` is a DICT, not a scalar — the state is `typecheck.status`, one of `"unavailable"` / `"clean"` / `"errors"` (never compare `typecheck` itself to a string). The three-state rule: `typecheck.status == "unavailable"` (the default — the runner is opt-in via `CHAMELEON_ALLOW_TSC`; the human-readable why is `typecheck.reason`) is reported as one fact line and is NOT a needs-human reason; `typecheck.status == "errors"` (the error files are `typecheck.files`, the count is `typecheck.diagnostics`) also appears in `reasons` and routes needs-human. (`tests` mirrors this: `tests.status` is `"unavailable"` / `"clean"` / `"failures"`.)

The superpowers reviewer asks "are all tests passing?" — that is OUT OF SCOPE for this static review: it runs nothing and makes no network call. The deterministic test-integrity facts above (and the opt-in typecheck/test states) are the proxy; relay them as the heads-up and say plainly that the suite's actual pass/fail was not executed here.

Read it together with the findings verdict, never instead of it: a change is a credible "no human review needed" candidate only when the findings verdict is APPROVE AND `auto_pass_eligible` is true. A clean findings verdict on a change the router sent to a human (an auth/payment/migration surface, say) is NOT a skip candidate — state that plainly. If the tool is unavailable this session, skip this step and note it in one line, the same as the cross-file passes.

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

### Step 4: Output

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

`refute_finding(repo=<repo_id>, findings=[{id, kind, severity, file, line, claim, evidence}, ...], base_ref=<base>)`

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
- **Cross-file findings cite their tool or artifact, not intuition.** The existence-break FIX (Step 2.9c) relays only `get_crossfile_context` findings with `high_confidence=true` AND whose module is in this diff's changed-file set, citing the symbol, the module, and the importer file:line the tool returned; a finding without the flag is dropped, and a high-confidence break on a module this diff never touched is pre-existing — hygiene note, never the verdict. The duplication finding (Step 2.9b) is allowed only when a `get_duplication_candidates` `matches` entry carried a `candidates` member you judged equivalent, citing that candidate's `name` and `file`; never claim duplication with no returned candidate. The layering finding (Step 2.9a) cites a `conventions.layering` `forbidden_upward_edges` entry. The co-change FIX (Step 2.8) cites the curated `rule_id` and the added trigger file. The caller-contract signature break (Step 2.9e) relays only `get_contract_breaks` findings that NARROWED a positional contract AND have committed callers, citing the symbol, the `old`->`new` required-positional count, and the returned caller `file:line` list. None of these is bare model intuition; each points at a tool result or an artifact entry, same bar as a lint violation.
- **Security findings carry their own honesty bar.** A secret BLOCK (Step 2.6a) cites the `secret-detected-in-content` violation the scanner returned — a witnessed fact, like a lint violation — but only after both 2.6a gates passed: `secret_hard` is true AND the line sits inside an added/changed hunk. The deterministic lint sinks (Step 2.6d: error-severity `eval-call` → BLOCK; `command-injection`/`sql-string-interpolation`/`insecure-deserialization`/`weak-hash`/`insecure-random` + the `warning`-severity `eval-call` Rails idiom → FIX) are witnessed facts too: cite the returned violation and its parsed ` at line N`, respect its `severity`, run it through the hunk gate, and never send it to the refuter. The 2.6d test-quality rules are whole-file NITs. A new-dependency ACK (Step 2.5a) is a human provenance gate, not a finding — never render it as BLOCK/FIX/NIT and never let it change the verdict or the ledger record. A low-precision heuristic hit or an out-of-hunk hit presented as a BLOCK is a false claim, the exact kind that destroys trust in a green gate. The authz FIX (2.6b) and the taint/SSRF/traversal FIX (2.6c) are judgments, not witnessed facts: they must carry their advisory labels, must never claim a structured profile cite (no profile data maps callbacks to actions), and the taint line must be inside the diff. Do not present a judgment as if it had the same backing as a gated secret hit or a lint violation.
- **Migration findings carry their own honesty bar.** The irreversible-`change` BLOCK (Step 2.7a) cites the irreversible operation in the diff — a witnessed structural fact. The null:false and add_index FIXes (2.7b/2.7c) are table-size reminders, not confirmed defects: the dangerous condition is a row count this static read cannot see, and the repo's own safe migrations share the same shapes. They must keep their "verify table size" label and never reach BLOCK. Do not present either reminder as if it were a confirmed migration bug.
- **The coverage-delta view is advisory and grounded only in archetypes.** The Step 3g partition rests on the file's archetype name (source vs test) and the test-vs-source archetype path mirror in `archetypes.json`. It must not claim a specific missing test file ("`foo.rb` needs `foo_spec.rb`") — chameleon has no source-to-test path map, and the diff lists only changed files, so a pre-existing untouched test is invisible. Keep it as a heads-up listing changed source in a test-paired layer, never a FIX or BLOCK, and never count an assertions delta: there is no assertion counter in chameleon, so an eyeball count of diff hunks would be exactly the ungrounded finding the integrity rule forbids.
- **3-round grounding loop.** After producing the review, re-read each BLOCK, FIX, and NIT finding. For each one, verify: (1) does the canonical witness, conventions data (`error_handling`/`required_guards`/`callable_signatures`/`test_pairing`/`layering`), the removed (`-`) lines of the hunk, a parsed manifest/lockfile line, a returned secret violation, a returned deterministic lint-sink violation (Step 2.6d), or a returned tool result (`get_duplication_candidates` candidate, `get_crossfile_context` finding with `high_confidence=true`, `get_contract_breaks` finding with returned callers) actually support this claim? (2) for per-line logic findings, the stale-comment NIT (3f-ii), the taint/SSRF/traversal findings (2.6c), AND the line-anchored 2.6d security sinks, is the anchor line inside an added/changed hunk range (Step 4a)? Drop any finding that fails either check. The hunk gate is the deterministic answer to "PR-introduced vs pre-existing"; do not override it by judgment. The whole-diff cross-file findings (Step 2.8/2.9) are not hunk-gated; they are gated on their tool/artifact backing instead. Round 3 is the independent engine refutation pass for surviving model-judgment findings — see Step 4b.

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

## Honesty Rules

- Never invent a violation. Every BLOCK/FIX/NIT cites a real `file:line` inside an added/changed hunk plus the artifact that backs it: a returned lint/secret violation, a `conventions.json` entry, a returned tool result, or a parsed manifest/lockfile line.
- Distinguish a witnessed fact (a returned secret/lint violation, an irreversible migration op in the diff) from a judgment (authz, taint, error-shape). Label judgments advisory, never present one as a witnessed fact, and never let a judgment reach BLOCK.
- The hunk gate answers "PR-introduced vs pre-existing": if the anchor line is not in an added/changed hunk, drop the finding. Don't override the gate by judgment.
- Run the grounding loop: re-read each finding and drop any the witness / conventions / tool backing or the hunk gate does not support. A finding that cannot survive the round-3 refuter does not ship.
- Never read an empty `get_callers`/cross-file result as dead code, and never relay a duplication or existence break without its returned candidate / `high_confidence` backing.
- State what you verified clean, too. Don't pad the review with hypothetical concerns to look thorough. This is a REPORT-phase rule, not a generation-phase one: during ATTACK and RECALL, write every candidate down (including borderline ones) and let the hunk gate and the refuter kill the weak ones — a candidate never written down never reaches the gates that exist to adjudicate it. Filter at the end, not at the moment of noticing.
