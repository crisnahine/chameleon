# chameleon — Architecture v2

> *"Code that blends in."*

> **Status:** v2 after Round 1 review (6 parallel agents). Addresses 14 critical issues + important concerns.
> **Date:** 2026-05-10
> **Author:** Cris Nahine + Claude
> **Successor relationship:** Generic engine that supersedes the EF-specific `claude-measure-twice`. EF profiles ship as a **separate companion plugin** (`chameleon-ef-pack`), not bundled with the engine.
> **v1 archived at:** `ARCHITECTURE-v1.md` (for diff reference)

---

## What changed from v1 (Round 1 changelog)

This v2 incorporates Round 1 review findings. Major changes:

**Architectural simplifications (load-bearing):**
- ❌ Dropped dynamic skill registration → ✅ MCP-driven dispatch via `using-chameleon` + PreToolUse `mcp_tool` hook (Claude Code only scans 4 canonical skill roots; dynamic runtime-skills directory was platform-incompatible)
- ❌ Dropped multi-language extractors in v1 → ✅ **TypeScript-only v1**, prove the loop, expand to Ruby/Python in v1.5 (drops dependency burden, scopes honestly)
- ❌ Dropped multi-harness directories from v1 file tree → ✅ **Claude Code only in v1**, other harnesses are roadmap (no theatrical empty directories)
- ❌ Dropped EF profile pack bundling → ✅ **EF profiles ship as separate companion plugin**, decoupling engine from project-specific bloat
- ❌ Dropped sequential hook stacking assumption → ✅ Combined preflight + archetype-advisor into single command hook (hooks run in parallel on shared matchers per Claude Code platform)

**New sections (load-bearing):**
- ✅ Skill test plan section (RED-GREEN-REFACTOR per skill, addresses Iron Law violation)
- ✅ Bootstrap acceptance test (mirrors superpowers' "react todo list" pattern)
- ✅ Comprehensive security mitigations (secret scanner, lstat, schema validation, pack code-exec ban, HMAC bug fix, repo size guard)
- ✅ Real Problem Evidence section (with explicit "TO BE FILLED" markers — no fabrication)
- ✅ Cache breakpoint strategy
- ✅ Multi-repo cost scaling subsection

**Honest revisions:**
- Cost model: prime budget revised 500-800 → 1,200-1,800 tokens; multi-repo cost acknowledged; range "$0.30-0.50 typical" not hard "$0.40 ceiling"
- Bootstrap interview: cut from 6-10 turns to 3 prompts max
- Slash commands: consolidated 8 → 5
- Dropped "2.1.138+" version stamp for `mcp_tool` (unsourced)
- Reconciled preflight inheritance: 1001 lines (not 556), with HMAC bug fix from claude-measure-twice (NOT verbatim)
- Removed Tagalog/Cebuano interview text (no localization story); replaced with neutral English placeholders

---

## Purpose

A Claude Code plugin that gives the AI deep understanding of any codebase's conventions — so AI-generated code blends with each repo's existing style on the first try.

Reviewers focus on logic, security, and tests rather than file shape, naming, or idiom usage.

**Target outcome:** measurable reduction in reviewer comments on file shape / naming / idiom usage on AI-generated code, validated against baseline transcripts (see Real Problem Evidence section).

---

## Real Problem Evidence

> **⚠️ This section requires evidence from dogfooding to be filled in. v2 architecture proceeds on a working hypothesis pending verification.**

### Working hypothesis

AI-generated code in established codebases routinely violates local conventions in ways that cost reviewer time but don't affect correctness. The hypothesis is supported by:
- The existence and active development of `claude-measure-twice` (this design's predecessor) as one team's response to observed friction
- Community demand signals (Anthropic's published guidance on giving Claude codebase context, the `CLAUDE.md` convention adoption rate)
- Anecdotal reports from the author's day-to-day work at Empire Flippers

### Evidence required before v1 release

Per Jesse Vincent perspective Round 1 review: **"the architecture asserts a problem without showing it. Where are the transcripts? What edit went wrong, in which file, costing what rework?"**

This section must be filled with:
- 5+ concrete transcripts of Claude (without chameleon) writing off-pattern code in real EF api/client repos
- Per transcript: what was generated, what reviewer flagged, time-to-fix, what the convention-correct version looked like
- Quantified cost of rework (review cycles, fix commits)

**Owner:** Cris (human partner). **Deadline:** before Phase 7 (release).

If insufficient evidence emerges during dogfooding, the plugin's value proposition must be revisited honestly rather than shipped on speculation.

---

## Goals

1. **Universal applicability** — works on any TypeScript/JavaScript repo in v1; expand to Ruby/Python in v1.5
2. **Single install, multi-repo** — install once; plugin handles every repo on the dev's machine
3. **Auto-onboarding** — first time in a new repo, plugin offers to bootstrap a profile via explicit `/chameleon-init` (no auto-trigger)
4. **Co-existence** — works alongside superpowers and any other Claude Code plugin
5. **Pre-built profile packs via companion plugins** — teams ship hand-curated profiles as their own plugin (engine remains generic)
6. **Honest cost model** — bootstrap acceptable high (one-time investment), steady-state $0.30-0.50/session typical, capped to <$50/month per developer
7. **Skill discipline** — every skill has a baseline + rationalization table per `superpowers:writing-skills`; no skill ships without a failing test first

---

## Plugin name: `chameleon`

> *Tagline: "Code that blends in."*

A chameleon adapts coloring to its environment without losing identity. Same with this plugin: Claude remains Claude underneath, but the AI's output adapts to blend with each repo's style.

**Conventions following from the name:**
- Plugin/repo name: `chameleon` (no `claude-` prefix; mirrors `superpowers`)
- Slash command prefix: `/chameleon-*` (with `/cham-*` short alias)
- Skill prefix: `chameleon-*`
- Foundation skill: `using-chameleon`
- MCP server: `chameleon-mcp`
- Python package: `chameleon_mcp`
- Profile dir: `.chameleon/`
- Env var prefix: `CHAMELEON_*`

---

## Core principles

1. **Foundation generic, brain per-repo.** The engine ships with no repo-specific knowledge. Profiles supply that.
2. **Profile is a portable artifact.** Committed JSON + Markdown, reviewable in PRs, team-shareable.
3. **Two-tier dimensions.** Auto-derivable (AST + statistical) vs. hand-curated (`idioms.md` escape hatch).
4. **Discovery before action.** Every code edit injects archetype context before the model writes — via MCP-driven dispatch, not dynamic skills.
5. **Inject context, don't deny.** Only the security layer hard-denies; conformance is advisory.
6. **Plugin coexistence first-class.** Namespaced everything, token budget discipline, parallel-hook-aware design.
7. **Honest scoping over breadth claim.** v1 = TypeScript only, v1 = Claude Code only. Prove the loop before expanding.
8. **Skills as code.** No skill ships without a failing test first (Iron Law from `superpowers:writing-skills`).

---

## High-level architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       chameleon (engine, v1: TS + Claude Code)           │
│                                                                          │
│  ┌──────────────────────────┐     ┌──────────────────────────────────┐ │
│  │ Hooks (parallel-aware)   │     │ Skills (static, no runtime gen)  │ │
│  │ ─────                    │     │ ──────                           │ │
│  │ SessionStart             │     │ using-chameleon (bootstrap)      │ │
│  │  → session-start         │     │                                  │ │
│  │  → inject using-chameleon│     │ Slash commands (5):              │ │
│  │  + profile primer        │     │  chameleon-init                  │ │
│  │ PreToolUse Edit/Write    │     │  chameleon-refresh               │ │
│  │  → preflight-and-advise  │     │  chameleon-status                │ │
│  │   (combined: safety      │     │  chameleon-refine                │ │
│  │    + MCP excerpt inject) │     │  chameleon-apply-pack            │ │
│  │ PostToolUse Bash         │     │                                  │ │
│  │  → posttool-recorder     │     │ Short aliases: /cham-*           │ │
│  │  (HMAC log, BUG FIXED)   │     │                                  │ │
│  │ UserPromptSubmit         │     │                                  │ │
│  │  → callout-detector      │     │                                  │ │
│  └──────────────┬───────────┘     └──────────────┬───────────────────┘ │
│                 └────────────────┬────────────────┘                    │
│                                  ▼                                     │
│  ┌─────────────────────────────────────────────────────────────────┐  │
│  │                   MCP Server (chameleon-mcp)                     │  │
│  │  detect_repo            get_archetype       lint_file            │  │
│  │  get_canonical_excerpt  get_rules           get_drift_status     │  │
│  │  refresh_repo           bootstrap_repo      list_profiles        │  │
│  │  apply_profile_pack     refine_profile                           │  │
│  │  (every file-reading tool: lstat first, refuse symlinks)         │  │
│  └─────────────┬───────────────────────────────┬───────────────────┘  │
│                │                               │                      │
│                ▼                               ▼                      │
│  ┌──────────────────────────────┐  ┌─────────────────────────────────┐│
│  │ Profile storage              │  │ Bootstrap engine                ││
│  │ ────────────────             │  │ ────────────────                ││
│  │ Committed (team-shared):     │  │ 1. Detect language (TS only v1) ││
│  │  <repo>/.chameleon/          │  │ 2. AST scan + path cluster      ││
│  │   profile.json               │  │    (excludes generated code)    ││
│  │   archetypes.json            │  │ 3. Tool config files = ground   ││
│  │   rules.json                 │  │    truth (.prettierrc, etc.)    ││
│  │   canonicals.json            │  │ 4. Statistical pattern extract  ││
│  │   idioms.md                  │  │ 5. Bimodal distribution check   ││
│  │   profile.summary.md  (PRs)  │  │ 6. Secret scanner pass          ││
│  │                              │  │    on canonical excerpts        ││
│  │ Local-only (not committed):  │  │ 7. Archetype proposal           ││
│  │  ${PLUGIN_DATA}/             │  │ 8. User confirms (single prompt)││
│  │   <repo_id>/                 │  │ 9. Save destination prompt      ││
│  │    drift.db                  │  │                                 ││
│  │    cache.json                │  │ ≤ 3 user-facing prompts total   ││
│  └──────────────────────────────┘  └─────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                    AST extractor (TypeScript only in v1)                 │
│  Single language: TS Compiler API via subprocess                         │
│  v1.5 expansion: Ruby (Prism), Python (libcst)                           │
│  v2.0 expansion: Go, Rust, PHP, Java                                     │
│                                                                          │
│  Provides: parse_repo, extract_archetypes, extract_patterns              │
│  Subprocess limits: 5s CPU, 512 MB RSS, 1 MB file ceiling                │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│              Companion plugin pattern (EF + others)                      │
│  ────────────────────────────────────────────────────────────────────    │
│  Pre-built profile packs ship as SEPARATE Claude Code plugins:           │
│   - chameleon-ef-pack   (EF api + client; was claude-measure-twice)      │
│   - chameleon-rails-7   (idiomatic Rails 7 baseline) — third-party       │
│   - chameleon-nextjs-14 (Next.js App Router)        — third-party        │
│                                                                          │
│  Companion plugin contributes profile data to chameleon engine via:      │
│   - Drop pack files into ${PLUGIN_DATA}/packs/<pack-name>/ at install    │
│   - Register match signatures (git remote URL pattern, signature heur.)  │
│  Packs are DATA-ONLY: companion plugins MAY ship hooks/scripts, but      │
│  pack directories themselves are forbidden from containing executables   │
│  (loader rejects packs with non-data files).                             │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Plugin structure (v1, Claude Code only)

```
chameleon/
├── .claude-plugin/
│   ├── plugin.json              # name/version (pinned)/author/keywords
│   └── marketplace.json         # self-distribution manifest
├── CLAUDE.md                    # AI agent guidelines
├── AGENTS.md                    # symlink → CLAUDE.md
├── README.md
├── CHANGELOG.md
├── RELEASE-NOTES.md
├── LICENSE
├── package.json                 # version anchor (synced with plugin.json)
├── hooks/
│   ├── hooks.json               # hook manifest
│   ├── run-hook.cmd             # cross-platform polyglot wrapper
│   ├── session-start            # SessionStart: inject using-chameleon + primer
│   ├── preflight-and-advise     # PreToolUse: safety + archetype injection (combined)
│   ├── posttool-recorder        # PostToolUse Bash: HMAC log (BUG FIXED from predecessor)
│   └── callout-detector         # UserPromptSubmit: frustration phrase reminder
├── skills/
│   ├── using-chameleon/         # bootstrap skill (loaded by SessionStart)
│   │   ├── SKILL.md
│   │   └── tests/               # baseline + rationalization scenarios
│   ├── chameleon-init/
│   │   ├── SKILL.md
│   │   └── tests/
│   ├── chameleon-refresh/
│   │   ├── SKILL.md
│   │   └── tests/
│   ├── chameleon-status/
│   │   ├── SKILL.md
│   │   └── tests/
│   ├── chameleon-refine/
│   │   ├── SKILL.md
│   │   └── tests/
│   └── chameleon-apply-pack/
│       ├── SKILL.md
│       └── tests/
├── mcp/
│   ├── pyproject.toml
│   └── chameleon_mcp/
│       ├── server.py            # FastMCP entry
│       ├── tools/               # MCP tool implementations (with lstat checks)
│       ├── extractors/
│       │   ├── _base.py         # extractor protocol
│       │   └── typescript.py    # via TS Compiler API subprocess
│       ├── bootstrap/           # interview-driven profile generation
│       ├── profile/             # schema + persistence + secret scanner
│       ├── packs/               # companion-plugin pack discovery
│       └── drift/               # mtime/sha tracking (local-only)
├── scripts/
│   ├── ts_dump.mjs              # TS Compiler API subprocess
│   ├── bump-version.sh          # syncs plugin.json + package.json versions
│   └── secret-scan.sh           # detect-secrets wrapper for canonicals
├── tests/
│   ├── skill-triggering/        # behavioral evals (mirroring superpowers)
│   │   ├── prompts/             # pressure scenarios per skill
│   │   ├── run-all.sh
│   │   └── run-test.sh
│   ├── unit/                    # extractors, MCP tools, hooks
│   ├── integration/             # end-to-end bootstrap + steady-state
│   └── corpus/                  # benchmark TS repos for conformance testing
├── docs/
│   └── chameleon/
│       ├── specs/               # design specs
│       ├── plans/               # implementation plans
│       └── reference/           # user-facing reference
└── assets/
```

**Note on harness directories:** v1 ships Claude Code only. `.codex-plugin/`, `.cursor-plugin/`, `.opencode/`, `gemini-extension.json`, `GEMINI.md` are explicitly **NOT** in v1. They are roadmap items for v2.0+.

**Note on EF profile pack:** `packs/empire-flippers-*/` is **NOT** in this repo. EF profiles ship as a separate companion plugin.

---

## Bootstrap mechanism

```
SessionStart hook fires (matcher: startup|clear|compact)
  → run-hook.cmd session-start
  → bash script:
      1. Read skills/using-chameleon/SKILL.md
      2. Detect active repo (file-path walk-up if available, else cwd)
      3. Check profile state:
           - <repo>/.chameleon/profile.json present?         → load summary
           - per-user cache populated for repo_id?            → load summary
           - companion plugin profile pack match?             → suggest /chameleon-apply-pack
           - language unsupported in v1 (not TS)?             → suppress primer (graceful degradation)
           - none of the above?                                → suggest /chameleon-init
      4. Inject as additionalContext (per-harness JSON dispatch:
         Cursor → additional_context, Claude Code → hookSpecificOutput.additionalContext, SDK → additionalContext)
         Wrap in <EXTREMELY_IMPORTANT> tags (coordinate with superpowers — see Plugin Coexistence)
         Content: using-chameleon SKILL.md + repo+profile primer (~1,500 tokens, see Cost model)
```

**Note on `resume` matcher:** Deliberately omitted (matches superpowers approach). Resumed sessions already have the prior `additionalContext`; re-injection would double-prime.

**Note on graceful degradation for unsupported languages:** SessionStart detects language signals (Gemfile, go.mod, etc.). If none match v1's TS-only support, the primer is **suppressed entirely** — no "you don't have a profile" nag. Plugin is silent in unsupported repos.

---

## Hook stack

```
SessionStart (matcher: startup|clear|compact):
  1. session-start
       Inject using-chameleon + repo profile primer
       Budget: ~1,200-1,800 tokens (revised from v1; see Cost model)

PreToolUse (matcher: Edit|Write|NotebookEdit):
  1. preflight-and-advise (SINGLE COMBINED HOOK — hooks run in parallel on shared matchers)
       a. Safety hard-denies (path traversal, secrets, lockfiles, vendored, generated)
          (Inherited logic from claude-measure-twice — REVIEWED, NOT verbatim;
           HMAC path bug fixed: ${TMPDIR:-/tmp}/.claude_exec_log/ used consistently)
       b. lstat check on file_path (refuses symlinks — TOCTOU mitigation)
       c. If safety passes: synchronously calls chameleon-mcp::get_canonical_excerpt
          for the file's archetype, injects 500-800 token annotated excerpt
       d. Hook injection cap per turn: 1,500 tokens max (truncated with ellipsis)

PostToolUse (matcher: Bash):
  1. posttool-recorder
       HMAC-signed exit code log
       BUG FIX from predecessor: writes AND reads use ${TMPDIR:-/tmp} consistently
       Key handling fail-loud: if /dev/urandom fails, error explicitly (don't silent-degrade)

UserPromptSubmit:
  1. callout-detector
       Frustration phrase → rule-update-first reminder
```

**Why combined `preflight-and-advise` (not two separate hooks):**

Round 1 review (Platform expert) flagged that hooks on shared matchers run **in parallel**, not sequential. The v1 design assumed preflight hard-deny would gate the advisor injection — that's not enforceable. v2 combines them into one synchronous command hook so safety check completes before MCP call.

---

## Skill design

### Foundation skill: `using-chameleon`

```yaml
---
name: using-chameleon
description: Use when starting any conversation in a repo with a chameleon profile present, before any Edit, Write, or NotebookEdit operation
---
```

**Description rules followed (per `superpowers:writing-skills`):**
- "Use when..." third-person ✓
- Triggering conditions only — no workflow summary ✓
- Under 1024 chars ✓
- Specific symptoms (Edit/Write/NotebookEdit operations) ✓

**Body sections** (mirroring `using-superpowers`):
- `<EXTREMELY-IMPORTANT>` block: pattern conformance is mandatory if profile is present
- `<SUBAGENT-STOP>` block: subagents skip
- The Rule: invoke `chameleon-mcp::detect_repo` + `get_canonical_excerpt` BEFORE editing in profiled repos
- Process flowchart (graphviz `dot`): when to call MCP tools
- Red Flags table: rationalizations to skip pattern conformance (see Skill Test Plan section for the rationalizations to defeat)
- Available slash commands (5 user-facing)
- Profile state interpretation

### User-invokable skills (consolidated from 8 to 5)

| Skill | Slash command | Short alias | Purpose |
|---|---|---|---|
| `chameleon-init` | `/chameleon-init` | `/cham-init` | Bootstrap a new repo profile (3-prompt interview) |
| `chameleon-refresh` | `/chameleon-refresh` | `/cham-refresh` | Re-analyze repo, detect drift, update profile |
| `chameleon-status` | `/chameleon-status` | `/cham-status` | Show current profile, drift, plugin health (merges old `doctor` + `profile` + `status`) |
| `chameleon-refine` | `/chameleon-refine` | `/cham-refine` | Iterate on profile based on observed misses; **owns the idioms.md collection** (deferred from bootstrap) |
| `chameleon-apply-pack` | `/chameleon-apply-pack <name>` | `/cham-apply-pack` | Apply a companion-plugin profile pack (also: `/chameleon-init --pack <name>`) |

(Implemented as Skills with `disable-model-invocation: true`, mirroring superpowers.)

**No dynamic archetype skills.** The original v1 design proposed runtime-generated skills at `${PLUGIN_DATA}/runtime-skills/...`. **Claude Code does not discover skills outside its 4 canonical roots.** Replaced with MCP-driven dispatch: `using-chameleon` mandates `get_archetype` + `get_canonical_excerpt` calls; the PreToolUse `preflight-and-advise` hook injects archetype-keyed context per edit. This is simpler, works correctly, and matches superpowers' philosophy of skills as static authored artifacts.

---

## Skill test plan

> **Iron Law from `superpowers:writing-skills`:** "NO SKILL WITHOUT A FAILING TEST FIRST."

For each skill in chameleon, the RED-GREEN-REFACTOR cycle must be completed before merge. Specific rationalizations to be captured during baseline testing per `superpowers:testing-skills-with-subagents` methodology.

### `using-chameleon` test plan

**RED (baseline scenarios — MUST be run before writing skill body):**
- Pressure scenario 1: TS repo with profile; user says "just add this small one-line fix"
- Pressure scenario 2: TS repo with profile; user says "I know the pattern, skip the MCP call"
- Pressure scenario 3: TS repo with profile; user is rushing ("dinner in 5 min")
- Pressure scenario 4: TS repo without profile; agent invents pattern instead of suggesting `/chameleon-init`
- Pressure scenarios 5+: combined pressures (time + sunk cost + authority)

**Rationalizations to capture verbatim:** TBD during baseline run. Anticipated patterns (to validate empirically):
- "This is just a one-line fix"
- "I already know this codebase"
- "Calling MCP for every edit is wasteful"
- "The profile is probably outdated anyway"

**GREEN (skill addresses captured failures):** Write Red Flags table + foundational principle ("violating the letter is violating the spirit") addressing exactly the rationalizations from RED.

**REFACTOR:** Re-test, capture new rationalizations, plug holes until bulletproof.

### `chameleon-init` test plan

**RED:** Run scenarios where:
- Bootstrap on a 200-file TS repo (in-budget)
- Bootstrap on a 5,000-file TS repo (sampling required)
- Bootstrap on a multi-framework monorepo
- Bootstrap on a half-migrated codebase (CommonJS → ESM)
- Bootstrap when interrupted mid-flow (Ctrl-C)
- Bootstrap when AST extractor fails on individual files

**GREEN:** Skill body must:
- Cap total interview to 3 user-facing prompts
- Surface bimodal distributions explicitly
- Resume gracefully on partial profile state
- Sanity-check against tool config files (`.prettierrc`, `tsconfig.json`)

### `chameleon-refresh` test plan

**RED:** Scenarios where:
- Refresh detects drift > threshold; user is mid-task
- Refresh on a repo where canonical files have been deleted
- Refresh after profile pack auto-update

**GREEN:** Diff summary + explicit consent before applying changes.

### `chameleon-refine` test plan

**RED:** Scenarios where user provides feedback like "Claude wrote `useCustomQuery` wrong":
- Pattern was missed (idiom not in profile)
- Pattern was misclassified (wrong archetype)
- Pattern was fabricated (hallucinated convention)

**GREEN:** Updates `idioms.md` with provenance, re-tests with subagent.

### `chameleon-status` test plan

Reference skill (low-discipline, high-readability). Test for clarity rather than rationalization-resistance.

### `chameleon-apply-pack` test plan

**RED:** Scenarios where:
- Pack signature matches but version is incompatible
- Pack from untrusted publisher (signature fails)
- Pack contains executable files (must be rejected)
- Multiple packs match same repo

**GREEN:** Strict validation; refuses unsigned packs without explicit `--allow-unsigned` flag.

---

## Bootstrap acceptance test

Per superpowers' multi-harness PR template precedent:

> **Acceptance test for chameleon bootstrap.** Open a clean Claude Code session in a directory containing a `.chameleon/profile.json` with at least one archetype. Send exactly:
>
> > `Add a new endpoint at /api/v1/widgets that returns a list of widgets.`
>
> A working integration:
> 1. SessionStart hook fires; `using-chameleon` is injected as `additionalContext`
> 2. Before generating any code, the agent invokes `chameleon-mcp::detect_repo` and `get_canonical_excerpt`
> 3. The agent's first edit follows the canonical excerpt's pattern (file location, naming, imports, error handling)
>
> If the agent writes code without first invoking the MCP tools, the integration is broken. The bootstrap is not optional.

This acceptance test must pass before any release. Transcript pasted in release notes per phase.

---

## MCP server (`chameleon-mcp`)

FastMCP-based, stdio transport (NOT exposed over network — explicit v1 constraint).

| Tool | Input | Output | Security note |
|---|---|---|---|
| `detect_repo` | file_path | repo_id, profile_status | repo_id is sha256 (never raw path) |
| `get_archetype` | repo, file_path | archetype name + content_signal match, alternatives | content_signal field new in v2 |
| `get_canonical_excerpt` | repo, archetype | annotated excerpt (500-800 tokens) | **lstat check on canonical file path before read** |
| `get_rules` | repo, archetype? | rules + citations | — |
| `lint_file` | repo, archetype, content | AST violations list | content size capped at 100 KB |
| `get_drift_status` | repo | freshness + recommended action | reads from local-only drift.db |
| `refresh_repo` | repo, force | re-analyze | rate-limited, max 1/hour per repo |
| `bootstrap_repo` | path, mode | first-time analysis | repo size guard (refuse >50k files without explicit globs) |
| `list_profiles` | — | all known repos | — |
| `apply_profile_pack` | repo, pack_id | install pre-built profile | strict signature validation |
| `refine_profile` | repo, feedback | apply user-driven correction | feedback sanitization (strip ANSI/zero-width) |

State at:
- `${CLAUDE_PLUGIN_DATA}/<repo_id>/` (per-user cache, includes `drift.db`)
- `<repo>/.chameleon/profile.json + archetypes.json + rules.json + canonicals.json + idioms.md + profile.summary.md` (committed, team-shared)

**Note:** `drift.db` is **never committed**. Auto-`.gitignore` on `chameleon-init`.

---

## TypeScript-first extractor (v1 single language)

v1 ships TypeScript only via TS Compiler API subprocess. Reasoning:
- Drops dependency burden (no Prism, no libcst, no FastMCP-with-multiple-shims)
- Lets us prove the engine + bootstrap loop on one language before generalizing
- Most claude-measure-twice EF client work is TS, so dogfooding is feasible
- Honest scope claim — addresses Round 1 critique that "6 bundled parsers ≠ superpowers-style discipline"

**v1.5 expansion:** Ruby (Prism) + Python (libcst) — only after v1 ships and the engine is stable
**v2.0 expansion:** Go, Rust, PHP, Java

**Subprocess limits per file parse:**
- 5s CPU
- 512 MB RSS
- 1 MB file size ceiling
- Reject files matching generated-code signals (header comments, paths matching `**/__generated__/**`, `**/dist/**`, `.gitattributes` `linguist-generated=true`)

**Tool config files as ground truth:**
Bootstrap reads `.prettierrc`, `tsconfig.json`, `.eslintrc*`, `.editorconfig` BEFORE running statistical analysis. Rules sourced from these files take priority over inferred rules. Bootstrap reports: "rules sourced from .prettierrc + AST stats" not just AST stats.

---

## Profile schema

```
.chameleon/   (committed, team-shared)
  ├── profile.json         # manifest entry-point (version, created_at, source)
  ├── archetypes.json      # path patterns + content_signal → archetype + globs
  ├── rules.json           # per-archetype rules + citations
  ├── canonicals.json      # chosen reference files + line ranges + sha + secret-scan-passed
  ├── idioms.md            # human-curated free-form (size cap: 50 KB)
  └── profile.summary.md   # human-readable overview for PR review

${CLAUDE_PLUGIN_DATA}/<repo_id>/   (local-only, NEVER committed)
  ├── drift.db             # sqlite mtime/sha map, GC'd weekly
  └── cache.json           # per-user runtime cache
```

**`.gitignore` automation:** `chameleon-init` adds these lines (asks consent):
```
.chameleon/drift.db
.chameleon/.skip
```

`archetypes.json` shape with **content_signal** (new in v2 — addresses Pattern detection critique):

```json
{
  "version": 2,
  "archetypes": {
    "next-server-component": {
      "paths": ["app/**/*.tsx"],
      "content_signal": {
        "absent_directives": ["use client", "use server"]
      },
      "alternatives": ["next-client-component", "next-server-action", "next-route-handler"],
      "canonical": {
        "path": "app/dashboard/page.tsx",
        "lines": [1, 60],
        "sha": "abc123...",
        "secret_scan_passed": true,
        "scanned_at": "2026-05-10T..."
      },
      "confidence": "high",
      "cluster_size": 23,
      "outlier_paths": ["app/legacy-dashboard/page.tsx"],
      "source": "bootstrap"
    },
    "next-client-component": {
      "paths": ["app/**/*.tsx"],
      "content_signal": {
        "directive": "use client"
      },
      "canonical": { "...": "..." },
      "confidence": "high",
      "cluster_size": 18
    }
  }
}
```

**Schema changes from v1:**
- `confidence` is now ordinal (`high|medium|low`), not float — addresses Pattern reviewer's "0.94 is unfalsifiable"
- `content_signal` field added — addresses Next.js App Router / Django CBV/FBV / Pydantic v1+v2 detection
- `cluster_size` and `outlier_paths` exposed — supports interview UX
- `secret_scan_passed` flag on canonicals — Security mitigation
- `scope` field (optional) — for monorepo subtree scoping (Rails engines, lerna packages)

`profile.summary.md` (NEW — addresses DX reviewer):

```markdown
# chameleon profile summary

Generated: 2026-05-10  |  Engine: chameleon v1.0  |  Source: bootstrap

## 8 archetypes detected

- **next-server-component** (high confidence, 23 files) — canonical: `app/dashboard/page.tsx`
- **next-client-component** (high, 18) — canonical: `app/components/SearchBar.tsx`
- **next-server-action** (medium, 6) — canonical: `app/actions/createWidget.ts`
- ...

## 14 rules

(Sourced from .prettierrc + tsconfig + AST stats)
- File names: kebab-case
- Indent: 2 spaces
- Quotes: single
- ...

## 5 idioms (from /chameleon-refine)

- We use `useCustomQuery` (not `useQuery`) — banned: `useQuery` direct
- Error responses go through `apiError(code, message)` — banned: `Response.json({error: ...})`
- ...
```

This is what reviewers actually read on a profile-change PR.

---

## Companion plugin pattern (replaces bundled EF packs)

Pre-built profiles ship as **separate Claude Code plugins** that contribute data to chameleon at install time.

```
chameleon-ef-pack/                           (separate plugin, separate repo)
├── .claude-plugin/plugin.json
├── packs/
│   ├── empire-flippers-api/
│   │   ├── manifest.json
│   │   ├── archetypes.json
│   │   ├── rules.json
│   │   ├── canonicals.json
│   │   └── idioms.md
│   └── empire-flippers-client/
│       └── ...
└── hooks/
    └── post-install            # registers packs with chameleon engine
```

**Pack contract:**
- Pack directories are **DATA-ONLY**. The chameleon loader rejects packs containing executables (any `*.sh`, `*.py`, `*.js`, `*.cmd` in pack subdirectories triggers refusal).
- Companion plugins themselves MAY ship hooks/scripts (they're full plugins), but the pack data they deliver must be data only.
- Match strategy (priority order):
  1. Git remote URL exact match (e.g., `git@bitbucket.org:empire-flippers/api.git` → pack `empire-flippers-api`)
  2. Signature heuristics (Gemfile/package.json deps + characteristic file paths)
  3. Explicit user assignment via `/chameleon-apply-pack <name>`
- Signature verification: companion plugins MUST sign packs (Sigstore/cosign or ed25519). Unsigned packs require explicit `--allow-unsigned` flag.

**EF migration path:**
- Existing `claude-measure-twice` profiles → migrate as a separate plugin `chameleon-ef-pack`
- Plugin lives in EF's private Bitbucket
- Auto-installs into chameleon engine on `claude plugin install chameleon-ef-pack@empire-flippers-marketplace`
- chameleon engine remains generic and public-shippable

---

## Bootstrap interview flow (`/chameleon-init`)

**Reduced from v1's 6-10 turns to ≤3 user-facing prompts.**

```
1. User runs /chameleon-init in a TS repo
   (No auto-trigger — explicit consent required)

2. Engine (no user prompts):
   a. Detect language → TS detected, proceed (else: graceful error)
   b. Read tool config files (.prettierrc, tsconfig.json, .eslintrc, .editorconfig) — ground truth for rules
   c. AST scan repo:
        - <500 files: full pass
        - 500-50,000: stratified sample (top-N by directory frequency, recent commits)
        - >50,000: refuse without explicit globs ("repo too large for unguided bootstrap")
   d. Exclude generated code (header comments, paths matching **/__generated__/**, **/dist/**, .gitattributes flag)
   e. Statistical pattern extraction (naming, structure, imports, type annotation density)
   f. Cluster files by content_signal + path → archetype proposals
   g. Bimodal distribution check: any pattern split 60/40 or worse → flag for confirmation
   h. Sparse cluster check: any cluster <5 files → propose merging or asking
   i. Secret scan canonical excerpts (detect-secrets pass) → flag any hits

3. Engine → user (PROMPT 1: archetype confirmation):
   "Detected 8 archetypes:
    - next-server-component (high confidence, 23 files): app/dashboard/page.tsx
    - next-client-component (high, 18): app/components/SearchBar.tsx
    - [...]
    - half-migrated-component (BIMODAL — 14 files use Pattern A, 9 use Pattern B; pick canonical for new code)

    Apply as proposed? [Y/n/edit]"

4. Engine → user (PROMPT 2: bimodal resolution if any):
   "For half-migrated-component, which pattern is canonical for new code?
    A) ApolloClient.query() (14 files)
    B) useQuery hook (9 files, newer)"

5. Engine → user (PROMPT 3: save destination):
   "Save profile to <repo>/.chameleon/ (committed, team-shared) or per-user cache?
    [committed/private]"

6. Engine writes profile artifacts + .gitignore additions + profile.summary.md
   → "Profile ready. 8 archetypes, 14 rules, 0 idioms (run /chameleon-refine to add).
      Cost: $X.XX. Drift tracking enabled."

7. Idioms collection deferred to /chameleon-refine.
   First 5-10 edits with chameleon active → user sees what was missed → /chameleon-refine
   captures specific idioms incrementally with provenance.
```

**Cost estimate per bootstrap (revised):**
- AST scan + tool config read: 0 Claude tokens (local computation)
- Sampling reads: ~50-100 files × ~500 tokens = ~25-50k tokens input
- Secret scanner: 0 Claude tokens (local detect-secrets)
- 3 user-facing prompts × ~3k tokens each (with archetype display) = ~9k tokens
- Profile generation output: ~5-10k tokens
- **Total: $0.50–2.00 per repo (one-time)** — revised down from v1's $1.50-5.00

(Math at Sonnet 4.6 pricing: 60k input × $3/M = $0.18; 8k output × $15/M = $0.12 → ~$0.30 minimum; range $0.50-2.00 accounts for larger repos and longer interview turns.)

---

## Multi-repo handling

- Profile keyed by `repo_id = sha256(git_remote_url || abs_path_canonical)` — always sha256, never raw path
- Storage:
  - In-repo: `<repo>/.chameleon/...` (preferred default — team shares the brain)
  - Per-user cache: `${CLAUDE_PLUGIN_DATA}/<repo_id>/` (drift.db + cache.json)
- Detection: file-path walk-up on each tool call (NOT cwd)
- Drift tracking: per-repo sqlite, GC'd weekly (records older than 30 days purged)

**Multi-repo cost scaling (NEW — addresses cost reviewer):**

| Open repos in session | Prime cost | Cache behavior |
|---|---|---|
| 1 | ~1,500 tokens once | Warm; standard $0.30-0.50 session |
| 5 | ~7,500 tokens (5×) | Each repo switch = cache prefix break |
| 20 | ~30,000 tokens (20×) | Heavy cache thrashing; expect $0.80-1.20 sessions |

Architecture acknowledges multi-repo sessions cost more. Optimization opportunities for future versions:
- Lazy primer (only on first edit per repo, not all on SessionStart)
- Repo-keyed cache breakpoints (Anthropic API may add multi-breakpoint support)

---

## Plugin coexistence

**Hygiene rules:**
- Slash commands namespaced: `/chameleon-*` (with `/cham-*` aliases)
- Env vars namespaced: `CHAMELEON_*`
- Hooks: parallel-aware design (no ordering assumptions on shared matchers)
- Inject context, don't deny (security layer is the only hard-deny)
- Token budget discipline: ~1,500 prime + ≤1,500 per-edit injection cap (truncated)
- Distinct MCP server (`chameleon-mcp`)
- Per-repo opt-out: `.chameleon/.skip` file → primer suppressed in this repo (for scratch/docs/unsupported repos)
- Global opt-out: `CHAMELEON_DISABLE=1` env

**`<EXTREMELY_IMPORTANT>` wrapper coordination:**
Both superpowers and chameleon currently inject `<EXTREMELY_IMPORTANT>` blocks at SessionStart. This creates competing "most important thing" framings.

**v1 mitigation:** chameleon uses `<CHAMELEON_IMPORTANT>` instead, scoping the urgency to its own domain. Documents this choice in `using-chameleon`.

**Specifically with superpowers:**
- Superpowers operates at "how to approach work" layer (process: brainstorm, debug, plan, review)
- chameleon operates at "how to write code that fits this repo" layer (output shape)
- Complementary, not overlapping
- Combined token cost when both active: ~1,500 (superpowers prime) + ~1,500 (chameleon prime) = ~3,000 prime tokens per session — within 5-min cache TTL budget

---

## Cost model (revised)

| Scenario | Estimate | Notes |
|---|---|---|
| SessionStart prime | **~1,200-1,800 tokens** | Revised up from v1's 500-800 — empirical floor matches superpowers SKILL.md size |
| Per-edit context injection | ~500-800 tokens | Combined hook output (safety result + canonical excerpt) |
| Per-edit hook injection cap | **1,500 tokens hard cap** | Truncated with ellipsis if exceeded — addresses cost stack-up |
| **Steady-state per session** (Sonnet 4.6, 30 turns, single repo, warm cache) | **$0.30-0.50** | Honest range, not "$0.40 ceiling" |
| **Per-month at 100 sessions** | **$30-50** | Comfortably under $50 ceiling for most users |
| **Bootstrap per repo (one-time)** | **$0.50-2.00** | Revised down — math derived from 60k input × $3/M + 8k output × $15/M |
| **Multi-repo session (20 open)** | **$0.80-1.20** | Cache thrashing acknowledged — see Multi-repo cost scaling |
| **Per-team-month with 5 devs sharing committed profile** | **$150-250** | 5 × $35-50/mo, profile sharing eliminates redundant bootstrap |

### Cache breakpoint strategy (NEW — addresses cost reviewer)

The architecture pins prompt cache breakpoints as follows:
- **Breakpoint 1**: SessionStart prime (using-chameleon + profile primer) — pinned for the session
- **Per-edit injections** flow as ephemeral input AFTER the cached prefix, never breaking it
- **Repo switches mid-session** invalidate Breakpoint 1 (different profile primer for different repo); a new prime is injected with cache_control fresh

Idle gaps >5 min trigger reprime. Acceptable cost (~$0.006/reprime), documented in primer with cost surfaced ("This session: $X.XX so far").

### Cost transparency (NEW — addresses DX reviewer)

The SessionStart primer includes a small footer: `"Recent sessions: $0.32, $0.41, $0.28. This month: $14.20."` Builds user trust via real numbers; meets the cost-transparency goal.

---

## Security mitigations (NEW comprehensive section)

Round 1 security review surfaced 5 critical issues + 12 important issues. v2 addresses all critical items in-design:

### Critical mitigations

**1. Canonical excerpt secret scanner**
- Bootstrap and refresh both run extracted snippets through `detect-secrets` (or `gitleaks`) before writing to `canonicals.json`
- On hits: warn user, refuse to commit, offer to pick alternative canonical
- `secret_scan_passed: true` flag in `canonicals.json` schema; loader refuses to use unscanned canonicals

**2. Profile pack code-execution ban**
- Pack directories MUST contain only data files: `manifest.json`, `archetypes.json`, `rules.json`, `canonicals.json`, `idioms.md`
- Loader walks pack directory at install time; refuses if any `*.sh`, `*.py`, `*.js`, `*.cmd`, `*.exe`, `*.bin` found
- Companion plugins delivering packs may ship their own hooks/scripts; pack contents are sandboxed to data

**3. Symlink lstat in MCP file reads**
- Every MCP tool that reads a file path performs `os.lstat(path)` and refuses if `S_ISLNK` flag set
- Mirrors preflight-check's symlink defense; closes TOCTOU exfiltration vector

**4. HMAC bug fix (preflight inheritance — NOT verbatim)**
- claude-measure-twice's preflight reads exec_log from hardcoded `/tmp/.claude_exec_log/`; posttool-recorder writes to `${TMPDIR}/.claude_exec_log/`
- On macOS, these are different directories → silent HMAC bypass
- v2 chameleon: BOTH read and write paths use `${TMPDIR:-/tmp}/.claude_exec_log/` consistently
- Documented as bug fix from predecessor; "verbatim inheritance" claim removed
- Preflight is 1001 lines (not 556 as v1 claimed) — reconciled

**5. profile.json schema validation on load**
- Strict JSON schema (max lengths, regex patterns for archetype names: `^[a-z][a-z0-9-]{0,63}$`, enum for `source` field)
- Loader rejects any profile that fails validation
- Defends against prompt-injection via crafted profile data

### Important mitigations

**6. Repo size guard at bootstrap**
- Hard ceiling: 50,000 files
- Above ceiling: refuse without explicit globs ("This repo has 1.2M files. Specify globs (e.g., `--paths 'src/**/*.ts'`) to scope the analysis.")

**7. AST extractor subprocess limits**
- 5s CPU limit per file parse
- 512 MB RSS limit
- 1 MB file size ceiling
- Reject files larger than ceiling; never parse generated code

**8. Bootstrap interview output sanitization**
- User-typed content (idioms, custom archetype names) stripped of: ANSI escapes, zero-width Unicode, shell metacharacters, HTML tags
- Hard 50 KB size cap on `idioms.md`

**9. drift.db local-only contract**
- Auto-`.gitignore` on `chameleon-init`
- Schema documented as ephemeral; never committed

**10. HMAC key fail-loud**
- If `/dev/urandom` read fails or key file is zero bytes: emit explicit stderr warning + refuse to operate in unsigned mode
- No silent degradation

**11. Profile pack signing**
- Built-in chameleon engine ships with maintainer signing key
- Companion plugins distribute their packs signed (Sigstore/cosign or ed25519)
- Unsigned packs require explicit `--allow-unsigned` flag

**12. First-use prompt for committed profiles**
- When a `.chameleon/profile.json` is first encountered (e.g., after cloning a repo): "This repo has a chameleon profile. Trust it for this session? [y/N]"
- Treats committed profile like `.envrc` — trust on explicit consent
- Stored per-repo per-user

---

## Phase plan (revised — TS-only v1)

| Phase | Effort | Exit criteria |
|---|---|---|
| Phase 1 — Foundation | ~60h | Hooks + skills shells + MCP scaffold + plugin manifest. Acceptance test passes on stub profile. |
| Phase 2 — TS extractor + bootstrap | ~80h | `/chameleon-init` produces working profile on 5 test TS repos (Next.js 14, classic React, Node API, Vite SPA, monorepo). Generated-code detection working. Tool config files honored as ground truth. |
| Phase 3 — Skills with eval | ~50h | All 6 skills (1 foundation + 5 user) pass RED-GREEN-REFACTOR cycle per `superpowers:writing-skills`. Acceptance test transcripts captured. |
| Phase 4 — Security mitigations | ~30h | Secret scanner integrated. Pack code-exec ban enforced. Symlink lstat in all MCP file reads. Schema validation. HMAC bug fix verified. |
| Phase 5 — Companion plugin pattern | ~40h | EF profile pack migrated to separate plugin. Pack signing infrastructure. Trust-on-first-use prompt. |
| Phase 6 — Conformance benchmarking | ~40h | 90%+ on archetype-matched tasks across 3 test TS repos (rather than v1's 95% — honest target). Cost ceiling validated. |
| Phase 7 — Documentation + dogfooding | ~40h | Real Problem Evidence section filled with actual transcripts. Docs complete. Dogfooding green for 2 weeks. |
| **Total** | **~340h** | **~9 weeks of focused work** |

(Down from v1's 350h — scoping to TS-only saves ~30h on extractors, but added ~20h for explicit security work and skill testing.)

---

## Open decisions for Round 2 (adversarial review)

1. ~~**Plugin name**~~ — decided as `chameleon`
2. ~~**Profile location default**~~ — committed `.chameleon/profile.json`
3. ~~**Bootstrap consent**~~ — explicit `/chameleon-init` only
4. ~~**Initial language support**~~ — TypeScript only in v1; Ruby/Python deferred to v1.5
5. **Profile pack distribution** — companion plugins. **Round 2: how does pack discovery + signing scale to 50+ packs?**
6. ~~**Drift detection**~~ — mtime/sha sqlite, local-only
7. **MCP transport** — stdio only in v1. **Round 2: does any platform need HTTP transport?**
8. ~~**Skill cross-references**~~ — `chameleon:skill-name` style
9. ~~**Bootstrap cost ceiling**~~ — $0.50-2.00/repo
10. ~~**Steady-state target**~~ — $0.30-0.50/session honest range

---

## Areas explicitly seeking adversarial review (Round 2)

For Round 2 reviewers, who will TRY to break this design:

**Cost adversary** — find worst-case cost scenarios. 100k-file monorepo bootstrap. Polyglot stack. Long-running sessions (200+ turns). Heavy refactor sessions with high edit count. Subtle prompt cache breakage scenarios.

**Pattern adversary** — find archetypes/idioms that cannot auto-detect. DSL-heavy code. Metaprogramming. Auto-generated APIs (tRPC, GraphQL codegen). Half-migrated codebases. Files that intentionally violate conventions (compatibility shims, third-party adapters).

**Plugin compatibility adversary** — find conflicts. Multiple plugins competing for `<EXTREMELY_IMPORTANT>` framing. Hook ordering surprises (parallel firing edge cases). MCP server name collisions. Skill name collisions across plugins. Token budget collisions (chameleon + superpowers + custom plugins all priming at SessionStart).

**Bootstrap edge case adversary** — find weird repos. Vendored monorepos. Multi-framework. Half-migrated. Yarn workspace. Lerna package. Pnpm workspace. Submodule-heavy. Generated code-heavy. Test-only repos. Empty repos. Repos with `.chameleon/` from a different version of chameleon.

**Maintainability adversary** — find what breaks at 6 months / 1 year. Claude Code platform API drift. TS Compiler API breaking changes. detect-secrets/gitleaks updates. SQLite schema migration. Profile schema versioning. Companion plugin signature key rotation.

---

## Out of scope for v1

- Multi-language extractors (Ruby/Python deferred to v1.5)
- Multi-harness support beyond Claude Code (deferred to v2.0)
- Cross-repo pattern transfer (e.g., "this team uses Pattern X across all their repos")
- Auto-PR opening for profile updates (manual `/chameleon-refresh` only)
- Full-history learning (only AST snapshot at bootstrap time; future versions could ingest git log)
- IDE-specific features beyond Claude Code harness
- Profile diffing UI (text diffs only in v1)
- Cost telemetry dashboard (CLI surface only in v1)
- HTTP transport for MCP (stdio only in v1)
- Auto-trigger of `/chameleon-init` on first session in unprofiled repos (explicit user action required)

---

## Inheritance from claude-measure-twice

What's preserved (the proven pieces, REVIEWED — not "verbatim"):
- Preflight-check safety hard-deny logic (1001 lines per current source — RECONCILED from v1's incorrect 556 claim)
- Posttool-recorder HMAC exec log (with **GC bug fix + path mismatch fix**)
- Callout-detector frustration phrase reminder
- TS Compiler API extractor approach (Ruby Prism deferred)
- MCP server + Skills + PostToolUse pattern

What's redesigned from scratch (Round 1 findings):
- Combined preflight-and-advise hook (was two parallel-firing hooks)
- MCP-driven dispatch (was dynamic skill registration)
- Bootstrap interview (was 6-10 turns; now 3 max)
- Profile schema (added content_signal, ordinal confidence, cluster_size, secret_scan_passed)
- Companion plugin pattern (was bundled EF packs)
- Security mitigations (new comprehensive section addressing 5 critical + 12 important findings)
- Cost model (was overoptimistic prime budget; now honest)

What's discarded:
- EF-specific archetype taxonomy in core (becomes companion plugin)
- Multi-language v1 extractors (TS only; rest deferred)
- Multi-harness v1 directories (theatrical without shipping support)
- Dynamic archetype skills (platform-incompatible)
- "verbatim inheritance" claim for preflight (was inaccurate)

---

*End of v2 architecture. Addresses 14 critical issues + most important issues from Round 1 review (6 agents). Ready for Round 2 adversarial pressure-testing (5 agents).*
