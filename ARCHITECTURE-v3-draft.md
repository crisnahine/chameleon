# chameleon — Architecture v3

> *"Code that blends in."*

> **Status:** v3 after Round 2 adversarial review (5 agents). Addresses 9 BLOCKING items + ~25 SIGNIFICANT items. **Major scope shift**: drops "framework support" framing entirely — chameleon does best-effort pattern clustering on whatever AST it can parse, with idioms.md absorbing what AST cannot infer.
> **Date:** 2026-05-10
> **Author:** Cris Nahine + Claude
> **Predecessors:** `claude-measure-twice` (EF-specific) → ships as separate companion plugin `chameleon-ef-pack`
> **Versions:** v1 archived at `ARCHITECTURE-v1.md`. v2 archived at `ARCHITECTURE-v2.md`.

---

## What changed from v2 (Round 2 changelog)

### Strategic scope shift (load-bearing)

**v2 claimed**: "works on any TypeScript/JavaScript repo in v1; expand to Ruby/Python in v1.5"

**v3 says**: chameleon does **best-effort pattern clustering** on whatever AST it can parse. Where AST + statistics produce clean archetypes (file shape, naming, imports, structural conventions): excellent. Where they don't (NestJS decorators, tRPC builder chains, type-level discriminants, runtime-driven semantics): the engine falls back to interview + `/chameleon-refine` to capture the un-inferable. **Chameleon learns YOUR repo's patterns, not TYPES of repos.**

This dissolves the "two-cell toy" critique on `content_signal`: we don't claim to handle every framework's archetypes via a matcher DSL. We acknowledge the boundary honestly: AST-derivable vs human-curated, with a single coherent fallback path (idioms.md).

### BLOCKING issues addressed

1. ❌ Dropped dual-format `additionalContext` emission → ✅ **Single-format per platform** (mirrors `superpowers/hooks/session-start` exactly; eliminates double-prime hazard)
2. ❌ Dropped blocking trust prompt → ✅ **Trust as primer warning** (non-blocking; user explicitly approves via `/chameleon-trust` if desired)
3. ❌ Dropped silent pack version mismatch → ✅ **`engine_min_version` and `engine_max_version` in pack manifest** (loader rejects out-of-range)
4. ❌ Dropped `<CHAMELEON_IMPORTANT>` framing → ✅ **Neutral `<chameleon-context>` tag** (no framing competition with superpowers)
5. ❌ Dropped statistical-mode-wins clustering → ✅ **Recency-weighted clustering** (last 90 days = 2× cluster vote; defeats archive-majority repos)
6. ❌ Dropped test-eligible canonicals → ✅ **Canonical exclusion paths** (`__tests__/`, `legacy/`, `archive/`, `deprecated/` excluded from canonical selection)
7. ❌ Dropped workspace-collapse bootstrap → ✅ **Per-workspace detection** (pnpm/yarn/lerna/turbo workspace files trigger per-workspace `.chameleon/` profiles)
8. ❌ Dropped silent plugin-prettierrc drop → ✅ **Plugin detection warning** (when `.prettierrc` references JS plugins, warn user that those rules are invisible)
9. ❌ Dropped binary bimodal forcing → ✅ **Tertiary state: "team accepts both, prefer A for new code"** (or "route-dependent" with subtree split)

### SIGNIFICANT items addressed

**Cost honesty:**
- Dropped "$0.30-0.50 typical session ceiling" → **honest tiered pricing** ($0.30-0.50 single-repo, $1-4 multi-repo, $7+ first-bootstrap-tRPC, $2-5/session for 50+ repo consultants)
- Added explicit 30-turn-session assumption to all dollar claims
- Added cold-start row in cost table
- Added output-dominance row in cost table
- Added pricing-volatility hedge ("all dollar amounts assume Sonnet 4.6 at 2026-05 rates")
- Added total-hooks-per-turn cap (≤2,000 tokens, sum of all 4 hooks)
- Added `--paths` glob post-count enforcement (50k cap applies even with explicit globs)
- Added cache_control discipline section (lstat results, HMAC log, posttool exit codes never in cached prefix)

**Pattern detection (scope honest):**
- Documented "best-effort engine, not framework-aware" framing
- Multi-canonical archetypes supported: `canonicals: [array]`, picked by similarity
- Confidence threshold function specified (function of cluster_purity + cluster_size + recency_weight)
- Documented detect-secrets/gitleaks rule policy (vendored at known version, quarterly bump cadence)

**Maintainability:**
- Added "Versioning & Compatibility" section (engine vN supports schemas vN-1, vN-2; refuses older with migration prompt)
- Added schema migration directory: `mcp/chameleon_mcp/profile/migrations/`
- Added ADR directory: `docs/chameleon/decisions/`
- Added MAINTAINER.md outline (key rotation, dep upgrade cadence, schema migration runbook)
- Added lock files mandate (`package-lock.json`, `uv.lock` committed)
- Added TS Compiler API vendor strategy (pinned in `mcp/node_modules/typescript`, quarterly bump)
- Added FastMCP version pin policy
- Added pack signing key rotation policy (annual; signing infrastructure documented)
- Added `--allow-unsigned` as session-only flag (never persisted)
- Added pack ID namespacing: `<publisher>/<pack>`
- Added staleness in primer (`days_since_refresh`, refresh nag at >90 days)
- Added canonical lookup via AST query + sha hint (not strict sha match)
- Added idiom deprecation tracking in `idioms.md` schema
- Added CI checks for skill RED-GREEN-REFACTOR cycles + acceptance test

---

## Purpose

A Claude Code plugin that gives the AI deep understanding of YOUR repo's conventions — not a list of pre-known framework patterns, but the patterns you actually wrote.

The engine clusters AST + statistical signals from your code, asks targeted questions about what it cannot infer, and iterates via post-edit feedback. Over time, the profile becomes a living artifact that captures your team's actual coding style.

**Target outcome:** measurable reduction in reviewer comments on file shape / naming / idiom usage on AI-generated code, validated against baseline transcripts collected during dogfooding (see Real Problem Evidence section).

---

## Real Problem Evidence

> **⚠️ This section requires evidence from EF dogfooding to be filled before v1.0 release. Documented as a CI gate.**

### Working hypothesis

AI-generated code in established codebases routinely violates local conventions in ways that cost reviewer time but don't affect correctness. Hypothesis is supported by:
- Active development of `claude-measure-twice` (predecessor) as one team's response to observed friction
- The `CLAUDE.md` convention adoption rate across Claude Code users (~all serious projects ship one)
- Anecdotal reports from author's day-to-day work at Empire Flippers

### Evidence required before v1.0 release

- 5+ concrete transcripts of Claude (without chameleon active) writing off-pattern code in real EF api/client repos
- Per transcript: what was generated, what reviewer flagged, time-to-fix, what the convention-correct version looked like
- Quantified cost of rework (review cycles, fix commits)

**Owner:** Cris (human partner). **Deadline:** before v1.0.0 semver tag.

**Enforcement:** CI release-tag check — `tag-v*.*.*` requires `docs/chameleon/REAL-PROBLEM-EVIDENCE.md` to contain ≥5 H2 sections matching the transcript schema. Build fails otherwise.

If insufficient evidence emerges during dogfooding, the plugin's value proposition must be revisited honestly rather than shipped on speculation.

---

## Goals

1. **Best-effort pattern clustering** on any TS repo — not framework-aware, not "supported list"
2. **Single install, multi-repo** — install once; plugin handles every TS repo on the dev's machine
3. **Auto-onboarding** — first time in a new TS repo, plugin offers to bootstrap a profile via explicit `/chameleon-init` (no auto-trigger)
4. **Co-existence** — works alongside superpowers and any other Claude Code plugin without framing competition or double-priming
5. **Companion plugin packs** — teams ship hand-curated profiles as separate plugins (engine remains generic)
6. **Honest cost model** — bootstrap acceptable high (one-time investment), steady-state $0.30-0.50/single-repo session, multi-repo and refactor-marathon explicitly higher
7. **Skill discipline** — every skill has a baseline + rationalization table per `superpowers:writing-skills`; no skill ships without a failing test first
8. **Graceful boundaries** — where AST falls short, the engine asks (interview) and iterates (`/chameleon-refine`); no claim of "supports framework X"

---

## Plugin name: `chameleon`

> *Tagline: "Code that blends in."*

**Conventions:**
- Plugin/repo name: `chameleon` (no `claude-` prefix)
- Slash command prefix: `/chameleon-*` (with `/cham-*` short alias)
- Skill prefix: `chameleon-*`
- Foundation skill: `using-chameleon`
- Context tag: `<chameleon-context>` (NEUTRAL — no `<EXTREMELY_IMPORTANT>` or `<CHAMELEON_IMPORTANT>` framing)
- MCP server: `chameleon-mcp`
- Python package: `chameleon_mcp`
- Profile dir: `.chameleon/`
- Env var prefix: `CHAMELEON_*`

---

## Core principles

1. **Foundation generic, brain per-repo.** Engine ships with no repo-specific knowledge.
2. **Best-effort, not framework-aware.** No claim that any framework is "supported." The engine clusters what AST can express; what it can't, the user provides via interview + refine.
3. **Profile is a portable artifact.** Committed JSON + Markdown, reviewable in PRs.
4. **Two-tier dimensions.** Auto-derivable (AST + statistical + recency-weighted) vs hand-curated (`idioms.md`).
5. **Discovery before action.** Every code edit injects archetype context before the model writes — via MCP-driven dispatch.
6. **Inject context, don't deny.** Only the security layer hard-denies; conformance is advisory.
7. **Plugin coexistence first-class.** Single-format JSON dispatch, neutral context tags, parallel-hook-aware design.
8. **Honest scoping.** v1 = TypeScript only, v1 = Claude Code only. Prove the loop before expanding.
9. **Skills as code.** No skill ships without a failing test first (Iron Law).
10. **Long-term maintainability.** Lock files, version pins, schema migrations, ADRs from day one.

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
│  │  → SINGLE-FORMAT JSON    │     │ Slash commands (5):              │ │
│  │    DISPATCH per platform │     │  chameleon-init                  │ │
│  │  → inject using-chameleon│     │  chameleon-refresh               │ │
│  │    + profile primer      │     │  chameleon-status                │ │
│  │  + staleness footer      │     │  chameleon-refine                │ │
│  │  in <chameleon-context>  │     │  chameleon-apply-pack            │ │
│  │ PreToolUse Edit/Write    │     │                                  │ │
│  │  → preflight-and-advise  │     │ Optional: /chameleon-trust       │ │
│  │   (combined: safety      │     │  (per-repo committed profile     │ │
│  │    + lstat + MCP excerpt)│     │   approval)                      │ │
│  │ PostToolUse Bash         │     │                                  │ │
│  │  → posttool-recorder     │     │ Short aliases: /cham-*           │ │
│  │  (HMAC log, BUG FIXED:   │     │                                  │ │
│  │   ${TMPDIR:-/tmp})       │     │                                  │ │
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
│  │  apply_profile_pack     refine_profile      trust_profile        │  │
│  │  (every file-reading tool: lstat first, refuse symlinks)         │  │
│  └─────────────┬───────────────────────────────┬───────────────────┘  │
│                │                               │                      │
│                ▼                               ▼                      │
│  ┌──────────────────────────────┐  ┌─────────────────────────────────┐│
│  │ Profile storage              │  │ Bootstrap engine                ││
│  │ ────────────────             │  │ ────────────────                ││
│  │ Committed (team-shared):     │  │ 1. Detect language (TS only v1) ││
│  │  <repo>/.chameleon/          │  │ 2. WORKSPACE DETECTION:         ││
│  │   profile.json (manifest)    │  │    pnpm-workspace.yaml,         ││
│  │   archetypes.json            │  │    yarn workspaces, lerna.json, ││
│  │   rules.json                 │  │    turbo.json, nx.json          ││
│  │   canonicals.json            │  │    → per-workspace .chameleon/  ││
│  │   idioms.md                  │  │ 3. AST scan + path cluster      ││
│  │   profile.summary.md  (PRs)  │  │    + RECENCY WEIGHT (90d=2x)    ││
│  │                              │  │ 4. Tool config = ground truth   ││
│  │ Local-only (per-user):       │  │    (with plugin-detection warn) ││
│  │  ${PLUGIN_DATA}/             │  │ 5. EXCLUDE generated, vendor,   ││
│  │   <repo_id>/                 │  │    legacy/, archive/,           ││
│  │    drift.db                  │  │    __tests__/ from canonicals   ││
│  │    cache.json                │  │ 6. Statistical pattern extract  ││
│  │    .trust                    │  │ 7. Bimodal/sparse surfacing     ││
│  │                              │  │ 8. Secret scan (vendored rules) ││
│  │                              │  │ 9. ≤3 user-facing prompts       ││
│  └──────────────────────────────┘  └─────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│              AST extractor (TypeScript only in v1)                       │
│  Single language: TS Compiler API via subprocess                         │
│  TypeScript pinned in mcp/node_modules/typescript (vendored)             │
│  Quarterly version bump cadence documented in MAINTAINER.md              │
│  v1.5 expansion: Ruby (Prism), Python (libcst)                           │
│                                                                          │
│  Subprocess limits per file: 5s CPU, 512 MB RSS, 1 MB file ceiling       │
│  Inode-based file dedup (catches hardlinks, not just symlinks)           │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│              Companion plugin pattern (EF + others)                      │
│  ────────────────────────────────────────────────────────────────────    │
│  Pre-built profile packs ship as SEPARATE Claude Code plugins:           │
│   - chameleon-ef-pack       (EF api + client; PRIMARY DOGFOOD CASE)      │
│                                                                          │
│  Pack manifest REQUIRED fields:                                          │
│   - publisher (namespace: <publisher>/<pack>)                            │
│   - engine_min_version, engine_max_version (compat range)                │
│   - signed (Sigstore/cosign or ed25519)                                  │
│                                                                          │
│  Loader rejects: out-of-range engine versions, unsigned w/o flag,        │
│                  packs containing executables (data-only contract)       │
│                                                                          │
│  Match strategy: git remote URL exact → publisher manifest signature →   │
│                  user explicit /chameleon-apply-pack <publisher/pack>    │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Plugin structure (v1, Claude Code only, with maintenance scaffolding)

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
├── package-lock.json            # MUST commit (Round 2 maintainability)
├── hooks/
│   ├── hooks.json
│   ├── run-hook.cmd             # cross-platform polyglot wrapper
│   ├── session-start            # SessionStart: SINGLE-FORMAT dispatch
│   ├── preflight-and-advise     # PreToolUse: safety + lstat + MCP injection
│   ├── posttool-recorder        # PostToolUse Bash: HMAC log (BUG FIXED)
│   └── callout-detector         # UserPromptSubmit: frustration phrase reminder
├── skills/
│   ├── using-chameleon/
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
│   ├── uv.lock                  # MUST commit (Round 2 maintainability)
│   ├── chameleon_mcp/
│   │   ├── server.py            # FastMCP entry (version pinned)
│   │   ├── tools/               # MCP tools (with lstat checks)
│   │   ├── extractors/
│   │   │   ├── _base.py
│   │   │   └── typescript.py    # via vendored TS Compiler subprocess
│   │   ├── bootstrap/
│   │   ├── profile/
│   │   │   ├── schema.py        # JSON schema validators
│   │   │   ├── migrations/      # SCHEMA MIGRATION SCRIPTS
│   │   │   │   ├── v1_to_v2.py  # template (no migrations needed yet)
│   │   │   │   └── README.md    # migration policy + window
│   │   │   └── secret_scanner.py # vendored detect-secrets rules
│   │   ├── packs/               # companion-plugin pack discovery + signing
│   │   └── drift/               # mtime/sha tracking (local-only)
│   └── node_modules/             # VENDORED (TypeScript pinned)
│       └── typescript/          # specific version, quarterly bump
├── scripts/
│   ├── ts_dump.mjs              # TS Compiler API subprocess
│   ├── bump-version.sh
│   └── secret-scan.sh           # detect-secrets wrapper
├── tests/
│   ├── skill-triggering/        # behavioral evals (CI-enforced)
│   │   ├── prompts/
│   │   ├── run-all.sh           # CI: fails if any skill lacks baseline.md
│   │   └── run-test.sh
│   ├── unit/
│   ├── integration/
│   ├── corpus/                  # benchmark TS repos
│   └── acceptance/              # ACCEPTANCE TEST REPO (committed)
│       ├── README.md            # how to reproduce the test
│       └── golden-transcript.md # expected output
├── docs/
│   └── chameleon/
│       ├── specs/
│       ├── plans/
│       ├── reference/
│       ├── decisions/           # ADR DIRECTORY (Round 2 add)
│       │   ├── 0001-best-effort-vs-framework-aware.md
│       │   ├── 0002-companion-plugin-vs-bundled.md
│       │   ├── 0003-ts-only-v1-scope.md
│       │   └── ...
│       ├── MAINTAINER.md        # KEY ROTATION + DEP CADENCE + MIGRATION RUNBOOK
│       ├── REAL-PROBLEM-EVIDENCE.md  # CI-gated transcripts before v1.0
│       └── ROUND-1-REVIEWS.md, ROUND-2-REVIEWS.md (review history)
└── assets/
```

**Note:** Multi-harness directories (`.codex-plugin/`, `.cursor-plugin/`, `.opencode/`, `gemini-extension.json`, `GEMINI.md`) are **NOT** in v1.

**Note:** `packs/empire-flippers-*/` is **NOT** in this repo. EF profiles ship as separate plugin `chameleon-ef-pack`.

---

## Bootstrap mechanism (Round 2 BLOCKING fixes applied)

```
SessionStart hook fires (matcher: startup|clear|compact)
  → run-hook.cmd session-start
  → bash script:
      1. Read skills/using-chameleon/SKILL.md
      2. Detect active repo (file-path walk-up if available, else cwd)
      3. Detect language → if not TS, suppress primer (graceful degradation)
      4. Check profile state:
           - <repo>/.chameleon/profile.json present?  → load summary
           - per-user cache populated for repo_id?     → load summary
           - companion plugin profile pack match?      → suggest /chameleon-apply-pack
           - none?                                      → suggest /chameleon-init
      5. Check trust state:
           - profile is committed and ${PLUGIN_DATA}/<repo_id>/.trust missing?
              → mark as UNTRUSTED in primer (non-blocking warning, NOT [y/N] prompt)
              → user runs /chameleon-trust to approve (writes .trust file)
      6. SINGLE-FORMAT JSON DISPATCH (per platform):
           - if CURSOR_PLUGIN_ROOT set → emit { "additional_context": "..." }
           - elif CLAUDE_PLUGIN_ROOT && !COPILOT_CLI → emit { "hookSpecificOutput": ... }
           - else → emit { "additionalContext": "..." }
           NEVER emit both. (Mirrors superpowers/hooks/session-start verbatim.)
      7. Wrap content in <chameleon-context> tags (NEUTRAL — no importance framing)
      8. Content: using-chameleon SKILL.md + repo+profile primer + staleness footer
         (~1,500 tokens budget; see Cost model)
```

**Note on `resume` matcher:** Deliberately omitted (matches superpowers approach).

**Note on graceful degradation:** Non-TS repos get **complete primer suppression** — no "you don't have a profile" nag. Plugin is silent.

**Note on trust model:**
- Committed `.chameleon/profile.json` is data, but the engine treats it as **untrusted-by-default per repo per user**
- First time encountered: primer warns "Untrusted profile in this repo. Run `/chameleon-trust` to approve."
- Trust state stored at `${PLUGIN_DATA}/<repo_id>/.trust` (per-user, never committed)
- This is **non-blocking** — Claude can still respond to user; profile guidance is just suppressed until trust granted
- Eliminates the deadlock with superpowers' "invoke skill before any response" mandate

---

## Hook stack (Round 2 cost + parallelism fixes)

```
SessionStart (matcher: startup|clear|compact):
  1. session-start
       Inject using-chameleon + repo profile primer + staleness footer
       Budget: ~1,200-1,800 tokens
       Output uses cache_control breakpoint (pinned for session)

PreToolUse (matcher: Edit|Write|NotebookEdit):
  1. preflight-and-advise (SINGLE COMBINED HOOK)
       a. Safety hard-denies (path traversal, secrets, lockfiles, vendored, generated)
          (Inherited logic from claude-measure-twice — REVIEWED, NOT verbatim;
           HMAC path bug FIXED: ${TMPDIR:-/tmp}/.chameleon_exec_log/ used consistently;
           inherited preflight is 1001 lines, not 556 as v1 claimed)
       b. lstat check on file_path (refuses symlinks — TOCTOU mitigation)
       c. If safety passes AND profile is trusted: synchronously call
          chameleon-mcp::get_canonical_excerpt for the file's archetype
          Inject 500-800 token annotated excerpt
       d. Per-edit injection cap: 1,500 tokens max (truncated)
       e. Cache_control: lstat output + advisor output flow as ephemeral input
          (NEVER in cached prefix)

PostToolUse (matcher: Bash):
  1. posttool-recorder
       HMAC-signed exit code log
       BUG FIX: writes AND reads use ${TMPDIR:-/tmp} consistently
       Key handling fail-loud: error explicitly if /dev/urandom fails
       Output: ~50 tokens (HMAC line)
       Cache_control: ephemeral input (HMAC varies per turn)

UserPromptSubmit:
  1. callout-detector
       Frustration phrase → rule-update-first reminder
       Output: ~200 tokens on match, 0 otherwise

TOTAL-HOOKS-PER-TURN CAP: ≤2,000 tokens summed across all hooks (truncated)
```

**Why combined `preflight-and-advise` (not two separate hooks):** Hooks on shared matchers run in **parallel** per Claude Code platform. v1's two-hook design assumed sequential ordering — that's not enforceable. Combining into one synchronous command hook ensures safety check completes before MCP call. (Note: if Anthropic adds hook ordering controls in a future version, this combined hook becomes legacy; ADR `0004-combined-hook-vs-ordered.md` documents the decision.)

---

## Skill design

### Foundation skill: `using-chameleon`

```yaml
---
name: using-chameleon
description: Use when starting any conversation in a TypeScript repo with a chameleon profile present, before any Edit, Write, or NotebookEdit operation
---
```

**Description rules followed (per `superpowers:writing-skills`):**
- "Use when..." third-person ✓
- Triggering conditions only — no workflow summary ✓
- Under 1024 chars ✓
- Specific symptoms ✓

**Body sections:**
- `<chameleon-context>` block (NOT `<EXTREMELY-IMPORTANT>` — neutral)
- `<SUBAGENT-STOP>` block: subagents skip
- The Rule: invoke `chameleon-mcp::detect_repo` + `get_canonical_excerpt` BEFORE editing in profiled repos
- Process flowchart (graphviz `dot`)
- Red Flags table (rationalizations to capture during baseline testing — see Skill test plan)
- Available slash commands (5 user-facing + `/chameleon-trust`)
- Profile state interpretation (trusted vs untrusted)
- Coordination with superpowers: "After `using-superpowers` triggers `brainstorming`, but before any Edit/Write" (explicit priority)

### User-invokable skills (5 commands + 1 trust)

| Skill | Slash command | Short alias | Purpose |
|---|---|---|---|
| `chameleon-init` | `/chameleon-init` | `/cham-init` | Bootstrap a new repo profile (3-prompt interview) |
| `chameleon-refresh` | `/chameleon-refresh` | `/cham-refresh` | Re-analyze repo, detect drift, update profile |
| `chameleon-status` | `/chameleon-status` | `/cham-status` | Show current profile, drift, plugin health |
| `chameleon-refine` | `/chameleon-refine` | `/cham-refine` | Iterate on profile based on observed misses; **owns idioms.md collection** |
| `chameleon-apply-pack` | `/chameleon-apply-pack <publisher/pack>` | `/cham-apply-pack` | Apply a companion-plugin profile pack |
| `chameleon-trust` | `/chameleon-trust` | `/cham-trust` | Approve a committed profile for this user (writes per-user `.trust` file) |

(Implemented as Skills with `disable-model-invocation: true`.)

**No dynamic archetype skills.** Replaced with MCP-driven dispatch (rationale documented in v2 changelog and ADR `0005-mcp-dispatch-vs-dynamic-skills.md`).

---

## Skill test plan

> **Iron Law from `superpowers:writing-skills`:** "NO SKILL WITHOUT A FAILING TEST FIRST."

**CI enforcement:** `tests/skill-triggering/run-all.sh` fails if any `skills/<name>/` lacks a `tests/baseline.md` file with documented rationalizations. PR cannot merge with missing baseline.

### `using-chameleon` test plan

**RED (baseline scenarios):**
- Pressure scenario 1: TS repo with profile; user says "just add this small one-line fix"
- Pressure scenario 2: TS repo with profile; user says "I know the pattern, skip the MCP call"
- Pressure scenario 3: TS repo with profile; user is rushing ("dinner in 5 min")
- Pressure scenario 4: TS repo without profile; agent invents pattern instead of suggesting `/chameleon-init`
- Pressure scenario 5: profile is UNTRUSTED; agent must remind user about `/chameleon-trust` and use canonical guidance only after approval
- Pressure scenarios 6+: combined pressures (time + sunk cost + authority)

**Rationalizations to capture verbatim:** TBD during baseline run.

### Skill test plans for chameleon-init, refresh, refine, status, apply-pack, trust

(Documented per skill in their respective `tests/baseline.md` files. Each must capture:
- Pressure scenarios that cover the skill's discipline-enforcing aspects
- Verbatim rationalizations from baseline runs
- Closed loopholes after refactoring)

---

## Bootstrap acceptance test

> **Acceptance test for chameleon bootstrap.** Open a clean Claude Code session in `tests/acceptance/` (a committed test repo containing a `.chameleon/profile.json` with at least one archetype). Send exactly:
>
> > `Add a new endpoint at /api/v1/widgets that returns a list of widgets.`
>
> A working integration:
> 1. SessionStart hook fires; `using-chameleon` is injected as `additionalContext`
> 2. Before generating any code, the agent invokes `chameleon-mcp::detect_repo` and `get_canonical_excerpt`
> 3. The agent's first edit follows the canonical excerpt's pattern
>
> If the agent writes code without first invoking the MCP tools, the integration is broken.

**The acceptance test must also be run with superpowers active** — both `using-superpowers` and `using-chameleon` must coexist correctly. Both `brainstorming` (process) and chameleon's pattern conformance (output shape) must apply. Test transcripts captured in `tests/acceptance/golden-transcript.md`.

**CI enforcement:** Release tags require updated `golden-transcript.md` matching expected output.

---

## MCP server (`chameleon-mcp`)

FastMCP-based, stdio transport (NEVER exposed over network).

| Tool | Input | Output | Security note |
|---|---|---|---|
| `detect_repo` | file_path | repo_id, profile_status, trust_state | repo_id is sha256; trust_state new in v3 |
| `get_archetype` | repo, file_path | archetype name + content_signal match, alternatives | content_signal stays simple (no DSL expansion) |
| `get_canonical_excerpt` | repo, archetype | annotated excerpt (500-800 tokens) | **lstat check; AST-query lookup with sha hint, not strict sha match** |
| `get_rules` | repo, archetype? | rules + citations | — |
| `lint_file` | repo, archetype, content | AST violations list | content size capped at 100 KB |
| `get_drift_status` | repo | freshness + recommended action + days_since_refresh | reads from local-only drift.db |
| `refresh_repo` | repo, force | re-analyze | rate-limited 1/hour per repo |
| `bootstrap_repo` | path, mode, paths_glob? | first-time analysis | repo size guard (post-glob 50k cap) |
| `list_profiles` | — | all known repos | — |
| `apply_profile_pack` | repo, pack_id (publisher/pack) | install pre-built profile | strict signature + version range validation |
| `refine_profile` | repo, feedback | apply user-driven correction | feedback sanitization |
| `trust_profile` | repo | mark profile as trusted | writes per-user `.trust` file |

**Cache_control discipline:** lstat output, drift.db queries, HMAC log entries, posttool exit codes, dynamic timestamps — all flow as ephemeral input. **Never in cached prefix.**

---

## TypeScript-first extractor (v1 single language, vendored)

v1 ships TypeScript only via TS Compiler API subprocess.

**Vendoring strategy:**
- TypeScript pinned at specific version in `mcp/node_modules/typescript`
- Quarterly bump cadence documented in `MAINTAINER.md`
- Lock file (`package-lock.json`) committed
- Subprocess uses vendored binary, not user's system TypeScript

**v1.5 expansion plan:** Ruby (Prism) + Python (libcst), each with own vendored toolchain
**v2.0 expansion plan:** Go, Rust, PHP, Java

**Subprocess limits per file parse:**
- 5s CPU
- 512 MB RSS
- 1 MB file size ceiling
- Inode-based file dedup (catches hardlinks)
- Reject files matching generated-code signals

**Tool config files as ground truth (Round 2 fix):**
- Bootstrap reads `.prettierrc`, `tsconfig.json`, `.eslintrc*`, `.editorconfig`
- **Plugin detection warning:** if `.prettierrc` references JS plugins (`{"plugins": [...]}`) or `.eslintrc` extends from JS plugins, warn user that plugin-specific rules are invisible
- Per-workspace tool config scoped to workspace subtree (not collapsed to root)
- Bootstrap reports: "rules sourced from .prettierrc + AST stats (3 plugin rules invisible — see warnings)"

---

## Profile schema

```
.chameleon/   (committed, team-shared)
  ├── profile.json         # manifest (version, created_at, source, engine_version)
  ├── archetypes.json      # path patterns + content_signal → archetype + cluster_size + outliers
  ├── rules.json           # per-archetype rules + citations
  ├── canonicals.json      # chosen reference files + AST query + sha hint + secret_scan_passed
  ├── idioms.md            # human-curated, with deprecation tracking
  └── profile.summary.md   # human-readable for PR review

${CLAUDE_PLUGIN_DATA}/<repo_id>/   (local-only, NEVER committed)
  ├── drift.db             # sqlite, GC'd weekly, 30-day record purge
  ├── cache.json           # per-user runtime cache
  └── .trust               # per-user profile approval marker
```

**`.gitignore` automation on `chameleon-init`:**
```
.chameleon/.tmp/
```

(drift.db and .trust are in `${PLUGIN_DATA}`, not in repo, so don't need `.gitignore`.)

`archetypes.json` shape (Round 2 multi-canonical support):

```json
{
  "schema_version": 3,
  "engine_min_version": "1.0",
  "archetypes": {
    "next-server-component": {
      "paths": ["app/**/*.tsx"],
      "content_signal": {
        "absent_directives": ["use client", "use server"]
      },
      "canonicals": [
        {
          "path": "app/dashboard/page.tsx",
          "ast_query": "ExportNamedDeclaration > FunctionDeclaration[name='Page']",
          "sha_hint": "abc123...",
          "secret_scan_passed": true,
          "scanned_at": "2026-05-10T..."
        },
        {
          "path": "app/products/page.tsx",
          "ast_query": "...",
          "sha_hint": "def456...",
          "secret_scan_passed": true,
          "scanned_at": "2026-05-10T..."
        }
      ],
      "confidence": "high",
      "confidence_function": "cluster_purity * 0.4 + recency_weight * 0.3 + cluster_size_log * 0.3",
      "cluster_size": 23,
      "outlier_paths": ["app/legacy-dashboard/page.tsx"],
      "recency_weight": 0.85,
      "source": "bootstrap",
      "scope": "apps/web"
    }
  }
}
```

**Schema highlights (Round 2 fixes):**
- **Multi-canonical** (`canonicals: [array]`) — teams have multiple idiomatic ways
- **AST query lookup** — get_canonical_excerpt finds the canonical pattern via AST query against current file head, with sha as hint not requirement (defends against legitimate refactors)
- **Recency weight** — exposes the 90-day-2x-vote multiplier
- **Confidence function specified** — explicit formula (not unfalsifiable)
- **Schema versioning** — `schema_version` + `engine_min_version` (loader rejects out-of-range)
- **Workspace scope** — `scope: "apps/web"` for per-workspace archetypes

`idioms.md` schema (Round 2 deprecation tracking):

```markdown
# idioms

## active

### use-custom-query
Status: active (added 2026-05-10)
Use `useCustomQuery` from `@/hooks/useCustomQuery`, not `useQuery` directly.
Reason: shared error handling and retry logic.

## deprecated

### use-query-direct
Status: deprecated 2026-05-10 (replaced by use-custom-query)
Reason: bypasses our shared error handling.
Migration: replace `useQuery(...)` with `useCustomQuery(...)`.
```

---

## Companion plugin pattern (BLOCKING fixes applied)

Pre-built profiles ship as **separate Claude Code plugins** with strict version compat:

```
chameleon-ef-pack/                           (separate plugin, separate repo)
├── .claude-plugin/plugin.json
├── pack.manifest.json                       # NEW v3: pack metadata
└── packs/
    ├── empire-flippers/api/                 # publisher/pack-name namespace
    │   ├── manifest.json
    │   ├── archetypes.json
    │   ├── rules.json
    │   ├── canonicals.json
    │   └── idioms.md
    └── empire-flippers/client/
        └── ...
```

**`pack.manifest.json` (REQUIRED fields):**
```json
{
  "publisher": "empire-flippers",
  "pack_id": "api",
  "engine_min_version": "1.0",
  "engine_max_version": "1.x",
  "schema_version": 3,
  "signed": true,
  "signature": "...sigstore-or-ed25519..."
}
```

**Loader contracts:**
- Pack ID is namespaced: `<publisher>/<pack>` (prevents typo-squat)
- Engine version range enforced (loader rejects out-of-range)
- Signature required (unsigned packs need session-only `--allow-unsigned` flag, NEVER persisted)
- Pack directories are DATA-ONLY (loader rejects packs containing `*.sh`, `*.py`, `*.js`, `*.cmd`, `*.exe`, `*.bin`)
- Max companion packs per session: 10 (prevents quintuple-priming)

**Match strategy (priority order):**
1. Git remote URL exact match → resolve via signed publisher manifest
2. User explicit assignment via `/chameleon-apply-pack <publisher/pack>`
3. Signature heuristics (Gemfile/package.json deps + characteristic file paths) — only as suggestion, not auto-apply

**EF migration path:**
- Existing `claude-measure-twice` profiles → migrated as `chameleon-ef-pack` (separate Bitbucket repo)
- Plugin lives in EF's private Bitbucket
- Auto-installs via `claude plugin install chameleon-ef-pack@empire-flippers-marketplace`
- chameleon engine remains generic and public-shippable
- **PRIMARY DOGFOOD CASE for v1.0** — EF api + EF client are the first repos to validate the engine

---

## Bootstrap interview flow (Round 2 silently-wrong fixes applied)

**≤3 user-facing prompts.**

```
1. User runs /chameleon-init in a TS repo (no auto-trigger)

2. Engine (no user prompts):
   a. Detect language → TS confirmed
   b. Detect workspace structure (pnpm-workspace.yaml, yarn workspaces, lerna.json,
      turbo.json, nx.json) → if found, ask user: per-workspace bootstrap or root?
   c. Read tool config files (per-workspace if applicable):
      .prettierrc, tsconfig.json, .eslintrc, .editorconfig
      WARNING: if .prettierrc references JS plugins, flag for user
   d. AST scan repo:
        - <500 files: full pass
        - 500-50,000: stratified sample (top-N by directory frequency, recent commits)
        - >50,000: refuse without explicit globs
        - WITH globs: still enforce 50k cap on post-glob count
   e. Inode-dedup file list (catches hardlinks)
   f. Exclude generated code, vendor/, node_modules/, dist/, __generated__/
      AND exclude from CANONICAL SELECTION: __tests__/, test/, legacy/, archive/, deprecated/, _archive/, .archive/
   g. Statistical pattern extraction with RECENCY WEIGHTING (last 90 days = 2× vote)
   h. Cluster files by content_signal + path → archetype proposals
   i. Bimodal/sparse surfacing
   j. Secret scan canonical excerpts (vendored detect-secrets rules)

3. PROMPT 1 (archetype confirmation):
   "Detected 8 archetypes:
    - next-server-component (high confidence, 23 files): app/dashboard/page.tsx
    - next-client-component (high, 18): app/components/SearchBar.tsx
    - [...]

    Excluded from canonical pool: 47 test files, 12 archived files, 3 deprecated files
    (see .chameleon/.skipped.log)

    Tool config: rules sourced from .prettierrc + tsconfig.json + AST stats.
    WARNING: .prettierrc references prettier-plugin-tailwindcss; plugin rules invisible.

    Apply as proposed? [Y/n/edit]"

4. PROMPT 2 (bimodal/sparse resolution if any):
   "For half-migrated-component, which is canonical for new code?
    A) ApolloClient.query() (14 files, last edited avg 200 days ago)
    B) useQuery hook (9 files, last edited avg 30 days ago)
    C) Both — route-dependent (split per subtree)
    D) Both — team accepts both, prefer B for new"

5. PROMPT 3 (save destination):
   "Save profile to <repo>/.chameleon/ (committed, team-shared) or per-user cache?
    [committed/private]"

6. Engine writes profile artifacts + .gitignore additions + profile.summary.md
   Reports: "Profile ready. 8 archetypes, 14 rules, 0 idioms (run /chameleon-refine to add).
            Cost: $X.XX. Drift tracking enabled. Run /chameleon-trust to approve for this user."

7. Idioms collection deferred to /chameleon-refine.
```

**Cost estimate per bootstrap (revised again):**
- AST scan + tool config read: 0 Claude tokens (local)
- Sampling reads: ~50-100 files × ~500 tokens = ~25-50k tokens input
- Secret scanner: 0 Claude tokens (local detect-secrets, vendored rules)
- 3 user-facing prompts × ~3k tokens = ~9k tokens
- Profile generation output: ~5-10k tokens
- **Total: $0.50–2.00 per repo (one-time)** for typical TS repos
- **$3–7 per repo** for tRPC-heavy or large monorepo cases (Round 2 honest acknowledgment)

---

## Multi-repo handling

- Profile keyed by `repo_id = sha256(git_remote_url || abs_path_canonical_normalized)`
- `abs_path_canonical_normalized` uses Unicode NFC normalization (cross-platform consistency)
- Storage:
  - In-repo: `<repo>/.chameleon/...` (preferred default — team shares)
  - Per-user: `${CLAUDE_PLUGIN_DATA}/<repo_id>/` (drift.db + cache.json + .trust)
- Detection: file-path walk-up on each tool call
- Drift tracking: per-repo sqlite, GC'd weekly (records older than 30 days purged)
- Submodule semantics: file-path walk-up stops at the **innermost `.git` boundary** (submodule has its own profile separate from outer repo)

**Multi-repo cost scaling (Round 2 honest tier additions):**

| Open repos in session | Prime cost | Cache behavior | Realistic session cost |
|---|---|---|---|
| 1 (single-repo) | ~1,500 tokens once | Warm; standard | $0.30–0.50 |
| 5 | ~7,500 tokens | Each switch breaks prefix | $0.60–1.00 |
| 20 | ~30,000 tokens | Heavy thrashing | $0.80–1.20 |
| **50–80 (consultant tier)** | **~75,000–120,000** | **Constant cache miss** | **$2–5** |
| 100+ (extreme) | $5+ | Effectively cache-cold | $5+ |

The consultant/freelancer tier is **explicitly outside the $50/month ceiling claim** for typical users. Document this prominently.

---

## Plugin coexistence (Round 2 BLOCKING fixes)

**Hygiene rules:**
- Slash commands namespaced: `/chameleon-*` (with `/cham-*` aliases)
- Env vars namespaced: `CHAMELEON_*`
- Hooks: parallel-aware design
- Inject context, don't deny
- Token budget discipline: ~1,500 prime + ≤2,000 total-hooks-per-turn cap
- Distinct MCP server (`chameleon-mcp`)
- Per-repo opt-out: `.chameleon/.skip` file
- Global opt-out: `CHAMELEON_DISABLE=1` env

**Context tag (CRITICAL FIX):**
- Use `<chameleon-context>` (NEUTRAL — no importance framing)
- NOT `<EXTREMELY_IMPORTANT>` (collides with superpowers' framing)
- NOT `<CHAMELEON_IMPORTANT>` (still competes for "this is the most important thing")

**SessionStart JSON dispatch (CRITICAL FIX):**
- Mirror `superpowers/hooks/session-start` lines 41-55 exactly
- Detect platform via env vars (`CURSOR_PLUGIN_ROOT`, `CLAUDE_PLUGIN_ROOT`, `COPILOT_CLI`)
- Emit ONLY ONE format per platform (never both `additional_context` and `hookSpecificOutput`)
- Regression test in `tests/integration/session-start-dispatch.bats` asserts JSON shape

**Coordination with superpowers:**
- `using-chameleon` documents priority order: "After `using-superpowers` triggers `brainstorming`, but before any Edit/Write"
- `using-chameleon` is a **constraint layer** — applies AFTER process skills (brainstorming) and BEFORE/DURING implementation skills
- Combined token cost when both active: ~1,500 (superpowers) + ~1,500 (chameleon) = ~3,000 prime tokens
- For 4+ companion packs: max 10 concurrent packs guard at engine load

**Hook coordination signal:**
- chameleon's PreToolUse hook sets env var `CHAMELEON_ADVISORY_INFLIGHT=1` while running
- Other plugins' PreToolUse hooks can check this and skip duplicate work
- Best-effort coordination, not enforced

**Untrusted profile non-blocking:**
- First-time `.chameleon/profile.json` encountered → primer warning, NOT `[y/N]` prompt
- User runs `/chameleon-trust` when ready
- Eliminates deadlock with superpowers' "invoke skill before any response" mandate

---

## Cost model (revised AGAIN per Round 2 honest tiers)

| Scenario | Estimate | Notes |
|---|---|---|
| SessionStart prime | ~1,200-1,800 tokens | Empirical floor matches superpowers SKILL.md size |
| Per-edit context injection | ~500-800 tokens | Combined hook output |
| Per-edit injection cap | 1,500 tokens hard cap | Truncated with ellipsis |
| **Total-hooks-per-turn cap** | **2,000 tokens hard cap** | Sum of all 4 hooks |
| **Steady-state per session, single-repo, 30 turns, warm cache** | **$0.30-0.50** | The HAPPY PATH ceiling |
| **Multi-repo session (5 repos)** | **$0.60-1.00** | Standard team workflow |
| **Multi-repo session (20 repos)** | **$0.80-1.20** | Heavy switching |
| **Consultant tier (50-80 repos)** | **$2-5** | Outside $50/mo ceiling |
| **Extreme multi-repo (100+)** | **$5+** | Acknowledged edge case |
| **200-turn refactoring marathon** | **$6-12** | Output dominates |
| **Cold-start morning (cache fully expired)** | **+$0.012** | 1.25× cache write surcharge |
| **Per-month at 100 sessions, single-repo** | **$30-50** | Under $50 ceiling |
| **Bootstrap per repo (typical)** | **$0.50-2.00** | 50-100 file analysis + interview |
| **Bootstrap (tRPC-heavy, 80% codegen)** | **$3-7** | Generated code creates noise |
| **Bootstrap (1.2M-file monorepo with --paths glob)** | **REFUSED** | Post-glob 50k cap enforces |
| **Per-team-month with 5 devs sharing committed profile** | **$150-250** | 5 × $35-50/mo |
| **Per-team-month, 5 consultants (50+ repos each)** | **$1,000-2,500** | Outside typical claim; document |

**Pricing assumptions:** Sonnet 4.6 at 2026-05 pricing ($3/M input, $15/M output, $0.30/M cache read, $3.75/M cache write 1.25×). Pricing changes affect all numbers proportionally.

### Cache breakpoint strategy

- **Breakpoint 1**: SessionStart prime (using-chameleon + profile primer) — pinned for session via `cache_control`
- **Per-edit injections** flow as ephemeral input AFTER cached prefix, never breaking it
- **Repo switches** invalidate Breakpoint 1; new prime injected with cache_control fresh
- **Hook outputs** with non-deterministic content (lstat, HMAC, timestamps) — flow as ephemeral input, NEVER in cached prefix

### Cost transparency in primer

The SessionStart primer footer:
```
Recent sessions: $0.32, $0.41, $0.28. This month: $14.20.
Profile last refreshed 47 days ago.
```

Both costs and staleness surfaced to build trust.

---

## Security mitigations (Round 1 + Round 2)

### Critical mitigations
1. **Canonical excerpt secret scanner** — vendored detect-secrets rules; refuses unscanned canonicals
2. **Profile pack code-execution ban** — pack dirs are data-only; loader rejects executables
3. **Symlink lstat in MCP file reads** — closes TOCTOU exfiltration vector
4. **Hardlink defense** — inode-based dedup catches hardlinks (lstat only refuses S_ISLNK)
5. **HMAC bug fix** — `${TMPDIR:-/tmp}/.chameleon_exec_log/` consistently in both write and read paths
6. **profile.json schema validation** — strict JSON schema; rejects malformed
7. **engine_min/max_version** in pack manifest — rejects incompatible packs
8. **Pack signature validation** — Sigstore/cosign or ed25519; `--allow-unsigned` is session-only

### Important mitigations
9. **Repo size guard** — 50k file ceiling, post-glob enforced
10. **AST extractor subprocess limits** — 5s CPU + 512 MB RSS + 1 MB file ceiling
11. **Bootstrap interview output sanitization** — strip ANSI/zero-width, 50 KB cap on idioms.md
12. **drift.db local-only** — never committed; in `${PLUGIN_DATA}` not `<repo>`
13. **HMAC key fail-loud** — explicit error if `/dev/urandom` fails
14. **Trust model** — committed profiles untrusted-by-default per user; `/chameleon-trust` to approve
15. **Pack signing key rotation** — annual; documented in `MAINTAINER.md`
16. **`--allow-unsigned` session-only** — never persisted to config

---

## Versioning & Compatibility (Round 2 maintainability section)

**Engine version policy:**
- Engine vN supports profile schemas v(N-1) to v(N+0); refuses older with migration prompt; refuses newer with upgrade prompt
- Schema migrations live in `mcp/chameleon_mcp/profile/migrations/`
- Each migration is a Python script + integration test

**Pack version compatibility:**
- Pack manifest declares `engine_min_version` and `engine_max_version`
- Loader rejects packs out-of-range with clear message
- Companion plugin upgrade triggers re-validation

**Dependency pinning:**
- TypeScript: vendored at `mcp/node_modules/typescript@<version>`; quarterly bump cadence
- FastMCP: pinned in `pyproject.toml`; quarterly bump
- detect-secrets/gitleaks rules: vendored at known version; quarterly bump
- Python minimum: 3.11 until October 2027 (Python 3.11 EOL); upgrade path to 3.12 documented
- Node minimum for `ts_dump.mjs`: documented in `MAINTAINER.md`; LTS rotation policy
- All locks committed: `package-lock.json`, `uv.lock`, `mcp/uv.lock`

**Runbook (`docs/chameleon/MAINTAINER.md` outline):**
- Quarterly dependency bump checklist
- HMAC key generation + rotation
- Pack signing key rotation (annual)
- Schema migration authoring guide
- Release checklist (CI gates: real-problem-evidence, golden-transcript, skill-baselines)
- Decision register (`docs/chameleon/decisions/`)

---

## Phase plan (revised — TS-only v1, with maintenance scaffolding)

| Phase | Effort | Exit criteria |
|---|---|---|
| Phase 1 — Foundation | ~70h | Hooks + skills shells + MCP scaffold + plugin manifest + lock files + ADR template + MAINTAINER.md draft. Acceptance test passes on stub profile. |
| Phase 2 — TS extractor + bootstrap | ~80h | `/chameleon-init` produces working profile on 5 test TS repos (Next.js 14, classic React, Node API, Vite SPA, monorepo). Generated-code + workspace + plugin-prettierrc detection working. |
| Phase 3 — Skills with eval | ~60h | All 6 skills + `/chameleon-trust` pass RED-GREEN-REFACTOR cycle. Acceptance test transcripts captured (with superpowers active). CI enforcement live. |
| Phase 4 — Security mitigations | ~30h | All 16 mitigations integrated. Schema validation. HMAC bug fix verified. Trust model working. Pack signing infrastructure. |
| Phase 5 — Companion plugin pattern + EF dogfood | ~50h | EF profile pack migrated to separate plugin `chameleon-ef-pack`. Pack signing + version compat enforced. Trust-on-first-use prompt. **Real Problem Evidence transcripts collected from EF dogfooding.** |
| Phase 6 — Conformance benchmarking | ~40h | 80%+ on archetype-matched tasks across 3 test TS repos (honest target — Round 2 reduced from 95% claim). Cost ceiling validated. Multi-repo scenarios tested. |
| Phase 7 — Documentation + release | ~40h | All docs complete (README, MAINTAINER, REAL-PROBLEM-EVIDENCE, ADRs). Dogfooding green for 2 weeks. CI release-tag gates working. |
| **Total** | **~370h** | **~10 weeks of focused work** (up from v2's 340h due to maintenance scaffolding) |

---

## Open decisions for Phase 6 / future iterations

(Not BLOCKING for Phase 1 — these are honest acknowledgments of remaining uncertainty.)

1. **Companion pack registry** — for >10 packs, need search/discovery mechanism. Defer until 5+ packs exist in the wild.
2. **MCP transport beyond stdio** — only if a future platform requires it. v1 is stdio-only.
3. **Multi-canonical similarity ranking** — when archetype has multiple canonicals, how is "the right one" picked for an edit? Heuristic in v1, ML in future?
4. **Pack abandonment policy** — what happens when a companion plugin maintainer leaves? Stale-pack age-out warning? Defer to v1.5.
5. **Skill priority with superpowers** — `using-chameleon` documents "after process, before implementation" — codify this in superpowers' priority hierarchy too?
6. **Profile schema v3 → v4 migration** — the migration template exists in `migrations/`; first real migration's complexity is unknown until needed.

---

## Out of scope for v1

- Multi-language extractors (Ruby/Python deferred to v1.5)
- Multi-harness support beyond Claude Code (deferred to v2.0)
- Cross-repo pattern transfer
- Auto-PR opening for profile updates
- Full-history learning (only AST snapshot at bootstrap time)
- IDE-specific features beyond Claude Code
- Profile diffing UI
- Cost telemetry dashboard (CLI surface only)
- HTTP transport for MCP
- Auto-trigger of `/chameleon-init` (explicit user action required)
- Framework-aware archetype detection (BEST-EFFORT only — no claim of "supports framework X")

---

## Inheritance from claude-measure-twice

What's preserved (REVIEWED — not "verbatim"):
- Preflight-check safety hard-deny logic (1001 lines per current source — RECONCILED)
- Posttool-recorder HMAC exec log (with **GC bug fix + path mismatch fix**)
- Callout-detector frustration phrase reminder
- TS Compiler API extractor approach (vendored)
- MCP server + Skills + PostToolUse pattern

What's redesigned (Round 1 + Round 2 fixes):
- Combined preflight-and-advise hook (parallelism-aware)
- MCP-driven dispatch (no dynamic skill registration)
- Bootstrap interview (≤3 prompts, recency-weighted, workspace-aware)
- Profile schema (multi-canonical, AST-query lookup, ordinal confidence with formula, deprecation tracking)
- Companion plugin pattern (signed, version-bounded, namespaced)
- Security mitigations (16 items)
- Cost model (honest tiered pricing)
- Trust model (non-blocking warning, not [y/N])
- Maintenance scaffolding (locks, ADRs, MAINTAINER.md, schema migrations)

What's discarded:
- Framework-aware claim (best-effort instead)
- Multi-harness v1 directories
- Bundled EF packs
- Dynamic archetype skills
- `<EXTREMELY_IMPORTANT>` framing
- Dual-format JSON dispatch
- Strict sha matching for canonicals (AST-query lookup with sha hint instead)
- "Verbatim inheritance" claim for preflight

---

*End of v3 architecture. Addresses 9 BLOCKING + ~25 SIGNIFICANT items from Round 2 adversarial review. Ready for Phase 6: Jesse Vincent final verification (slop / gap / quality check against superpowers' standards).*
