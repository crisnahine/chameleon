# /goal — Chameleon: full correctness, proven by real usage

> **How to use this goal:** Complete Task 0 and Task 1 first (they make every later
> claim verifiable). Then work the Sequencing plan top to bottom. A subsystem is only
> "done" when a human has signed it off via the Verification Protocol on every required
> matrix cell. Nothing else counts as evidence of correctness.

---

## Objective

Make all 15 Chameleon subsystems behave correctly across the **entire defined**
language/framework support matrix, proven by a human operating real repositories
through real Claude Code sessions. Proactively find and fix any logic gap, workflow
gap, missing step, edge case, inconsistency, bug, or performance regression before
declaring done.

---

## Definitions (these remove the ambiguity that makes "100%" unprovable)

- **Supported languages / frameworks** — the *enumerated* set produced in Task 0. "All"
  means "every cell of that matrix," not an open-ended universe. If it isn't in the
  matrix, it isn't in scope; if it should be in scope, add it to the matrix.
- **Works 100% correctly** — meets the subsystem's acceptance criterion (below) under
  real usage, with zero unhandled failures, on every *required* cell of the matrix.
- **Real-world testing** — a human running an actual repo through actual Claude Code
  sessions and observing actual behavior (transcript, files on disk, logs, terminal,
  timing). **No automated test result is accepted as evidence that a subsystem is done.**
- **Done** — see the Definition of Done section. It is the only finish line.

---

## Verification philosophy (and the one constraint to apply knowingly)

Acceptance evidence comes **only** from real usage. Automated checks (linters, type
checks, smoke scripts, fuzzers) are *permitted as developer scaffolding* to find bugs
faster, but they **earn zero "done" credit** — a passing suite never closes a matrix
cell; a human sign-off does.

The cost of this rule is real: pure human verification grows with the matrix, while
bugs hide in the cells you didn't hand-check. This goal keeps it tractable by (a)
bounding "all" via Task 0, and (b) tiering cells in Task 1 so the effort is finite. If
you later decide that's still too much hand-work, the *only* safe relaxation is to let
automation gate **Tier-2** cells while Tier-1 stays human-verified — change that
deliberately here, don't let it creep in.

**CI note:** because automated tests don't gate "done," your CI (item 14) is a
*build/package/version-sync/lint* pipeline, not a correctness gate. If you keep tests in
CI, treat them as scaffolding, not as the acceptance signal.

---

## Scope

**In scope:** the 15 subsystems below, on the Task 0 matrix.
**Out of scope:** new features, new language support not already shipped, and any
redesign that isn't required to fix a discovered gap. (If a fix *requires* a redesign,
log it as a gap and call it out rather than expanding scope silently.)

---

## Task 0 — Enumerate the support matrix (do this first)

Produce the finite matrix that everything else is measured against. Derive it from the
codebase, not memory.

- **Languages (the complete supported set — exactly three; there is no open-ended
  universe):**
  - **TypeScript/JavaScript** — `.ts .tsx .js .jsx .mjs .cjs`, AST via the TypeScript
    Compiler API (`plugin/scripts/ts_dump.mjs`).
  - **Ruby** — `.rb`, AST via Prism (`plugin/scripts/prism_dump.rb`).
  - **Python** — `.py .pyi`, CST via libcst (`plugin/scripts/libcst_dump.py`). Nuance: `.pyi`
    is linted/detected (`_PY_EXTENSIONS`, `lint_engine.py`) but bootstrap discovery
    globs `**/*.py` only (`_glob_for_extractor`, `bootstrap/orchestrator.py`), so
    `.pyi` files are never clustered into the profile — don't assume a `.pyi`-heavy
    cell is profiled.

  No other languages are supported. Go, Rust, Java, and C# have no extractor, dumper, or
  detection signal — `detect_language()` returns only these three
  (`plugin/mcp/chameleon_mcp/lint_engine.py`) and `EXTRACTORS = [TypeScript, Ruby, Python]`
  (`plugin/mcp/chameleon_mcp/extractors/registry.py`) — and MUST NOT appear in the matrix.
- **Frameworks / repo shapes:** Chameleon is framework-**agnostic** by default — it learns
  each repo's own conventions, so any framework in a supported language works. The named
  frameworks below add a deeper, framework-aware layer on top:
  - **Ruby** → Rails (else agnostic).
  - **Python** → Django, DRF (recognized via the Django-family classification plus a
    dedicated DRF/Django authz-guard layer — not a separate framework tag), Flask, FastAPI
    (else agnostic, incl. plain scripts).
  - **TypeScript/JavaScript** → Next.js, NestJS (else agnostic, incl. Node CLI / plain).

  Repo **shapes** (orthogonal to framework, all handled agnostically): single-package,
  monorepo / workspace (`packages` / `apps` / `libs` / `workspaces`), and hybrid
  frontend+backend. Source the list from `docs/language-support-matrix.md`, not memory.
- **Platform (explicitly scoped, not a matrix axis):** the matrix is language ×
  framework; platform is a separate, deliberately-scoped dimension. The code ships
  native Windows support (`plugin/hooks/run-hook.cmd` polyglot dispatch, the `msvcrt` lock
  fallback in `plugin/mcp/chameleon_mcp/locks.py`, `windows-latest` CI jobs in
  `.github/workflows/ci.yml`; the daemon stays POSIX-only). Windows is verified via
  the CI matrix plus manual spot-checks — per this repo's own testing conventions —
  not by multiplying every matrix cell by platform; the human protocol below runs on
  the primary (POSIX) platform.
- **Tiering:**
  - **Tier 1** = cells that must be *fully* human-verified for every relevant subsystem.
  - **Tier 2** = cells that get a human spot-check (or, if you relaxed the philosophy,
    automated scaffolding) on the subsystems most likely to vary by language.
- **Output:** a checked-in `docs/verification-matrix.md` table (rows = languages, cols =
  frameworks, each cell tagged Tier 1/2). This file *is* the source of truth for "all." It
  is a verification **sign-off tracker** (subsystem × tiered cell), distinct from the
  existing `docs/language-support-matrix.md` (a per-dimension capability-parity reference),
  which is an **input** to this task.

## Task 1 — Define the hot-path budget (do this second)

Item 15 says the hot path is a **budget, not a tool**, so the budget must be a number
before it can be enforced. The hot path is `get_pattern_context` (the PreToolUse
`preflight-and-advise` advise path). Real ceilings already exist — cite them rather than
inventing percentiles:

- **Hook timeout:** the five fast hooks (PreToolUse / PostToolUse / SessionStart /
  UserPromptSubmit) wrap the Python helper in a 3-second hard shell `timeout` and fail
  open; the Stop/SubagentStop backstop uses 55s (it wraps the ~45s turn-end correctness
  judge). (`plugin/hooks/*`, `docs/architecture.md`.)
- **Statusline render budget:** sub-100ms (`plugin/bin/chameleon-statusline.sh`).
- **Measurement method:** `tests/bench_hot_path.py` reports cold/warm p50/p99 for
  `get_pattern_context` and its sub-steps (repo-detect / profile-load / archetype-resolve)
  — it measures, but enforces no ceiling.
- the rule that the hot path is *never* invoked as a discretionary tool/step (see the
  item-15 caveat: `get_pattern_context` is *also* a registered MCP tool, so verify the
  enforced **hook** invocation specifically).

A percentile-latency SLO (p50/p95 in ms) on top of the existing timeout caps is not
codified today; if one is wanted it must be newly chosen, not cited from code.

Output: a `docs/hot-path-budget.md` with the ceilings and the measurement method.

---

## Golden repos (the test fixtures, but real)

Create one real, runnable repo per Tier-1 cell — actual source, actual git history,
actual framework wiring. Reuse and extend the assets that already exist rather than
building from scratch: the committed `tests/journey/` seed fixtures and the nine
bootstrapped repos under `~/Documents/Projects/Testing Apps/` (the preferred free test
bed; `qa_*.py` runs against `CHAMELEON_TEST_{TS,RUBY,PYTHON}_REPO`). Map one to each
Tier-1 (language × framework) cell. These are the repos the human drives during
verification. Additionally keep at least one repo that is *deliberately messy but valid*
(odd-but-legal syntax, large file count, pre-existing data-dir state, an in-progress
merge) — genuinely new — to surface edge cases that clean fixtures never will.

---

## Verification protocol (repeatable, human-driven)

For each (subsystem × required cell):

1. **Set up** — open the golden repo for that cell in a real Claude Code session.
2. **Drive** — perform the *real* interactions in the subsystem's checklist below
   (trigger the hook by doing the action; make the request that should fire the skill;
   cause the real merge conflict; etc.).
3. **Observe** — check the subsystem's "how a human sees it" signal: transcript,
   files on disk, logs, terminal state, and timing against the budget.
4. **Negative check** — confirm the *off / shouldn't-fire / failure* path too (kill
   switch off-state, skills that must NOT trigger, graceful degradation on error).
5. **Sign off** — record pass/fail + notes against that cell in `docs/verification-matrix.md`.
   A fail opens a gap (see Gap-handling loop); it does not get waved through.

---

## The 15 subsystems — acceptance criteria

Each entry: *what correct means in real usage* → *how a human observes it* → *cross-cell note*.

1. **Hooks** — Every configured hook fires on its real trigger event, runs to completion
   within budget, and produces its intended effect; on failure it degrades gracefully
   and never blocks or corrupts the session. *Observe:* do the real action that fires
   each hook event in each repo; confirm the effect and a clean transcript/log. *Note:*
   re-verify per language since triggers may key off file types/tooling.

2. **Skills** — Each skill is discovered, triggers on the intended real request, executes
   correctly, and does **not** mis-fire on requests it shouldn't claim. *Observe:* issue
   both should-trigger and should-NOT-trigger requests in each repo; confirm correct
   firing and useful output. *Note:* watch for description over-/under-triggering that
   only shows up in a specific language context.

3. **MCP tools** — Each tool is listed, callable, returns correct results with a valid
   schema, and handles errors, when Claude invokes it naturally during a real task.
   *Observe:* give tasks that lead Claude to call each tool; inspect the returned data
   and the on-disk/side effects. *Note:* verify tools that read source behave per
   language.

4. **Statusline** — Renders correct, current information in a live session, updates as
   state changes, and never garbles the terminal. *Observe:* watch the statusline across
   state transitions in each repo type. *Note:* confirm it reflects per-repo/per-user
   state correctly.

5. **Daemon** — Starts cleanly, stays healthy across a working session, restarts after a
   crash and after a reboot, releases resources on stop, and handles concurrent
   repos/users without corruption. *Observe:* run a real session; kill the daemon and
   confirm recovery; open two repos at once. *Note:* concurrency is the likely failure
   mode — exercise it.

6. **Merge driver** — On a real merge conflict in generated/profile files, produces a
   correct, non-corrupt result and falls back safely. Registration is **manual**, not part
   of install: the user copies `.gitattributes-template` into the repo's `.gitattributes`
   and runs `git config merge.chameleon.name` + `git config merge.chameleon.driver
   "<plugin>/scripts/chameleon-merge-driver.sh %O %A %B %P"`. *Observe:* register it the
   documented way, create a genuine conflict in a golden repo, run the merge, and inspect
   the merged file and git state. *Note:* install does NOT auto-register the driver
   (`plugin/scripts/setup.sh` / `docs/install.md` don't touch `.gitattributes`); confirm the documented
   manual registration works end to end, or log "install doesn't auto-register" as a gap.

7. **Migrations** — There is no migration framework to exercise
   (`plugin/mcp/chameleon_mcp/profile/migrations/` is a docstring-only stub); what must be
   verified are the *actual* upgrade mechanisms: refresh forces a full re-derive when
   the engine or profile schema changed (`_engine_version_changed` /
   `_profile_needs_rederive`, `plugin/mcp/chameleon_mcp/tools.py`); the loader loads older
   profiles (schema_version <= 7 under the current v8) but refuses anything newer than
   `MAX_SUPPORTED_SCHEMA_VERSION` (8) with a clean "upgrade chameleon-mcp" error
   (`profile/loader.py`); drift.db is a drop-and-recreate cache by policy
   for its cache table, plus one in-place durable-table migration (the
   `decision_log` content-digest column add, `_migrate_decision_log` in
   `drift/schema.py`); and index.db carries the other in-place migration (the
   one-time `repos` composite-PK conversion, `index_db.py`). Correct means each path
   runs cleanly, is idempotent, and loses no durable data (taught state like
   `idioms.md` survives a forced re-derive). *Observe:* take a repo with an
   older-engine profile, refresh, and confirm a real re-derive (not a noop-preserve)
   with taught state intact; load a too-new profile and confirm the clean refusal;
   corrupt drift.db and confirm drop-and-recreate recovery; run each path twice
   (idempotency) and kill a re-derive mid-run. *Note:* keep at least one fixture from
   each prior schema version to drive the re-derive and loader paths against.

8. **Generated profile artifacts** — Generated content is correct, deterministic where it
   should be, valid for whatever consumes it, and regenerates correctly when inputs
   change. *Observe:* generate, diff against expected, change an input, regenerate, and
   confirm the consumer accepts it. *Note:* check per language since extracted inputs
   differ.

9. **Per-user / per-repo data-dir state** — State is correctly scoped (no cross-repo or
   cross-user leakage), persists across sessions, and survives concurrent access; correct
   on both a fresh repo and an existing one. *Observe:* operate two repos / two users and
   confirm isolation; close and reopen to confirm persistence. *Note:* this underpins
   most other subsystems — verify it early.

10. **Language AST dumpers + extractor drivers** — For each supported language, the dumper
    produces a correct AST and the extractor pulls the right data from real source files,
    including odd-but-valid syntax; a failure on one file doesn't crash the batch.
    *Observe:* run the dumper/extractor over each golden repo (and the messy one); check
    extracted data and confirm partial-failure resilience. *Note:* this is the most
    language-specific subsystem — every Tier-1 language cell is required.

11. **Cross-cutting engines (multi-file)** — Engines reason over *multiple files together*
    (not one file at a time), produce consistent results, and handle large/real repos.
    *Observe:* run them on a multi-file golden repo and on the large/messy one; confirm
    cross-file conclusions are correct and stable. *Note:* verify behavior at realistic
    repo size, not toy size.

12. **Plugin packaging** — A clean install from the packaged artifact on a *fresh* machine
    works end to end; uninstall is clean; versions and dependencies resolve. *Observe:*
    install on a clean environment and run a full real session; then uninstall and confirm
    nothing is left behind. *Note:* hooks auto-register via `hooks.json` and the daemon
    auto-spawns from `session-start` on a real install; the merge driver does **not**
    auto-register (it is manual — see #6), so don't expect it to fire from install.

13. **`config.json` + `CHAMELEON_*` kill switches** — Per-repo config is read and honored;
    each kill switch, when set, *fully* disables its feature with zero residual effect;
    env-var vs config precedence is correct (env overrides config); the default state
    (nothing set) is the intended one. The `CHAMELEON_*` surface is **not one uniform
    set** — verify each off-state on its own: (a) 18 default-ON env kill switches:
    `CHAMELEON_DISABLE` (kill polarity `=1`) plus 17 `=0`-polarity switches
    (`CHAMELEON_VERIFY`, `ENFORCE`, `FETCH_PRODUCTION_REF`, `INTENT_CAPTURE`,
    `ATTESTATION`, `NEARBY_SIGNATURES`, `INBOUND_CALLERS`, `COUNTEREXAMPLE`,
    `ARCHETYPE_FACTS`, `CROSSWS_INDEX`, `MULTIROOT_STOP`, `FINDING_LEDGER`,
    `AUTOPASS_ATTESTATION`, `STOP_IDIOM_TERSE`, `JUDGE_TIERING`, `REVIEW_REFUTER`,
    `REVIEW_FANOUT`) — new features ship default-ON with a kill switch, so re-derive
    this list at verification time (grep the `== "0"` / `!= "0"` env reads across
    `plugin/mcp/chameleon_mcp/` + `hooks/`) rather than trusting this enumeration;
    (b) the 4-layer per-repo opt-out hierarchy (`.chameleon/.skip`,
    `CHAMELEON_DISABLE=1`, `.session_disabled.<sid>`, `.pause_until` — `optouts.py`);
    (c) ~7 default-OFF opt-**in** gates (`CHAMELEON_ALLOW_*`, `JUDGE_ASYNC`,
    `TRUST_REVALIDATE`) that are NOT kill switches; (d) the 18 `config.json`
    `enforcement.*` keys — `mode`, `stop_block_cap`, and 16 feature booleans
    (`EnforcementConfig`, `plugin/mcp/chameleon_mcp/profile/config.py`). *Observe:* for each,
    use the feature with it ON, then flip it and confirm the feature is genuinely gone
    (not just hidden); test env-vs-config precedence directly. *Note:* the off-state proof is itself a real-usage check — don't skip it.

14. **Version sync + build/CI scripts** — Versions are consistent across every manifest,
    the build produces the shippable artifact, and CI runs its intended build/package
    steps. *Observe:* run a real build, inspect the artifact and version stamps across
    files. *Note:* per the philosophy, CI is a build pipeline here, not the correctness
    gate — correctness still comes from real usage of the built artifact (ties to #12).

15. **Hot path as a budget, not a tool** — During real sessions the hot path
    (`get_pattern_context`, the PreToolUse advise path) stays within the Task-1 budget on
    every required cell, never blocks the user, and is never invoked as a discretionary
    tool/step. *Caveat:* `get_pattern_context` is *also* registered as an MCP tool
    (`plugin/mcp/chameleon_mcp/server.py`), so verify specifically that the **hook** invocation is
    automatic and budget-bound (the enforced path), distinct from that optional tool
    surface. *Observe:* measure real-session overhead (incl. on the large repo) against the
    ceilings; confirm the hook path runs as an enforced budget, not something Claude can
    choose to call. *Note:* measure on the heaviest Tier-1 cell, since that's where the
    budget breaks first.

---

## Gap-handling loop (the "proactively find and fix" part, made concrete)

For anything observed that isn't correct:

1. **Record** the gap (subsystem, cell, exact real-usage steps to reproduce).
2. **Categorize** it: logic gap / workflow gap / missing step / edge case /
   inconsistency / bug / performance (budget) regression.
3. **Fix** it.
4. **Re-verify** via the protocol on *every affected cell*, not just the one where it
   surfaced (a language-specific bug often implies siblings).
5. **Log** the resolution in a `docs/gap-log.md` and flip the affected cells back to "needs
   re-sign-off."

A gap is closed only after step 4's human re-sign-off.

---

## Sequencing (foundational subsystems first — others depend on them)

1. **Foundations:** #9 data-dir state → #13 config + kill switches → #12 packaging/install.
   (Almost everything else assumes these work.)
2. **Language layer:** #10 AST dumpers/extractors → #8 generated artifacts → #11
   cross-cutting engines.
3. **Surface features:** #1 hooks → #2 skills → #3 MCP tools → #4 statusline.
4. **Lifecycle/infra:** #5 daemon → #6 merge driver → #7 migrations.
5. **Build:** #14 version sync + build/CI.
6. **Budget under load:** #15 hot path, measured on the heaviest cells.
7. **Full-matrix sweep:** re-run the protocol across every required cell once fixes land.

---

## Definition of Done

All of the following are true:

- `docs/verification-matrix.md` exists, every **required** cell × relevant subsystem is
  signed off by a human via the Verification Protocol, including the negative/off-state checks.
- `gap-log.md` has no open gaps (or only gaps explicitly accepted and recorded as
  won't-fix with rationale).
- Each `CHAMELEON_*` kill switch is verified to *fully* disable its feature, and config
  precedence + default state are confirmed.
- A clean install on a fresh machine runs a full real session successfully, and uninstall
  is clean.
- The hot path is within the Task-1 budget on every required cell, including the heaviest.
- Versions are consistent across all manifests and the build produces the shippable
  artifact.

---

## Deliverables

- `docs/verification-matrix.md` (the tiered subsystem × cell sign-off tracker, fully
  signed off; distinct from the existing `docs/language-support-matrix.md` capability reference)
- `docs/hot-path-budget.md` (ceilings + measurement method, seeded from the existing
  timeout caps and `tests/bench_hot_path.py`)
- the golden-repo set (one per Tier-1 cell, reusing the `tests/journey/` fixtures and the
  `~/Documents/Projects/Testing Apps/` repos, plus the new messy repo)
- `docs/gap-log.md` (all gaps found, with resolutions; distinct from the roadmap
  `docs/parity-progress.md`)
- the code/config fixes themselves
