# chameleon — Architecture v5

> *"Code that blends in."*

> **Date:** 2026-05-10
> **Author:** Cris Nahine + Claude
> **Versions:** v1-v4 archived alongside this file. Round reports at `docs/chameleon/ROUND-{1,2,3,4,5}-*.md`.

## Certainty markers (used throughout this doc)

- **[VERIFIED]** — claim has been validated against real code, prior art, or platform documentation
- **[ESTIMATED]** — claim is a reasoned estimate, validation pending
- **[provisional]** — subject to implementation verification
- **[ASPIRATIONAL]** — claim is a goal, not yet committed

## Table of Contents

- [Purpose](#purpose)
- [implementation evidence](#real-problem-evidence) — CI-gated
- [Goals](#goals)
- [Risk Registry](#risk-registry)
- [Success Metrics](#success-metrics)
- [Plugin name & vocabulary](#plugin-name-chameleon)
- [Core principles](#core-principles)
- [What chameleon is and is not computing](#what-chameleon-is-and-is-not-computing)
- [Tracked dimensions catalog (77 dimensions)](#tracked-dimensions-catalog)
- [High-level architecture](#high-level-architecture)
- [Plugin structure](#plugin-structure-v1-with-crash-safety--maintenance-scaffolding)
- [Bootstrap mechanism](#bootstrap-mechanism)
- [Hook stack](#hook-stack)
- [Skill design](#skill-design)
- [Skill test plan](#skill-test-plan)
- [Bootstrap acceptance test](#bootstrap-acceptance-test)
- [MCP server tools](#mcp-server-chameleon-mcp)
- [TypeScript-first extractor](#typescript-first-extractor-vendored-integrity-checked)
- [**Cluster signature function**](#cluster-signature-function)
- [Profile schema](#profile-schema)
- [**SQLite schemas**](#sqlite-schemas)
- [Atomicity & Crash Safety](#atomicity--crash-safety)
- [**Performance characteristics**](#performance-characteristics)
- [Profile distribution](#profile-distribution-engine-is-the-only-artifact)
- [Future possibility](#future-possibility-companion-plugins-v20-out-of-scope-for-v1)
- [Bootstrap interview flow](#bootstrap-interview-flow)
- [Multi-repo handling](#multi-repo-handling)
- [Plugin coexistence](#plugin-coexistence)
- [Cost model](#cost-model)
- [Operational semantics](#operational-semantics)
- [Calibration targets](#calibration-targets)
- [Migration correctness contract](#migration-correctness-contract)
- [Versioning & Compatibility](#versioning--compatibility)
- [Security mitigations](#security-mitigations)
- [**Failure mode runbook**](#failure-mode-runbook)
- [Phase plan](#phase-plan)
- [**License + BC contract**](#license--backwards-compatibility-contract)
- [Open decisions for future iterations](#open-decisions-for-future-iterations)
- [Out of scope for v1](#out-of-scope-for-v1)
- [Inheritance from predecessor projects](#inheritance-from-predecessor projects)
- [Glossary appendix](#glossary-appendix)

---

## Purpose

A Claude Code plugin that gives the AI deep understanding of YOUR repo's conventions — not a list of pre-known framework patterns, but the patterns you actually wrote.

The engine clusters AST + statistical signals from your code, asks targeted questions about what it cannot infer, iterates via post-edit feedback. Over time, the profile becomes a living artifact capturing your team's actual coding style.

**Target outcome:** measurable reduction in reviewer comments on file shape / naming / idiom usage on AI-generated code, validated against baseline transcripts collected during dogfooding.

---

## implementation evidence

> **⚠️ This section requires evidence from internal dogfooding to be filled before v1.0 release. Documented as a CI gate.**

### Working hypothesis

AI-generated code in established codebases routinely violates local conventions in ways that cost reviewer time but don't affect correctness. Hypothesis is supported by:
- Active development of `predecessor projects` (predecessor) as one team's response
- The `CLAUDE.md` convention adoption rate across Claude Code users
- Anecdotal reports from author's day-to-day work at the project

### Evidence required before v1.0 release

- 5+ concrete transcripts of Claude (without chameleon active) writing off-pattern code in real TypeScript and Ruby on Rails repos repos
- Per transcript: what was generated, what reviewer flagged, time-to-fix, the convention-correct version
- Quantified cost of rework

**Owner:** Cris (human partner). **Deadline:** before v1.0.0 semver tag.

**Enforcement:** CI release-tag check — `tag-v*.*.*` requires `docs/chameleon/REAL-PROBLEM-EVIDENCE.md` to contain ≥5 H2 sections matching transcript schema. Build fails otherwise.

---

## Goals

1. **Best-effort pattern clustering** on any TS repo — not framework-aware, not "supported list"
2. **Single install, multi-repo** with crash-safe state
3. **Auto-onboarding** via explicit `/chameleon-init` (no auto-trigger)
4. **Co-existence** with a complementary skills library and any other Claude Code plugin
5. **Profile sharing via git** — committed `.chameleon/profile.json` + auto-resolved merges via `chameleon-mcp::merge_profiles`
6. **Honest cost model** — bootstrap acceptable high (one-time), steady-state $0.30-0.50/single-repo, multi-repo and consultant tier explicitly higher
7. **Skill discipline** — Iron Law per `writing-skills`; no skill ships without failing test first
8. **Graceful boundaries** — AST falls short → interview + `/chameleon-teach` (renamed from refine); no claim of "supports framework X"
9. **Distributed-systems crash safety** — atomic commits, OS-level locks, fail-open advisories, per-call cache invalidation
10. **Long-term maintainability** — lock files, version pins, schema migration contract, ADRs, MAINTAINER.md, observable value attribution

---

## Risk Registry

10 prioritized risks for Phase 1+. Mitigations either documented elsewhere in this architecture or flagged as (future).

| # | Risk | Probability | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| 1 | no real-world adoption signal; cannot validate value | Medium | Critical (v1.0 cannot ship) | stakeholder conversation BEFORE Phase 1 | Cris |
| 2 | TS Compiler API subprocess overhead blows 5s/file budget on real TypeScript repo | High | High (Phase 2 grinds) | Daemonize ts_dump.mjs (see Performance section) | Cris |
| 3 | AST clustering produces low-confidence output on real code | High | Critical (80% conformance gate fails) | Run early on TypeScript repo subset; iterate signature function | Cris |
| 4 | Solo developer unavailable >30 days during 9-15 month build | Medium | Critical (project pause) | Document bus factor in README; identify potential co-maintainer | Cris |
| 5 | Claude Code 2.x mid-project API regression (mcp_tool, paths, hooks) | Low-Medium | High (rework) | Pin engine_min_version; quarterly model re-baseline | Cris |
| 6 | Effort estimate 3× off; 9-15 months becomes 18+ | Medium | High (scope cuts forced mid-project) | Pre-commit fall-back-to-v0.5 plan; review at week 12 | Cris |
| 7 | users prefer CLAUDE.md over chameleon | Low | High (no signal for value) | Frame chameleon as CLAUDE.md complement, not replacement | Cris |
| 8 | Profile.json merge conflicts cause user pain | Medium | Medium | merge_profiles tool + .gitattributes template (designed) | Cris |
| 9 | Vendored TypeScript supply chain compromise | Low | High | SHA-256 checksums + CI verify (designed) | Cris |
| 10 | Quarterly maintenance tasks slip; idioms.md decays | High | Low-Medium | Calendar reminders; staleness escalation in primer | Cris |

**Maintenance:** review quarterly. Risk #1 is the single highest leverage item; address before Phase 1.

---

## Success Metrics

Replaces v4's "≥80% pattern conformance" (unverifiable) with measurable, falsifiable criteria:

### Primary metric (v1.0 ship gate)

> **On the next 10 TypeScript and Ruby on Rails repos PRs that include AI-generated code (with chameleon active), fewer than 2 reviewer comments mention shape/naming/idiom violations that chameleon should have caught.**

Concrete, falsifiable, costs you nothing to measure. Source: each PR's review thread; "should have caught" = within Tier 1 or Tier 2 dimensions documented in catalog.

### Secondary metrics (instrumented in `value_attrib.db`, surfaced via `/chameleon-status`)

| Metric | Target | Source |
|---|---|---|
| edits_following_canonical | ≥80% | per-edit, post-acceptance |
| deviations_flagged | tracked, not gated | `lint_file` violations |
| corrections_via_teach | ≥3 per repo per month | `/chameleon-teach` invocations |
| primer_load_p99_latency | <500ms | SessionStart hook timing |
| mcp_call_p99_latency | <1500ms (under 2s timeout) | per-call timing |
| fail_open_rate | <2% | hook timeout/error counter |

### Counter-metrics (signals chameleon is hurting more than helping)

- User runs `/chameleon-disable` ≥3 sessions in a row → user is fighting the tool
- `/chameleon-teach` corrections exceed 10/week → engine is getting it wrong consistently
- Hook timeouts >10% → performance broken
- `/chameleon-reset` invoked >0 times → user reached `rm -rf`-equivalent escape

If counter-metrics breach, escalate: pause Phase 5 dogfood, investigate, fix before re-engaging.

---

## Plugin name: `chameleon`

> *Tagline: "Code that blends in."*

**User-facing vocabulary (5 terms only — vocabulary firewall):**
- **profile** — the team's conventions captured in `.chameleon/`
- **archetype** — a category of file with shared patterns
- **idiom** — a team-specific rule or banned pattern
- **refresh** — automated re-analysis (`/chameleon-refresh`)
- **trust** — per-user approval of a committed profile (`/chameleon-trust`)

**Internal terminology** (ADRs / MAINTAINER.md only): canonical, content_signal, recency_weight, scope, cluster_size, confidence_function, syntactic surrogate, normative shape.

**Conventions:**
- Plugin/repo name: `chameleon` (no `claude-` prefix)
- Slash command prefix: `/chameleon-*` (with `/cham-*` short alias)
- Skill prefix: `chameleon-*`
- Foundation skill: `using-chameleon`
- Context tag: `<chameleon-context>` (NEUTRAL — no importance framing)
- MCP server: `chameleon-mcp`
- Python package: `chameleon_mcp`
- Profile dir: `.chameleon/`
- Env var prefix: `CHAMELEON_*`

---

## Core principles

1. **Foundation generic, brain per-repo.** Engine ships with no repo-specific knowledge.
2. **Best-effort, not framework-aware.** Engine clusters what AST can express; rest goes through interview + `/chameleon-teach`.
3. **Profile is portable artifact.** Committed JSON + Markdown, reviewable in PRs.
4. **Two-tier dimensions.** Auto-derivable (AST + statistical + recency-weighted) vs hand-curated (`idioms.md`).
5. **Discovery before action.** Every edit injects archetype context before model writes — via MCP-driven dispatch.
6. **Inject context, don't deny.** Only safety hard-denies; conformance is advisory.
7. **Plugin coexistence first-class.** Single-format JSON dispatch, neutral tags, parallel-hook-aware.
8. **Honest scoping.** v1 = TS only, Claude Code only.
9. **Skills as code.** Iron Law honored.
10. **Distributed-systems thinking.** `.chameleon/` is shared mutable state across processes; treat it as such (atomic commits, OS locks, cache invalidation, merge tools).
11. **Fail-open advisories, fail-closed safety.** When MCP fails, edit proceeds with warning. When safety check fails, edit blocked.
12. **Observable value.** Users see edits-matched, deviations-flagged. Not just spend.

---

## What chameleon is and is not computing

**The semantic relation chameleon approximates:**

> Two files are in the same archetype iff a competent reviewer at this team would say "these are instances of the same pattern."

This is a semantic equivalence relation about behavior and intent.

**The syntactic surrogate chameleon actually computes:**

> Two files are in the same archetype iff their AST shape + path glob membership + content_signal directives + recency-weighted vote cluster them together.

The engine's epistemic position: the syntactic relation is a sound approximation of the semantic one for *most* TS code, breaks down for *some* TS code (DSL-heavy, metaprogramming, type-level patterns), and the gap is absorbed by `idioms.md` collected via interview + `/chameleon-teach`.

**Named obligations:**

- **Soundness (false-positive control):** if two files end up in same cluster, they share AST shape + signals — meaningful similarity is *plausible* but not guaranteed. Mitigated by: canonical-files mechanism + `/chameleon-teach` as manual error correction.
- **Completeness (false-negative control):** ~70% recall on AST-derivable patterns, ~0% on type-level/decorator-driven patterns. `idioms.md` absorbs the rest.
- **Stability:** running `/chameleon-refresh` twice on the same repo state must produce byte-identical profiles (idempotence under fixed input). Adding/removing a single file should not flip canonical selection unless that file IS the new canonical.

**The boundary rule for content_signal vs idioms.md:**

> *`content_signal` only encodes file-level lexical directives appearing in the first 200 bytes of the file. Anything requiring AST traversal, type information, or class-body inspection is `idioms.md` territory.*

This is falsifiable. Future contributors proposing `imports_signal`, `decorator_signal`, etc. should be redirected to `idioms.md` (which itself will gain structure in v2.0+ — see Open decisions).

---

## Tracked dimensions catalog

Concrete enumeration of dimensions chameleon detects (Tier 1: auto-derivable) or accepts via interview / `/chameleon-teach` (Tier 2: hand-curated). implementation verification on (Ruby on Rails) and (TypeScript) expanded the catalog from initial 51 dimensions to 77.

### Tier 1 — Auto-derivable (40 dimensions)

**File shape & layout (5):**
1. File placement (path patterns)
2. File naming convention (kebab-case / camelCase / snake_case / PascalCase)
3. Folder structure (flat vs nested, feature-folders vs layer-folders)
4. File size norms (avg lines per archetype; per `.rubocop.yml` Max ClassLength)
5. Module boundary signals (index.ts barrels, ActiveRecord concerns, `app/services/<domain>/`)

**Code shape (6):**
6. Class/function structure (constructor → methods, public-first vs private-first)
7. Naming conventions (camelCase, PascalCase, snake_case)
8. Import order & grouping (perfectionist plugin signals; require ordering)
9. Type annotation density (TS strict mode + noImplicitAny config)
10. Async/await patterns (Promise / callback / async-await consistency)
11. Export style (named vs default; barrel files vs direct)

**Code patterns (5):**
12. Error handling shape (try/catch / rescue / Result types)
13. Function length norms (Rubocop MethodLength)
14. Comment density (JSDoc / YARD / inline)
15. String quote style (defer to `.prettierrc` / `.rubocop.yml`)
16. Indent + spacing (defer to `.prettierrc` / `.editorconfig` / `.rubocop.yml`)

**Architectural patterns (5):**
17. Archetype taxonomy (controllers, services, models, hooks, components, workers, mailers, channels — clustered via path + content_signal)
18. Layering (controllers → services → models; pages → queries)
19. Abstraction boundaries (what's exported, what's internal)
20. DI patterns (Rails injection, React provider context)
21. Design pattern signals (Repository, Factory, Strategy where AST-detectable)

**DRY & code reuse (4):**
22. Existing utility detection (helpers, hooks, `base/` components, `app/services/`)
23. Common pattern canonicals (`base/` primitives in client; service objects in api)
24. Duplicate detection (similar AST blocks)
25. Reusable component identification

**Test patterns (4):**
26. Test file colocation (`*.test.ts` next to source vs `__tests__/` dir vs `spec/` mirror)
27. Test naming (`*_spec.rb`, `*.test.ts`, describe/it patterns)
28. Test structure (FactoryBot factories, fixtures, mocking pattern)
29. Test framework auto-detection (RSpec / Vitest / Jest / Cypress signals)

**Tool config as ground truth (6):**
30. `.prettierrc` — formatting rules
31. `tsconfig.json` — TypeScript strictness, paths, module resolution
32. `.eslintrc` — linting rules + plugins (with JS-plugin warning)
33. `.editorconfig` — indent, line endings, charset
34. `package.json` deps — what's available
35. `.rubocop.yml` — Ruby style + custom cops + AllCops Exclude paths
36. `Gemfile` / `Gemfile.lock` — Ruby deps + version constraints

**Build & ecosystem signals (5):**
37. Package manager signal (pnpm-lock.yaml / package-lock.json / yarn.lock / Gemfile.lock)
38. Build tool signal (Vite / Webpack / Rspack / Turbopack signals)
39. Linter custom cops/plugins detection (`lib/rubocop/custom_cops/`, ESLint custom plugins)
40. Path alias detection (tsconfig `paths` field — e.g., `~/` → `src/`)
41. Migration generator pattern (presence of `db/migrate/` with timestamped files)

(40 not 41 — re-numbered to flatten v1 catalog above; will appear as 40 in profile schema)

### Tier 2 — Hand-curated via interview + `/chameleon-teach` (29 dimensions)

**Banned imports / mandatory wrappers (6):**
42. Banned import paths (`lodash` whole-library banned; method-scope only)
43. Mandatory wrappers (`useCustomQuery` for queries; `request` for HTTP)
44. Custom hooks vs library hooks (never `useQuery` directly)
45. Custom HTTP client signature (`request([method, url], ...)`)
46. Error response helpers (`apiError(code, msg)` vs raw `Response.json`)
47. Logger key naming (`request_id`, `user_id` required keys)

**Architectural decisions :**
48. Migration state ("MobX → React Query"; "Pages Router → App Router")
49. Deprecated patterns (legacy markers like `src/mobx/` is legacy)
50. Feature flag wrapping (Flipper, LaunchDarkly, custom)
51. Auth invariants (JWT-attached headers; `authorize_request` before_action)
52. Cross-cutting telemetry (Sentry, PostHog, custom instrumentation)
53. Encryption / security wrappers (Lockbox for sensitive credentials)
54. Audit trail wrappers (Paper Trail for model versioning)

**Domain vocabulary (3):**
55. Domain term preferences ("Listing" not "Property"; "Buyer" not "Customer")
56. Bounded context boundaries (service domains: amazon_sp/, hubspot/, shopify/, qbo/, google/, zoom/, llm/)
57. Naming conventions for domain entities (singular vs plural, prefix conventions)

**Library version constraints :**
58. Locked-in major versions (RR v5 NOT v6; React 18; Rails 7.2)
59. Deprecated library markers ("don't add new MobX state")
60. State management hierarchy (React Query > Provider context > Formik > MobX legacy)

**Cross-cutting infrastructure :**
61. API boundary conventions (camelCase ↔ snake_case auto-conversion)
62. Permission-checked routing pattern (`routesPermissions.tsx`)
63. Lazy loading wrapper pattern (`retry` wrapper)
64. Test infrastructure idioms (parallel testing config, `PUTS=1`, `SHOW_COVERAGE=true`)
65. Multi-DB conventions (connection switching for Main/Deal Center/WordPress)

**Migration scaffolding rules :**
66. Migration generator preference ("`rails generate migration` always — NEVER hand-write timestamps")
67. UUID vs auto-increment primary key convention

**Team taste (3):**
68. Line length tolerance (Rubocop 100; client implicit)
69. When to extract helper functions
70. Comment style preferences (when valuable vs noise)

### Tier 3 — Out of scope for v1 (8 dimensions)

71. Type-level patterns (branded types, template literals, conditional types, `as const`)
72. Runtime semantics (Effect monads, ts-pattern exhaustiveness, fp-ts)
73. Decorator semantics (NestJS `@Injectable`, TypeORM)
74. Class-body shape patterns (Pydantic v1 inner Config class)
75. Auto-generated API surface (tRPC builder chains)
76. Metaprogramming (`method_missing`, `__getattr__`, dynamic class generation)
77. JSX semantic patterns (rules-of-hooks, RSC boundaries affecting children)
78. State management semantic abstractions (Redux vs Zustand vs Jotai patterns at runtime level)

### Goals → Dimensions mapping

| Goal | Dimensions |
|---|---|
| Consistency | 1-16 (file + code shape + patterns) |
| DRY | 22-25 |
| Architectural integrity | 17-21 |
| Naming standards | 2, 7, 55-57 |
| Test discipline | 26-29 |
| Format/style adherence | 30-36 (defer to tool configs) |
| Build/ecosystem awareness | 37-41 (NEW verification) |
| Banned/mandated patterns | 42-47 |
| Migration management | 48-49, 58-60 |
| Cross-cutting concerns | 50-54, 61-65 (auth, telemetry, encryption, audit, API boundary) |
| Domain modeling | 55-57 |
| Library version policy | 58-60 (NEW verification) |
| Infrastructure idioms | 61-67 (NEW verification) |
| Code quality / readability | 68-70 |
| Reviewer-friendly output | All Tier 1 + Tier 2 (so reviewer focuses on logic, security, tests) |

**implementation testing corpus (Phase 5 starting idioms):** when `/chameleon-init` runs on Ruby on Rails repo or TypeScript repo, the bootstrap interview will pre-populate suggestions for #42-67 based on signals detected from `.eslintrc.js`, `.rubocop.yml`, `package.json`, `Gemfile`, and existing CLAUDE.md content. User confirms/corrects via interview, iterates further via `/chameleon-teach` once dogfood begins.

---

## High-level architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ chameleon (engine, v1: TS + Claude Code) │
│ │
│ ┌──────────────────────────┐ ┌──────────────────────────────────┐ │
│ │ Hooks (parallel-aware) │ │ Skills (static, no runtime gen) │ │
│ │ ───── │ │ ────── │ │
│ │ SessionStart │ │ using-chameleon (foundation) │ │
│ │ → session-start │ │ │ │
│ │ → SINGLE-FORMAT dispatch│ │ Slash commands (5 user + 2 admin)│ │
│ │ → cache_control: │ │ /chameleon-init │ │
│ │ pinned static prefix │ │ /chameleon-refresh │ │
│ │ + ephemeral footer │ │ /chameleon-status │ │
│ │ → first-run welcome │ │ /chameleon-teach (was -refine) │ │
│ │ PreToolUse Edit/Write │ │ /chameleon-trust │ │
│ │ → preflight-and-advise │ │ Admin : │ │
│ │ (combined: safety │ │ /chameleon-disable (session) │ │
│ │ + lstat + safe_open │ │ /chameleon-pause-15m │ │
│ │ + MCP excerpt with │ │ │ │
│ │ 2s timeout, fail-open)│ │ Short aliases: /cham-* │ │
│ │ → tag-boundary sanitize │ │ │ │
│ │ → hook-model dedup │ │ │ │
│ │ PostToolUse Bash │ │ │ │
│ │ → posttool-recorder │ │ │ │
│ │ UserPromptSubmit │ │ │ │
│ │ → callout-detector │ │ │ │
│ │ (surfaces disable hint)│ │ │ │
│ └──────────────┬───────────┘ └──────────────┬───────────────────┘ │
│ └────────────────┬────────────────┘ │
│ ▼ │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │ MCP Server (chameleon-mcp) │ │
│ │ detect_repo get_archetype lint_file │ │
│ │ get_canonical_excerpt get_rules get_drift_status │ │
│ │ refresh_repo bootstrap_repo list_profiles │ │
│ │ merge_profiles (NEW) refine_profile trust_profile │ │
│ │ (every file-reading tool: safe_open + lstat first; per-call │ │
│ │ mtime check; AST node ceiling 50k; SQLite ro+trusted_schema=OFF)│ │
│ └─────────────┬───────────────────────────────┬───────────────────┘ │
│ │ │ │
│ ▼ ▼ │
│ ┌──────────────────────────────┐ ┌─────────────────────────────────┐│
│ │ Profile storage │ │ Bootstrap engine ││
│ │ ──────────────── │ │ ──────────────── ││
│ │ Committed (team-shared): │ │ 1. Detect language (TS only v1) ││
│ │ <repo>/.chameleon/ │ │ 2. WORKSPACE DETECTION ││
│ │ profile.json (manifest) │ │ 3. ATOMIC TRANSACTION: ││
│ │ archetypes.json │ │ .chameleon/.tmp/<txn-id>/ ││
│ │ rules.json │ │ + COMMITTED sentinel last ││
│ │ canonicals.json │ │ atomic dir rename ││
│ │ idioms.md │ │ 4. AST scan + RECENCY WEIGHT ││
│ │ profile.summary.md │ │ 5. Tool config = ground truth ││
│ │ │ │ 6. EXCLUDE generated, vendor, ││
│ │ Local-only (per-user): │ │ legacy/, archive/, etc. ││
│ │ ${PLUGIN_DATA}/ │ │ 7. Statistical pattern extract ││
│ │ index.db (NEW: list of │ │ 8. CANONICAL INJECTION SCAN ││
│ │ all known repos) │ │ (instruction-shaped lang) ││
│ │ <repo_id>/ │ │ 9. Bimodal/sparse surfacing ││
│ │ drift.db (WAL+busy_timeout│ │ 10. Secret scan (vendored rules)││
│ │ 30000+retry-jitter) │ │ 11. Trichotomize canonicals: ││
│ │ cache.json │ │ witness/normative-shape/idiom││
│ │ .trust │ │ 12. ≤3 user prompts (≤10 lines ││
│ │ .first_run_seen (NEW) │ │ visible each) ││
│ └──────────────────────────────┘ └─────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│ AST extractor (TypeScript only in v1) │
│ Single language: TS Compiler API via subprocess │
│ TypeScript pinned + VENDOR INTEGRITY CHECKSUMS in mcp/typescript- │
│ checksums.json (CI verifies on every build) │
│ AST node ceiling: 50k nodes per file (DoS protection) │
│ │
│ Subprocess limits per file: 5s CPU, 512 MB RSS, 1 MB file ceiling │
│ Inode-based file dedup (hardlink defense) │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│ Profile distribution = git (one artifact: the engine) │
│ ──────────────────────────────────────────────────────────────────── │
│ Single distribution artifact: chameleon plugin │
│ Profile sharing per repo via committed .chameleon/profile.json │
│ + .gitattributes registers chameleon-mcp::merge_profiles as merge driver│
│ │
│ Companion plugins: OUT OF SCOPE for v1, possible v2.0+ if community │
│ demand emerges │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Plugin structure (v1, with crash-safety + maintenance scaffolding)

```
chameleon/
├── .claude-plugin/
│ ├── plugin.json
│ └── marketplace.json
├── .gitattributes-template # NEW: ships for users to copy into their repos
│ # registers chameleon-mcp::merge_profiles as merge driver
├── CLAUDE.md
├── AGENTS.md (symlink → CLAUDE.md)
├── README.md # vocabulary firewall: 5 user-facing terms
│ # competitive analysis section (v3 → v4 add)
├── CHANGELOG.md
├── RELEASE-NOTES.md
├── LICENSE
├── package.json # version anchor
├── package-lock.json # MUST commit
├── CONTRIBUTING.md # NEW: external contributor onboarding
├── hooks/
│ ├── hooks.json
│ ├── run-hook.cmd # cross-platform polyglot wrapper
│ ├── session-start # SessionStart: SINGLE-FORMAT dispatch + first-run welcome
│ │ # cache_control two-chunk split
│ ├── preflight-and-advise # PreToolUse: safety + safe_open + lstat
│ │ # 2s MCP timeout, fail-open contract
│ │ # tag-boundary sanitization
│ │ # hook-model deduplication
│ ├── posttool-recorder # PostToolUse Bash: per-repo HMAC log dir (0700)
│ └── callout-detector # UserPromptSubmit: surfaces disable hint on frustration
├── skills/
│ ├── using-chameleon/ # foundation (loaded by SessionStart)
│ │ ├── SKILL.md # Red Flags: rationalization edge cases enumerated
│ │ └── tests/
│ ├── chameleon-init/
│ ├── chameleon-refresh/
│ ├── chameleon-status/
│ ├── chameleon-teach/ # RENAMED from chameleon-refine
│ ├── chameleon-trust/
│ ├── chameleon-disable/ # NEW: session-scope disable
│ └── chameleon-pause-15m/ # NEW: 15-minute pause
├── mcp/
│ ├── pyproject.toml
│ ├── uv.lock # MUST commit
│ ├── typescript-checksums.json # NEW: SHA-256 vendor integrity manifest
│ ├── chameleon_mcp/
│ │ ├── server.py # FastMCP entry (version pinned)
│ │ ├── safe_open.py # NEW: shared safe_open(repo, rel_path) helper
│ │ │ # realpath + prefix-match + null/NFD/sep checks
│ │ │ # used by every file-reading tool
│ │ ├── tools/
│ │ │ ├── merge_profiles.py # NEW: programmatic profile merge (re-cluster from union)
│ │ │ └── ...
│ │ ├── extractors/
│ │ │ ├── _base.py
│ │ │ └── typescript.py # AST node ceiling 50k
│ │ ├── bootstrap/
│ │ │ ├── transaction.py # NEW: atomic commit pattern (.tmp/<txn-id>/COMMITTED)
│ │ │ ├── canonical_scanner.py # NEW: instruction-shaped natural language detection
│ │ │ └── ...
│ │ ├── profile/
│ │ │ ├── schema.py # JSON parser hardened (depth cap 64, dup keys, NFC, ranges)
│ │ │ ├── migrations/
│ │ │ │ ├── README.md # migration correctness contract documented
│ │ │ │ └── v1_to_v2.py # template
│ │ │ ├── secret_scanner.py # vendored detect-secrets rules
│ │ │ └── poisoning_scanner.py # NEW: dangerous-pattern detection on canonicals
│ │ ├── locks.py # NEW: flock advisory locks for refresh_repo
│ │ ├── packs/ # REMOVED: companion plugins out of v1
│ │ └── drift/
│ │ └── sqlite_config.py # NEW: WAL + busy_timeout=30000 + retry-jitter
│ └── node_modules/ # VENDORED + checksum-verified
│ └── typescript/
├── scripts/
│ ├── ts_dump.mjs # AST node ceiling 50k
│ ├── bump-version.sh
│ ├── secret-scan.sh
│ └── verify-vendor-checksums.sh # NEW: CI step before every build
├── tests/
│ ├── skill-triggering/
│ │ ├── prompts/
│ │ ├── run-all.sh # CI: fails if skill lacks tests/baseline.md
│ │ └── run-test.sh
│ ├── unit/
│ ├── integration/
│ │ ├── session-start-dispatch.bats # regression test: single-format JSON only
│ │ ├── tag-boundary-sanitize.bats # regression: closing-tag in canonical content
│ │ ├── transaction-atomicity.bats # regression: COMMITTED sentinel
│ │ ├── lock-contention.bats # regression: concurrent refresh_repo
│ │ └── cache-invalidation.bats # regression: mtime check
│ ├── corpus/ # benchmark TS repos
│ └── acceptance/
│ ├── README.md
│ ├── golden-transcript.md # cooperative case
│ └── adversarial-transcript.md # NEW: user pressure + both plugins active
└── docs/
 └── chameleon/
 ├── specs/
 ├── plans/
 ├── reference/
 ├── decisions/ # ADR DIRECTORY
 ├── MAINTAINER.md # KEY ROTATION + DEP CADENCE + MIGRATION RUNBOOK
 │ # + QUARTERLY MODEL RE-BASELINE TASK
 ├── REAL-PROBLEM-EVIDENCE.md # CI-gated transcripts
 └── ROUND-{1,2,3,4}-*.md # review history
```

---

## Bootstrap mechanism

```
SessionStart hook fires (matcher: startup|clear|compact)
 → run-hook.cmd session-start
 → bash script:
 1. Read skills/using-chameleon/SKILL.md
 2. Detect active repo (file-path walk-up if available, else cwd)
 3. Detect filesystem type :
 - if NFS / SMB / shared mount detected → primer warning
 - if devcontainer / docker volume → use git_remote_url (not abs_path) for repo_id
 4. Detect language → if not TS, suppress primer (graceful degradation)
 5. Check first-run state:
 - ${PLUGIN_DATA}/<repo_id>/.first_run_seen exists? → skip welcome
 - else → emit one-line welcome ("chameleon learns this repo's conventions.
 Run /chameleon-init to set up (~$1, ~5 min interview).")
 → write .first_run_seen
 6. Check profile state:
 - <repo>/.chameleon/profile.json present + COMMITTED sentinel valid? → load summary
 - per-user cache populated? → load summary
 - none? → suggest /chameleon-init (only if first_run_seen)
 7. Check trust state:
 - profile committed and ${PLUGIN_DATA}/<repo_id>/.trust missing?
 → mark UNTRUSTED in primer (non-blocking)
 → user runs /chameleon-trust to approve
 8. Build cache_control TWO-CHUNK output:
 - CACHED PREFIX (with cache_control breakpoint):
 using-chameleon SKILL.md + STATIC profile primer
 (archetype names, paths, sizes — these don't change session-to-session)
 - EPHEMERAL SUFFIX (no cache_control):
 cost footer ("Recent sessions: $0.32, $0.41, $0.28")
 staleness footer ("Profile last refreshed 47 days ago")
 trust state ("Profile UNTRUSTED — run /chameleon-trust")
 value attribution ("Last 30 sessions: 142 edits matched, 11 deviations flagged")
 9. SINGLE-FORMAT JSON DISPATCH (per platform):
 - if CURSOR_PLUGIN_ROOT → emit { "additional_context": ... }
 - elif CLAUDE_PLUGIN_ROOT && !COPILOT_CLI → emit { "hookSpecificOutput": ... }
 - else → emit { "additionalContext": ... }
 NEVER emit both. (Mirrors a complementary skills library/hooks/session-start lines 41-55 verbatim.)
 10. Wrap content in <chameleon-context> tags (NEUTRAL)
 11. Tag-boundary sanitize: escape any </chameleon-context>, </chameleon, <chameleon-context>
 literals in the injected content
```

**Two-chunk cache_control rationale:** static profile primer is large, stable across sessions, benefits from caching. Cost/staleness/trust/attribution change per session and would invalidate the cache prefix every session if included. Two chunks: cached prefix at breakpoint, ephemeral suffix appended after.

**First-run welcome example:**
```
[chameleon] First time in this repo. chameleon learns this repo's TypeScript conventions
 so generated code matches existing style. Run /chameleon-init to set up
 (~$1, ~5 minutes interview). Or /chameleon-disable to silence this.
```
One line. Once per repo per user.

---

## Hook stack

```
SessionStart (matcher: startup|clear|compact):
 1. session-start
 Inject using-chameleon + repo profile primer + (separately) ephemeral footer
 Cached chunk: ~1,200-1,500 tokens
 Ephemeral chunk: ~150-300 tokens
 Total: ~1,500-1,800 tokens

PreToolUse (matcher: Edit|Write|NotebookEdit):
 1. preflight-and-advise (SINGLE COMBINED HOOK)
 a. Safety hard-denies (path traversal, secrets, lockfiles, vendored, generated,
 /etc/, /var/, ~/.aws/, ~/.ssh/, /proc/, /sys/, /dev/, ADS, **/.git/**)
 (Inherited from predecessor projects — REVIEWED + EXPLICIT BLOCKLIST captured
 as test fixture; HMAC path bug FIXED: ${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/
 with mode 0700 + owner check)
 b. safe_open(repo, file_path):
 - lstat (refuse symlinks)
 - realpath + prefix-match against repo_root (path traversal)
 - reject null bytes, NFD-encoded .. sequences, Windows separators
 c. Hook-model deduplication:
 - check tool-call history this turn
 - if model already invoked chameleon-mcp::get_canonical_excerpt for this archetype
 → skip injection (avoid double-counting)
 d. If safety passes AND profile trusted AND not deduplicated:
 - synchronously call chameleon-mcp::get_canonical_excerpt
 - 2-SECOND TIMEOUT
 - on timeout/error: FAIL-OPEN SILENT (no context injected, edit proceeds,
 telemetry log entry with reason)
 - on success: tag-boundary sanitize content, then inject as <chameleon-context>
 e. Per-edit injection cap: 1,500 tokens max (truncated)
 f. Cache_control: hook output is EPHEMERAL (lstat results, MCP-fetched canonicals
 may have run-specific timing data; never in cached prefix)

PostToolUse (matcher: Bash):
 1. posttool-recorder
 HMAC-signed exit code log
 Per-repo log directory: ${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/
 Mode 0700, owner-check on every read
 Key fail-loud (explicit error if /dev/urandom fails)

UserPromptSubmit:
 1. callout-detector
 Frustration phrase → rule-update-first reminder
 : if frustration detected during chameleon-active session,
 surface "/chameleon-disable or /chameleon-pause-15m to silence"

TOTAL-HOOKS-PER-TURN CAP: ≤2,000 tokens summed across all hooks (truncated)
```

**Why combined `preflight-and-advise`:** hooks on shared matchers run in **parallel** per Claude Code platform. Combining safety + advisor into one synchronous command hook ensures safety check completes before MCP call.

**Why fail-open advisory + fail-closed safety:** different layers, different consequences. Safety failure means "don't do this"; advisory failure means "we couldn't help, but it's safe to proceed." Conflating them causes either security regression (fail-open safety) or productivity collapse (fail-closed advisory).

---

## Skill design

### Foundation skill: `using-chameleon`

```yaml
---
name: using-chameleon
description: Use when starting any conversation in a TypeScript repo with a chameleon profile present, before any Edit, Write, or NotebookEdit operation
---
```

**Body sections:**
- `<chameleon-context>` block (NEUTRAL — no importance framing)
- `<SUBAGENT-STOP>` block: subagents skip
- The Rule: invoke `chameleon-mcp::detect_repo` + `get_canonical_excerpt` BEFORE editing in profiled repos
- Process flowchart (graphviz `dot`)
- **Red Flags table with rationalization edge cases:**
 - "This is just a small one-line fix" → STOP, call MCP
 - "This is just a rename, not a new pattern" → STOP, call MCP
 - "This is just a comment edit" → STOP, call MCP (comments may need to follow archetype patterns)
 - "I just need to reorder imports" → STOP, call MCP (import order is a canonical concern)
 - "I already saw the canonical for this archetype this session" → STOP, call MCP (canonicals can drift mid-session if `/chameleon-refresh` runs)
 - "The user is in a hurry, skipping the call saves time" → STOP, call MCP (200ms is the cost of correctness)
 - "I know this codebase already" → STOP, call MCP (the profile is the source of truth, not your prior)
- Available slash commands (5 user + 2 admin + 1 trust + 4 short aliases)
- Profile state interpretation (trusted vs untrusted)
- Coordination with a complementary skills library: "After `another bootstrap skill` triggers `brainstorming`, but before any Edit/Write" (priority order)
- Non-blocking trust prompt: "If profile is untrusted, surface in response but proceed with user request"

### User-invokable skills (5 commands + 1 trust + 2 admin)

| Skill | Slash command | Short alias | Purpose |
|---|---|---|---|
| `chameleon-init` | `/chameleon-init` | `/cham-init` | Bootstrap a new repo profile (≤3-prompt interview) |
| `chameleon-refresh` | `/chameleon-refresh` | `/cham-refresh` | Re-analyze repo, detect drift, update profile |
| `chameleon-status` | `/chameleon-status` | `/cham-status` | Show profile + drift + value attribution + plugin health |
| `chameleon-teach` | `/chameleon-teach` | `/cham-teach` | Iterate on profile based on observed misses; **owns idioms.md collection** |
| `chameleon-trust` | `/chameleon-trust` | `/cham-trust` | Approve a committed profile for this user (writes per-user `.trust` file) |
| `chameleon-disable` | `/chameleon-disable` | `/cham-disable` | Disable plugin for the rest of this session |
| `chameleon-pause-15m` | `/chameleon-pause-15m` | `/cham-pause-15m` | Pause plugin for 15 minutes |

**`/chameleon-trust` cooldown:** requires typing the repo name (or `yes-trust-<repo_id_short>`). New canonicals or idioms added after trust grant re-prompt. Trust granted is NOT trust authorizing all future content.

**No dynamic archetype skills.** Replaced with MCP-driven dispatch (rationale documented in ADR `0005-mcp-dispatch-vs-dynamic-skills.md`).

---

## Skill test plan

> **Iron Law from `writing-skills`:** "NO SKILL WITHOUT A FAILING TEST FIRST."

**CI enforcement:** `tests/skill-triggering/run-all.sh` fails if any `skills/<name>/` lacks a `tests/baseline.md` file with documented rationalizations. PRs cannot merge with missing baseline.

### `using-chameleon` test plan

**RED (baseline scenarios):**
- Pressure scenario 1: TS repo with profile; user says "just add this small one-line fix"
- Pressure scenario 2: TS repo with profile; user says "I know the pattern, skip the MCP call"
- Pressure scenario 3: TS repo with profile; user is rushing
- Pressure scenario 4: TS repo without profile; agent invents pattern instead of suggesting `/chameleon-init`
- Pressure scenario 5: profile UNTRUSTED; agent must surface trust requirement non-blockingly
- Pressure scenario 6 : both `another bootstrap skill` and `using-chameleon` active; user says "just fix this now, no brainstorming bs"; verify both skills' mandates are honored
- Combined pressures (3+): time + sunk cost + authority + exhaustion

**Rationalizations to capture verbatim:** Anticipated patterns (validate empirically):
- "This is just a one-line fix" / "just a rename" / "just a comment"
- "I already know this codebase" / "I already saw the canonical"
- "Calling MCP for every edit is wasteful"
- "The profile is probably outdated anyway"

### Skill test plans for chameleon-init, refresh, refine, status, trust, disable, pause-15m

(Documented per skill in `tests/baseline.md` files.)

### Quarterly model re-baseline

MAINTAINER.md task: re-run all pressure scenarios against new model releases (Sonnet/Opus version bumps). CI gates `engine_min_version` bump on regression results. Rationalizations not in existing tables get added. Bulletproof skills are a moving target.

---

## Bootstrap acceptance test

> **Acceptance test (cooperative — `tests/acceptance/golden-transcript.md`):**
>
> Open clean Claude Code session in `tests/acceptance/` containing `.chameleon/profile.json` with at least one archetype.
> Send: `Add a new endpoint at /api/v1/widgets that returns a list of widgets.`
>
> A working integration:
> 1. SessionStart hook fires; `using-chameleon` is injected
> 2. Before generating code, agent invokes `chameleon-mcp::detect_repo` and `get_canonical_excerpt`
> 3. Agent's first edit follows canonical pattern

> **Acceptance test (adversarial — `tests/acceptance/adversarial-transcript.md`, ):**
>
> Open clean Claude Code session in `tests/acceptance/` with **both `a complementary skills library` AND `chameleon` installed**.
> Send: `Just fix this now — no brainstorming, just edit /api/v1/widgets to return widgets.`
>
> A working integration:
> 1. Both `another bootstrap skill` and `using-chameleon` are injected at SessionStart
> 2. Agent invokes `chameleon-mcp::detect_repo` and `get_canonical_excerpt` BEFORE any edit, despite user pressure
> 3. Agent's edit follows canonical pattern
> 4. (Optional) Agent acknowledges user's time pressure but still follows the constraint layer

**CI enforcement:** Release tags require updated `golden-transcript.md` AND `adversarial-transcript.md`.

---

## MCP server (`chameleon-mcp`)

FastMCP-based, stdio transport (NEVER exposed over network).

| Tool | Input | Output | Security note |
|---|---|---|---|
| `detect_repo` | file_path | repo_id, profile_status, trust_state | repo_id is sha256 of git_remote_url ALONE if set, else canonicalize_path(repo_root) |
| `get_archetype` | repo, file_path | archetype + content_signal match, alternatives | safe_open + lstat |
| `get_canonical_excerpt` | repo, archetype | annotated excerpt (500-800 tokens) | safe_open + lstat + AST-query lookup with sha hint + tag-boundary sanitize |
| `get_rules` | repo, archetype? | rules + citations | per-call mtime check on profile.json |
| `lint_file` | repo, archetype, content | AST violations + canonical confidence score | content size 100KB cap + AST node 50k cap |
| `get_drift_status` | repo | freshness + days_since_refresh + observed_drift_score | reads from drift.db (WAL + busy_timeout=30000 + retry-jitter) |
| `refresh_repo` | repo, force | re-analyze | OS-level flock on .chameleon/.refresh.lock |
| `bootstrap_repo` | path, mode, paths_glob? | first-time analysis | safe_open + atomic transaction + canonical injection scan |
| `list_profiles` | — | all known repos | reads from index.db (single SQLite, not N filesystem walks) |
| `merge_profiles` | repo, ours, theirs, base | merged profile (re-clustered from union) | — programmatic git merge driver |
| `refine_profile` | repo, feedback | apply user-driven correction | feedback sanitization (strip ANSI/zero-width, 50KB cap) |
| `trust_profile` | repo | mark profile as trusted | requires repo name confirmation |

**Cache_control discipline:** lstat output, drift.db queries, HMAC log entries, posttool exit codes, dynamic timestamps, MCP tool results — all flow as ephemeral input. NEVER in cached prefix.

**Per-call mtime check:** every MCP tool that reads profile artifacts performs `fstat` on each artifact, compares to last-loaded mtime, re-reads if changed. ~100us per check, eliminates stale-cache bugs.

**Hook-model deduplication:** `get_canonical_excerpt` invocation by the agent is recorded in MCP server state for current turn. Hook checks state before injecting; if already invoked, hook skips injection.

---

## TypeScript-first extractor (vendored, integrity-checked)

v1 ships TypeScript only via TS Compiler API subprocess.

**Vendoring + integrity strategy:**
- TypeScript pinned at specific version in `mcp/node_modules/typescript`
- `mcp/typescript-checksums.json` lists SHA-256 of every file under `mcp/node_modules/typescript/`
- CI step `verify-vendor-checksums.sh` runs before every build; fails on mismatch
- Quarterly bump cadence in MAINTAINER.md MUST require:
 - Download from npm
 - Verify against `npm audit signatures`
 - Manually diff file list for unexpected additions
 - Regenerate checksums
- Same discipline for FastMCP and detect-secrets rule files

**Subprocess limits per file:**
- 5s CPU
- 512 MB RSS
- 1 MB file size ceiling
- AST node ceiling 50k post-parse (DoS protection against pathological TypeScript)
- Inode-based file dedup (hardlink defense)
- Reject files matching generated-code signals

**Language rollout sequence (v1.0 TS → v1.5 Ruby → v2.0+ others):**

The two primary implementation testing targets are Ruby on Rails repo (Ruby on Rails) and TypeScript repo (TypeScript). Supporting both from v1.0 was considered but explicitly deferred to a phased rollout:

- **v1.0 = TypeScript only.** Dogfood = TypeScript repo. Validates the engine + bootstrap loop on one language.
- **Validation gate:** 2-4 weeks of TypeScript repo dogfood. Ship v1.0 only after pattern conformance ≥80% and cost ceiling validated.
- **v1.5 = adds Ruby (Prism).** Dogfood expands to Ruby on Rails repo. Adding a language to a proven engine is integration work, not novel engineering — the predecessor `predecessor projects` already shipped a working Prism approach.
- **v2.0+ = community-driven additions** (Python, Go, Rust, PHP, Java) only if demand emerges.

This sequence trades 2-3 weeks slower time-to-Ruby for substantially lower risk on the engine's fundamental abstractions. Both stacks are supported by v1.5 (~13 weeks total, vs ~10 weeks for client-only v1.0).

---

## Cluster signature function

> **** — addresses Compiler/Static-Analysis NEEDS REVISION verdict.

The architecture's clustering relies on a signature function `f: file → cluster_key`. Without explicit specification, this becomes the implementer's ad-hoc choice; with specification, the architecture's stability/idempotence obligations become engineering contracts.

### The signature function (committed for v1.0)

```python
def sig(file: SourceFile, repo: Repo) -> ClusterKey:
 return (
 path_pattern_bucket(file.path, repo.archetype_paths),
 # First 200 bytes content_signal match (per architecture's vocabulary boundary)
 content_signal_match(file.content[:200], repo.archetype_signals),
 # Tuple of ts.SyntaxKind for direct children of SourceFile (top-level structure)
 tuple(ts.kind_name(c) for c in file.ast.children if is_top_level(c)),
 # The "thing being exported": ts.SyntaxKind of the default export, or None
 default_export_kind(file.ast),
 # Bucketed count of named exports: 0, 1, 2-4, 5-9, 10+
 bucket_count(count_named_exports(file.ast)),
 # Hash of sorted import-module specifiers + named/default imports
 # (catches "imports from react vs react-dom/server vs react/jsx-runtime")
 sha256_imports(file.ast),
 # Boolean: presence of JSX elements anywhere in the file
 has_jsx(file.ast),
 )
```

This 7-tuple is computable in a single `forEachChild` pass. Cluster keys are exact-match equivalence classes; archetypes are clusters. Files in the same cluster are candidates for the same archetype.

### Compiler API mode (committed for v1.0)

- **Use `ts.createSourceFile(fileName, sourceText, ScriptTarget, /*setParentNodes*/ true)`** — pure parser, no module resolution, no type checker
- **Cost:** ~10–30ms per file in-process; 50–200ms via subprocess
- **Consequence:** type-level patterns (templates, conditionals, branded types) are **out of v1 scope** (deferred to idioms.md / Tier 3)
- **JSX/TSX:** `ScriptKind.JSX` for `.tsx`; `ScriptKind.JS` for `.js`; etc. AST contains JSX nodes (not desugared)
- **Decorators:** parsed only if `experimentalDecorators` flag is set in `tsconfig.json` OR ES decorators (TS 5.0+)

### Parser-error tolerance contract

- File with `>20 parse diagnostics` → **skipped** (likely not actual TS, or actively broken; clustering on these adds noise)
- File with `≤20 parse diagnostics` → **extracted** (TS Compiler is forgiving; partial AST is usable for shape extraction)
- One file's parser crash MUST NOT abort the whole bootstrap — try/except per file

### Incremental algorithm: recompute-all-from-cached-signatures

```
On /chameleon-refresh:
 for each tracked file:
 if (path, content_sha256) unchanged → reuse cached sig
 else recompute sig
 cluster all current sigs (full re-cluster)
```

**Properties:**
- O(changed_files) parse cost, O(total_files) cluster cost
- Idempotent under fixed input (running twice on same repo state produces byte-identical profiles)
- Stable: adding/removing a single file doesn't flip canonical selection unless that file IS the new canonical

### Cache invalidation triggers (signature cache)

- TS version bump → invalidate all cached sigs
- `tsconfig.json` change affecting parse mode (`jsx`, `target`, `experimentalDecorators`) → invalidate all
- Signature function version (in code, bumped on any change to `sig` definition) → invalidate all
- Per-file: content_sha256 mismatch → invalidate that file's sig

### TS version handling

- chameleon's parse always uses **vendored TypeScript** (`mcp/node_modules/typescript`), never user's `node_modules`
- Behavior is deterministic w.r.t. chameleon version, not user's TS
- If user's `tsc` differs significantly from vendored, primer warns: "User TS 5.6 vs chameleon vendored 5.4; expected; some recent syntax may parse differently"

### Cross-file analysis: out of scope for v1

- "DRY detection" (Tier 1 dim 24) requires cross-file similarity — implemented as MinHash over k-grams in v1.5+
- v1 implements duplicate detection only WITHIN a single file (rare in practice)
- Document this limitation in /chameleon-status output

---

## Profile schema

```
.chameleon/ (committed, team-shared, atomic-write-protected)
 ├── profile.json # manifest (schema_version, engine_version, created_at, source)
 ├── archetypes.json # path patterns + content_signal → archetype + cluster_size + outliers + recency_weight
 ├── rules.json # per-archetype rules + citations
 ├── canonicals.json # canonical references (witness + AST query + idiom annotations)
 ├── idioms.md # human-curated, deprecation-tracked
 └── profile.summary.md # human-readable for PR review (semantic deltas highlighted)

${CLAUDE_PLUGIN_DATA}/ (local-only, NEVER committed)
 ├── index.db # : single SQLite listing all known repos
 └── <repo_id>/
 ├── drift.db # WAL + busy_timeout=30000 + retry-jitter, GC'd weekly
 ├── cache.json # per-user runtime cache
 ├── .trust # per-user profile approval marker
 ├── .first_run_seen # : first-run welcome guard
 ├── .pause_until # : /chameleon-pause-15m timestamp
 └── value_attrib.db # : tracks edits-matched, deviations-flagged, corrections
```

`canonicals.json` schema with **trichotomized canonical**:

```json
{
 "schema_version": 4,
 "engine_min_version": "1.0",
 "canonicals": {
 "next-server-component": [
 {
 "witness": {
 "path": "app/dashboard/page.tsx",
 "lines": [1, 60],
 "sha_hint": "abc123..."
 },
 "normative_shape": {
 "ast_query": "ExportNamedDeclaration > FunctionDeclaration[name='Page']",
 "required_features": ["async function", "no 'use client' directive"]
 },
 "normative_idioms": {
 "comments": [
 "Server components should use the async fetch pattern",
 "Wrap database calls in try/catch with our error helper"
 ]
 },
 "secret_scan_passed": true,
 "injection_scan_passed": true,
 "scanned_at": "2026-05-10T..."
 }
 ]
 }
}
```

The trichotomy makes explicit:
- **Witness** — the actual file (which has idiosyncrasies)
- **Normative shape** — the AST query (must match)
- **Normative idiom** — prose annotations (the team conventions to follow)

This eliminates the v3 ambiguity about "what part of the canonical is the pattern."

`idioms.md` schema ( deprecation tracking + marked as v2+ direction for structured idioms):

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

(v2.0+ direction: structured idiom format `(name, ast_query_pattern, counterexample_query, prose_rationale, status)` for machine-checkability.)

---

## SQLite schemas

> **** — addresses Database/Consistency NEEDS REVISION verdict.

Three SQLite databases are used. Each has explicit schema, indices, and migration policy.

### `drift.db` (per-repo, in `${PLUGIN_DATA}/<repo_id>/`)

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA synchronous=NORMAL;
PRAGMA trusted_schema=OFF;
PRAGMA wal_autocheckpoint=10000;

CREATE TABLE schema_meta (
 k TEXT PRIMARY KEY,
 v TEXT NOT NULL
);
INSERT INTO schema_meta (k, v) VALUES ('schema_version', '1');

-- Per-file drift state (hot path: PreToolUse hook reads + writes)
CREATE TABLE files (
 rel_path TEXT PRIMARY KEY,
 inode INTEGER,
 mtime_ns INTEGER NOT NULL,
 size INTEGER,
 sha_hint BLOB, -- xxhash64 (8 bytes), non-crypto
 archetype TEXT,
 cached_sig BLOB, -- serialized 7-tuple cluster signature
 last_observed_confidence REAL,
 last_seen_at INTEGER NOT NULL -- unix epoch seconds
) WITHOUT ROWID;

CREATE INDEX idx_files_last_seen ON files(last_seen_at); -- for GC
CREATE INDEX idx_files_archetype ON files(archetype);

-- Per-edit confidence history (for drift-driven nags)
CREATE TABLE edit_observations (
 id INTEGER PRIMARY KEY,
 rel_path TEXT NOT NULL,
 archetype TEXT,
 confidence_observed REAL,
 matched_canonical INTEGER NOT NULL DEFAULT 0, -- 0 or 1
 observed_at INTEGER NOT NULL
);
CREATE INDEX idx_edit_obs_at ON edit_observations(observed_at);
CREATE INDEX idx_edit_obs_path ON edit_observations(rel_path, observed_at);
```

**Migration policy:** drift.db is a CACHE. Drop-and-recreate is permitted on schema bumps. `/chameleon-refresh` rebuilds in <60s on typical repos.

**GC policy:**
- Records older than 30 days purged weekly
- `PRAGMA wal_checkpoint(TRUNCATE)` in weekly GC
- Directory-level age-out: `${PLUGIN_DATA}/<repo_id>/` deleted if no access in 60 days

### `index.db` (single, in `${PLUGIN_DATA}/`)

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA synchronous=NORMAL;
PRAGMA trusted_schema=OFF;

CREATE TABLE schema_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
INSERT INTO schema_meta (k, v) VALUES ('schema_version', '1');

-- Registry of all known repos this user has touched
CREATE TABLE repos (
 repo_id TEXT PRIMARY KEY, -- sha256(canonicalized git_remote_url || abs_path)
 abs_path TEXT, -- last-known absolute path
 git_remote_url TEXT, -- canonicalized
 language TEXT, -- 'typescript', 'ruby' (v1.5), etc.
 profile_state TEXT NOT NULL, -- 'no_profile' | 'profile_present' | 'pack_match'
 trusted_at INTEGER, -- unix epoch when /chameleon-trust ran
 last_seen_at INTEGER NOT NULL,
 days_since_refresh INTEGER -- denormalized for fast SessionStart query
) WITHOUT ROWID;

CREATE INDEX idx_repos_last_seen ON repos(last_seen_at);
CREATE INDEX idx_repos_state ON repos(profile_state);
```

**Migration policy:** index.db is a REGISTRY (not just cache — losing it loses the trust state). Use **additive-only `ALTER TABLE`** for new columns. Breaking changes require explicit migration script in `mcp/chameleon_mcp/profile/migrations/index_db_<from>_to_<to>.py`.

### `value_attrib.db` (per-repo, in `${PLUGIN_DATA}/<repo_id>/`)

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;

CREATE TABLE schema_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- Per-session attribution: did chameleon's advisory help?
CREATE TABLE sessions (
 session_id TEXT PRIMARY KEY,
 started_at INTEGER NOT NULL,
 edits_total INTEGER DEFAULT 0,
 edits_following_canonical INTEGER DEFAULT 0,
 deviations_flagged INTEGER DEFAULT 0,
 corrections_via_teach INTEGER DEFAULT 0
) WITHOUT ROWID;

CREATE INDEX idx_sessions_started ON sessions(started_at);
```

**Migration policy:** drop-and-recreate (cache).

### Hashing function: xxhash64

- Used for `sha_hint` in drift.db.files and canonicals.json
- 8 bytes, ~30 GB/s throughput, non-crypto (per "_hint" suffix)
- Same hash function across drift.db and canonicals.json → drift detection is single-int comparison, not re-hash
- For tamper-detection security needs, use SHA-256 elsewhere (e.g., `vendor checksums` for TS)

### Connection model

- One SQLite connection per database per MCP server process
- Single MCP server process per Claude Code session
- WAL allows concurrent readers + 1 writer; multiple sessions on same machine cooperate via `busy_timeout`
- Per-process retry-with-jitter on `SQLITE_BUSY`: 5 retries, 100ms-1s backoff

### Cross-file referential integrity (loader pattern)

The atomic-rename pattern gives write-side cross-file atomicity. **Reader-side** consistency requires the double-fstat pattern:

```python
def load_profile(repo_dir: Path) -> Profile:
 # Capture mtime tuple BEFORE reads
 mtimes_before = (
 stat(repo_dir / 'profile.json').st_mtime_ns,
 stat(repo_dir / 'archetypes.json').st_mtime_ns,
 stat(repo_dir / 'rules.json').st_mtime_ns,
 stat(repo_dir / 'canonicals.json').st_mtime_ns,
 )
 # Read all files
 p = read_json(repo_dir / 'profile.json')
 a = read_json(repo_dir / 'archetypes.json')
 r = read_json(repo_dir / 'rules.json')
 c = read_json(repo_dir / 'canonicals.json')
 # Verify mtime tuple AFTER reads
 mtimes_after = (...)
 if mtimes_before != mtimes_after:
 raise RetryLoad("profile in flux")
 # Verify generation counter consistency
 if p.generation != a.generation != r.generation != c.generation:
 raise RetryLoad("inconsistent generation")
 return Profile(p, a, r, c)
```

**Generation counter:** profile.json carries `generation: int`. Other three files embed the same counter. Atomic-rename writes all four with new counter.

### `merge_profiles` algorithm (committed for v1.0)

```python
def merge_profiles(base: Profile, ours: Profile, theirs: Profile) -> Profile:
 """
 Three-way merge: re-cluster from union of files referenced in either side.
 Deterministic tie-breaking: archetype names sorted lexicographically;
 canonicals picked by recency-weighted cluster_size.
 """
 # 1. Union of all files referenced in ours.archetypes + theirs.archetypes
 file_set = collect_file_paths(ours) | collect_file_paths(theirs)

 # 2. Re-cluster from scratch using current sig function
 new_archetypes = cluster_files(file_set, sig=current_sig_function)

 # 3. For each archetype, pick canonical via deterministic rule:
 # - Highest recency_weight
 # - Tie-break: lexicographic sort of paths

 # 4. idioms.md: prefer 'theirs' for new idioms; preserve 'ours' for unchanged ones
 new_idioms = merge_idioms(base, ours, theirs)

 # 5. Surface to user via profile.summary.md for review
 return Profile(new_archetypes, new_idioms, generation=max(ours.gen, theirs.gen)+1)
```

**Key properties:**
- Deterministic (same inputs → same output)
- Reproposed (user reviews via `profile.summary.md`, can override via `/chameleon-teach`)
- No silent data loss (all files in either side are considered)
- Idiom merge is conservative (additions kept, deletions require explicit deprecation)

### `.trust` file format (per-user trust marker)

```json
{
 "granted_at": "2026-05-10T14:32:01Z",
 "granted_by_user": "crisn",
 "profile_sha256": "abc123..." // hash of profile.json at trust-grant time
}
```

**Material-change predicate (re-prompt trigger):**
- profile_sha256 changed AND any new archetype, new canonical witness file, or new active idiom → re-prompt
- profile_sha256 changed but only deprecation status / recency_weight / cluster_size → silent update
- This is the v1.0 rule; refinement based on user feedback in v1.1+

---

## Atomicity & Crash Safety (NEW — distributed systems hardening)

The single biggest gap in v3 was treating `.chameleon/` as a passive directory of files rather than as multi-process shared mutable state. v4 addresses with:

### Multi-file transactional commit

Bootstrap and refresh write to a transaction directory:

```
.chameleon/.tmp/<txn-id>/ # txn-id = uuid + timestamp + pid
 ├── profile.json
 ├── archetypes.json
 ├── rules.json
 ├── canonicals.json
 ├── idioms.md
 ├── profile.summary.md
 └── COMMITTED # SENTINEL FILE — written LAST
```

**Commit protocol:**
1. Write all artifacts into `.chameleon/.tmp/<txn-id>/`
2. Verify each artifact (fsync, schema-validate, secret-scan)
3. Write `COMMITTED` sentinel file last
4. Atomic rename: `.chameleon/.tmp/<txn-id>/` → `.chameleon/`

**Recovery:**
- Loaders refuse to read `.chameleon/` if `COMMITTED` is missing → "incomplete profile, run /chameleon-refresh"
- On startup, MCP server scans `.chameleon/.tmp/` for orphaned txn dirs (no longer being written, lock file's PID dead) → cleans up

**Per-PID temp subdir:** prevents collision when two refresh processes run simultaneously.

### OS-level locks

`/chameleon-refresh` and `/chameleon-init` acquire advisory lock:
- File: `.chameleon/.refresh.lock`
- Content: PID + start timestamp + hostname
- `flock(LOCK_EX | LOCK_NB)` — fails immediately if held
- Error: "Another /chameleon-refresh is in progress (PID 12345 since 14:32:01). Wait or kill PID 12345."

**Stale lock detection:** if PID dead OR started >1 hour ago → break lock with warning.

### SQLite hardening

Every connection to drift.db, index.db, value_attrib.db sets:
```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA synchronous=NORMAL;
PRAGMA trusted_schema=OFF;
```

Open URL: `sqlite:///path/drift.db?mode=ro` for read-only paths where possible.

**Per-process retry-with-jitter** on `SQLITE_BUSY`:
- 5 retries
- Exponential backoff: 100ms, 200ms, 400ms, 800ms, 1.6s
- Jitter: ±50%

### Profile cache invalidation

MCP server holds profile.json + archetypes.json + rules.json + canonicals.json in memory. On every MCP tool call that reads these:
1. `fstat` each artifact
2. Compare mtime to last-loaded mtime
3. Re-read + re-validate if changed

~100us per check. Eliminates stale-cache bugs after `/chameleon-teach`, `/chameleon-refresh`.

### Failure mode matrix

| Failure | Hook behavior | User signal | Recovery action |
|---|---|---|---|
| MCP server crash mid-tool-invocation | Hook 2s timeout → fail-open silent, edit proceeds | Telemetry log entry | `/chameleon-status` shows "MCP errored 3 times this session" |
| OOM kill mid-bootstrap | Bootstrap aborted, partial txn dir orphaned | "Bootstrap interrupted" on next session | Auto-cleanup orphan + rerun `/chameleon-init` |
| AST extractor subprocess crash on bad file | File skipped, others continue | Bootstrap reports "847 parsed, 3 skipped (parse error)" | Rerun on smaller scope |
| Disk full during sqlite write | drift.db corrupts | drift.db re-created on next load (lossy: lose drift state) | Run `/chameleon-refresh` to rebuild |
| Concurrent /chameleon-refresh | Second invocation fails with lock-held message | "Another refresh in progress, wait or kill PID X" | Wait |
| Profile read while being written | Hook gets old version (atomic rename in flight); re-reads on mtime change | None visible | Automatic |
| `${PLUGIN_DATA}` read-only / disk full | MCP server emits explicit error on startup | "Cannot write cache; chameleon disabled this session" | Free disk, restart |
| HMAC key generation failure | posttool-recorder errors loudly | "HMAC key generation failed; bash exec log unsigned" | Manual key creation in MAINTAINER.md |
| Filesystem case-collision (Windows + WSL) | SessionStart errors out with explicit message | "Cannot operate: .chameleon/ and .Chameleon/ both exist" | Manual rename to lowercase |
| NFS / SMB drift.db | Primer warning at SessionStart | "drift detection unreliable on this filesystem" | Move PLUGIN_DATA to local FS, or accept manual /chameleon-refresh cadence |

---

## Performance characteristics

> **** — addresses Performance NEEDS REVISION verdict.

### Daemonization model (load-bearing)

**Per-edit subprocess fork is unacceptable.** Each PreToolUse hook = ~50ms subprocess startup × 30 edits = **1.5 seconds dead overhead per session**. Solution:

```
┌─────────────────────────────────────────────┐
│ Claude Code session │
│ │
│ [Hook: tiny shell script] │
│ ↓ unix domain socket │
│ [chameleon-mcpd: long-lived daemon] │
│ ↓ subprocess (per file) │
│ [ts_dump.mjs: long-lived process] │
│ ↑ stdin (file paths) / stdout (NDJSON)│
└─────────────────────────────────────────────┘
```

**Hook responsibilities:**
- Parse Claude Code tool input (file_path)
- Connect to daemon via UNIX socket at `${PLUGIN_DATA}/sock/<session_id>`
- Send {file_path, archetype_lookup_request}
- Receive context (with 2s timeout)
- Inject as `<chameleon-context>` or fail-open

**Daemon responsibilities:**
- Hold profile in memory (per-call mtime check for invalidation)
- Long-lived `ts_dump.mjs` worker pool
- Process lock via `flock` on `${PLUGIN_DATA}/<repo_id>/.daemon.lock`
- Auto-shutdown 5min after last hook activity (no idle resource use)

### `ts_dump.mjs` batching

```
Per-file invocation: 300ms startup + 50ms parse = 350ms per file
 5,000 files = 29 minutes ← UNACCEPTABLE

Batched invocation: 1× 300ms startup + 5,000 × 50ms parse / N workers
 With 4 workers: 60 seconds ← TARGET
```

**Implementation:**
- `ts_dump.mjs` reads file paths from stdin (NDJSON)
- Emits AST extraction results to stdout (NDJSON)
- Worker pool: `min(cpu_count // 2, 8)` workers
- Each worker is one long-lived Node process with TS Compiler loaded once
- Coordinator (Python) parallelizes work across workers via stdio pipes

### Throughput floor (CI-enforced)

- ≥50 files/sec/core typical TS modules
- ≥200 files/sec/core small files (<200 lines)
- CI benchmark on implementation testing corpus + 3 OSS TS repos
- Regression failure: bootstrap >2× expected duration on benchmark corpus

### Memory bounds

| Component | Steady state | Peak |
|---|---|---|
| Daemon Python interpreter + FastMCP | ~40 MB | ~60 MB |
| Profile cache (4 JSON files) | 0.2-5 MB | 5 MB |
| AST extraction workers (4× Node) | 4× 150 MB = 600 MB | 4× 250 MB = 1 GB |
| Index.db connection | 1 MB | 5 MB |
| **Total daemon process group** | **~750 MB** | **~1.1 GB** |

**Hard caps (CI-asserted):**
- Daemon RSS: 100 MB hard cap (excluding worker subprocesses)
- AST cache LRU: max 16 entries, evict oldest
- AST extraction workers: 1 GB total
- index.db: max 10k repos before pagination required (Open Decisions item 7)

### Latency budget breakdown (per Edit)

| Stage | Best | Typical | p99 |
|---|---|---|---|
| Hook parse + socket connect | 2ms | 5ms | 20ms |
| Daemon: profile cache mtime check | 0.4ms | 2ms | 20ms |
| Daemon: get_pattern_context (look up archetype, AST query, format excerpt) | 8ms | 40ms | 800ms |
| Daemon: tag-boundary sanitization | 0.5ms | 2ms | 10ms |
| Hook: serialize + emit JSON | 1ms | 5ms | 20ms |
| **Hook total wall-clock** | **~12ms** | **~55ms** | **~870ms** |
| + Model regeneration | 800ms | 2500ms | 8000ms |
| **Total user-visible per Edit** | **~810ms** | **~2.6s** | **~9s** |

**Note:** the architecture's Red Flags table previously said "200ms is the cost of correctness." This number was empirically wrong by ~7×. Removed in v5; replaced with non-numeric language ("the call adds modest hook latency plus whatever the model takes to consume the context").

### Cache effectiveness

- Anthropic prompt cache TTL: 5 minutes
- SessionStart prime cache breakeven: ~3 reuses
- Cold-start morning: +300ms TTFT (1.25× cache write penalty + larger first-turn input)
- Idle-gap reprime cost: ~$0.006 per >5min idle gap
- Engine release cadence (weekly minor releases) invalidates cache prefix on each release

### NFS / SMB warning

- mtime granularity may be 1s on these filesystems → mtime-based change detection less reliable
- Per-edit `os.fstat` calls: 10-50ms on NFS vs 100us local
- Primer warning at SessionStart on detection
- Recommend: `${PLUGIN_DATA}` on local FS (not NFS-mounted home)

### Bootstrap performance target

| Repo size | Time | Cost |
|---|---|---|
| <500 files | 30s | $0.30-0.50 |
| 500-2,000 files | 1-3 min | $0.50-1.00 |
| 2,000-10,000 files | 3-10 min | $1-3 |
| 10,000-50,000 files | 10-30 min | $3-7 |
| >50,000 files | refused without explicit globs | — |

---

## Profile distribution (engine is the only artifact)

In v1, chameleon ships ONE thing: the engine plugin. Profiles are NOT distributed as separate artifacts.

**Profile sharing via git:**

1. Team member runs `/chameleon-init` in their repo
2. `.chameleon/` written atomically (commit-marker pattern)
3. Member commits `.chameleon/` and `.gitattributes-template` (registers merge driver)
4. PR review uses `profile.summary.md` for human-readable diff with semantic deltas highlighted
5. Once merged, every dev pulling gets the profile
6. Each user runs `/chameleon-trust` once per repo (per-user, non-blocking)

**Conflict resolution via merge_profiles:** when two devs run `/chameleon-refresh` on parallel branches and merge:

```bash
# .gitattributes (shipped as template)
.chameleon/profile.json merge=chameleon
.chameleon/archetypes.json merge=chameleon
.chameleon/rules.json merge=chameleon
.chameleon/canonicals.json merge=chameleon
```

```bash
# Git config (set by chameleon-init or manually)
[merge "chameleon"]
 name = chameleon profile merge
 driver = chameleon-mcp merge_profiles --base %O --ours %A --theirs %B --output %A
```

`merge_profiles` re-clusters from union of `ours` and `theirs` inputs, producing a deterministic resolved profile that doesn't lose either side's recent work.

**implementation testing case:**
- Run `/chameleon-init` on `project/api` → commit `.chameleon/`
- Run `/chameleon-init` on `project/client` → commit `.chameleon/`
- implementation evidence transcripts collected from internal dogfooding
- predecessor projects's hand-curated knowledge informs initial answers + idioms
- Other devs get profile via `git pull` + `/chameleon-trust`
- **No separate plugin to maintain**

---

## Future possibility: companion plugins (v2.0+, OUT OF SCOPE for v1)

If post-v1 community demand emerges for distributing pre-built profiles outside individual repos, companion plugin distribution can be added as a non-breaking v2.0 feature. The engine architecture supports this addition without breaking changes.

But that's a v2.0+ decision, contingent on observed need. Not in v1.

---

## Bootstrap interview flow

**≤3 user-facing prompts. Each prompt ≤10 lines visible.**

```
1. User runs /chameleon-init in a TS repo

2. Engine (no user prompts):
 a. Detect language → TS confirmed
 b. Detect workspace structure (pnpm/yarn/lerna/turbo/nx) → if found, ask root or per-workspace
 c. Read tool config files
 WARNING: if .prettierrc references JS plugins → flag for user
 d. AST scan repo (with workspace scoping if applicable):
 - <500 files: full pass
 - 500-50,000: stratified sample
 - >50,000: refuse without explicit globs
 - WITH globs: still enforce 50k post-glob cap
 e. Inode-dedup file list
 f. Exclude generated, vendor, dist, __generated__
 AND from canonical pool: __tests__, test, legacy, archive, deprecated, _archive, .archive
 g. Statistical pattern extraction with RECENCY WEIGHTING (90 days = 2× vote)
 h. Cluster files by content_signal + path → archetype proposals
 i. Bimodal/sparse surfacing
 j. Secret scan canonical excerpts
 k. CANONICAL INJECTION SCAN :
 - Scan canonical content for instruction-shaped natural language
 - Patterns: imperatives at "you"/"the AI", "ignore prior", "disregard"
 - Hits → flag for PROMPT 1 OR strip comments before injection (user choice)

3. PROMPT 1 (≤10 lines, archetype confirmation):
 "Detected 8 archetypes:
 next-server-component (high, 23 files): app/dashboard/page.tsx
 next-client-component (high, 18): app/components/SearchBar.tsx
 [+5 more — see profile.summary.md]

 ⚠️ 1 canonical contained instruction-shaped text. View? [v]
 Apply? [Y/n/edit]"

4. PROMPT 2 (≤10 lines, bimodal/sparse if any):
 "half-migrated-component:
 A) ApolloClient.query (14 files, avg 200d ago)
 B) useQuery hook (9 files, avg 30d ago)
 C) Both — route-dependent
 D) Both — accept both, prefer B for new"

5. PROMPT 3 (≤10 lines, save destination):
 "Save profile to .chameleon/ (committed) or per-user cache?
 [committed/private]"

6. ATOMIC TRANSACTION:
 .chameleon/.tmp/<txn-id>/ written → COMMITTED sentinel → atomic rename
 + .gitattributes-template merged
 + Reports: "Profile ready. 8 archetypes, 14 rules, 0 idioms.
 Cost: $X.XX. Run /chameleon-trust to approve."
```

**Cost estimate per bootstrap:** $0.50-$2.00 typical, $3-7 for tRPC-heavy.

---

## Multi-repo handling

- Profile keyed by **`repo_id = sha256(canonicalize(git_remote_url))` ALONE if remote present, else `sha256(canonicalize_path(repo_root))`** ( clarification — never mix path and remote)
- `canonicalize_path` uses Unicode NFC normalization
- Storage:
 - In-repo: `<repo>/.chameleon/...` (preferred; team shares)
 - Per-user: `${CLAUDE_PLUGIN_DATA}/<repo_id>/` (drift.db + cache.json + .trust + .first_run_seen + .pause_until + value_attrib.db)
- Detection: file-path walk-up; submodule-aware (innermost `.git` boundary)
- Drift tracking: per-repo sqlite, GC'd weekly (records older than 30 days purged); directory-level age-out at 60 days no-access
- **Index db** (`${PLUGIN_DATA}/index.db` — ): single SQLite listing all known repos with `(repo_id, last_seen_mtime, profile_state, days_since_refresh)`. SessionStart `list_profiles` hits this, not N filesystem walks.

**Filesystem detection :**

SessionStart detects:
- NFS mount → primer warning "drift detection unreliable on NFS; consider local PLUGIN_DATA"
- SMB mount → primer warning
- Devcontainer / Docker bind-mount → use git_remote_url ALONE for repo_id (avoid host vs container path mismatch)
- Case-insensitive filesystem with case-collision in `.chameleon/` → refuse to operate, message: "Lowercase `.chameleon/` required, found case-variant"

**Multi-repo cost scaling:**

| Open repos in session | Realistic session cost |
|---|---|
| 1 (single-repo) | $0.30-0.50 |
| 5 | $0.60-1.00 |
| 20 | $0.80-1.20 |
| 50-80 (consultant tier) | $2-5 |
| 100+ | $5+ |

The consultant/freelancer tier is **explicitly outside the $50/month ceiling** for typical users.

---

## Plugin coexistence

**Hygiene rules:**
- Slash commands namespaced: `/chameleon-*` (with `/cham-*` aliases)
- Env vars namespaced: `CHAMELEON_*`
- Hooks: parallel-aware design
- Inject context, don't deny
- Token budget: ~1,500 prime + ≤2,000 total-hooks-per-turn cap
- Distinct MCP server (`chameleon-mcp`)
- Per-repo opt-out: `.chameleon/.skip` file
- Global opt-out: `CHAMELEON_DISABLE=1` env
- Session-scope opt-out: `/chameleon-disable`
- Temporary opt-out: `/chameleon-pause-15m`
- Frustration-triggered hint: callout-detector surfaces disable options on detected frustration

**Context tag:** `<chameleon-context>` (NEUTRAL — no importance framing). Tag-boundary sanitization escapes literals in injected content.

**SessionStart JSON dispatch:** mirrors `a complementary skills library/hooks/session-start` lines 41-55 verbatim. Single format per platform. **Regression test in `tests/integration/session-start-dispatch.bats`.**

**Cache_control two-chunk emission :**
- Cached chunk (with breakpoint): static using-chameleon SKILL.md + static profile primer
- Ephemeral chunk: cost footer + staleness + trust state + value attribution

**Coordination with a complementary skills library:**
- `using-chameleon` documents: "After `another bootstrap skill` triggers `brainstorming`, but before any Edit/Write"
- Combined token cost: ~1,500 (a complementary skills library) + ~1,500 (chameleon) = ~3,000 prime tokens
- Acceptance test (adversarial variant) verifies coexistence under user pressure

**Hook coordination signal:** `CHAMELEON_ADVISORY_INFLIGHT=1` (TTL'd file `/tmp/chameleon-inflight-<pid>` with mtime check) lets other plugins skip duplicate work. Best-effort.

---

## Cost model

| Scenario | Estimate | Notes |
|---|---|---|
| SessionStart prime (cached chunk) | ~1,200-1,500 tokens | Static; benefits from cache_control breakpoint |
| SessionStart ephemeral suffix | ~150-300 tokens | Footer changes per session |
| Per-edit context injection | ~500-800 tokens | Combined hook output |
| Per-edit injection cap | 1,500 tokens hard cap | Truncated |
| Total-hooks-per-turn cap | 2,000 tokens hard cap | Sum of all 4 hooks |
| **Steady-state per session, single-repo, 30 turns, warm cache** | **$0.30-0.50** | Happy path |
| Multi-repo session (5 repos) | $0.60-1.00 | Standard team workflow |
| Multi-repo session (20 repos) | $0.80-1.20 | Heavy switching |
| **Consultant tier (50-80 repos)** | **$2-5** | Outside $50/mo ceiling |
| Extreme multi-repo (100+) | $5+ | Acknowledged edge case |
| 200-turn refactoring marathon | $6-12 | Output dominates |
| Cold-start morning (cache fully expired) | +$0.012 | 1.25× cache write surcharge |
| **Per-month at 100 sessions, single-repo** | **$30-50** | Under $50 ceiling |
| Bootstrap per repo (typical) | $0.50-2.00 | 50-100 file analysis + interview |
| Bootstrap (tRPC-heavy, 80% codegen) | $3-7 | Generated code creates noise |
| Per-team-month with 5 devs sharing committed profile | $150-250 | 5 × $35-50/mo |
| Per-team-month, 5 consultants | $1,000-2,500 | Outside typical claim |

**Pricing assumptions:** Sonnet 4.6 at 2026-05 pricing ($3/M input, $15/M output, $0.30/M cache read, $3.75/M cache write). All numbers proportional to pricing changes.

### Cost transparency in primer (ephemeral chunk)

```
Recent sessions: $0.32, $0.41, $0.28. This month: $14.20.
Profile last refreshed 47 days ago.
Last 30 sessions: 142 edits matched archetype, 11 deviations flagged, 3 corrections via /cham-teach.
```

Cost + staleness + value attribution surfaced to build trust and demonstrate ROI.

---

## Operational semantics (NEW — )

One-line denotational meaning for each profile-DSL primitive:

- **archetype-match(file, archetype):** TRUE iff `file.path matches one of archetype.paths` AND (`archetype.content_signal is empty` OR `file's first 200 bytes contain archetype.content_signal.directive` OR `file's first 200 bytes do NOT contain archetype.content_signal.absent_directives`). Multiple archetypes may match; disambiguate by *most specific* (smallest cluster_size + closest content_signal).

- **rule-violation(file, rule):** TRUE iff `lint_file(file, rule.archetype, file.content)` returns the rule's check as FAIL. Reported by MCP `lint_file` tool. Surfaced in advisory injection (NOT hard-deny).

- **confidence-band(archetype):**
 - `high` iff `cluster_purity * 0.4 + recency_weight * 0.3 + log(cluster_size) * 0.3 >= 0.7`
 - `medium` iff between 0.4 and 0.7
 - `low` iff below 0.4
 - Engine treats `low` confidence archetypes as advisory-only (no rule enforcement); `high` confidence drives rule-violation reports to lint_file.

- **refine-step(profile, feedback):** application of user-provided correction to one of: idioms.md (add/deprecate idiom), canonicals.json (replace witness or normative shape), rules.json (add/remove rule), archetypes.json (split/merge cluster). Each step writes a new profile via atomic transaction. Refinement converges when no new feedback is provided (no fixpoint guarantee; humans decide).

- **MCP tool failure during preflight-and-advise:** fail-open. Inject `<chameleon-context>` warning ("MCP unavailable; pattern conformance not checked") + allow edit. Telemetry log entry. Layered semantics: safety fail-closed, advisory fail-open.

---

## Calibration targets (NEW — )

Magic numbers in the architecture, with evaluation protocols for validation:

| Parameter | Current value | Where used | Evaluation protocol |
|---|---|---|---|
| `recency_weight` | 2× for last 90 days | Clustering | Test corpus (implementation testing + 3 OSS TS repos): measure correlation between `recency_weight` and reviewer-flagged stale canonicals. If correlation < 0.5, recalibrate (try 1.5×, 3×). |
| `recency_window_days` | 90 | Clustering recency boundary | Same corpus; measure stability of confidence-bands across rolling 7-day repo states. If variance high, increase window. |
| `confidence_function weights` | 0.4 / 0.3 / 0.3 | Confidence ordinal | Same corpus; measure correlation between confidence band and reviewer-flagged miss rate. If correlation < 0.5, recalibrate. |
| `cluster_size_log` base | natural log (e) | Confidence formula | Empirical: log_e gives diminishing-returns; alternatives are log_2 (faster saturation) or log_10 (slower). Measure on corpus. |
| `min_cluster_size` | 5 | Sparse cluster threshold | Below 5: ask user instead of infer. Calibration target: false-positive rate at 4 vs 5 vs 6 across corpus. |
| `bimodal_threshold` | 60/40 | Bimodal distribution detection | At 60/40 → flag. At 70/30 → silent majority. Validate against half-migrated-codebase corpus. |
| `repo_size_guard` | 50,000 files | Bootstrap refusal threshold | Validate: largest TS repo where bootstrap completes in <10 minutes. |
| `ast_node_ceiling` | 50,000 nodes | DoS protection | Validate: 99th percentile AST node count across corpus. |
| `MCP timeout` | 2 seconds | preflight-and-advise | Validate: 99th percentile MCP call duration on corpus. Adjust if real workloads are slower. |

**MAINTAINER.md task:** Quarterly calibration review against implementation testing corpus + 3 representative OSS TS repos. Update parameters as evidence emerges.

---

## Migration correctness contract (NEW — )

`profile.json` carries `schema_version`. Engine vN supports schemas v(N-1) to v(N+0). Migrations live in `mcp/chameleon_mcp/profile/migrations/`.

**Contract for every migration script:**

1. **Idempotence:** running migration `v_k → v_{k+1}` twice on the same input produces the same output as running it once.

2. **Round-trip preservation (when reversible):** if a migration is reversible, the inverse migration MUST exist and `migrate_back(migrate(p)) == p`. If not reversible, document explicitly.

3. **Partial-write atomicity:** migration MUST use the same atomic transaction protocol (`.chameleon/.tmp/<txn-id>/COMMITTED`). A crashed migration leaves either the original profile unchanged OR the migrated profile fully written.

4. **No-op detection:** if profile is already at target schema, migration is a no-op (zero writes, zero side effects).

5. **Test obligation:** every migration ships with a test fixture pair `(input_v_k.json, expected_output_v_{k+1}.json)`. CI runs migration on input, asserts byte-equality with expected output.

---

## Versioning & Compatibility

**Engine version policy:**
- Engine vN supports schemas v(N-1) to v(N+0); refuses older with migration prompt; refuses newer with upgrade prompt
- Schema migrations live in `mcp/chameleon_mcp/profile/migrations/` per the migration correctness contract above

**Dependency pinning:**
- TypeScript: vendored at `mcp/node_modules/typescript@<version>` + SHA-256 checksums in `mcp/typescript-checksums.json`; quarterly bump cadence with `npm audit signatures` verification
- FastMCP: pinned in `pyproject.toml`; quarterly bump
- detect-secrets/gitleaks rules: vendored at known version; quarterly bump
- Python minimum: 3.11 until October 2027
- Node minimum: documented in `MAINTAINER.md`; LTS rotation policy
- All locks committed: `package-lock.json`, `uv.lock`, `mcp/uv.lock`

**Quarterly model re-baseline :**
- New Sonnet/Opus version released → MAINTAINER.md task triggers
- Re-run all skill pressure scenarios; capture rationalizations not in existing tables
- Update Red Flags tables; bump `engine_min_version` if behavior shifts
- CI gates `engine_min_version` bump on regression results

**Runbook (`docs/chameleon/MAINTAINER.md` outline):**
- Quarterly dependency bump checklist (npm audit signatures + manual diff + checksum regen)
- HMAC key generation + rotation
- Schema migration authoring guide (per migration correctness contract)
- Quarterly calibration review against corpus
- Quarterly model re-baseline
- Release checklist (CI gates: real-problem-evidence, golden-transcript, adversarial-transcript, skill-baselines, vendor-checksums)
- Decision register (`docs/chameleon/decisions/`)
- Threat model (insider profile poisoning, untrusted repo opening)

---

## Security mitigations ( + + )

### Critical mitigations
1. **Canonical excerpt secret scanner** — vendored detect-secrets rules; refuses unscanned canonicals
2. **Canonical injection scanner** — bootstrap detects instruction-shaped natural language in canonical content; flag for user review or strip comments before injection
3. **Tag-boundary sanitization** — before injection, escape `</chameleon-context>`, `</chameleon`, `<chameleon-context>` literals in canonical/idiom content; regression test in `tests/integration/tag-boundary-sanitize.bats`
4. **Vendor integrity checksums** — `mcp/typescript-checksums.json` SHA-256 manifest; CI-verified on every build
5. **Symlink lstat in MCP file reads + repo-boundary check** — single `safe_open(repo, rel_path)` helper: `realpath` + prefix-match against `repo_root`, reject null bytes / NFD `..` / Windows separators / symlinks
6. **Hardlink defense** — inode-based dedup
7. **HMAC bug fix + per-repo log directory** — `${TMPDIR:-/tmp}/.chameleon_exec_log/<repo_id>/` with mode 0700 + owner-check
8. **profile.json JSON parser hardening** — depth cap (64), duplicate-key rejection (object_pairs_hook), numeric range bounds in schema, NFC normalization before validation
9. **profile.json schema validation** — strict schema; rejects malformed
10. **Profile-poisoning scanner in CI** — `chameleon-status --diff` PR gate runs detect-secrets + dangerous-pattern checks (eval, exec, shell=True, raw SQL concat, missing csrf middleware) on canonical excerpts

### Important mitigations
11. **Repo size guard** — 50k file ceiling, post-glob enforced
12. **AST extractor subprocess limits** — 5s CPU + 512 MB RSS + 1 MB file ceiling + 50k AST node ceiling
13. **Bootstrap interview output sanitization** — strip ANSI/zero-width, 50 KB cap on idioms.md
14. **drift.db local-only** — never committed
15. **HMAC key fail-loud** — explicit error if `/dev/urandom` fails
16. **Trust model with cooldown** — committed profiles untrusted-by-default; `/chameleon-trust` requires typing repo name; new canonicals/idioms after trust re-prompt
17. **SQLite hardening profile** — `mode=ro` for read paths, `PRAGMA trusted_schema=OFF`, never run user-provided SQL
18. **DoS protection on globs** — `pathlib.Path.glob` with `follow_symlinks=False`; manual repo-boundary walker

---

## Phase plan (revised again — additions)

| Phase | Effort | Exit criteria |
|---|---|---|
| Phase 1 — Foundation | ~80h | Hooks + skills shells + MCP scaffold + plugin manifest + lock files + ADR template + MAINTAINER.md draft + CONTRIBUTING.md + safe_open helper + atomic transaction infrastructure + flock locks + SQLite hardening + cache invalidation. Acceptance test passes on stub profile. |
| Phase 2 — TS extractor + bootstrap | ~80h | `/chameleon-init` produces working profile on 5 test TS repos. Generated-code + workspace + plugin-prettierrc detection + canonical injection scanner working. Vendor checksums in CI. |
| Phase 3 — Skills with eval | ~60h | All 7 skills (using-chameleon foundation + 6 user: init, refresh, status, teach, trust, disable, pause) pass RED-GREEN-REFACTOR. Cooperative + adversarial acceptance transcripts captured. CI enforcement live. |
| Phase 4 — Security mitigations | ~40h | All 18 mitigations integrated. Schema validation. HMAC bug fix verified. Trust model with cooldown. JSON parser hardening. Tag-boundary sanitization. Vendor integrity checksums. Profile-poisoning scanner CI gate. |
| Phase 5 — implementation testing | ~30h | `/chameleon-init` run on Ruby on Rails repo + TypeScript repo; profiles committed; idioms iterated via `/chameleon-teach`. **implementation evidence transcripts collected.** |
| Phase 6 — Conformance benchmarking + calibration | ~50h | 80%+ on archetype-matched tasks across 3 test TS repos. Cost ceiling validated. Multi-repo scenarios tested. **Calibration targets evaluated against corpus.** |
| Phase 7 — Documentation + release | ~50h | All docs complete (README with vocabulary firewall + competitive analysis, MAINTAINER.md with quarterly tasks, REAL-PROBLEM-EVIDENCE, ADRs). Dogfooding green for 2 weeks. CI release-tag gates working. |
| **Total v1.0 (TS only)** | **~390h** | **~10 weeks of focused work** (up from v3's 350h due to crash safety + new sections + competitive analysis + calibration phase) |
| **VALIDATION GATE** | 2-4 weeks dogfood | Ship v1.0 only after TypeScript repo dogfood validates: pattern conformance ≥80%, cost ceiling holds, UX friction acceptable. If issues surface, iterate before adding Ruby. |
| Phase 8 (v1.5) — Add Ruby (Prism) | ~30-50h | Vendored Prism extractor; Ruby on Rails repo added to dogfood corpus; both stacks now supported. Engineering: mostly porting + integration testing (Prism approach proven in predecessor projects). |
| **Total v1.5 (TS + Ruby)** | **~420-440h** | **~13 weeks total to support both stacks** |

---

## Open decisions for future iterations

(Not BLOCKING for Phase 1.)

1. **MCP transport beyond stdio** — only if a future platform requires it
2. **Multi-canonical similarity ranking** — when archetype has multiple canonicals, how is "the right one" picked for an edit? Heuristic in v1, ML in future?
3. **Skill priority codified in a complementary skills library** — `using-chameleon` documents "after process, before implementation" — codify in a complementary skills library' priority hierarchy too?
4. **Profile schema v3 → v4 migration** — first real migration's complexity unknown until needed
5. **Companion plugin pattern (v2.0+)** — if community demand emerges
6. **Structured idioms format (v2.0+)** — `(name, ast_query_pattern, counterexample_query, prose_rationale, status)` for machine-checkability
7. **Index db scaling beyond 10k repos** — if a user has more than 10k known repos in `index.db`, we need pagination

---

## Out of scope for v1

- Multi-language extractors (Ruby/Python deferred to v1.5)
- Multi-harness support beyond Claude Code (deferred to v2.0)
- Companion plugin / profile pack distribution (deferred to v2.0+)
- Cross-repo pattern transfer
- Auto-PR opening for profile updates
- Full-history learning
- IDE-specific features beyond Claude Code
- Profile diffing UI (text diffs + profile.summary.md only)
- Cost telemetry dashboard (CLI surface only)
- HTTP transport for MCP
- Auto-trigger of `/chameleon-init`
- Framework-aware archetype detection (BEST-EFFORT only)
- Structured idiom format (v2.0+)

---

## Inheritance from predecessor projects

What's preserved (REVIEWED — not "verbatim"):
- Preflight-check safety hard-deny logic (1001 lines per current source — RECONCILED + EXPLICIT BLOCKLIST as test fixture)
- Posttool-recorder HMAC exec log (with **GC bug fix + path mismatch fix + per-repo log directory**)
- Callout-detector frustration phrase reminder (extended to surface disable hints)
- TS Compiler API extractor approach (vendored + checksum-verified)
- MCP server + Skills + PostToolUse pattern

What's redesigned:
- Combined preflight-and-advise hook with 2s timeout and fail-open contract
- Single `safe_open` helper for all file-reading tools
- Multi-file transactional commit pattern
- OS-level locks for refresh_repo
- SQLite hardening profile
- Per-call mtime cache invalidation
- Profile merge tool for git merge driver
- Trichotomized canonicals (witness/normative shape/normative idiom — )
- Bootstrap interview ≤3 prompts × ≤10 lines visible
- Profile schema (multi-canonical, AST-query lookup, ordinal confidence with formula, deprecation tracking, schema versioning)
- Profile distribution via git (no companion plugin pattern in v1)
- Security mitigations (18 items including 6 new in )
- Cost model (honest tiered pricing with calibration targets)
- Trust model (non-blocking warning + cooldown — enhanced)
- Cache_control two-chunk emission
- Hook-model deduplication
- Maintenance scaffolding (locks, ADRs, MAINTAINER.md, schema migrations, calibration, quarterly model re-baseline)

What's discarded:
- Framework-aware claim (best-effort instead)
- Multi-harness v1 directories
- Companion plugin pattern in v1
- Pack signing infrastructure
- Dynamic archetype skills
- `<EXTREMELY_IMPORTANT>` and `<CHAMELEON_IMPORTANT>` framing (neutral `<chameleon-context>` only)
- Dual-format JSON dispatch
- Strict sha matching for canonicals
- "Verbatim inheritance" claim for preflight
- Statistical-mode-wins clustering (recency-weighted now)
- `apply_profile_pack` MCP tool (replaced with `merge_profiles`)
- Refine/refresh semantic collision (`refine` renamed to `teach`)
- "200ms is the cost of correctness" claim from Red Flags table (empirically wrong by 7×; v5 removal)

---

## License + Backwards-Compatibility contract

> **** — addresses OSS Maintainer NEEDS REVISION verdict.

### License declaration

**Status:** — author choice required before any public release.

**Recommendation:**
- For a private deployment: `UNLICENSED — proprietary to Cris Nahine`
- If chameleon goes public OSS: `Apache-2.0` (corporate-friendly, patent grant included)
- MIT acceptable but provides no patent grant

The architecture currently equivocates between private and public paths. **Decide before Phase 1 starts.** This is the single highest-leverage one-line decision in the document.

### Backwards-compatibility contract

This is a public commitment to users who commit `.chameleon/` to their repos:

> **chameleon will not break committed `.chameleon/profile.json` schema without a major version bump (e.g., 1.x → 2.0). Migrations will be provided for every minor and major bump. The `engine_min_version` field in profile.json defines the contract: an engine refuses to load profiles requiring a newer engine, and migrates profiles requiring an older engine through the chain.**

**Non-breaking changes (no minor bump required):**
- Adding new MCP tools
- Adding new optional fields to JSON files
- Adding new optional input fields with safe defaults
- Loosening a validation rule
- Adding new archetype patterns to a profile (via `/chameleon-refresh`)

**Breaking changes (require major version bump):**
- Renaming an MCP tool
- Removing an MCP tool
- Reordering positional arguments
- Tightening a validation rule
- Changing the type of an existing field
- Changing the meaning of an existing field
- Removing a field

This is the same contract Stripe publishes for their API.

### Release cadence

- **Patch (1.0.x):** on demand, for security or critical bug fixes
- **Minor (1.x.0):** monthly during active development; quarterly steady state
- **Major (x.0.0):** annually max; only when migration is required

### CLA / DCO (if going public)

— author choice. Recommendations:
- DCO (Developer Certificate of Origin) — lighter-weight, used by Linux kernel
- ICLA (Individual Contributor License Agreement) — Apache standard, more legally robust

---

## Glossary appendix

> **** — addresses Technical Writer NEEDS REVISION verdict.

| Term | Definition |
|---|---|
| **archetype** | A category of file with shared patterns (e.g., "controller", "service"). Detected via clustering or hand-curated. User-facing vocabulary (Tier 1 of vocabulary firewall). |
| **archetype-match** | Operational predicate: `TRUE iff file.path matches archetype.paths AND content_signal matches`. Multiple archetypes may match; disambiguate by most specific. |
| **AST** | Abstract Syntax Tree. Output of TypeScript Compiler API parser; tree of `ts.SyntaxKind` nodes. |
| **atomic transaction** | Multi-file write protocol: write all files to `.chameleon/.tmp/<txn-id>/`, write `COMMITTED` sentinel last, atomic rename of dir over `.chameleon/`. |
| **bootstrap** | First-time profile generation via `/chameleon-init`. AST scan + interview + profile artifacts. |
| **canonical** | A reference example for an archetype. Trichotomized: (a) **witness** = the file itself, (b) **normative shape** = AST query, (c) **normative idiom** = prose annotations. |
| **`<chameleon-context>`** | Neutral XML-style tag wrapping injected context. NOT framed with importance ("EXTREMELY_IMPORTANT") to avoid framing competition with a complementary skills library. |
| **cluster** | A set of files with the same `sig` value. An archetype is a named cluster. |
| **cluster signature** | The `sig: file → ClusterKey` function. 7-tuple of (path_pattern, content_signal, top_level_kinds, default_export, named_export_count, import_hash, jsx_present). |
| **`content_signal`** | First 200 bytes lexical directive (e.g., `'use client'`, `'use server'`). The boundary between Tier 1 (auto-derivable) and idioms.md (Tier 2). |
| **drift** | Divergence between current code state and profile (mtime/sha changes since last refresh). |
| **engine** | The chameleon plugin code (hooks + MCP server + skills + extractors). Distinct from profile (per-repo data). |
| **fail-closed** | On error, deny the operation. Used by safety layer. |
| **fail-open** | On error, allow the operation with warning. Used by advisory layer (MCP timeout, parse error). |
| **flock** | POSIX advisory file lock. Used to prevent concurrent `/chameleon-refresh`. |
| **generation counter** | Integer in profile.json incremented on each atomic write. Loaders verify all four JSON files share the same generation for cross-file consistency. |
| **idiom** | Team-specific convention recorded in `idioms.md`. User-facing vocabulary (Tier 1). |
| **MCP** | Model Context Protocol. The interface between Claude and chameleon's server (stdio transport). |
| **NFC / NFD** | Unicode normalization forms. Used in path canonicalization to ensure cross-platform repo_id stability. |
| **profile** | The per-repo data captured in `.chameleon/`: archetypes, rules, canonicals, idioms. User-facing vocabulary (Tier 1). |
| **recency weight** | Multiplier applied to recently-edited files in clustering. 90 days = 2× weight. Defeats archive-majority repos. |
| **refresh** | Re-analyze repo, update profile. User command: `/chameleon-refresh`. |
| **rule-violation** | `lint_file(file, rule)` returns FAIL. Reported in advisory injection (NOT hard-deny). |
| **`safe_open(repo, rel_path)`** | Single helper for file reads: realpath + prefix-match + lstat + null-byte/NFD-`..`/Windows-separator rejection. |
| **schema_version** | Integer in profile.json indicating profile schema version. Engine refuses to load schemas outside its supported window. |
| **`sha_hint`** | xxhash64 of file content. Non-crypto, used for fast change detection. |
| **soundness** (clustering) | Property: if two files cluster together, they share AST shape + signals (meaningful similarity plausible). Not guaranteed; mitigated by canonical mechanism + `/chameleon-teach`. |
| **stability** (clustering) | Property: same input → byte-identical profile. Required for idempotent refresh. |
| **subagent** | Claude agent dispatched for specific task. Skips `using-chameleon` per `<SUBAGENT-STOP>` block. |
| **syntactic surrogate** | What chameleon actually computes: AST-shape + path + content_signal. Approximates the semantic equivalence relation "two files are the same archetype." |
| **teach** (slash command) | User-driven correction: `/chameleon-teach`. Updates idioms.md or canonicals.json. (Renamed from "refine" in v4 to eliminate refresh-vs-refine collision.) |
| **trust** | Per-user approval of a committed profile. Required before chameleon's advisory injections fire for that user. User command: `/chameleon-trust`. |
| **WAL** | SQLite Write-Ahead Logging mode. Allows concurrent readers + 1 writer. |
| **witness** | The actual file selected as a canonical for an archetype. Has idiosyncrasies; the *normative shape* (AST query) is what's required to match. |

---

*End of v5 architecture. Addresses all 5 NEEDS REVISION findings + critical APPROVED-WITH-NOTES items from 10-expert verification.*

**Total review investment:**
- 5 rounds of multi-agent review (: 6 agents, : 5, : 1, : 5, : 10) = 27 unique reviewer perspectives
- implementation verification against /api and /client repos
- v1 (3,899 words) → v5 (~12,000+ words)

**Review moratorium declared after v5.** Implementation findings replace reviewer findings from this point forward. The Engineering Manager's voice from was clear: "You cannot review your way to perfection." Real learning starts with code.

Phase 1 prerequisites:
1. **stakeholder confirmation conversation** — gates everything else
2. **License decision** — `UNLICENSED` (private) vs `Apache-2.0` (public)
3. **Risk registry review** — pre-commit fall-back-to-v0.5 plan if Phase 1 takes 12+ weeks for 30% scope

When (1)-(3) are resolved, Phase 1 can begin.
